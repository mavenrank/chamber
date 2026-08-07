"""Configuration — one dataclass, populated from env with explicit overrides.

Kept as plain dataclasses rather than pydantic-settings so that constructing a
config in code is obvious and has no hidden env reads: `ChamberConfig()` reads env
only through `from_env()`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

Provider = Literal["openai", "anthropic"]

load_dotenv(override=False)


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """Which model answers, and how it is asked.

    `tool_calling` picks between native function-calling and JSON-in-text. Cheap
    models frequently claim tool support and then emit malformed shapes; the text
    mode plus the format-teaching prompt is measurably more reliable for them, so
    it stays a first-class path rather than a fallback.
    """

    provider: Provider = "openai"
    model: str = "mimo-v2.5"
    base_url: str | None = None
    api_key: str | None = None
    temperature: float = 0.0
    max_tokens: int = 2048
    # On by default, and it is the single biggest lever on reliability with a small
    # model: the API validates the arguments against the schema, so a malformed
    # action is structurally impossible rather than merely repaired.
    #
    # Measured on mimo-v2.5, same task class: 37% of steps needed a format repair
    # with JSON-in-text, 0% with native tools.
    #
    # The earlier project concluded cheap models "need a custom system prompt to
    # output correct JSON" — true, but that was about JSON *in text*. It does not
    # follow that they cannot call tools, and mimo-v2.5 calls them correctly.
    # `LLM` falls back to the text path automatically if an endpoint rejects tools,
    # so a local server that does not implement them still works.
    tool_calling: bool = True
    request_timeout_s: float = 120.0
    # Ollama-only: how long to keep the model resident after a request. Ollama
    # evicts from VRAM after 5 minutes idle by default, and a cold reload of a 3B
    # VLM costs ~90s — measured as 107s cold against 19s warm. In an agent loop the
    # gaps between vision calls routinely exceed 5 minutes, so without this every
    # look pays the reload. Ignored by providers that do not understand it.
    keep_alive: str | None = None

    def redacted(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key": f"...{self.api_key[-4:]}" if self.api_key else None,
            "tool_calling": self.tool_calling,
        }


@dataclass(frozen=True, slots=True)
class BrowserConfig:
    """How the window comes up.

    `headless` exists but defaults off and should stay off: the entire premise is a
    window a human watches. It is here for CI, not for normal use.
    """

    executable_path: Path | None = None
    profile: str = "default"
    headless: bool = False
    # Fill the screen. More visible page means more controls land in-viewport and
    # a screenshot carries more of the page, which is exactly what both the
    # extractor and the vision model are limited by. `viewport` is the fallback
    # size when this is off.
    maximized: bool = True
    viewport: tuple[int, int] = (1440, 900)
    extensions: tuple[Path, ...] = ()
    load_ubo: bool = True
    devtools_panel: bool = False
    locale: str = "en-US"
    timezone: str | None = None
    extra_args: tuple[str, ...] = ()
    slow_mo_ms: int = 0


@dataclass(frozen=True, slots=True)
class LoopConfig:
    """Budgets for the agent loop.

    `max_steps` is a hard stop, not a target. `stuck_after` is the number of
    consecutive steps with no observable page change before the loop escalates —
    which means attaching a screenshot, not giving up.
    """

    max_steps: int = 40
    max_consecutive_errors: int = 4
    # Steps with no observable page change before the loop tells the model it is
    # getting nowhere. It gets a nudge first — read more of the page, try something
    # different — because the structural view usually *does* hold the answer and a
    # local vision call costs ~19s.
    stuck_after: int = 3
    # ...and this many before the vision model is actually consulted. The gap
    # between the two is the model's chance to solve it from text on its own.
    vision_after: int = 5
    screenshot_on_stuck: bool = True
    screenshot_every_step: bool = False
    # Screenshots are shrunk before going to the vision model. Its cost scales with
    # image *area* — Qwen2.5-VL tiles the input dynamically — so halving each
    # dimension quarters the tiles. 1.0 disables scaling.
    vision_scale: float = 0.6
    step_timeout_s: float = 90.0
    think_aloud: bool = True


@dataclass(frozen=True, slots=True)
class ChamberConfig:
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    # The step model: sees the page, emits the next action. Called every step, so
    # this is where token cost lives — a cheap model belongs here.
    model: ModelConfig = field(default_factory=ModelConfig)
    # Optional planner. Called rarely — at the start, when a stage completes, and
    # when the loop is stuck — to set the plan the step model works against.
    # Splitting them means the expensive model thinks a handful of times per run
    # instead of forty, and the cheap model never has to hold the whole task.
    # None collapses to a single-model loop, exactly as before.
    orchestrator: ModelConfig | None = None
    loop: LoopConfig = field(default_factory=LoopConfig)
    # Optional second model that looks at screenshots. Separate from `model` on
    # purpose: the planner may have no vision at all and does not need any, and a
    # small local VLM is enough for "what is on screen / what is in the way".
    # None disables the vision path entirely; screenshots still save to disk.
    vision: ModelConfig | None = None
    # Tried in order when `vision` fails or answers uselessly. A small local model
    # is free and private but sometimes hedges ("not visible in this part of the
    # screen"); a hosted one is the backstop for the cases that matter.
    vision_fallbacks: tuple[ModelConfig, ...] = ()
    trace: bool = True

    # --- convenience ------------------------------------------------------
    @property
    def profile(self) -> str:
        return self.browser.profile

    def with_profile(self, name: str) -> ChamberConfig:
        return replace(self, browser=replace(self.browser, profile=name))

    @classmethod
    def from_env(cls, **overrides: object) -> ChamberConfig:
        """Build from environment, then apply keyword overrides.

        Provider autodetection prefers an explicit CHAMBER_PROVIDER, then whichever
        key is actually present. An OpenAI-compatible base_url with no key is a
        legitimate local-server setup (LM Studio, vLLM), so a missing key is not by
        itself an error here — it fails later, at call time, with a clearer message.
        """
        provider = os.environ.get("CHAMBER_PROVIDER", "").strip().lower()
        anthropic_key = os.environ.get("CHAMBER_ANTHROPIC_API_KEY")
        openai_key = os.environ.get("CHAMBER_OPENAI_API_KEY") or os.environ.get(
            "OPENCODE_API_KEY"
        )
        if provider not in ("openai", "anthropic"):
            provider = "anthropic" if (anthropic_key and not openai_key) else "openai"

        if provider == "anthropic":
            model = ModelConfig(
                provider="anthropic",
                model=os.environ.get("CHAMBER_MODEL", "claude-sonnet-5"),
                base_url=os.environ.get("CHAMBER_ANTHROPIC_BASE_URL"),
                api_key=anthropic_key,
                tool_calling=_env_bool("CHAMBER_TOOL_CALLING", True),
            )
        else:
            model = ModelConfig(
                provider="openai",
                model=os.environ.get("CHAMBER_MODEL", "mimo-v2.5"),
                base_url=os.environ.get(
                    "CHAMBER_OPENAI_BASE_URL", "https://opencode.ai/zen/go/v1"
                ),
                api_key=openai_key,
                tool_calling=_env_bool("CHAMBER_TOOL_CALLING", True),
            )

        # Vision is opt-in by naming a model. Ollama's OpenAI-compatible endpoint is
        # the default base URL because that is what a local VLM on a laptop is
        # almost always behind, and it needs no key.
        vision_model = os.environ.get("CHAMBER_VISION_MODEL")
        vision_base = os.environ.get("CHAMBER_VISION_BASE_URL", "http://localhost:11434/v1")
        vision = (
            ModelConfig(
                provider=os.environ.get("CHAMBER_VISION_PROVIDER", "openai"),  # type: ignore[arg-type]
                model=vision_model,
                base_url=vision_base,
                api_key=os.environ.get("CHAMBER_VISION_API_KEY", "ollama"),
                max_tokens=512,
                # A local model on a laptop is slower than an API; a short timeout
                # here reads as "vision is broken" when it is merely thinking.
                request_timeout_s=180.0,
                # Ollama-only. Sending it to a hosted endpoint is at best ignored
                # and at worst a 400, so it is scoped to where it means something.
                keep_alive=(
                    os.environ.get("CHAMBER_VISION_KEEP_ALIVE", "30m")
                    if "11434" in vision_base
                    else None
                ),
            )
            if vision_model
            else None
        )

        def _named(prefix: str, default_base: str, default_key: str) -> ModelConfig | None:
            name = os.environ.get(f"{prefix}_MODEL")
            if not name:
                return None
            base = os.environ.get(f"{prefix}_BASE_URL", default_base)
            return ModelConfig(
                provider=os.environ.get(f"{prefix}_PROVIDER", "openai"),  # type: ignore[arg-type]
                model=name,
                base_url=base,
                api_key=os.environ.get(f"{prefix}_API_KEY", default_key),
                max_tokens=512,
                request_timeout_s=180.0,
                keep_alive=(
                    os.environ.get("CHAMBER_VISION_KEEP_ALIVE", "30m")
                    if "11434" in base
                    else None
                ),
            )

        # The planner. Same endpoint and key as the step model unless overridden,
        # because the common setup is two models from one provider.
        orchestrator = _named(
            "CHAMBER_ORCHESTRATOR",
            model.base_url or "https://api.openai.com/v1",
            model.api_key or "",
        )
        if orchestrator is not None:
            # A planner writes a plan, not a JSON action, and needs room for it.
            orchestrator = replace(orchestrator, max_tokens=2048, request_timeout_s=120.0)

        fallback = _named(
            "CHAMBER_VISION_FALLBACK",
            model.base_url or "https://api.openai.com/v1",
            model.api_key or "",
        )

        exe = os.environ.get("CHAMBER_BROWSER")
        browser = BrowserConfig(
            executable_path=Path(exe) if exe else None,
            profile=os.environ.get("CHAMBER_PROFILE", "default"),
            headless=_env_bool("CHAMBER_HEADLESS", False),
        )
        cfg = cls(
            browser=browser,
            model=model,
            orchestrator=orchestrator,
            vision=vision,
            vision_fallbacks=(fallback,) if fallback else (),
        )
        return _apply_overrides(cfg, overrides)


def _apply_overrides(cfg: ChamberConfig, overrides: dict[str, object]) -> ChamberConfig:
    """Apply dotted or nested overrides: `profile="x"`, `browser__headless=True`."""
    top: dict[str, object] = {}
    nested: dict[str, dict[str, object]] = {"browser": {}, "model": {}, "loop": {}}
    for key, value in overrides.items():
        if value is None:
            continue
        if "__" in key:
            section, _, attr = key.partition("__")
            if section not in nested:
                raise ValueError(f"unknown config section {section!r}")
            nested[section][attr] = value
        elif key == "profile":
            nested["browser"]["profile"] = value
        elif key in ("browser", "model", "loop", "vision", "trace"):
            top[key] = value
        else:
            raise ValueError(f"unknown config key {key!r}")

    if nested["browser"]:
        top.setdefault("browser", replace(cfg.browser, **nested["browser"]))
    if nested["model"]:
        top.setdefault("model", replace(cfg.model, **nested["model"]))
    if nested["loop"]:
        top.setdefault("loop", replace(cfg.loop, **nested["loop"]))
    return replace(cfg, **top) if top else cfg


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
