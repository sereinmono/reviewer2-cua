"""Frozen OmniJev diagnoser backed by a local Flide `/v1/systemone` server.

This is the M3 real-model adapter for the two-gate pilot: it does **not** decide
anything itself. It asks the frozen OneJev model for calibrated `noul`
probabilities and returns them as the normalized signals consumed by
``gates.py``. The server used in this repo is deployed by
``D:\\Research26fall\\omnijev-deploy`` (OneJev1.6-0.8B, Flide 0.1.4).

No LLM SDK and no CUA base changes: plain stdlib HTTP against localhost.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

from nano_cua import AgentContext, Proposal

DEFAULT_URL = "http://127.0.0.1:8000/v1/systemone"


def _action_text(action: Any) -> str:
    if action is None:
        return "none"
    args = ", ".join(f"{key}={value!r}" for key, value in action.args.items())
    return f"{action.space}.{action.kind}({args})"


def _result_text(result: Any) -> str:
    if result is None:
        return "none"
    if result.success:
        return "ok"
    return f"failed: {result.error}"


def _noul(answers: Mapping[str, Any], name: str) -> float:
    try:
        value = answers[name]["noul"]
    except (KeyError, TypeError):
        raise RuntimeError(f"OmniJev response has no answers[{name!r}].noul") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"OmniJev answers[{name!r}].noul is not a number: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise RuntimeError(f"OmniJev answers[{name!r}].noul is not finite")
    return number


class OmniJevDiagnoseModel:
    """`DiagnoseModel` implementation calling a local Flide SystemOne endpoint."""

    def __init__(self, *, url: str = DEFAULT_URL, timeout_s: float = 120.0) -> None:
        if not url:
            raise ValueError("url must be non-empty")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")
        self._url = url
        self._timeout_s = timeout_s
        self.calls = 0
        self.usage_input_tokens = 0
        self.usage_output_tokens = 0
        self.last_raw: dict[str, Any] | None = None

    @property
    def model_name(self) -> str | None:
        if not self.last_raw:
            return None
        name = self.last_raw.get("model")
        return name if isinstance(name, str) else None

    def _base_state(self, context: AgentContext) -> dict[str, Any]:
        state: dict[str, Any] = {
            "task": context.task,
            "actions_executed": context.action_count,
            "last_action": _action_text(context.last_action),
            "last_result": _result_text(context.last_result),
            "observation_fresh": context.observation_is_fresh,
        }
        return state

    def _observation_media(self, context: AgentContext) -> list[dict[str, str]]:
        observation = context.last_observation
        if observation is None:
            return []
        payload = observation.payload
        if not isinstance(payload, bytes) or not payload:
            return []
        content_type = observation.metadata.get("content_type")
        if not isinstance(content_type, str) or "/" not in content_type:
            content_type = "image/png"
        encoded = base64.b64encode(payload).decode("ascii")
        return [{"type": "image", "data": f"data:{content_type};base64,{encoded}"}]

    def _payload(
        self,
        context: AgentContext,
        questions: Mapping[str, Any],
        *,
        candidate: Proposal | None = None,
    ) -> dict[str, Any]:
        state = self._base_state(context)
        if candidate is not None:
            state["candidate_action"] = _action_text(candidate)
        media = self._observation_media(context)
        if media:
            state["screen"] = "<image:1>"
        return {"state": state, "media": media, "questions": dict(questions)}

    async def observe_signals(self, context: AgentContext) -> Mapping[str, float]:
        payload = self._payload(
            context,
            {
                "info_missing": {
                    "type": "noul",
                    "instructions": "The current observation contains too little information to choose the next action",
                },
                "possibly_stale": {
                    "type": "noul",
                    "instructions": "The observation may be out of date because an action was executed after it was captured",
                },
            },
        )
        data = await asyncio.to_thread(self._post, payload)
        answers = data.get("answers")
        if not isinstance(answers, Mapping):
            raise RuntimeError("OmniJev response has no 'answers' object")
        return {
            "info_missing": _noul(answers, "info_missing"),
            "possibly_stale": _noul(answers, "possibly_stale"),
        }

    async def human_signals(self, context: AgentContext, candidate: Proposal) -> Mapping[str, float]:
        # A noul *meta* question ("is the next action ambiguous? should we ask?")
        # was strongly biased to yes on both clear and ambiguous states. A
        # concrete factual claim about the target is better calibrated here:
        # high P(unique target) => low intent ambiguity. The fixed 0.65 gate
        # threshold is unchanged; only the frozen model's signal changes.
        payload = self._payload(
            context,
            {
                "target_unique": {
                    "type": "noul",
                    "instructions": "The instruction identifies a unique target element for the candidate action",
                },
            },
            candidate=candidate,
        )
        data = await asyncio.to_thread(self._post, payload)
        answers = data.get("answers")
        if not isinstance(answers, Mapping):
            raise RuntimeError("OmniJev response has no 'answers' object")
        unique_target = _noul(answers, "target_unique")
        return {"intent_ambiguity": 1.0 - unique_target}

    def _post(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        # The Flide server is local; do not let a Windows proxy env intercept it.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=self._timeout_s) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OmniJev request failed: {type(exc).__name__}: {exc}") from exc
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise RuntimeError("OmniJev response is not a JSON object")
        usage = data.get("usage")
        if isinstance(usage, Mapping):
            for key, attr in (("input_tokens", "usage_input_tokens"), ("output_tokens", "usage_output_tokens")):
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    setattr(self, attr, getattr(self, attr) + value)
        self.calls += 1
        self.last_raw = data
        return data
