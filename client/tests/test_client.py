"""Deployment-level client tests: health, Context discovery, and binding."""

from __future__ import annotations

import logging

import pytest
import respx
from httpx import Response

from ezclassify_rest_client import (
    EzClassifyClient,
    EzClassifyClientError,
    EzClassifyContext,
    MalformedResponseError,
    RestClientCredentials,
    __version__,
)
from ezclassify_rest_client._spec_version import CLIENT_SPEC_VERSION

_BASE = "http://localhost:8080"


def _health(engine_version: str = "1.0.0", sha: str = "", tag: str = "") -> dict:
    """A health report with every member the contract requires."""
    return {
        "status": "ok",
        "engineVersion": engine_version,
        "engineSha": sha,
        "gitTag": tag,
        "openapiVersion": engine_version,
        "abiVersion": "v1",
    }


def _description(ctx_id: str, version: str) -> dict:
    """The smallest Context description the contract allows."""
    return {
        "id": ctx_id,
        "version": version,
        "scheme": {"id": "levels", "version": "1.0.0", "resolvedVersion": "1.0.0"},
        "schemes": [
            {"selector": "levels", "id": "levels", "version": "1.0.0", "roles": []}
        ],
    }


def test_credentials_reject_blank_values() -> None:
    with pytest.raises(ValueError, match="access_token"):
        RestClientCredentials(access_token="  ")
    with pytest.raises(ValueError, match="context_token"):
        RestClientCredentials(context_token="")


@respx.mock
def test_credentials_are_scoped_to_their_independent_boundaries() -> None:
    contexts = respx.get(f"{_BASE}/v1/ctx").mock(
        return_value=Response(200, json={"contexts": ["default"]})
    )
    health = respx.get(f"{_BASE}/v1/health").mock(
        return_value=Response(200, json=_health("1.0.0"))
    )
    credentials = RestClientCredentials(
        access_token="caller-token", context_token="context-token"
    )
    with EzClassifyClient(_BASE, credentials=credentials) as client:
        assert client.list_contexts() == ["default"]
        client.ping()

    context_headers = contexts.calls.last.request.headers
    assert context_headers["Authorization"] == "Bearer caller-token"
    assert context_headers["x-ezclassify-context-token"] == "context-token"
    health_headers = health.calls.last.request.headers
    assert health_headers["Authorization"] == "Bearer caller-token"
    assert "x-ezclassify-context-token" not in health_headers


@respx.mock
def test_list_contexts_returns_visible_ids_and_sends_no_query() -> None:
    route = respx.get(f"{_BASE}/v1/ctx").mock(
        return_value=Response(200, json={"contexts": ["default", "restricted"]})
    )
    with EzClassifyClient(_BASE) as client:
        assert client.list_contexts() == ["default", "restricted"]
    # The operation declares no query parameter and the server refuses one.
    assert route.calls.last.request.url.query == b""


@respx.mock
def test_list_contexts_rejects_malformed_response() -> None:
    respx.get(f"{_BASE}/v1/ctx").mock(
        return_value=Response(200, json={"contexts": ["ok", 7]})
    )
    with EzClassifyClient(_BASE) as client:
        with pytest.raises(MalformedResponseError) as raised:
            client.list_contexts()
    assert raised.value.path == "$.contexts[1]"
    assert raised.value.status_code == 200


@pytest.mark.parametrize(
    ("route", "body", "call", "path"),
    [
        ("/v1/ctx", {}, "list_contexts", "$.contexts"),
        ("/v1/ctx/default", {"versions": []}, "list_context_versions", "$.id"),
        ("/v1/health", {"status": "ok"}, "ping", "$.engineVersion"),
    ],
)
@respx.mock
def test_discovery_and_health_refuse_an_answer_missing_a_required_member(
    route: str, body: dict, call: str, path: str
) -> None:
    respx.get(f"{_BASE}{route}").mock(return_value=Response(200, json=body))
    with EzClassifyClient(_BASE) as client:
        method = getattr(client, call)
        with pytest.raises(MalformedResponseError) as raised:
            method("default") if call == "list_context_versions" else method()
    assert raised.value.path == path


@respx.mock
def test_connect_refuses_a_description_missing_a_required_member() -> None:
    body = _description("default", "1.0.0")
    del body["schemes"]
    respx.get(f"{_BASE}/v1/ctx/default/latest").mock(
        return_value=Response(200, json=body)
    )
    with EzClassifyClient(_BASE) as deployment:
        with pytest.raises(MalformedResponseError) as raised:
            deployment.context("default")
    assert raised.value.path == "$.schemes"


@respx.mock
def test_list_context_versions_encodes_context_id() -> None:
    route = respx.get(f"{_BASE}/v1/ctx/tenant%20a%2Fdefault").mock(
        return_value=Response(
            200, json={"id": "tenant a/default", "versions": ["2.0.0", "1.0.0"]}
        )
    )
    with EzClassifyClient(_BASE) as client:
        assert client.list_context_versions("tenant a/default") == [
            "2.0.0",
            "1.0.0",
        ]
    assert route.called


def test_list_context_versions_rejects_blank_id() -> None:
    with EzClassifyClient(_BASE) as client:
        with pytest.raises(ValueError, match="context_id"):
            client.list_context_versions("  ")


@respx.mock
def test_context_resolves_before_returning_bound_handle() -> None:
    respx.get(f"{_BASE}/v1/ctx/default/1.0.0").mock(
        return_value=Response(
            200,
            json=_description("default", "1.0.0"),
            headers={"X-EzClassify-Resolved-Version": "1.0.0"},
        )
    )
    with EzClassifyClient(_BASE, headers={"X-Test": "yes"}) as deployment:
        context = deployment.context("default", "1.0.0")
        try:
            assert isinstance(context, EzClassifyContext)
            assert context.resolved_version == "1.0.0"
        finally:
            context.close()


@respx.mock
def test_ping_returns_build_identity() -> None:
    respx.get(f"{_BASE}/v1/health").mock(
        return_value=Response(
            200,
            json=_health("1.0.0", sha="abc", tag="v1"),
        )
    )
    with EzClassifyClient(_BASE) as client:
        result = client.ping()
    assert (result.engine_version, result.engine_sha, result.git_tag) == (
        "1.0.0",
        "abc",
        "v1",
    )


@respx.mock
def test_ping_warns_once_for_minor_mismatch(caplog: pytest.LogCaptureFixture) -> None:
    respx.get(f"{_BASE}/v1/health").mock(
        return_value=Response(200, json=_health("9.9.0"))
    )
    with EzClassifyClient(_BASE) as client, caplog.at_level(logging.WARNING):
        client.ping()
        client.ping()
    assert caplog.text.count("connected to v9.9.0") == 1


@respx.mock
def test_json_api_error_fields_are_preserved() -> None:
    respx.get(f"{_BASE}/v1/ctx").mock(
        return_value=Response(
            403,
            json={"error": "forbidden", "code": "DENIED", "detail": "wrong org"},
        )
    )
    with EzClassifyClient(_BASE) as client:
        with pytest.raises(EzClassifyClientError) as raised:
            client.list_contexts()
    assert raised.value.status_code == 403
    assert raised.value.code == "DENIED"
    assert raised.value.detail == "wrong org"


@respx.mock
def test_ping_warns_on_a_later_mismatch_after_a_matching_first_ping(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The once-only flag records that a warning was emitted, not that a ping
    # happened: a client that first sees a matching deployment must still warn
    # when a later ping reaches a differently versioned one.
    route = respx.get(f"{_BASE}/v1/health")
    route.side_effect = [
        Response(200, json=_health(CLIENT_SPEC_VERSION)),
        Response(200, json=_health("9.9.0")),
    ]
    with EzClassifyClient(_BASE) as client, caplog.at_level(logging.WARNING):
        client.ping()
        client.ping()
    assert caplog.text.count("connected to v9.9.0") == 1


@respx.mock
def test_version_warning_names_the_installed_package_version(
    caplog: pytest.LogCaptureFixture,
) -> None:
    respx.get(f"{_BASE}/v1/health").mock(
        return_value=Response(200, json=_health("9.9.0"))
    )
    with EzClassifyClient(_BASE) as client, caplog.at_level(logging.WARNING):
        client.ping()
    assert f"ezclassify-rest-client v{__version__}" in caplog.text
    assert f"rest-api v{CLIENT_SPEC_VERSION}" in caplog.text


@respx.mock
def test_non_json_success_body_raises_the_client_error() -> None:
    respx.get(f"{_BASE}/v1/ctx").mock(
        return_value=Response(
            200, content=b"<html>proxy</html>", headers={"content-type": "text/html"}
        )
    )
    with EzClassifyClient(_BASE) as client:
        with pytest.raises(MalformedResponseError, match="non-JSON body") as raised:
            client.list_contexts()
    assert raised.value.status_code == 200
    assert raised.value.path == "$"
    assert "text/html" in str(raised.value)


@respx.mock
def test_unstructured_error_body_is_excerpted_but_kept_on_the_exception() -> None:
    body = "x" * 5000
    respx.get(f"{_BASE}/v1/ctx").mock(return_value=Response(500, content=body.encode()))
    with EzClassifyClient(_BASE) as client:
        with pytest.raises(EzClassifyClientError) as raised:
            client.list_contexts()
    assert len(str(raised.value)) < 400
    assert str(raised.value).endswith("...")
