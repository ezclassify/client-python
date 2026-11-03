"""Application grants stay Context-scoped and may accompany one user bearer."""

from typing import Any
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import respx

from ezclassify_rest_client import (
    EzClassifyClient,
    EzClassifyClientError,
    EzClassifyContext,
    RestClientCredentials,
    connect_context,
)

_BASE = "https://runtime.example.test"
_HEADER = "X-EzClassify-Application-Grant"
_GRANT = "opaque.not-decoded.value"


@pytest.mark.parametrize(
    "factory", [EzClassifyClient, EzClassifyContext, connect_context]
)
def test_application_and_user_credentials_over_real_http(factory: Any) -> None:
    seen: list[tuple[str, str | None, str | None, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def answer(self, status: int, body: object) -> None:
            seen.append(
                (
                    self.path,
                    self.headers.get("Authorization"),
                    self.headers.get(_HEADER),
                    self.headers.get("x-ezclassify-context-token"),
                )
            )
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            if self.path == "/v1/ctx":
                self.answer(200, {"contexts": ["default"]})
            elif self.path == "/v1/health":
                self.answer(
                    200,
                    {
                        "status": "ok",
                        "engineVersion": "1.0.0",
                        "engineSha": "",
                        "gitTag": "",
                        "openapiVersion": "1.0.0",
                        "abiVersion": "v1",
                    },
                )
            else:
                self.answer(200, _description())

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.answer(403, {"error": "Application refused"})

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        for user in ("caller-a", "caller-b"):
            options = {
                "headers": {_HEADER: _GRANT},
                "credentials": RestClientCredentials(access_token=user),
            }
            if factory is EzClassifyClient:
                with factory(base, **options) as client:
                    client.list_contexts()
                    client.ping()
                    with client.context("default", "1.0.0") as context:
                        with pytest.raises(EzClassifyClientError) as caught:
                            context.scheme.validate("ALPHA")
            else:
                with factory(
                    base, context_id="default", context_version="1.0.0", **options
                ) as context:
                    with pytest.raises(EzClassifyClientError) as caught:
                        context.scheme.validate("ALPHA")
            assert caught.value.status_code == 403
        for user in ("caller-a", "caller-b"):
            calls = [entry for entry in seen if entry[1] == f"Bearer {user}"]
            assert (
                len([entry for entry in calls if entry[0].endswith("/scheme/validate")])
                == 1
            )
            assert calls
            for path, _, grant, token in calls:
                assert token is None
                assert grant == (_GRANT if path.startswith("/v1/ctx") else None)
        assert all(entry[1] in {"Bearer caller-a", "Bearer caller-b"} for entry in seen)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def _description(context_id: str = "default") -> dict[str, Any]:
    return {
        "id": context_id,
        "version": "1.0.0",
        "scheme": {"id": "levels", "version": "1.0.0", "resolvedVersion": "1.0.0"},
        "schemes": [
            {"selector": "levels", "id": "levels", "version": "1.0.0", "roles": []}
        ],
    }


@respx.mock
def test_application_header_is_opaque_copied_and_context_scoped() -> None:
    contexts = respx.get(f"{_BASE}/v1/ctx").respond(200, json={"contexts": ["default"]})
    versions = respx.get(f"{_BASE}/v1/ctx/default").respond(
        200, json={"id": "default", "versions": ["1.0.0"]}
    )
    describe = respx.get(f"{_BASE}/v1/ctx/default/1.0.0").respond(
        200,
        json=_description(),
        headers={"X-EzClassify-Resolved-Version": "1.0.0"},
    )
    health = respx.get(f"{_BASE}/v1/health").respond(
        200,
        json={
            "status": "ok",
            "engineVersion": "1.0.0",
            "engineSha": "",
            "gitTag": "",
            "openapiVersion": "1.0.0",
            "abiVersion": "v1",
        },
    )
    headers = {_HEADER: _GRANT, "X-Customer": "preserved"}
    with EzClassifyClient(_BASE, headers=headers) as client:
        headers[_HEADER] = "changed-after-construction"
        assert client.list_contexts() == ["default"]
        assert client.list_context_versions("default") == ["1.0.0"]
        client.ping()
        with client.context("default", "1.0.0"):
            pass
    for route in (contexts, versions, describe):
        sent = route.calls.last.request.headers
        assert sent[_HEADER] == _GRANT
        assert sent["X-Customer"] == "preserved"
        assert "authorization" not in sent
        assert "x-ezclassify-context-token" not in sent
    assert _HEADER not in health.calls.last.request.headers
    assert health.calls.last.request.headers["X-Customer"] == "preserved"


@pytest.mark.parametrize(
    "factory", [EzClassifyClient, EzClassifyContext, connect_context]
)
@pytest.mark.parametrize(
    ("headers", "credentials"),
    [
        ({_HEADER: ""}, None),
        ({_HEADER: "   "}, None),
        ({_HEADER: " leading"}, None),
        ({_HEADER: "trailing "}, None),
        ({_HEADER: "line\r\nbreak"}, None),
        ({_HEADER: "tab\tvalue"}, None),
        ({_HEADER: "null\x00value"}, None),
        ({_HEADER: "delete\x7fvalue"}, None),
        ({_HEADER: 42}, None),
        ({_HEADER: _GRANT, _HEADER.lower(): _GRANT}, None),
        ({_HEADER: _GRANT, "aUtHoRiZaTiOn": "Bearer caller"}, None),
        ({_HEADER: _GRANT, "X-EZCLASSIFY-CONTEXT-TOKEN": "context"}, None),
        ({_HEADER: _GRANT}, RestClientCredentials(context_token=_GRANT)),
        (
            {_HEADER: _GRANT},
            RestClientCredentials(access_token=_GRANT, context_token=_GRANT),
        ),
    ],
)
def test_invalid_application_auth_refuses_before_client_creation(
    factory: Any,
    headers: dict[str, Any],
    credentials: RestClientCredentials | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_transport(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("invalid application configuration reached HTTP transport")

    monkeypatch.setattr(httpx, "Client", refuse_transport)
    arguments: dict[str, Any] = {"headers": headers, "credentials": credentials}
    if factory is not EzClassifyClient:
        arguments["context_id"] = "default"
    with pytest.raises(ValueError, match="application credential"):
        factory(_BASE, **arguments)


@pytest.mark.parametrize("user_token", [None, "caller"])
@respx.mock(assert_all_called=False)
def test_application_credential_does_not_follow_cross_origin_redirect(
    respx_mock: respx.MockRouter,
    user_token: str | None,
) -> None:
    first = respx_mock.get(f"{_BASE}/v1/ctx").respond(
        302, headers={"Location": "https://other.example.test/v1/ctx"}
    )
    other = respx_mock.get("https://other.example.test/v1/ctx").respond(
        200, json={"contexts": []}
    )
    with EzClassifyClient(
        _BASE,
        headers={_HEADER: _GRANT},
        credentials=RestClientCredentials(access_token=user_token),
    ) as client:
        with pytest.raises(EzClassifyClientError):
            client.list_contexts()
    assert first.call_count == 1
    assert other.call_count == 0
    assert first.calls.last.request.headers.get("Authorization") == (
        f"Bearer {user_token}" if user_token else None
    )


@pytest.mark.parametrize("factory", [EzClassifyContext, connect_context])
@respx.mock(assert_all_called=False)
def test_context_entry_points_carry_application_auth_without_replay(
    factory: Any,
    respx_mock: respx.MockRouter,
) -> None:
    description = respx_mock.get(f"{_BASE}/v1/ctx/default/1.0.0").respond(
        200,
        json=_description(),
        headers={"X-EzClassify-Resolved-Version": "1.0.0"},
    )
    refusal = respx_mock.post(f"{_BASE}/v1/ctx/default/1.0.0/scheme/validate").respond(
        403,
        json={"error": "Application refused"},
        headers={"X-EzClassify-Resolved-Version": "1.0.0"},
    )
    headers = {_HEADER.lower(): _GRANT}
    with factory(
        _BASE, context_id="default", context_version="1.0.0", headers=headers
    ) as context:
        headers[_HEADER.lower()] = "mutated"
        with pytest.raises(EzClassifyClientError) as caught:
            context.scheme.validate("ALPHA")
    assert caught.value.status_code == 403
    assert refusal.call_count == 1
    assert description.call_count == (1 if factory is connect_context else 0)
    for call in respx_mock.calls:
        assert call.request.url.host == "runtime.example.test"
        assert call.request.headers[_HEADER] == _GRANT
        assert "authorization" not in call.request.headers
        assert "x-ezclassify-context-token" not in call.request.headers


@pytest.mark.parametrize("factory", [EzClassifyContext, connect_context])
@pytest.mark.parametrize("user_token", [None, "caller"])
@respx.mock(assert_all_called=False)
def test_context_application_credential_does_not_follow_redirect(
    factory: Any,
    respx_mock: respx.MockRouter,
    user_token: str | None,
) -> None:
    respx_mock.get(f"{_BASE}/v1/ctx/default/1.0.0").respond(
        200,
        json=_description(),
        headers={"X-EzClassify-Resolved-Version": "1.0.0"},
    )
    first = respx_mock.post(f"{_BASE}/v1/ctx/default/1.0.0/scheme/validate").respond(
        302,
        headers={
            "Location": "https://other.example.test/v1/ctx/default/1.0.0/scheme/validate"
        },
    )
    other = respx_mock.post(
        "https://other.example.test/v1/ctx/default/1.0.0/scheme/validate"
    ).respond(200, json={})
    with factory(
        _BASE,
        context_id="default",
        context_version="1.0.0",
        headers={_HEADER: _GRANT},
        credentials=RestClientCredentials(access_token=user_token),
    ) as context:
        with pytest.raises(EzClassifyClientError):
            context.scheme.validate("ALPHA")
    assert first.call_count == 1
    assert other.call_count == 0
    assert first.calls.last.request.headers[_HEADER] == _GRANT
    assert first.calls.last.request.headers.get("Authorization") == (
        f"Bearer {user_token}" if user_token else None
    )


@pytest.mark.parametrize(
    "factory", [EzClassifyClient, EzClassifyContext, connect_context]
)
def test_byte_header_names_refuse_before_transport(
    factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_transport(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("byte header name reached transport")

    monkeypatch.setattr(httpx, "Client", refuse_transport)
    arguments: dict[str, Any] = {"headers": {b"X-EzClassify-Application-Grant": _GRANT}}
    if factory is not EzClassifyClient:
        arguments["context_id"] = "default"
    with pytest.raises(ValueError, match="header names"):
        factory(_BASE, **arguments)


@pytest.mark.parametrize("context_id", [".", ".."])
@pytest.mark.parametrize("factory", [EzClassifyContext, connect_context])
def test_context_dot_segments_refuse_before_transport(
    factory: Any, context_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_transport(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("dot segment reached transport")

    monkeypatch.setattr(httpx, "Client", refuse_transport)
    with pytest.raises(ValueError, match="context_id"):
        factory(_BASE, context_id=context_id, headers={_HEADER: _GRANT})


@pytest.mark.parametrize("context_id", [".", ".."])
@respx.mock
def test_discovery_dot_segments_do_not_make_requests(context_id: str) -> None:
    with EzClassifyClient(_BASE, headers={_HEADER: _GRANT}) as client:
        with pytest.raises(ValueError, match="context_id"):
            client.list_context_versions(context_id)
    assert len(respx.calls) == 0


@pytest.mark.parametrize("context_version", [".", ".."])
@pytest.mark.parametrize("factory", [EzClassifyContext, connect_context])
def test_version_dot_segments_refuse_before_transport(
    factory: Any, context_version: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_transport(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("version dot segment reached transport")

    monkeypatch.setattr(httpx, "Client", refuse_transport)
    with pytest.raises(ValueError, match="context_version"):
        factory(
            _BASE,
            context_id="default",
            context_version=context_version,
            headers={_HEADER: _GRANT},
        )
