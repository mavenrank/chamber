"""The action schema and the feedback it produces."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from chamber.actions.result import ActionResult, Outcome, format_batch
from chamber.actions.schema import ACTION_TYPES, ActionEnvelope, parse_action, tool_schemas


class TestSchema:
    def test_every_action_round_trips(self):
        samples = {
            "navigate": {"url": "https://x.example"},
            "go_back": {},
            "go_forward": {},
            "reload": {},
            "click": {"ref": "e1"},
            "hover": {"ref": "e1"},
            "drag": {"from_ref": "e1", "to_ref": "e2"},
            "type_text": {"ref": "e1", "text": "hi"},
            "press_key": {"key": "Enter"},
            "select_option": {"ref": "e1", "value": "Price"},
            "upload_file": {"ref": "e1", "paths": ["/tmp/cv.pdf"]},
            "dismiss_overlay": {},
            "copy": {"ref": "e1"},
            "paste": {"ref": "e1"},
            "clipboard": {},
            "scroll": {},
            "scroll_to": {"ref": "e1"},
            "open_tab": {},
            "switch_tab": {"tab_id": "t2"},
            "close_tab": {},
            "wait_for": {"ms": 500},
            "read_page": {},
            "screenshot": {},
            "inspect": {"ref": "e1"},
            "console_log": {},
            "network_log": {},
            "evaluate_js": {"expression": "1+1"},
            "ask_human": {"question": "solve this"},
            "done": {"summary": "finished"},
        }
        assert set(samples) == set(ACTION_TYPES), "a new action needs a sample here"
        for name, payload in samples.items():
            action = parse_action({"action": name, **payload})
            assert action.action == name

    def test_every_action_is_described_to_the_model(self):
        """An action missing from the prompt's groups is one the model never learns
        it has — accepted by the validator, invisible in the reference."""
        from chamber.agent.prompt import system_prompt

        reference = system_prompt(tool_calling=False)
        for name in ACTION_TYPES:
            assert f"`{name}`(" in reference, f"{name} is not in the action reference"

    def test_rejects_unknown_fields(self):
        with pytest.raises(ValidationError):
            parse_action({"action": "click", "ref": "e1", "nonsense": 1})

    def test_rejects_missing_required(self):
        with pytest.raises(ValidationError):
            parse_action({"action": "type_text", "ref": "e1"})

    def test_navigate_adds_a_scheme(self):
        assert parse_action({"action": "navigate", "url": "x.example"}).url == "https://x.example"

    def test_navigate_rejects_a_weird_scheme(self):
        with pytest.raises(ValidationError):
            parse_action({"action": "navigate", "url": "ftp://x.example"})

    def test_envelope_requires_at_least_one_action(self):
        with pytest.raises(ValidationError):
            ActionEnvelope.model_validate({"thought": "hm", "actions": []})

    def test_envelope_caps_batch_size(self):
        with pytest.raises(ValidationError):
            ActionEnvelope.model_validate(
                {"actions": [{"action": "reload"} for _ in range(6)]}
            )


class TestToolSchemas:
    """The prompt and the validator are generated from the same classes; if this
    drifts, the model is told to do something that will be rejected."""

    def test_one_per_action(self):
        schemas = tool_schemas()
        assert {s["name"] for s in schemas} == set(ACTION_TYPES)

    def test_shape_is_function_calling_ready(self):
        for schema in tool_schemas():
            assert schema["description"]
            assert schema["input_schema"]["type"] == "object"
            # "action" is the discriminator, not something the model supplies.
            assert "action" not in schema["input_schema"]["properties"]

    def test_required_fields_are_declared(self):
        click = next(s for s in tool_schemas() if s["name"] == "click")
        assert "ref" in click["input_schema"]["required"]


class TestFeedback:
    def test_success_reads_cleanly(self):
        text = ActionResult.success("click", "clicked Add to cart").for_model()
        assert text.startswith("✓")
        assert "Add to cart" in text

    def test_failure_names_the_outcome_and_suggests_a_fix(self):
        result = ActionResult.failure(Outcome.OCCLUDED, "click", "covered by div#banner")
        text = result.for_model()
        assert "✗" in text
        assert "occluded" in text
        assert "div#banner" in text
        # The hint is the part that changes what the model does next.
        assert "dismiss" in text.lower()

    @pytest.mark.parametrize("outcome", list(Outcome))
    def test_every_failure_mode_has_a_hint(self, outcome: Outcome):
        if outcome is Outcome.OK:
            return
        hint = ActionResult(outcome).hint
        assert hint, f"{outcome} has no hint — the model would have nothing to try"
        assert len(hint) > 20

    def test_repairs_are_surfaced_not_hidden(self):
        result = ActionResult.success("click", "clicked")
        result.repairs.append("scrolled it into view")
        assert "scrolled it into view" in result.for_model()

    def test_batch_numbers_results(self):
        text = format_batch(
            [
                ActionResult.success("type_text", "typed"),
                ActionResult.failure(Outcome.STALE_REF, "click", "gone"),
            ]
        )
        assert "1." in text and "2." in text

    def test_batch_of_one_is_not_numbered(self):
        assert not format_batch([ActionResult.success("reload", "done")]).startswith("1.")

    def test_batch_explains_skipped_actions(self):
        text = format_batch(
            [
                ActionResult.failure(Outcome.STALE_REF, "click", "gone"),
                ActionResult.success("type_text", "typed"),
                ActionResult.success("reload", "done"),
            ]
        )
        assert "skipped" in text
