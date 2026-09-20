"""The vision sensor.

No Ollama required — the transport is stubbed. What is worth pinning here is the
*contract*, not the model: images reach the wire in the right shape for each
provider, and a sensor failure never takes the run down.
"""

from __future__ import annotations

import base64

import pytest

from chamber.agent.llm import LLM, LLMError, Message
from chamber.agent.vision import Vision
from chamber.config import ModelConfig

PNG = b"\x89PNG\r\n\x1a\n" + b"fake image bytes"


def vision_config(**kw) -> ModelConfig:
    defaults = dict(
        provider="openai",
        model="qwen2.5vl:3b",
        base_url="http://localhost:11434/v1",
        api_key="ollama",
        keep_alive="30m",
    )
    defaults.update(kw)
    return ModelConfig(**defaults)


class TestImagesOnTheWire:
    """A picture that does not reach the model is the failure this guards."""

    def test_openai_shape(self):
        payload = Message("user", "what is this?", images=["QUJD"]).to_openai()
        assert payload["role"] == "user"
        parts = payload["content"]
        assert parts[0] == {"type": "text", "text": "what is this?"}
        assert parts[1]["type"] == "image_url"
        assert parts[1]["image_url"]["url"] == "data:image/png;base64,QUJD"

    def test_anthropic_shape(self):
        payload = Message("user", "what is this?", images=["QUJD"]).to_anthropic()
        blocks = payload["content"]
        assert blocks[0]["type"] == "image"
        assert blocks[0]["source"] == {
            "type": "base64",
            "media_type": "image/png",
            "data": "QUJD",
        }
        # Text after the image: both providers attend better that way.
        assert blocks[-1] == {"type": "text", "text": "what is this?"}

    def test_plain_text_is_left_as_a_string(self):
        # Wrapping every text-only turn in a content array would change the payload
        # for every existing caller for no reason.
        assert Message("user", "hello").to_openai() == {"role": "user", "content": "hello"}
        assert Message("user", "hello").to_anthropic() == {"role": "user", "content": "hello"}

    def test_multiple_images(self):
        parts = Message("user", "compare", images=["QQ==", "Qg=="]).to_openai()["content"]
        assert sum(1 for p in parts if p["type"] == "image_url") == 2

    def test_responses_shape(self):
        payload = Message("user", "what is this?", images=["QUJD"]).to_responses()
        assert payload["content"][0] == {"type": "input_text", "text": "what is this?"}
        assert payload["content"][1] == {
            "type": "input_image",
            "image_url": "data:image/png;base64,QUJD",
        }


class TestRequestBuilding:
    def test_keep_alive_is_sent_when_configured(self):
        # Ollama evicts after 5 minutes idle and a cold reload costs ~90s.
        llm = LLM(vision_config())
        payload, url, _ = llm._openai_request("sys", [Message("user", "hi")], None)
        assert payload["keep_alive"] == "30m"
        assert url.endswith("/chat/completions")

    def test_keep_alive_absent_by_default(self):
        # Providers that are not Ollama have no use for it.
        llm = LLM(ModelConfig(provider="openai", model="gpt", base_url="https://api.x/v1"))
        payload, _, _ = llm._openai_request("sys", [Message("user", "hi")], None)
        assert "keep_alive" not in payload

    def test_images_survive_into_the_openai_payload(self):
        llm = LLM(vision_config())
        payload, _, _ = llm._openai_request(
            "sys", [Message("user", "look", images=["QUJD"])], None
        )
        assert payload["messages"][1]["content"][1]["type"] == "image_url"

    def test_official_responses_payload_has_reasoning_and_flat_tools(self):
        llm = LLM(
            ModelConfig(
                provider="openai",
                model="gpt-5.6-luna",
                base_url="https://api.openai.com/v1",
                api_key="sk-test",
                api_style="responses",
                reasoning_effort="medium",
            )
        )
        payload, url, _ = llm._responses_request(
            "sys",
            [Message("user", "hi")],
            [{"name": "click", "description": "d", "input_schema": {"type": "object"}}],
        )
        assert url.endswith("/responses")
        assert payload["instructions"] == "sys"
        assert payload["reasoning"] == {"effort": "medium", "summary": "auto"}
        assert payload["tools"][0]["name"] == "click"
        assert "function" not in payload["tools"][0]

    def test_parses_responses_output_and_summary(self):
        response = LLM._parse_responses(
            {
                "status": "completed",
                "output": [
                    {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Checked the page."}]},
                    {"type": "message", "content": [{"type": "output_text", "text": "Ready"}]},
                    {"type": "function_call", "name": "click", "arguments": '{"ref":"e1"}', "call_id": "c1"},
                ],
                "usage": {"input_tokens": 12, "output_tokens": 4},
            }
        )
        assert response.text == "Ready"
        assert response.reasoning_summary == "Checked the page."
        assert response.tool_calls[0].arguments == {"ref": "e1"}
        assert response.input_tokens == 12


class TestToolCallingFallback:
    """Native tool calling is the default because it makes malformed actions
    structurally impossible — but not every OpenAI-compatible server implements
    it, and a run must not die on one that doesn't."""

    def test_tools_are_sent_by_default(self):
        llm = LLM(ModelConfig(provider="openai", model="m", base_url="https://x/v1"))
        assert llm.using_tools
        payload, _, _ = llm._openai_request(
            "sys", [Message("user", "hi")], [{"name": "click", "description": "d", "input_schema": {}}]
        )
        assert payload["tools"][0]["function"]["name"] == "click"

    def test_a_rejecting_endpoint_drops_to_text_mode(self):
        llm = LLM(ModelConfig(provider="openai", model="m", base_url="https://x/v1"))
        llm._tools_work = False  # what the 400 handler sets
        assert not llm.using_tools
        payload, _, _ = llm._openai_request(
            "sys", [Message("user", "hi")], [{"name": "click", "description": "d", "input_schema": {}}]
        )
        assert "tools" not in payload

    def test_config_can_force_text_mode(self):
        llm = LLM(ModelConfig(provider="openai", model="m", tool_calling=False))
        assert not llm.using_tools


class TestPromptSize:
    def test_tool_mode_omits_the_action_reference(self):
        # The tool schemas already carry every name and field; repeating them is
        # ~2,000 characters a small model has to hold alongside the page.
        from chamber.agent import prompt

        text_mode = prompt.system_prompt(tool_calling=False)
        tool_mode = prompt.system_prompt(tool_calling=True)
        assert "# Actions" in text_mode
        assert "# Actions" not in tool_mode
        assert len(tool_mode) < len(text_mode) * 0.6

    def test_both_modes_still_state_the_json_shape(self):
        # Tool mode needs it as a fallback for the moment an endpoint drops tools
        # mid-session — otherwise the prompt would describe a path it cannot take.
        from chamber.agent import prompt

        for mode in (True, False):
            assert '"actions"' in prompt.system_prompt(tool_calling=mode)

    def test_both_modes_keep_the_working_rules(self):
        from chamber.agent import prompt

        for mode in (True, False):
            p = prompt.system_prompt(tool_calling=mode)
            assert "Refs are per-observation" in p
            assert "captcha" in p.lower()


class TestFailureIsNeverFatal:
    """The sensor is optional. It must degrade, not raise."""

    @pytest.fixture
    def vision(self):
        return Vision(vision_config())

    async def test_unreachable_model_returns_an_explanation(self, vision, monkeypatch):
        async def boom(*_a, **_kw):
            raise LLMError("connection refused")

        monkeypatch.setattr(vision._llm, "complete", boom)
        answer = await vision.look(PNG, "anything?")
        assert "no usable visual description" in answer
        assert "connection refused" in answer

    async def test_unreadable_file_returns_an_explanation(self, vision, tmp_path):
        answer = await vision.look(tmp_path / "does-not-exist.png")
        assert "could not be read" in answer

    async def test_empty_reply_is_reported_not_passed_through(self, vision, monkeypatch):
        class Empty:
            text = "   "

        async def blank(*_a, **_kw):
            return Empty()

        monkeypatch.setattr(vision._llm, "complete", blank)
        assert "no usable" in await vision.look(PNG)


class TestFallbackChain:
    """A local 3B model is free but hedges; the hosted backstop costs money, so it
    must fire when the cheap answer was no use — and only then."""

    def _chain(self, primary_answer: str, fallback_answer: str, monkeypatch):
        vision = Vision(vision_config(), vision_config(model="gpt-5.6-luna"))
        calls: list[str] = []

        def responder(name: str, text: str | None):
            async def go(*_a, **_kw):
                calls.append(name)
                if text is None:
                    raise LLMError("boom")
                return type("R", (), {"text": text})()

            return go

        monkeypatch.setattr(vision._llm, "complete", responder("primary", primary_answer))
        monkeypatch.setattr(
            vision._fallback_llms[0], "complete", responder("fallback", fallback_answer)
        )
        return vision, calls

    async def test_a_good_primary_answer_skips_the_fallback(self, monkeypatch):
        vision, calls = self._chain(
            "There is no Add to Cart button; the item is Currently unavailable.", "unused", monkeypatch
        )
        answer = await vision.look(PNG)
        assert "Currently unavailable" in answer
        assert calls == ["primary"]
        assert "via" not in answer

    async def test_a_hedging_answer_falls_through(self, monkeypatch):
        vision, calls = self._chain(
            "I can't see that in this screenshot.",
            "There is no Add to Cart button.",
            monkeypatch,
        )
        answer = await vision.look(PNG)
        assert "no Add to Cart" in answer
        assert calls == ["primary", "fallback"]
        # Which model answered is stated — a silent billed fallback is invisible.
        assert "via gpt-5.6-luna" in answer

    async def test_a_crashed_primary_falls_through(self, monkeypatch):
        vision, calls = self._chain(None, "The page shows a login form.", monkeypatch)
        answer = await vision.look(PNG)
        assert "login form" in answer
        assert calls == ["primary", "fallback"]

    async def test_both_failing_still_returns_a_string(self, monkeypatch):
        vision, calls = self._chain(None, None, monkeypatch)
        answer = await vision.look(PNG)
        assert "no usable" in answer
        assert calls == ["primary", "fallback"]

    async def test_warm_on_a_hosted_endpoint_reports_nothing_to_do(self):
        # None, not (False, 0.0): a hosted model has no local weights, and
        # "did not preload" would be a warning about a non-problem.
        v = Vision(vision_config(base_url="https://opencode.ai/zen/go/v1"))
        assert await v.warm() is None


class TestQuestions:
    """The question shapes the answer; these are the ones the loop relies on."""

    @pytest.fixture
    def captured(self, monkeypatch):
        seen: dict = {}

        class Reply:
            text = "ok"

        async def capture(_system, messages, **_kw):
            seen["question"] = messages[0].content
            seen["images"] = messages[0].images
            return Reply()

        v = Vision(vision_config())
        monkeypatch.setattr(v._llm, "complete", capture)
        return v, seen

    async def test_why_stuck_names_the_goal_and_asks_about_obstruction(self, captured):
        vision, seen = captured
        await vision.why_stuck(PNG, "add the laptop to the cart")
        q = seen["question"]
        assert "add the laptop to the cart" in q
        assert "blocking" in q or "preventing" in q

    async def test_locate_asks_for_position(self, captured):
        vision, seen = captured
        await vision.locate(PNG, "the Add to Cart button")
        assert "the Add to Cart button" in seen["question"]
        assert "Where" in seen["question"]

    async def test_the_image_is_actually_attached(self, captured):
        vision, seen = captured
        await vision.look(PNG, "hi")
        assert seen["images"] == [base64.b64encode(PNG).decode("ascii")]


class TestConfig:
    def test_vision_is_off_unless_a_model_is_named(self, monkeypatch: pytest.MonkeyPatch):
        from chamber.config import ChamberConfig

        monkeypatch.delenv("CHAMBER_VISION_MODEL", raising=False)
        monkeypatch.setenv("CHAMBER_OPENAI_API_KEY", "sk-test")
        assert ChamberConfig.from_env().vision is None

    def test_naming_a_model_defaults_to_ollama(self, monkeypatch: pytest.MonkeyPatch):
        from chamber.config import ChamberConfig

        monkeypatch.setenv("CHAMBER_VISION_MODEL", "qwen2.5vl:3b")
        monkeypatch.delenv("CHAMBER_VISION_BASE_URL", raising=False)
        cfg = ChamberConfig.from_env()
        assert cfg.vision is not None
        assert "11434" in cfg.vision.base_url
        assert cfg.vision.keep_alive == "30m"

    def test_vision_fires_later_than_the_first_stuck_nudge(self):
        from chamber.config import LoopConfig

        loop = LoopConfig()
        # The model gets a couple of text-only chances before a ~19s local call.
        assert loop.vision_after > loop.stuck_after
