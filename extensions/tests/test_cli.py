"""CLI composition tests that never launch a browser.

They cover actor resolution, the demo script, error exits and the opt-in
screenshot writer, all offline and without any model API key.
"""

from __future__ import annotations

import argparse

import pytest

from nano_cua import Action, AgentContext, Finish, MockActor, Observation, RunResult
from nano_cua_extensions.demo_cli import (
    _demo_script,
    _resolve_actor,
    _save_screenshot,
    build_parser,
    main,
)
from nano_cua_extensions.vlm.actor import VLMActor

_MODEL_ENV = ("NANO_CUA_API_BASE", "NANO_CUA_MODEL", "NANO_CUA_API_KEY", "NANO_CUA_BROWSER_CHANNEL")


def _parse(monkeypatch, argv: list[str] | None = None) -> argparse.Namespace:
    for name in _MODEL_ENV:
        monkeypatch.delenv(name, raising=False)
    return build_parser().parse_args(argv or [])


def test_default_actor_is_mock_without_model_env(monkeypatch):
    args = _parse(monkeypatch)
    actor, label = _resolve_actor(args)
    assert isinstance(actor, MockActor)
    assert "mock" in label and "no API key" in label


def test_demo_script_clicks_the_viewport_centre(monkeypatch):
    args = _parse(monkeypatch, ["--width", "800", "--height", "600"])
    script = _demo_script(args)
    assert script[0] == Action(space="browser", kind="click", args={"x": 400, "y": 300})
    assert isinstance(script[1], Finish)
    assert script[1].success is True


def test_explicit_vlm_without_config_is_rejected(monkeypatch):
    args = _parse(monkeypatch, ["--actor", "vlm"])
    with pytest.raises(ValueError, match="API base"):
        _resolve_actor(args)


async def test_auto_actor_uses_vlm_when_both_env_values_are_set(monkeypatch):
    args = _parse(monkeypatch)
    monkeypatch.setenv("NANO_CUA_API_BASE", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("NANO_CUA_MODEL", "fake-model")
    actor, label = _resolve_actor(args)
    assert isinstance(actor, VLMActor)
    assert actor.model == "fake-model"
    assert label.startswith("vlm")
    await actor.aclose()


def test_partial_model_config_warns_and_falls_back_to_mock(monkeypatch, capsys):
    args = _parse(monkeypatch)
    monkeypatch.setenv("NANO_CUA_API_BASE", "http://127.0.0.1:9/v1")
    actor, _ = _resolve_actor(args)
    assert isinstance(actor, MockActor)
    assert "only one of API base/model" in capsys.readouterr().err


def test_main_returns_config_error_for_missing_vlm_settings(monkeypatch, capsys):
    for name in _MODEL_ENV:
        monkeypatch.delenv(name, raising=False)
    assert main(["--actor", "vlm"]) == 2
    assert "API base" in capsys.readouterr().err


def test_screenshot_write_failure_only_warns(tmp_path, capsys):
    target_is_a_directory = tmp_path / "not-a-file"
    target_is_a_directory.mkdir()
    observation = Observation(source="fake", kind="image", payload=b"png", captured_at=0.0)
    result = RunResult(
        task="t",
        success=True,
        reason="done",
        error=None,
        steps=(),
        observation_count=1,
        action_count=0,
        decision_time_s=0.0,
        observation_time_s=0.0,
        execution_time_s=0.0,
        total_time_s=0.0,
        context=AgentContext(task="t", observations=(observation,)),
    )
    _save_screenshot(result, str(target_is_a_directory))  # OSError is swallowed into a warning
    captured = capsys.readouterr()
    assert "warning" in captured.err
    assert "screenshot:" not in captured.out
