"""The forgiving parser.

These cases are not hypothetical — they are the shapes cheap models actually emit.
Each one that passes is a round trip the agent does not have to spend correcting
format instead of doing the task.
"""

from __future__ import annotations

import pytest

from chamber.agent.parse import parse_response


def actions(text: str):
    parsed = parse_response(text)
    assert parsed.ok, f"failed to parse: {parsed.error}\n---\n{text}"
    return parsed


class TestCanonical:
    def test_exact_format(self):
        parsed = actions('{"thought": "clicking", "actions": [{"action": "click", "ref": "e12"}]}')
        assert parsed.thought == "clicking"
        assert parsed.actions[0].action == "click"
        assert parsed.actions[0].ref == "e12"
        assert parsed.repairs == []

    def test_multiple_actions(self):
        parsed = actions(
            '{"thought": "fill the form", "actions": ['
            '{"action": "type_text", "ref": "e1", "text": "ada"},'
            '{"action": "type_text", "ref": "e2", "text": "lovelace"}]}'
        )
        assert len(parsed.actions) == 2


class TestNearMisses:
    """Format slips that carry exactly one reading, so repairing them is safe."""

    def test_bare_int_ref(self):
        # browser-use style indexing: the model says 42, chamber's refs are e42.
        parsed = actions('{"actions": [{"action": "click", "ref": 42}]}')
        assert parsed.actions[0].ref == "e42"
        assert parsed.repairs

    def test_index_instead_of_ref(self):
        parsed = actions('{"actions": [{"action": "click", "index": 7}]}')
        assert parsed.actions[0].ref == "e7"

    def test_action_name_as_key(self):
        # The single most common cheap-model shape.
        parsed = actions('{"click": {"ref": "e3"}}')
        assert parsed.actions[0].action == "click"
        assert parsed.actions[0].ref == "e3"

    def test_action_name_as_key_with_bare_value(self):
        # The exact failure the earlier prototype hit: {"click": 42}
        parsed = actions('{"click": 42}')
        assert parsed.actions[0].action == "click"
        assert parsed.actions[0].ref == "e42"

    def test_navigate_with_bare_url(self):
        parsed = actions('{"navigate": "https://example.com"}')
        assert parsed.actions[0].action == "navigate"
        assert parsed.actions[0].url == "https://example.com"

    def test_value_instead_of_text(self):
        parsed = actions('{"actions": [{"action": "type_text", "ref": "e1", "value": "hi"}]}')
        assert parsed.actions[0].text == "hi"

    def test_alias_action_names(self):
        for alias, canonical in [
            ("goto", "navigate"),
            ("input", "type_text"),
            ("finish", "done"),
            ("back", "go_back"),
        ]:
            payload = {
                "navigate": f'{{"action": "{alias}", "url": "https://x.com"}}',
                "type_text": f'{{"action": "{alias}", "ref": "e1", "text": "x"}}',
                "done": f'{{"action": "{alias}", "summary": "ok"}}',
                "go_back": f'{{"action": "{alias}"}}',
            }[canonical]
            parsed = actions(payload)
            assert parsed.actions[0].action == canonical, alias

    def test_bare_domain_gets_scheme(self):
        parsed = actions('{"actions": [{"action": "navigate", "url": "example.com"}]}')
        assert parsed.actions[0].url == "https://example.com"

    def test_unknown_fields_are_dropped_not_fatal(self):
        parsed = actions(
            '{"actions": [{"action": "click", "ref": "e1", "confidence": 0.9, "selector": "#x"}]}'
        )
        assert parsed.actions[0].ref == "e1"
        assert any("ignored unknown field" in r for r in parsed.repairs)

    def test_function_calling_style_nesting(self):
        # Models trained heavily on tool-calling reach for this shape even when
        # asked for a flat one.
        parsed = actions(
            '{"thought": "go", "action": "click", "parameters": {"ref": "e12"}}'
        )
        assert parsed.actions[0].action == "click"
        assert parsed.actions[0].ref == "e12"

    @pytest.mark.parametrize("wrapper", ["parameters", "params", "args", "arguments", "input"])
    def test_all_the_nesting_names(self, wrapper: str):
        parsed = actions(
            f'{{"actions": [{{"action": "type_text", "{wrapper}": '
            f'{{"ref": "e1", "text": "hi"}}}}]}}'
        )
        assert parsed.actions[0].text == "hi"
        assert parsed.actions[0].ref == "e1"

    def test_top_level_fields_win_over_nested_ones(self):
        parsed = actions(
            '{"actions": [{"action": "click", "ref": "e9", "parameters": {"ref": "e1"}}]}'
        )
        assert parsed.actions[0].ref == "e9"

    def test_numbers_below_the_minimum_are_clamped(self):
        # Observed in a real run: the model asked read_page for a budget under the
        # 1000 minimum and lost a round trip to a validation error. The bound is
        # chamber's implementation detail; "read less" has one reading.
        parsed = actions('{"actions": [{"action": "read_page", "budget": 500}]}')
        assert parsed.actions[0].budget == 1000
        assert any("raised to the minimum" in r for r in parsed.repairs)

    def test_numbers_above_the_maximum_are_clamped(self):
        parsed = actions('{"actions": [{"action": "read_page", "budget": 999999}]}')
        assert parsed.actions[0].budget == 60_000
        assert any("lowered to the maximum" in r for r in parsed.repairs)

    def test_in_range_numbers_are_untouched(self):
        parsed = actions('{"actions": [{"action": "read_page", "budget": 8000}]}')
        assert parsed.actions[0].budget == 8000
        assert not parsed.repairs

    def test_clamping_applies_across_actions(self):
        parsed = actions('{"actions": [{"action": "click", "ref": "e1", "clicks": 9}]}')
        assert parsed.actions[0].clicks == 3

    def test_unknown_enum_value_falls_back_to_the_default(self):
        # Observed in a real run: the model invented an ask_human reason outside
        # the allowed set. Rejecting the escape hatch is the worst moment to be
        # strict — the model reaches for it precisely when it is already stuck.
        parsed = actions(
            '{"actions": [{"action": "ask_human", "question": "help", "reason": "stuck"}]}'
        )
        assert parsed.actions[0].reason == "blocked"
        assert any("not one of" in r for r in parsed.repairs)

    def test_valid_enum_values_pass_through(self):
        parsed = actions(
            '{"actions": [{"action": "ask_human", "question": "solve it", "reason": "captcha"}]}'
        )
        assert parsed.actions[0].reason == "captcha"
        assert not parsed.repairs

    def test_enum_fallback_does_not_touch_free_text(self):
        parsed = actions('{"actions": [{"action": "press_key", "key": "SomeOddKey"}]}')
        assert parsed.actions[0].key == "SomeOddKey"

    def test_booleans_are_not_treated_as_numbers(self):
        parsed = actions(
            '{"actions": [{"action": "read_page", "budget": 5000, "whole_page": true}]}'
        )
        assert parsed.actions[0].whole_page is True

    def test_single_action_at_top_level(self):
        parsed = actions('{"thought": "go", "action": "go_back"}')
        assert parsed.actions[0].action == "go_back"

    def test_nested_action_object(self):
        parsed = actions('{"thought": "go", "action": {"action": "reload"}}')
        assert parsed.actions[0].action == "reload"

    def test_bare_list_of_actions(self):
        parsed = actions('[{"action": "scroll", "direction": "down"}]')
        assert parsed.actions[0].action == "scroll"


class TestSurroundingNoise:
    def test_fenced_json(self):
        parsed = actions('Here is what I will do:\n```json\n{"actions": [{"action": "reload"}]}\n```')
        assert parsed.actions[0].action == "reload"

    def test_prose_before_and_after(self):
        parsed = actions(
            'I need to click the login button.\n'
            '{"thought": "log in", "actions": [{"action": "click", "ref": "e9"}]}\n'
            'That should do it.'
        )
        assert parsed.actions[0].ref == "e9"

    def test_trailing_comma(self):
        parsed = actions('{"actions": [{"action": "reload"},]}')
        assert parsed.actions[0].action == "reload"

    def test_prefers_the_action_object_over_a_decoy(self):
        parsed = actions(
            '{"note": "thinking"}\n'
            '{"thought": "the real one", "actions": [{"action": "click", "ref": "e5"}]}'
        )
        assert parsed.actions[0].ref == "e5"


class TestRejection:
    """Ambiguity must fail loudly. Guessing intent is worse than asking again."""

    def test_no_json_at_all(self):
        parsed = parse_response("I will click the blue login button now.")
        assert not parsed.ok
        assert "JSON" in parsed.error

    def test_unknown_action_name(self):
        parsed = parse_response('{"actions": [{"action": "teleport", "ref": "e1"}]}')
        assert not parsed.ok

    def test_empty_reply(self):
        parsed = parse_response("")
        assert not parsed.ok

    def test_click_without_a_ref_is_rejected(self):
        # There is no safe guess for which element was meant.
        parsed = parse_response('{"actions": [{"action": "click"}]}')
        assert not parsed.ok
        assert "ref" in parsed.error


class TestErrorMessagesAreActionable:
    @pytest.mark.parametrize(
        "text",
        [
            "no json here",
            '{"actions": [{"action": "click"}]}',
            '{"actions": [{"action": "nope"}]}',
        ],
    )
    def test_error_says_what_to_do(self, text: str):
        parsed = parse_response(text)
        assert not parsed.ok
        # An error a model cannot act on is worse than useless — it burns a step.
        assert len(parsed.error) > 30
        assert any(word in parsed.error.lower() for word in ("action", "json", "field"))
