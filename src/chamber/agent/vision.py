"""Vision as a sensor, not as the substrate.

Chamber reads pages structurally, and that is the right default — a measured run
answered a research question in 3 steps and 21k tokens with no image at all. But
structure runs out in specific, predictable places:

* content painted into a `<canvas>` or baked into an image
* a layout that is visually broken in a way the DOM cannot express
* the agent is stuck and the control list is not explaining why

So the design is a **split brain**. The planner stays on whatever model is driving
the loop — which may have no vision at all, and does not need any. A second, small,
local model looks at the screenshot and answers one narrow question, and its
*answer in words* is what enters the planner's context.

That split buys three things:

1. **The planner never pays for image tokens.** A description is a hundred tokens;
   a screenshot is thousands, on every subsequent turn it stays in context.
2. **It works with text-only planners.** Most cheap OpenAI-compatible endpoints are.
3. **The sensor can be tiny.** "What is covering the button" is a much easier job
   than "plan a shopping task", so a 3B model on a laptop GPU is genuinely enough.

Configure it with `CHAMBER_VISION_MODEL`; leave it unset and chamber behaves exactly
as before, saving screenshots to disk without describing them.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from chamber.agent.llm import LLM, LLMError, Message
from chamber.config import ModelConfig

log = logging.getLogger(__name__)

# Deliberately narrow. A vision model asked to "describe this screenshot" returns
# an essay about the header; asked what the agent needs, it returns two sentences.
_SYSTEM = """\
You are the eyes of a browser agent. You are shown a screenshot of a web page and
asked one question about it.

Answer in at most four sentences. Be concrete and literal: name what you can see,
where it is on screen, and what it says. Do not speculate about what the page is
for, do not give advice, and do not describe anything the question did not ask
about.

If the answer is not visible in the screenshot, say exactly that rather than
guessing — the agent will act on what you say, and a confident wrong answer costs
it several steps.
"""

_DEFAULT_QUESTION = (
    "What is on this screen? Name the main content, and any dialog, banner, "
    "cookie notice or overlay covering it."
)


# Answers that are technically a response but tell the agent nothing. A local 3B
# model hedges like this when the detail is small or ambiguous, and treating it as
# a real answer wastes the fallback it exists for.
_USELESS = (
    "i can't see",
    "i cannot see",
    "unable to determine",
    "not visible in this part",
    "no image",
    "cannot determine",
    "i don't see any image",
)


def _is_useless(answer: str) -> bool:
    low = answer.strip().lower()
    if len(low) < 15:
        return True
    return any(phrase in low for phrase in _USELESS)


class Vision:
    """A model that looks at screenshots and answers in words.

    Takes a chain: the first entry is tried first, later ones are the backstop.
    The intended shape is a small local model doing the routine looking — free,
    private, ~19s — falling through to a hosted one when it fails outright or
    hedges. The fallback costs money and latency, so it fires only when the cheap
    answer was actually no use.
    """

    def __init__(self, config: ModelConfig, *fallbacks: ModelConfig) -> None:
        self.config = config
        self.fallbacks = fallbacks
        self._llm = LLM(config)
        self._fallback_llms = [LLM(f) for f in fallbacks]

    @property
    def _chain(self) -> list[LLM]:
        return [self._llm, *self._fallback_llms]

    def attach_observer(self, observer) -> None:
        """Route this sensor's traffic to a viewer.

        Set after construction because the session builds `Vision` before a caller
        has decided whether anything is watching.
        """
        for llm in self._chain:
            llm.observer = observer
            llm.role = "vision"

    async def __aenter__(self) -> Vision:
        for llm in self._chain:
            await llm.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        for llm in self._chain:
            await llm.__aexit__(*exc)

    async def warm(self) -> tuple[bool, float] | None:
        """Load the model into VRAM before the run needs it.

        Returns None when there is nothing to preload — a hosted endpoint has no
        local weights, and reporting "did not preload" for one would be a warning
        about a non-problem.

        Ollama loads lazily and evicts after an idle period. Measured on this
        machine: a cold load of qwen2.5vl:3b costs ~90s (107s cold against 19s
        warm), and in an agent loop the gaps between vision calls routinely exceed
        the idle timeout — so without this, the *first* time the agent is stuck it
        waits a minute and a half before getting an answer.

        Uses Ollama's native load endpoint (a generate call with no prompt), which
        loads and returns without producing tokens. Returns (loaded, seconds); a
        failure is not fatal, the model will simply load on first use.
        """
        import time

        base = (self.config.base_url or "").rstrip("/")
        if "11434" not in base:
            return None  # hosted endpoint; nothing to preload
        root = base[: -len("/v1")] if base.endswith("/v1") else base

        started = time.monotonic()
        try:
            response = await self._llm.client.post(
                f"{root}/api/generate",
                json={
                    "model": self.config.model,
                    "keep_alive": self.config.keep_alive or "30m",
                },
                timeout=self.config.request_timeout_s,
            )
            ok = response.status_code < 400
        except Exception as exc:
            log.info("could not preload vision model: %s", exc)
            return False, time.monotonic() - started
        return ok, time.monotonic() - started

    async def release(self) -> bool:
        """Evict the model from VRAM now, instead of waiting out `keep_alive`.

        `keep_alive=30m` is right *during* a run — it stops a cold reload costing
        ~90s mid-task. It is wrong the moment the run ends: 2.2 GB of VRAM sitting
        idle for half an hour on an 8 GB laptop card is memory the machine's owner
        wanted back. Sending `keep_alive: 0` unloads immediately.

        Only meaningful for a local Ollama endpoint; a hosted model has nothing
        resident to release.
        """
        base = (self.config.base_url or "").rstrip("/")
        if "11434" not in base:
            return False
        root = base[: -len("/v1")] if base.endswith("/v1") else base
        try:
            response = await self._llm.client.post(
                f"{root}/api/generate",
                json={"model": self.config.model, "keep_alive": 0},
                timeout=20.0,
            )
            ok = response.status_code < 400
            if ok:
                log.info("released %s from VRAM", self.config.model)
            return ok
        except Exception as exc:
            log.debug("could not release the vision model: %s", exc)
            return False

    async def look(self, png: bytes | Path, question: str = "") -> str:
        """Answer one question about a screenshot.

        Never raises. A sensor that takes the run down when the local server is
        not running is worse than one that says it could not see — the agent can
        carry on with the structural view, which is what it was using anyway.
        """
        try:
            data = png.read_bytes() if isinstance(png, Path) else png
            encoded = base64.b64encode(data).decode("ascii")
        except OSError as exc:
            log.debug("could not read screenshot: %s", exc)
            return "(the screenshot could not be read)"

        messages = [Message("user", question or _DEFAULT_QUESTION, images=[encoded])]
        last_error = ""

        for i, llm in enumerate(self._chain):
            try:
                response = await llm.complete(_SYSTEM, messages)
            except LLMError as exc:
                last_error = str(exc)
                log.info("vision model %s unavailable: %s", llm.config.model, exc)
                continue

            answer = response.text.strip()
            if answer and not _is_useless(answer):
                # Say which model answered when it was not the primary — otherwise
                # a silent, slower, billed fallback is invisible in the trace.
                return answer if i == 0 else f"{answer} [via {llm.config.model}]"

            last_error = f"{llm.config.model} gave no usable answer: {answer[:80]!r}"
            log.info("%s", last_error)

        return f"(no usable visual description — {last_error or 'no vision model responded'})"

    async def describe(self, png: bytes | Path) -> str:
        return await self.look(png)

    async def why_stuck(self, png: bytes | Path, goal: str) -> str:
        """The question worth asking when the loop has detected no progress.

        Phrased around obstruction rather than description, because "nothing
        changed for three steps" almost always means something is in the way that
        the control list did not convey — a modal, an empty state, an error toast,
        or a page that simply never rendered.
        """
        return await self.look(
            png,
            f"The agent is trying to: {goal}\n\n"
            "It has taken several actions with no visible effect. Looking at this "
            "screenshot, what is actually on screen right now, and is anything "
            "blocking or preventing that goal — a dialog, an overlay, an error "
            "message, an empty result, or a page that has not loaded?",
        )

    async def locate(self, png: bytes | Path, target: str) -> str:
        """Where something is on screen, when the DOM route has failed."""
        return await self.look(
            png,
            f"Where on this screen is: {target}? Describe its position "
            "(for example 'top right of the header', 'below the price, on the "
            "right') and the exact text on or beside it. If it is not visible, "
            "say so.",
        )


def vision_from_config(config: ModelConfig | None) -> Vision | None:
    return Vision(config) if config else None
