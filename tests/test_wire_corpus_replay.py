"""Replay every recorded exchange through public adapters or named private boundaries."""

from __future__ import annotations

import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Iterator
from urllib.parse import urlsplit

import httpx
import pytest

from ezclassify_rest_client import (
    EzClassifyClient,
    EzClassifyClientError,
    EzClassifyContext,
)

from .wire_corpus import Replay, WireCase, WireCorpus
from .wire_drivers import (
    answered_in_process,
    credentials,
    run_context,
    run_deployment,
    wire_value,
)

CASE_IDS = sorted(case.id for case in WireCorpus.vendored().cases)


@pytest.fixture(scope="module")
def corpus() -> WireCorpus:
    """Owner-vendored inputs are required; absence is a failure, never a skip."""
    return WireCorpus.vendored()


@contextmanager
def serve_replay(replay: Replay) -> Iterator[str]:
    """An isolated socket server returns only recordings; no runtime is started."""
    errors: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *args: object) -> None:
            pass

        def answer(self) -> None:
            content = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            request = httpx.Request(
                self.command,
                "http://replay.invalid" + self.path,
                headers=dict(self.headers),
                content=content,
            )
            try:
                response = replay.answer(request)
            except AssertionError as error:
                errors.append(str(error))
                response = httpx.Response(
                    500, json={"error": "unrecorded_request", "detail": str(error)}
                )
            self.send_response(response.status_code)
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response.content)

        do_GET = answer
        do_POST = answer

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert not errors, errors


def run_case(case: WireCase, replay: Replay, base: str) -> None:
    """Verify the adapter's value or error, response metadata and one exact request."""
    parts = urlsplit(case.path).path.split("/")
    if len(parts) >= 5 and parts[1:3] == ["v1", "ctx"]:
        owner = EzClassifyContext(
            base,
            context_id=parts[3],
            context_version=parts[4],
            credentials=credentials(case),
            disable_cache=True,
        )

        def invoke() -> object:
            return run_context(case, owner)
    else:
        owner = EzClassifyClient(base, credentials=credentials(case))

        def invoke() -> object:
            return run_deployment(case, owner)

    received: list[httpx.Response] = []

    def observe_response(response: httpx.Response) -> None:
        response.read()
        received.append(response)

    owner._client.event_hooks["response"].append(observe_response)
    try:
        if case.status >= 400:
            with pytest.raises(EzClassifyClientError) as caught:
                invoke()
            assert caught.value.status_code == case.status
            assert caught.value.payload == case.response_body
            assert isinstance(case.response_body, dict)
            assert caught.value.code == case.response_body.get("code")
            assert caught.value.detail == case.response_body.get("detail")
        else:
            value = invoke()
            if (
                case.path in ("/v1/health",)
                or case.id == "denial.authenticated-health-still-open"
            ):
                assert value is None
            elif case.id in ("ctx-list.ok", "ctx-versions.ok"):
                assert isinstance(case.response_body, dict)
                key = "contexts" if case.id == "ctx-list.ok" else "versions"
                assert value == case.response_body[key]
            else:
                assert wire_value(value) == case.response_body
    finally:
        owner.close()
    replay.assert_complete()
    assert replay.hits == {case.id: 1}
    assert len(replay.responses) == len(replay.requests) == 1
    assert len(received) == 1
    response = received[0]
    assert response.status_code == case.status
    assert json.loads(response.content) == case.response_body
    for name, value in case.response_headers.items():
        assert response.headers[name] == value


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_every_wire_case_in_process(corpus: WireCorpus, case_id: str) -> None:
    replay = Replay(corpus, [case_id])
    with answered_in_process(replay.answer):
        run_case(corpus.by_id[case_id], replay, "http://replay.invalid")


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_every_wire_case_over_http(corpus: WireCorpus, case_id: str) -> None:
    replay = Replay(corpus, [case_id])
    with serve_replay(replay) as base:
        run_case(corpus.by_id[case_id], replay, base)
