"""Offline tests for the real-OmniJev adapter (no model, no network)."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cua_gates_pilot.omnijev import OmniJevDiagnoseModel
from nano_cua import Action, AgentContext, Observation

PNG = b"\x89PNG\r\n\x1a\nfake"


def make_context() -> AgentContext:
    return AgentContext(
        task="click the button",
        observations=(
            Observation(
                source="playwright",
                kind="screenshot",
                payload=PNG,
                captured_at=time.time(),
                metadata={"content_type": "image/png", "width": 1280, "height": 720},
            ),
        ),
        observation_is_fresh=True,
    )


class _Handler(BaseHTTPRequestHandler):
    payloads: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        self.payloads.append(body)
        questions = body["questions"]
        if "info_missing" in questions:
            answers = {"info_missing": {"type": "noul", "noul": 0.123}, "possibly_stale": {"type": "noul", "noul": 0.456}}
        elif "target_unique" in questions:
            answers = {"target_unique": {"type": "noul", "noul": 0.20}}
        else:  # pragma: no cover - request-shape test fails first
            answers = {}
        data = json.dumps({"model": "fake-onejev", "answers": answers,
                           "usage": {"input_tokens": 11, "output_tokens": 3}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture()
def fake_server():
    _Handler.payloads = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1/systemone"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_observe_signals_use_noul_and_send_image(fake_server):
    model = OmniJevDiagnoseModel(url=fake_server, timeout_s=10)
    signals = asyncio_run(model.observe_signals(make_context()))

    assert signals == pytest.approx({"info_missing": 0.123, "possibly_stale": 0.456})
    payload = _Handler.payloads[-1]
    assert payload["state"]["screen"] == "<image:1>"
    assert payload["media"][0]["data"].startswith("data:image/png;base64,")
    assert payload["questions"]["info_missing"]["type"] == "noul"


def test_usage_tokens_are_accumulated(fake_server):
    model = OmniJevDiagnoseModel(url=fake_server, timeout_s=10)
    asyncio_run(model.observe_signals(make_context()))
    asyncio_run(model.human_signals(make_context(), Action(space="browser", kind="click", args={"x": 1, "y": 2})))
    assert model.usage_input_tokens == 22
    assert model.usage_output_tokens == 6


def test_human_signals_invert_unique_target_probability(fake_server):
    model = OmniJevDiagnoseModel(url=fake_server, timeout_s=10)
    signals = asyncio_run(model.human_signals(make_context(), Action(space="browser", kind="click", args={"x": 10, "y": 20})))

    assert signals == pytest.approx({"intent_ambiguity": 0.80})
    question = _Handler.payloads[-1]["questions"]["target_unique"]
    assert question["type"] == "noul"
    assert "unique target" in question["instructions"]
    assert _Handler.payloads[-1]["state"]["candidate_action"] == "browser.click(x=10, y=20)"


def test_malformed_answer_raises(fake_server):
    class BrokenHandler(_Handler):
        def do_POST(self):  # noqa: N802
            data = json.dumps({"model": "x", "answers": {}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), BrokenHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/systemone"
        with pytest.raises(RuntimeError):
            asyncio_run(OmniJevDiagnoseModel(url=url, timeout_s=10).observe_signals(make_context()))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
