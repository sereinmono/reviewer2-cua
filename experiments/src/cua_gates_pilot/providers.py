"""Frozen diagnostic + human providers for the two gates.

No network, no SDK and no hidden environment access lives in this module: the
real OmniJev client (or a mock) is injected, and a HumanProvider is an external
callback. ``MockOmniJev`` / ``MockHumanProvider`` make the whole pilot testable
offline and without any API key.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from nano_cua import AgentContext, Proposal


class DiagnoseModel(Protocol):
    """Frozen diagnostic model: returns normalized signals in ``[0, 1]``."""

    async def observe_signals(self, context: AgentContext) -> Mapping[str, float]: ...

    async def human_signals(self, context: AgentContext, candidate: Proposal) -> Mapping[str, float]: ...


class CallableDiagnoseModel:
    """Adapt an externally owned frozen OmniJev client to ``DiagnoseModel``.

    The caller wraps whatever the actual OmniJev SDK/endpoint exposes; no
    OpenAI-compatible API shape is assumed here.
    """

    def __init__(
        self,
        *,
        observe_fn: Callable[[AgentContext], Awaitable[Mapping[str, float]]],
        human_fn: Callable[[AgentContext, Proposal], Awaitable[Mapping[str, float]]],
    ) -> None:
        self._observe_fn = observe_fn
        self._human_fn = human_fn

    async def observe_signals(self, context: AgentContext) -> Mapping[str, float]:
        return await self._observe_fn(context)

    async def human_signals(self, context: AgentContext, candidate: Proposal) -> Mapping[str, float]:
        return await self._human_fn(context, candidate)


def _pop_response(
    label: str,
    queue: list[Mapping[str, float] | Exception],
    default: Mapping[str, float] | None,
) -> Mapping[str, float]:
    if queue:
        item = queue.pop(0)
    elif default is not None:
        item = default
    else:
        raise RuntimeError(f"MockOmniJev has no scripted {label} response left")
    if isinstance(item, Exception):
        raise item
    return dict(item)


@dataclass
class MockOmniJev:
    """Scripted frozen diagnostic model for tests and demos.

    Responses are consumed in order; an ``Exception`` entry raises (to exercise
    conservative gate behavior). No real page state or task ground truth is
    available to this mock - callers script the signals explicitly.
    """

    observe_responses: list[Mapping[str, float] | Exception] = field(default_factory=list)
    human_responses: list[Mapping[str, float] | Exception] = field(default_factory=list)
    observe_default: Mapping[str, float] | None = None
    human_default: Mapping[str, float] | None = None
    observe_calls: int = 0
    human_calls: int = 0

    async def observe_signals(self, context: AgentContext) -> Mapping[str, float]:
        self.observe_calls += 1
        return _pop_response("observe", self.observe_responses, self.observe_default)

    async def human_signals(self, context: AgentContext, candidate: Proposal) -> Mapping[str, float]:
        self.human_calls += 1
        return _pop_response("human", self.human_responses, self.human_default)


@dataclass(frozen=True, slots=True)
class HumanQuestion:
    """Clarification request handed to a HumanProvider (no images / hidden state)."""

    task: str
    candidate: Proposal
    reason: str
    step: int


@dataclass(frozen=True, slots=True)
class HumanAnswer:
    text: str
    source: str = "mock"


class HumanProvider(Protocol):
    """External source of clarification; ``None`` means no usable answer."""

    async def ask(self, question: HumanQuestion) -> HumanAnswer | None: ...


@dataclass
class MockHumanProvider:
    """Deterministic human stand-in; answers are task-scripted, never page truth."""

    answers: list[str | None] = field(
        default_factory=lambda: ["Mock user: click the large button in the centre of the page."]
    )
    delay_s: float = 0.0
    error: Exception | None = None
    questions: list[HumanQuestion] = field(default_factory=list)

    @property
    def calls(self) -> int:
        return len(self.questions)

    async def ask(self, question: HumanQuestion) -> HumanAnswer | None:
        self.questions.append(question)
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        if not self.answers:
            return None
        text = self.answers.pop(0)
        return None if text is None else HumanAnswer(text=text)
