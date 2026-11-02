"""Opaque application credentials use only Context routes and one auth mode."""

from typing import Any

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
        ({_HEADER: _GRANT}, RestClientCredentials(access_token=_GRANT)),
        ({_HEADER: _GRANT}, RestClientCredentials(context_token=_GRANT)),
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


@respx.mock(assert_all_called=False)
def test_application_credential_does_not_follow_cross_origin_redirect(
    respx_mock: respx.MockRouter,
) -> None:
    first = respx_mock.get(f"{_BASE}/v1/ctx").respond(
        302, headers={"Location": "https://other.example.test/v1/ctx"}
    )
    other = respx_mock.get("https://other.example.test/v1/ctx").respond(
        200, json={"contexts": []}
    )
    with EzClassifyClient(_BASE, headers={_HEADER: _GRANT}) as client:
        with pytest.raises(EzClassifyClientError):
            client.list_contexts()
    assert first.call_count == 1
    assert other.call_count == 0


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
@respx.mock(assert_all_called=False)
def test_context_application_credential_does_not_follow_redirect(
    factory: Any,
    respx_mock: respx.MockRouter,
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
        _BASE, context_id="default", context_version="1.0.0", headers={_HEADER: _GRANT}
    ) as context:
        with pytest.raises(EzClassifyClientError):
            context.scheme.validate("ALPHA")
    assert first.call_count == 1
    assert other.call_count == 0
    assert first.calls.last.request.headers[_HEADER] == _GRANT


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
