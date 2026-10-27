"""Generic VLM Actor: OpenAI-compatible chat-completions -> ``Proposal``.

This adapter is action-space agnostic: the caller injects both the model-facing
system prompt and a proposal parser supplied by a compatibility group (for
example ``nano_cua_extensions.playwright_browser``). That makes the same actor
reusable for browser, desktop, bash or any other action space without touching
the nano-cua base or this class.

Model/provider details also stop at this boundary: the endpoint, model name and
optional credential are injected, traffic stays OpenAI-compatible, and the base
package never imports this module. Malformed model output raises
``ActionValidationError`` instead of being executed.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from nano_cua import ActionValidationError, AgentContext, Observation, Proposal

ProposalParser = Callable[[Mapping[str, Any], Observation], Proposal]
"""Injected by an action-space group: decoded JSON + observation -> Proposal."""

_FENCE_RE = re.compile(r"^```(?:json)?\s*(?P<body>.*?)\s*```$", re.DOTALL | re.IGNORECASE)


def parse_json_object(text: str) -> Mapping[str, Any]:
    """Extract one JSON object from a model reply (tolerating code fences/prose)."""
    stripped = text.strip()
    fenced = _FENCE_RE.match(stripped)
    if fenced:
        stripped = fenced.group("body").strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ActionValidationError(f"model reply is not a JSON object: {stripped[:120]!r}")
    candidate = stripped[start : end + 1]
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ActionValidationError(f"model reply is not valid JSON: {exc}") from None
    if not isinstance(payload, Mapping):
        raise ActionValidationError(f"model reply must contain a JSON object, got {type(payload).__name__}")
    return payload


def _observation_data_url(observation: Observation) -> str:
    payload = observation.payload
    if not isinstance(payload, bytes) or not payload:
        raise RuntimeError("VLMActor needs an observation with non-empty image bytes")
    content_type = observation.metadata.get("content_type")
    if not isinstance(content_type, str) or "/" not in content_type:
        content_type = "image/png"
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def _message_text(data: Mapping[str, Any]) -> str:
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("unexpected VLM response: missing choices[0].message") from None
    if not isinstance(message, Mapping):
        raise RuntimeError("unexpected VLM response: message is not an object")
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [str(part["text"]) for part in content if isinstance(part, Mapping) and isinstance(part.get("text"), str)]
        if texts:
            return "\n".join(texts)
    raise RuntimeError("VLM response contains no text content")


def _build_http_client(timeout_s: float) -> tuple[httpx.AsyncClient, bool]:
    """Create the actor's HTTP client, tolerating malformed proxy env vars.

    httpx parses ``HTTP(S)_PROXY`` / ``NO_PROXY`` eagerly; some Windows setups
    contain entries such as ``[::1]`` that httpx 0.28 rejects with InvalidURL.
    Falling back to a client that ignores environment proxies keeps the demo
    runnable instead of failing during construction.
    """
    timeout = httpx.Timeout(timeout_s)
    try:
        return httpx.AsyncClient(timeout=timeout), False
    except httpx.InvalidURL:
        return httpx.AsyncClient(timeout=timeout, trust_env=False), True


class VLMActor:
    """Chat-completions VLM behind the Actor port.

    ``base_url`` is an OpenAI-compatible endpoint root (for example
    ``https://host/v1``) or the full ``.../chat/completions`` URL. The optional
    ``client`` argument injects an ``httpx.AsyncClient`` (tests, connection
    reuse); when omitted the actor owns and must close its client.

    ``system_prompt`` and ``proposal_parser`` come from the action-space group,
    so this class never hardcodes a concrete action vocabulary.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        system_prompt: str,
        proposal_parser: ProposalParser,
        api_key: str | None = None,
        json_mode: bool = False,
        max_tokens: int = 512,
        timeout_s: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        if not model.strip():
            raise ValueError("model must be a non-empty string")
        if not system_prompt.strip():
            raise ValueError("system_prompt must be a non-empty string")
        if not callable(proposal_parser):
            raise TypeError("proposal_parser must be callable")
        normalized = base_url.rstrip("/")
        self._endpoint = normalized if normalized.endswith("/chat/completions") else f"{normalized}/chat/completions"
        self._model = model
        self._api_key = api_key
        self._system_prompt = system_prompt
        self._proposal_parser = proposal_parser
        self._json_mode = json_mode
        self._max_tokens = max_tokens
        if client is not None:
            self._client = client
            self._owns_client = False
            self._proxy_env_ignored = False
        else:
            self._client, self._proxy_env_ignored = _build_http_client(timeout_s)
            self._owns_client = True

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def model(self) -> str:
        return self._model

    @property
    def proxy_env_ignored(self) -> bool:
        """True when malformed proxy env vars forced a client without them."""
        return self._proxy_env_ignored

    async def propose(self, context: AgentContext) -> Proposal:
        observation = context.last_observation
        if observation is None:
            raise RuntimeError("VLMActor needs an observation in the context; ensure the policy observes first")
        messages = self._build_messages(context, observation)
        data = await self._post(messages)
        return self._proposal_parser(parse_json_object(_message_text(data)), observation)

    def _build_messages(self, context: AgentContext, observation: Observation) -> list[dict[str, Any]]:
        lines = [
            f"Task: {context.task}",
            f"Action number: {context.action_count + 1} (actions executed so far: {context.action_count})",
        ]
        width = observation.metadata.get("width")
        height = observation.metadata.get("height")
        if (
            isinstance(width, int)
            and isinstance(height, int)
            and not isinstance(width, bool)
            and not isinstance(height, bool)
            and width > 0
            and height > 0
        ):
            lines.append(f"Image size: {width}x{height} pixels.")
        if context.last_action is not None:
            lines.append(f"Last action: {context.last_action.space}.{context.last_action.kind} {dict(context.last_action.args)}")
        if context.last_result is not None:
            if context.last_result.success:
                lines.append("Last action result: ok")
            else:
                lines.append(f"Last action result: failed: {context.last_result.error}")
        lines.append("Decide the next single action. Reply with one JSON object only.")
        return [
            {"role": "system", "content": self._system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "\n".join(lines)},
                    {"type": "image_url", "image_url": {"url": _observation_data_url(observation)}},
                ],
            },
        ]

    async def _post(self, messages: list[dict[str, Any]]) -> Mapping[str, Any]:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": self._max_tokens,
            "stream": False,
        }
        if self._json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            response = await self._client.post(self._endpoint, json=body, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            excerpt = " ".join(exc.response.text[:300].split())
            raise RuntimeError(f"VLM request failed with HTTP {exc.response.status_code}: {excerpt}") from None
        except httpx.HTTPError as exc:
            raise RuntimeError(f"VLM request failed: {type(exc).__name__}: {exc}") from None
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(f"VLM response is not JSON: {exc}") from None
        if not isinstance(data, Mapping):
            raise RuntimeError("VLM response must be a JSON object")
        return data

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> VLMActor:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()
