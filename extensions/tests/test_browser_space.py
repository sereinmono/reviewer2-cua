"""Browser action-space validation tests (pure, no browser or model)."""

from __future__ import annotations

import pytest

from nano_cua import Action, ActionValidationError
from nano_cua_extensions.playwright_browser.browser_space import (
    ACTION_KINDS,
    SPACE,
    action_from_payload,
    normalize_action,
    validate_click,
    validate_key,
    validate_text,
)


def test_action_from_payload_builds_a_normalized_action():
    action = action_from_payload({"kind": "click", "x": 10, "y": 20}, width=100, height=50)
    assert action.space == SPACE
    assert action.kind == "click"
    assert dict(action.args) == {"x": 10, "y": 20}


def test_normalize_action_round_trips_and_revalidates():
    original = Action(space=SPACE, kind="press_key", args={"key": "Enter"})
    normalized = normalize_action(original, width=100, height=100)
    assert normalized == original
    assert dict(normalized.args) == {"key": "Enter"}


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "click", "x": 10, "y": 10},
        {"kind": "click", "x": 99, "y": 49},
    ],
)
def test_valid_clicks(payload):
    action = action_from_payload(payload, width=100, height=50)
    assert action.kind == "click"


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "click", "x": 100, "y": 10},
        {"kind": "click", "x": 10, "y": 50},
        {"kind": "click", "x": -1, "y": 10},
        {"kind": "click", "x": 10.5, "y": 10},
        {"kind": "click", "x": True, "y": 10},
        {"kind": "click", "x": "10", "y": 10},
        {"kind": "click", "x": 10},
        {"kind": "click", "x": 10, "y": 10, "target": "#a"},
        {"kind": "hover"},
        {"kind": "type_text", "text": ""},
        {"kind": "type_text", "text": 42},
        {"kind": "press_key", "key": "Control+NotAKey"},
        {"kind": "press_key", "key": ""},
        ["not", "a", "mapping"],
    ],
)
def test_invalid_payloads_are_rejected(payload):
    with pytest.raises(ActionValidationError):
        action_from_payload(payload, width=100, height=50)


def test_click_without_known_viewport_only_checks_non_negativity():
    assert validate_click(10, 10, width=None, height=None) == (10, 10)
    with pytest.raises(ActionValidationError):
        validate_click(-1, 0, width=None, height=None)


@pytest.mark.parametrize("key", ["Enter", "Tab", "Escape", "a", "A", "7", "Control+A", "Shift+Tab", "Alt+F4", "F12", "Meta+Space"])
def test_valid_keys(key):
    assert validate_key(key) == key


@pytest.mark.parametrize("key", ["", "Control+", "+A", "Ctrl+A", "NotAKey", "Control+NotAKey", "a" * 65, 42])
def test_invalid_keys(key):
    with pytest.raises(ActionValidationError):
        validate_key(key)


def test_text_validation_limits():
    assert validate_text("hello") == "hello"
    with pytest.raises(ActionValidationError):
        validate_text("")
    with pytest.raises(ActionValidationError):
        validate_text("x" * 2001)
    with pytest.raises(ActionValidationError):
        validate_text("a\x00b")


def test_normalize_action_rejects_wrong_space_or_kind():
    with pytest.raises(ActionValidationError, match="space"):
        normalize_action(Action(space="desktop", kind="click", args={"x": 1, "y": 1}))
    with pytest.raises(ActionValidationError, match="kind"):
        normalize_action(Action(space=SPACE, kind="drag", args={}))
    with pytest.raises(ActionValidationError):
        normalize_action(Action(space=SPACE, kind="click", args={"x": 10, "y": 10}), width=10, height=10)


def test_normalize_action_rejects_non_string_argument_names():
    action = Action(space=SPACE, kind="click", args={1: 2, "x": 1, "y": 1})  # type: ignore[dict-item]
    with pytest.raises(ActionValidationError, match="names must be strings"):
        normalize_action(action, width=100, height=100)


def test_action_kinds_are_whitelisted():
    assert ACTION_KINDS == ("click", "type_text", "press_key")
