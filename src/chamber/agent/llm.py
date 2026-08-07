"""One interface, two wire protocols.

Raw `httpx` rather than the vendor SDKs, deliberately. The whole surface used here
is one POST and one response shape per provider, and taking the SDKs would pull in
two dependency trees to save about forty lines. It also keeps OpenAI-compatible
endpoints — OpenCode Go, OpenRouter, vLLM, LM Studio, Ollama — working without
caring which one is on the other end.

Two output modes, both first-class:

* **Tool calling**, when the model does it properly. The action schema goes over as
  function definitions and comes back structured.
* **JSON in text**, for everything else. Cheaper models routinely advertise tool
  support and then emit `{"click": 42}` where `{"click": {"index": 42}}` was
  required. The prompt teaches the exact shape and `parse.py` is forgiving about
  the ways they get it wrong — which, measured on the earlier prototype, is what
  took a cheap model from falling apart after ~10 steps to finishing a 20-step task.

Retries cover the transport only. A model that returns something unparseable is not
a transport problem — that goes back through the loop as feedback, which is the one
mechanism that actually teaches it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from chamber.config import ModelConfig

log = logging.getLogger(__name__)

Role = Literal["system", "user", "assistant"]


@dataclass(slots=True)
class Message:
    role: Role
    content: str
    # Base64-encoded PNGs. Kept on the message rather than as a separate parameter
    # because an image is always *about* the text it arrives with — "here is the
    # screenshot, what is covering the button" is one turn, not two.
    images: list[str] = field(default_factory=list)

    def to_openai(self, image_style: str = "object") -> dict[str, Any]:
        """OpenAI-shaped message.

        `image_style` exists because "OpenAI-compatible" is not one format. The
        spec says `image_url` is an object with a `url` key, and OpenAI itself
        requires that — but some gateways accept only a bare data-URI string and
        return HTTP 400 for the object. Measured on OpenCode Go's gpt-5.6-luna:
        object → 400, string → 200 with an accurate description. The client
        detects which one the endpoint wants and remembers it.
        """
        if not self.images:
            return {"role": self.role, "content": self.content}
        parts: list[dict[str, Any]] = [{"type": "text", "text": self.content}]
        for b64 in self.images:
            uri = f"data:image/png;base64,{b64}"
            parts.append(
                {"type": "image_url", "image_url": uri}
                if image_style == "string"
                else {"type": "image_url", "image_url": {"url": uri}}
            )
        return {"role": self.role, "content": parts}

    def to_anthropic(self) -> dict[str, Any]:
        if not self.images:
            return {"role": self.role, "content": self.content}
        parts: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": b64},
            }
            for b64 in self.images
        ]
        # Text after images: both providers attend better to an instruction that
        # follows the thing it refers to.
        parts.append({"type": "text", "text": self.content})
        return {"role": self.role, "content": parts}


@dataclass(slots=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    call_id: str = ""


@dataclass(slots=True)
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.text.strip() and not self.tool_calls


class LLMError(RuntimeError):
    """Transport or provider error, after retries are exhausted."""


class LLM:
    """A chat model, whichever protocol it speaks."""

    def __init__(
        self,
        config: ModelConfig,
        *,
        client: httpx.AsyncClient | None = None,
        observer: Callable[[str, dict[str, Any]], None] | None = None,
        role: str = "model",
    ) -> None:
        self.config = config
        # Fired on every request and response. The live view is the only consumer;
        # without it the model traffic is invisible, which is the one part of the
        # system a person most wants to watch.
        self.observer = observer
        self.role = role
        self._client = client
        self._owns_client = client is None
        self.total_input = 0
        self.total_output = 0
        # Which image encoding this endpoint accepts. Starts at the spec-correct
        # object form and flips to the string form if the endpoint rejects it —
        # see `Message.to_openai`.
        self._image_style = "object"
        # Cleared if the endpoint turns out not to implement tool calling.
        self._tools_work = True

    @property
    def using_tools(self) -> bool:
        """Whether tool calling is both configured and actually working here."""
        return self.config.tool_calling and self._tools_work

    async def __aenter__(self) -> LLM:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.request_timeout_s)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.request_timeout_s)
        return self._client

    # ------------------------------------------------------------------ public

    async def complete(
        self,
        system: str,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        if not self.config.api_key and self.config.provider == "anthropic":
            raise LLMError("No API key. Set CHAMBER_ANTHROPIC_API_KEY.")

        has_images = any(m.images for m in messages)

        if self.config.provider == "anthropic":
            payload, url, headers = self._anthropic_request(system, messages, tools)
            parse = self._parse_anthropic
        else:
            payload, url, headers = self._openai_request(system, messages, tools)
            parse = self._parse_openai

        sent_tools = bool(tools) and self.config.tool_calling and self._tools_work
        started = time.monotonic()
        self._notify(
            "llm_request",
            role=self.role,
            model=self.config.model,
            system_chars=len(system),
            messages=len(messages),
            prompt_chars=sum(len(m.content) for m in messages),
            images=sum(len(m.images) for m in messages),
            tools=len(tools) if sent_tools else 0,
            last_user=next(
                (m.content for m in reversed(messages) if m.role == "user"), ""
            )[-2000:],
        )
        try:
            data = await self._post(url, payload, headers)
        except LLMError:
            # A 400 on a request carrying images is nearly always the endpoint
            # disagreeing about the image encoding rather than a real failure.
            # Flip the style once and retry; the choice sticks for the session so
            # the cost is paid at most once.
            if has_images and self.config.provider != "anthropic" and self._image_style == "object":
                log.info("image request rejected; retrying with the string image encoding")
                self._image_style = "string"
                payload, url, headers = self._openai_request(system, messages, tools)
                data = await self._post(url, payload, headers)
            elif sent_tools:
                # Not every OpenAI-compatible server implements tools — an older
                # vLLM or a bare llama.cpp will reject them. Drop to the JSON-in-
                # text path, which the prompt and parser fully support, rather
                # than failing the run. Remembered for the session.
                log.warning("endpoint rejected tools; falling back to JSON-in-text")
                self._tools_work = False
                payload, url, headers = self._openai_request(system, messages, None)
                data = await self._post(url, payload, headers)
            else:
                raise
        response = parse(data)
        self.total_input += response.input_tokens
        self.total_output += response.output_tokens
        self._notify(
            "llm_response",
            role=self.role,
            model=self.config.model,
            seconds=time.monotonic() - started,
            text=response.text[:2000],
            tool_calls=[
                {"name": c.name, "arguments": c.arguments} for c in response.tool_calls
            ],
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            stop_reason=response.stop_reason,
        )
        return response

    def _notify(self, event: str, **payload: Any) -> None:
        if self.observer is None:
            return
        try:
            self.observer(event, payload)
        except Exception:
            log.debug("llm observer raised", exc_info=True)

    # ----------------------------------------------------------------- request

    def _anthropic_request(
        self, system: str, messages: list[Message], tools: list[dict[str, Any]] | None
    ) -> tuple[dict[str, Any], str, dict[str, str]]:
        base = (self.config.base_url or "https://api.anthropic.com").rstrip("/")
        payload: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "system": system,
            "messages": [m.to_anthropic() for m in messages if m.role != "system"],
        }
        if tools and self.using_tools:
            payload["tools"] = tools
        return (
            payload,
            f"{base}/v1/messages",
            {
                "x-api-key": self.config.api_key or "",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )

    def _openai_request(
        self, system: str, messages: list[Message], tools: list[dict[str, Any]] | None
    ) -> tuple[dict[str, Any], str, dict[str, str]]:
        base = (self.config.base_url or "https://api.openai.com/v1").rstrip("/")
        payload: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "messages": [
                {"role": "system", "content": system},
                *(m.to_openai(self._image_style) for m in messages),
            ],
        }
        if tools and self.using_tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["input_schema"],
                    },
                }
                for t in tools
            ]
        if self.config.keep_alive:
            # Ollama reads this on its OpenAI-compatible endpoint; other servers
            # ignore unknown top-level fields.
            payload["keep_alive"] = self.config.keep_alive

        headers = {"content-type": "application/json"}
        if self.config.api_key:
            headers["authorization"] = f"Bearer {self.config.api_key}"
        return payload, f"{base}/chat/completions", headers

    # ------------------------------------------------------------------- wire

    async def _post(
        self, url: str, payload: dict[str, Any], headers: dict[str, str], *, attempts: int = 4
    ) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                response = await self.client.post(url, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
                await self._backoff(attempt)
                continue

            if response.status_code in (429, 500, 502, 503, 504, 529):
                # Honour Retry-After when the provider bothers to send one; it is
                # better information than any backoff curve we could guess.
                retry_after = response.headers.get("retry-after")
                delay = float(retry_after) if (retry_after or "").replace(".", "", 1).isdigit() else None
                last = LLMError(f"HTTP {response.status_code}: {response.text[:300]}")
                log.warning("provider returned %s, retrying", response.status_code)
                await self._backoff(attempt, fixed=delay)
                continue

            if response.status_code >= 400:
                raise LLMError(
                    f"HTTP {response.status_code} from {url}\n{response.text[:800]}"
                )

            try:
                return response.json()
            except json.JSONDecodeError as exc:
                raise LLMError(f"Provider returned non-JSON: {response.text[:300]}") from exc

        raise LLMError(f"{attempts} attempts failed: {last}")

    @staticmethod
    async def _backoff(attempt: int, *, fixed: float | None = None) -> None:
        # Jitter matters when several chamber sessions share one key — synchronised
        # retries turn one rate limit into a stampede.
        delay = fixed if fixed is not None else min(2**attempt, 16) * (0.6 + random.random() * 0.8)
        await asyncio.sleep(delay)

    # ----------------------------------------------------------------- parsing

    @staticmethod
    def _parse_anthropic(data: dict[str, Any]) -> LLMResponse:
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(
                    ToolCall(
                        name=block.get("name", ""),
                        arguments=block.get("input") or {},
                        call_id=block.get("id", ""),
                    )
                )
        usage = data.get("usage") or {}
        return LLMResponse(
            text="\n".join(text_parts).strip(),
            tool_calls=calls,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            stop_reason=data.get("stop_reason", ""),
            raw=data,
        )

    @staticmethod
    def _parse_openai(data: dict[str, Any]) -> LLMResponse:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"No choices in response: {json.dumps(data)[:300]}")
        message = choices[0].get("message") or {}

        calls: list[ToolCall] = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                # A model that emits malformed tool arguments should not take the
                # run down — surface it as text and let the repair path handle it.
                log.debug("unparseable tool arguments: %s", raw_args[:200])
                args = {"_raw": raw_args}
            calls.append(ToolCall(name=fn.get("name", ""), arguments=args, call_id=call.get("id", "")))

        usage = data.get("usage") or {}
        return LLMResponse(
            text=(message.get("content") or "").strip(),
            tool_calls=calls,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            stop_reason=choices[0].get("finish_reason", ""),
            raw=data,
        )
