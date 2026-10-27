"""VLM actor tests: strict parsing and request shape, with no network and no key.

``httpx.MockTransport`` intercepts the HTTP call, so these tests never contact a
real model platform and need no API key.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable

import httpx
import pytest
import pytest_asyncio

from nano_cua import Action, ActionResult, ActionValidationError, AgentContext, Finish, Observation
from nano_cua_extensions.playwright_browser.browser_space import (
    BROWSER_SYSTEM_PROMPT,
    browser_proposal_parser,
    proposal_from_payload,
)
from nano_cua_extensions.vlm.actor import VLMActor, parse_json_object

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-image-body"


def make_context(
    *,
    last_action: Action | None = None,
    last_result: ActionResult | None = None,
    with_observation: bool = True,
) -> AgentContext:
    observations = ()
    if with_observation:
        observations = (
            Observation(
                source="playwright",
                kind="screenshot",
                payload=PNG_BYTES,
                captured_at=time.time(),
                metadata={
                    "url": "file:///demo.html",
                    "content_type": "image/png",
                    "width": 1280,
                    "height": 720,
                },
            ),
        )
    return AgentContext(
        task="click the button",
        observations=observations,
        observation_is_fresh=True,
        last_action=last_action,
        last_result=last_result,
    )


def reply(text: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text}}]})


@pytest_asyncio.fixture
async def actor_factory():
    clients: list[httpx.AsyncClient] = []

    def factory(
        handler: Callable[[httpx.Request], httpx.Response],
        *,
        base_url: str = "http://vlm.test/v1",
        api_key: str | None = "secret-key",
        system_prompt: str = BROWSER_SYSTEM_PROMPT,
        proposal_parser=browser_proposal_parser,
        **kwargs: object,
    ) -> VLMActor:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return VLMActor(
            base_url=base_url,
            model="test-vlm",
            api_key=api_key,
            system_prompt=system_prompt,
            proposal_parser=proposal_parser,
            client=client,
            **kwargs,
        )

    yield factory
    for client in clients:
        await client.aclose()


async def test_click_action_is_parsed_and_the_request_is_well_formed(actor_factory):
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return reply('{"kind": "click", "x": 640, "y": 360}')

    actor = actor_factory(handler)
    proposal = await actor.propose(make_context())

    assert isinstance(proposal, Action)
    assert proposal.space == "browser"
    assert proposal.kind == "click"
    assert dict(proposal.args) == {"x": 640, "y": 360}

    request = captured[0]
    assert str(request.url) == "http://vlm.test/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer secret-key"
    body = json.loads(request.content)
    assert body["model"] == "test-vlm"
    assert body["stream"] is False
    assert body["temperature"] == 0
    assert body["messages"][0] == {"role": "system", "content": BROWSER_SYSTEM_PROMPT}
    text_part, image_part = body["messages"][1]["content"]
    assert text_part["type"] == "text"
    assert "click the button" in text_part["text"]
    assert "1280x720" in text_part["text"]
    assert image_part["type"] == "image_url"
    data_url = image_part["image_url"]["url"]
    assert data_url.startswith("data:image/png;base64,")
    assert base64.b64decode(data_url.split(",", 1)[1]) == PNG_BYTES


async def test_type_text_and_press_key_are_parsed(actor_factory):
    actor = actor_factory(lambda request: reply('{"kind": "type_text", "text": "hello"}'))
    proposal = await actor.propose(make_context())
    assert isinstance(proposal, Action)
    assert proposal.kind == "type_text"
    assert dict(proposal.args) == {"text": "hello"}

    actor = actor_factory(lambda request: reply('{"kind": "press_key", "key": "Control+A"}'))
    proposal = await actor.propose(make_context())
    assert isinstance(proposal, Action)
    assert proposal.kind == "press_key"
    assert dict(proposal.args) == {"key": "Control+A"}


async def test_fenced_and_prose_wrapped_replies_are_tolerated(actor_factory):
    fenced = '```json\n{"kind": "press_key", "key": "Enter"}\n```'
    actor = actor_factory(lambda request: reply(fenced))
    proposal = await actor.propose(make_context())
    assert isinstance(proposal, Action) and proposal.kind == "press_key"

    prose = 'Sure, here it is: {"kind": "press_key", "key": "Tab"} done.'
    actor = actor_factory(lambda request: reply(prose))
    proposal = await actor.propose(make_context())
    assert isinstance(proposal, Action) and dict(proposal.args) == {"key": "Tab"}


async def test_finish_reply_becomes_finish(actor_factory):
    actor = actor_factory(lambda request: reply('{"kind": "finish", "reason": "task complete"}'))
    proposal = await actor.propose(make_context())
    assert isinstance(proposal, Finish)
    assert proposal.success is True
    assert proposal.reason == "task complete"


async def test_finish_without_reason_is_allowed(actor_factory):
    actor = actor_factory(lambda request: reply('{"kind": "finish"}'))
    proposal = await actor.propose(make_context())
    assert isinstance(proposal, Finish) and proposal.reason == "finished"


async def test_content_parts_reply_is_supported(actor_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": '{"kind": "press_key", "key": "Enter"}'},
                            ]
                        }
                    }
                ]
            },
        )

    actor = actor_factory(handler)
    proposal = await actor.propose(make_context())
    assert isinstance(proposal, Action) and proposal.kind == "press_key"


async def test_prompt_includes_last_action_and_feedback(actor_factory):
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return reply('{"kind": "finish", "reason": "done"}')

    actor = actor_factory(handler)
    context = make_context(
        last_action=Action(space="browser", kind="click", args={"x": 1, "y": 2}),
        last_result=ActionResult(success=False, error="boom"),
    )
    await actor.propose(context)

    body = json.loads(captured[0].content)
    prompt = body["messages"][1]["content"][0]["text"]
    assert "Last action: browser.click" in prompt
    assert "failed: boom" in prompt


async def test_json_mode_is_opt_in(actor_factory):
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return reply('{"kind": "finish", "reason": "done"}')

    actor = actor_factory(handler, json_mode=True)
    await actor.propose(make_context())
    body = json.loads(captured[0].content)
    assert body["response_format"] == {"type": "json_object"}

    actor = actor_factory(handler)
    await actor.propose(make_context())
    body = json.loads(captured[1].content)
    assert "response_format" not in body


async def test_full_chat_completions_url_is_used_as_is(actor_factory):
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return reply('{"kind": "finish", "reason": "done"}')

    actor = actor_factory(handler, base_url="http://vlm.test/v1/chat/completions")
    assert actor.endpoint == "http://vlm.test/v1/chat/completions"
    await actor.propose(make_context())
    assert str(captured[0].url) == "http://vlm.test/v1/chat/completions"


async def test_no_api_key_sends_no_authorization_header(actor_factory):
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return reply('{"kind": "finish", "reason": "done"}')

    actor = actor_factory(handler, api_key=None)
    await actor.propose(make_context())
    assert "authorization" not in captured[0].headers


async def test_missing_observation_is_a_programming_error(actor_factory):
    actor = actor_factory(lambda request: reply('{"kind": "finish", "reason": "done"}'))
    with pytest.raises(RuntimeError, match="needs an observation"):
        await actor.propose(make_context(with_observation=False))


async def test_http_error_is_reported(actor_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    actor = actor_factory(handler)
    with pytest.raises(RuntimeError, match="HTTP 429"):
        await actor.propose(make_context())


async def test_malformed_response_is_reported(actor_factory):
    actor = actor_factory(lambda request: httpx.Response(200, json={"choices": []}))
    with pytest.raises(RuntimeError, match="choices"):
        await actor.propose(make_context())

    actor = actor_factory(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": None}}]}))
    with pytest.raises(RuntimeError, match="no text content"):
        await actor.propose(make_context())


@pytest.mark.parametrize(
    "model_reply",
    [
        "not json at all",
        '{"kind": "hover", "target": "button"}',
        '{"kind": "click", "x": 5000, "y": 10}',
        '{"kind": "click", "x": -1, "y": 10}',
        '{"kind": "click", "x": 10.5, "y": 10}',
        '{"kind": "click", "x": 10, "y": 10, "target": "#button"}',
        '{"kind": "click", "x": 10}',
        '{"kind": "type_text", "text": 42}',
        '{"kind": "press_key", "key": "Control+NotAKey"}',
        '{"kind": "finish", "reason": "done", "success": true}',
        '{"kind": "finish", "reason": 42}',
        '{"kind": "click", "x": true, "y": 10}',
    ],
)
async def test_invalid_model_replies_are_rejected(actor_factory, model_reply):
    actor = actor_factory(lambda request: reply(model_reply))
    with pytest.raises(ActionValidationError):
        await actor.propose(make_context())


async def test_default_client_survives_malformed_proxy_env(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1,[::1]")
    actor = VLMActor(
        base_url="http://127.0.0.1:9/v1",
        model="test-vlm",
        system_prompt=BROWSER_SYSTEM_PROMPT,
        proposal_parser=browser_proposal_parser,
    )
    try:
        assert actor.proxy_env_ignored is True
    finally:
        await actor.aclose()


async def test_injected_proposal_parser_is_used(actor_factory):
    def custom_parser(payload, observation):
        return Finish(reason=f"custom parser saw {payload['kind']}", success=True)

    actor = actor_factory(
        lambda request: reply('{"kind": "anything"}'),
        system_prompt="custom system prompt",
        proposal_parser=custom_parser,
    )
    proposal = await actor.propose(make_context())
    assert isinstance(proposal, Finish)
    assert proposal.reason == "custom parser saw anything"


async def test_injected_client_is_left_to_the_caller(actor_factory):
    actor = actor_factory(lambda request: reply('{"kind": "finish", "reason": "done"}'))
    assert actor.proxy_env_ignored is False
    await actor.aclose()  # caller owns the injected client, so this is a no-op


def test_parse_json_object_variants():
    assert dict(parse_json_object('{"kind": "finish"}')) == {"kind": "finish"}
    assert dict(parse_json_object('```json\n{"a": 1}\n```')) == {"a": 1}
    assert dict(parse_json_object('prefix {"a": 2} suffix')) == {"a": 2}
    with pytest.raises(ActionValidationError):
        parse_json_object("no object here")
    with pytest.raises(ActionValidationError):
        parse_json_object("{broken json}")


def test_proposal_from_payload_validates_strictly():
    action = proposal_from_payload({"kind": "click", "x": 3, "y": 4}, width=100, height=100)
    assert isinstance(action, Action) and dict(action.args) == {"x": 3, "y": 4}
    with pytest.raises(ActionValidationError):
        proposal_from_payload({"kind": "click", "x": 100, "y": 0}, width=100, height=100)
    with pytest.raises(ActionValidationError):
        proposal_from_payload(["not", "an", "object"])
    with pytest.raises(ActionValidationError):
        proposal_from_payload({"kind": "unknown"})
