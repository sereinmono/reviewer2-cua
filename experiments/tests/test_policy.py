"""Behavioural tests for GatedPolicy with in-memory fakes only.

No browser, no VLM API and no OmniJev: the tests assert insertion order,
observation counts, human clarification replanning, conservative failure and
independent gate switches.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from cua_gates_pilot.policy import GatedPolicy
from cua_gates_pilot.providers import MockHumanProvider, MockOmniJev
from nano_cua import (
    Action,
    ActionResult,
    AgentContext,
    AgentPolicy,
    Finish,
    Observation,
    ObservationRequest,
    Proposal,
    RunResult,
    Runner,
)

LOW_OBSERVE = {"info_missing": 0.1, "possibly_stale": 0.1}
HIGH_OBSERVE = {"info_missing": 1.0, "possibly_stale": 1.0}
LOW_HUMAN = {"intent_ambiguity": 0.1}
HIGH_HUMAN = {"intent_ambiguity": 1.0}

ACTION_A = Action(space="demo", kind="poke", args={"n": 1})
ACTION_B = Action(space="demo", kind="poke", args={"n": 2})


def make_observation(tag: str) -> Observation:
    return Observation(
        source="fake",
        kind="screenshot",
        payload=tag.encode("utf-8"),
        captured_at=time.time(),
        metadata={"width": 100, "height": 50},
    )


class FakeObserver:
    def __init__(self) -> None:
        self.calls = 0

    async def observe(self, request: ObservationRequest) -> Observation:
        self.calls += 1
        return make_observation(f"frame-{self.calls}")


class FakeExecutor:
    def __init__(self) -> None:
        self.actions: list[Action] = []
        self.results: list[ActionResult] = []

    async def execute(self, action: Action) -> ActionResult:
        self.actions.append(action)
        result = ActionResult(success=True, duration_s=0.0)
        self.results.append(result)
        return result


class ScriptedActor:
    def __init__(self, script: list[Proposal]) -> None:
        self._script = list(script)
        self._index = 0
        self.contexts: list[AgentContext] = []

    async def propose(self, context: AgentContext) -> Proposal:
        self.contexts.append(context)
        if self._index >= len(self._script):
            raise RuntimeError("actor script exhausted")
        proposal = self._script[self._index]
        self._index += 1
        return proposal


@dataclass
class Harness:
    policy: GatedPolicy
    observer: FakeObserver
    executor: FakeExecutor
    actor: ScriptedActor
    human: MockHumanProvider
    observe_jev: MockOmniJev
    ask_jev: MockOmniJev
    result: RunResult


async def run_policy(
    *,
    script: list[Proposal],
    observe_responses: list[dict] | None = None,
    human_responses: list[dict] | None = None,
    human_answers: list[str | None] | None = None,
    human_delay_s: float = 0.0,
    human_error: Exception | None = None,
    observe_gate: bool = True,
    ask_gate: bool = True,
    human_timeout_s: float = 10.0,
    max_actions: int = 5,
    max_observations: int = 6,
) -> Harness:
    actor = ScriptedActor(script)
    observer = FakeObserver()
    executor = FakeExecutor()
    observe_jev = MockOmniJev(observe_responses=list(observe_responses or []))
    ask_jev = MockOmniJev(human_responses=list(human_responses or []))
    human = MockHumanProvider(
        answers=list(human_answers if human_answers is not None else []),
        delay_s=human_delay_s,
        error=human_error,
    )
    policy = GatedPolicy(
        actor=actor,
        observe_diagnoser=observe_jev,
        ask_diagnoser=ask_jev,
        human_provider=human,
        observe_gate_enabled=observe_gate,
        ask_human_gate_enabled=ask_gate,
        human_timeout_s=human_timeout_s,
    )
    runner = Runner(
        policy=policy,
        observer=observer,
        executor=executor,
        task="test task",
        max_actions=max_actions,
        max_observations=max_observations,
    )
    result = await runner.run()
    return Harness(policy, observer, executor, actor, human, observe_jev, ask_jev, result)


async def test_initial_observation_is_forced_exactly_once():
    harness = await run_policy(script=[Finish(reason="done")])

    assert harness.result.success is True
    assert harness.observer.calls == 1
    assert harness.result.observation_count == 1
    assert harness.policy.metrics.forced_initial_observations == 1
    assert harness.policy.metrics.observe_gate_events == []
    assert harness.policy.metrics.main_vlm_calls == 1
    assert harness.observe_jev.observe_calls == 0
    assert harness.executor.actions == []


async def test_reuse_decision_skips_new_screenshot():
    harness = await run_policy(
        script=[ACTION_A, Finish(reason="done")],
        observe_responses=[LOW_OBSERVE],
        human_responses=[LOW_HUMAN],
    )

    assert harness.result.success is True
    assert harness.observer.calls == 1
    assert harness.result.observation_count == 1
    assert [event.decision for event in harness.policy.metrics.observe_gate_events] == ["REUSE"]
    assert harness.executor.actions == [ACTION_A]
    assert harness.policy.metrics.main_vlm_calls == 2
    assert harness.policy.metrics.execution_feedback[0].success is True


async def test_observe_decision_refreshes_exactly_once():
    harness = await run_policy(
        script=[ACTION_A, Finish(reason="done")],
        observe_responses=[HIGH_OBSERVE],
        human_responses=[LOW_HUMAN],
    )

    assert harness.result.success is True
    assert harness.observer.calls == 2
    assert harness.result.observation_count == 2
    assert [event.decision for event in harness.policy.metrics.observe_gate_events] == ["OBSERVE"]
    assert harness.executor.actions == [ACTION_A]
    assert harness.policy.metrics.main_vlm_calls == 2


async def test_ask_gate_pass_executes_original_candidate():
    harness = await run_policy(
        script=[ACTION_A, Finish(reason="done")],
        observe_responses=[LOW_OBSERVE],
        human_responses=[LOW_HUMAN],
    )

    assert harness.executor.actions == [ACTION_A]
    assert harness.human.calls == 0
    assert [event.decision for event in harness.policy.metrics.ask_gate_events] == ["PASS"]
    assert harness.result.success is True


async def test_ask_gate_ask_replans_and_never_executes_original_action():
    harness = await run_policy(
        script=[ACTION_A, ACTION_B, Finish(reason="done")],
        observe_responses=[LOW_OBSERVE, LOW_OBSERVE],
        human_responses=[HIGH_HUMAN],
        human_answers=["Click the centre button."],
    )

    assert harness.executor.actions == [ACTION_B]
    assert ACTION_A not in harness.executor.actions
    assert harness.human.calls == 1
    assert [event.decision for event in harness.policy.metrics.ask_gate_events] == ["ASK"]
    assert len(harness.actor.contexts) == 3
    assert "User clarification: Click the centre button." in harness.actor.contexts[1].task
    assert harness.policy.metrics.human_requests == 1
    assert harness.policy.metrics.human_replans == 1
    assert harness.policy.metrics.main_vlm_calls == 3
    assert harness.result.success is True
    assert harness.result.observation_count == 1


async def test_unanswered_human_request_stops_without_execution():
    harness = await run_policy(
        script=[ACTION_A],
        human_responses=[HIGH_HUMAN],
        human_answers=[None],
    )

    assert harness.result.success is False
    assert "needs_human" in harness.result.reason
    assert harness.executor.actions == []
    assert harness.human.calls == 1
    assert harness.policy.metrics.main_vlm_calls == 1


async def test_human_provider_error_stops_without_execution():
    harness = await run_policy(
        script=[ACTION_A],
        human_responses=[HIGH_HUMAN],
        human_error=RuntimeError("human offline"),
    )

    assert harness.result.success is False
    assert "needs_human" in harness.result.reason and "human offline" in harness.result.reason
    assert harness.executor.actions == []
    assert harness.human.calls == 1


async def test_human_provider_timeout_stops_without_execution():
    harness = await run_policy(
        script=[ACTION_A],
        human_responses=[HIGH_HUMAN],
        human_delay_s=0.2,
        human_timeout_s=0.01,
    )

    assert harness.result.success is False
    assert "needs_human" in harness.result.reason
    assert harness.executor.actions == []
    assert harness.human.calls == 1


async def test_observe_diagnostic_error_requests_conservative_refresh():
    harness = await run_policy(
        script=[ACTION_A, Finish(reason="done")],
        observe_responses=[RuntimeError("diag down")],
        human_responses=[LOW_HUMAN],
    )

    assert harness.result.success is True
    assert harness.observer.calls == 2
    assert harness.policy.metrics.observe_gate_events[0].decision == "OBSERVE"
    assert "diag down" in (harness.policy.metrics.observe_gate_events[0].error or "")


async def test_human_diagnostic_error_asks_conservatively():
    harness = await run_policy(
        script=[ACTION_A, ACTION_B, Finish(reason="done")],
        observe_responses=[LOW_OBSERVE, LOW_OBSERVE],
        human_responses=[RuntimeError("ask diag down")],
        human_answers=["Click the centre button."],
    )

    assert harness.policy.metrics.ask_gate_events[0].decision == "ASK"
    assert "ask diag down" in (harness.policy.metrics.ask_gate_events[0].error or "")
    assert harness.executor.actions == [ACTION_B]
    assert harness.human.calls == 1


async def test_finish_skips_ask_gate_and_extra_observation():
    harness = await run_policy(script=[Finish(reason="done")])

    assert harness.result.success is True
    assert harness.observer.calls == 1
    assert harness.observe_jev.observe_calls == 0
    assert harness.ask_jev.human_calls == 0
    assert harness.human.calls == 0
    assert harness.policy.metrics.ask_gate_events == []


async def test_gates_can_be_disabled_independently():
    both_off = await run_policy(
        script=[ACTION_A, Finish(reason="done")],
        observe_gate=False,
        ask_gate=False,
    )
    assert both_off.observer.calls == 2
    assert both_off.policy.metrics.observe_gate_events == []
    assert both_off.policy.metrics.ask_gate_events == []
    assert both_off.human.calls == 0
    assert both_off.executor.actions == [ACTION_A]

    observe_off = await run_policy(
        script=[ACTION_A, Finish(reason="done")],
        observe_gate=False,
        human_responses=[LOW_HUMAN],
    )
    assert observe_off.observer.calls == 2
    assert observe_off.policy.metrics.observe_gate_events == []
    assert observe_off.human.calls == 0
    assert observe_off.executor.actions == [ACTION_A]

    ask_off = await run_policy(
        script=[ACTION_A, Finish(reason="done")],
        observe_responses=[HIGH_OBSERVE],
        ask_gate=False,
    )
    assert ask_off.observer.calls == 2
    assert ask_off.policy.metrics.ask_gate_events == []
    assert ask_off.human.calls == 0
    assert ask_off.executor.actions == [ACTION_A]


async def test_metrics_record_timings_counts_and_execution_results():
    harness = await run_policy(
        script=[ACTION_A, Finish(reason="done")],
        observe_responses=[LOW_OBSERVE],
        human_responses=[LOW_HUMAN],
    )

    metrics = harness.policy.metrics
    assert metrics.main_vlm_calls == 2
    assert metrics.observe_commands == 1  # only the forced initial observation
    assert metrics.execute_commands == 1
    assert metrics.actor_total_s > 0
    assert metrics.observe_gate_total_s > 0
    assert metrics.ask_gate_total_s > 0
    assert metrics.observe_gate_events[0].latency_ms >= 0
    assert metrics.ask_gate_events[0].latency_ms >= 0
    assert len(metrics.execution_feedback) == 1
    assert harness.result.steps[-2].command == "execute"
    assert harness.result.steps[-2].success is True


async def test_policy_satisfies_agent_policy_protocol_and_matches_observation_count():
    harness = await run_policy(
        script=[ACTION_A, Finish(reason="done")],
        observe_responses=[HIGH_OBSERVE],
        human_responses=[LOW_HUMAN],
    )

    assert isinstance(harness.policy, AgentPolicy)
    assert harness.observer.calls == harness.result.observation_count
