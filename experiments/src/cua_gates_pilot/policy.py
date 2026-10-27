"""``GatedPolicy``: external AgentPolicy implementing the two-gate pilot.

The nano-cua base is untouched. This class uses only public interfaces:

* ``AgentPolicy.next_command`` is the single integration point;
* observations are requested only by returning ``Observe`` commands (the Runner
  then calls the injected Observer);
* the main Actor is called directly through the public ``Actor`` protocol;
* user clarification is injected by constructing a public ``AgentContext``
  (frozen dataclass, ``dataclasses.replace``) with the clarification appended to
  the task description, then calling the same Actor again.

Order per action step: Observe Gate -> (Observe?) -> Actor -> AskHuman Gate ->
(PASS: Execute | ASK: HumanProvider -> Actor replan -> Execute). At most one
human request is made per action step. No screenshots or actions are executed by
this policy itself.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace

from nano_cua import (
    Action,
    ActionResult,
    Actor,
    AgentContext,
    Command,
    Execute,
    Finish,
    ObservationRequest,
    Observe,
    Proposal,
)

from .gates import (
    GateDecision,
    conservative_human_decision,
    conservative_observe_decision,
    decide_human,
    decide_observe,
)
from .providers import DiagnoseModel, HumanProvider, HumanQuestion


@dataclass
class GateMetrics:
    """Run-level counters/timings for the two gates (inspectable in tests/mains)."""

    observe_gate_enabled: bool = True
    ask_human_gate_enabled: bool = True
    forced_initial_observations: int = 0
    observe_commands: int = 0
    execute_commands: int = 0
    main_vlm_calls: int = 0
    human_requests: int = 0
    human_replans: int = 0
    observe_gate_events: list[GateDecision] = field(default_factory=list)
    ask_gate_events: list[GateDecision] = field(default_factory=list)
    execution_feedback: list[ActionResult] = field(default_factory=list)
    actor_total_s: float = 0.0
    observe_gate_total_s: float = 0.0
    ask_gate_total_s: float = 0.0


class GatedPolicy:
    """Observe Gate + AskHuman Gate around an injected base Actor."""

    def __init__(
        self,
        *,
        actor: Actor,
        observe_diagnoser: DiagnoseModel,
        ask_diagnoser: DiagnoseModel,
        human_provider: HumanProvider | None = None,
        observe_gate_enabled: bool = True,
        ask_human_gate_enabled: bool = True,
        observation_request: ObservationRequest | None = None,
        human_timeout_s: float = 10.0,
    ) -> None:
        if human_timeout_s <= 0:
            raise ValueError("human_timeout_s must be > 0")
        self._actor = actor
        self._observe_diagnoser = observe_diagnoser
        self._ask_diagnoser = ask_diagnoser
        self._human_provider = human_provider
        self._observation_request = observation_request if observation_request is not None else ObservationRequest()
        self._human_timeout_s = human_timeout_s
        # Lightweight phase flag: after we return Observe, the next policy call
        # goes straight to the Actor (never two consecutive observations).
        self._awaiting_actor = False
        self.metrics = GateMetrics(
            observe_gate_enabled=observe_gate_enabled,
            ask_human_gate_enabled=ask_human_gate_enabled,
        )

    async def next_command(self, context: AgentContext) -> Command:
        self._record_execution_feedback(context)

        if self._awaiting_actor:
            self._awaiting_actor = False
            return await self._propose(context)

        if context.last_observation is None:
            # Zero-information initialization: force exactly one Observe and do
            # not ask the diagnostic model about an empty state.
            self.metrics.forced_initial_observations += 1
            self.metrics.observe_commands += 1
            self._awaiting_actor = True
            return Observe(self._observation_request)

        should_observe = await self._observe_gate(context)
        if should_observe:
            self.metrics.observe_commands += 1
            self._awaiting_actor = True
            return Observe(self._observation_request)

        return await self._propose(context)

    async def _observe_gate(self, context: AgentContext) -> bool:
        if not self.metrics.observe_gate_enabled:
            # Disabled gate: fall back to the base BasicPolicy observation rule.
            return not context.observation_is_fresh

        started = time.perf_counter()
        try:
            signals = await self._observe_diagnoser.observe_signals(context)
            decision = decide_observe(signals)
        except Exception as exc:  # noqa: BLE001 - conservative gate behavior
            decision = conservative_observe_decision(f"{type(exc).__name__}: {exc}")
        latency_ms = (time.perf_counter() - started) * 1000.0
        self.metrics.observe_gate_total_s += latency_ms / 1000.0
        self.metrics.observe_gate_events.append(replace(decision, latency_ms=latency_ms, step=context.step_count))
        return decision.decision == "OBSERVE"

    async def _propose(self, context: AgentContext) -> Command:
        candidate = await self._call_actor(context)
        if isinstance(candidate, Finish):
            return candidate  # Finish is never scored by the AskHuman gate
        if not isinstance(candidate, Action):
            raise TypeError(f"actor returned an unsupported proposal: {type(candidate).__name__}")

        if not self.metrics.ask_human_gate_enabled:
            self.metrics.execute_commands += 1
            return Execute(candidate)

        decision = await self._ask_gate(context, candidate)
        if decision.decision == "PASS":
            self.metrics.execute_commands += 1
            return Execute(candidate)
        return await self._ask_and_replan(context, candidate, decision)

    async def _ask_gate(self, context: AgentContext, candidate: Proposal) -> GateDecision:
        started = time.perf_counter()
        try:
            signals = await self._ask_diagnoser.human_signals(context, candidate)
            decision = decide_human(signals)
        except Exception as exc:  # noqa: BLE001 - conservative gate behavior
            decision = conservative_human_decision(f"{type(exc).__name__}: {exc}")
        latency_ms = (time.perf_counter() - started) * 1000.0
        self.metrics.ask_gate_total_s += latency_ms / 1000.0
        self.metrics.ask_gate_events.append(replace(decision, latency_ms=latency_ms, step=context.step_count))
        return decision

    async def _ask_and_replan(self, context: AgentContext, candidate: Action, decision: GateDecision) -> Command:
        self.metrics.human_requests += 1
        if self._human_provider is None:
            return Finish(reason="needs_human: no HumanProvider configured", success=False)

        question = HumanQuestion(task=context.task, candidate=candidate, reason=decision.reason, step=context.step_count)
        try:
            answer = await asyncio.wait_for(self._human_provider.ask(question), timeout=self._human_timeout_s)
        except Exception as exc:  # noqa: BLE001 - never guess after a failed human call
            return Finish(reason=f"needs_human: provider failure: {type(exc).__name__}: {exc}", success=False)
        if answer is None or not answer.text.strip():
            return Finish(reason="needs_human: no clarification received", success=False)

        # Clarification travels through the public AgentContext surface: a new
        # frozen context whose task text carries the user's clarification. The
        # original candidate action was never executed.
        clarified_context = replace(
            context,
            task=f"{context.task}\n\nUser clarification: {answer.text.strip()}",
        )
        replanned = await self._call_actor(clarified_context)
        if isinstance(replanned, Finish):
            return replanned
        if not isinstance(replanned, Action):
            raise TypeError(f"actor returned an unsupported proposal after clarification: {type(replanned).__name__}")
        # No second AskHuman evaluation in the same action step (max 1 ask/step).
        self.metrics.human_replans += 1
        self.metrics.execute_commands += 1
        return Execute(replanned)

    async def _call_actor(self, context: AgentContext) -> Proposal:
        started = time.perf_counter()
        try:
            return await self._actor.propose(context)
        finally:
            self.metrics.main_vlm_calls += 1
            self.metrics.actor_total_s += time.perf_counter() - started

    def _record_execution_feedback(self, context: AgentContext) -> None:
        if context.last_result is None:
            return
        if context.action_count > len(self.metrics.execution_feedback):
            self.metrics.execution_feedback.append(context.last_result)
