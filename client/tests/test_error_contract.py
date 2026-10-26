"""The refusals the contract declares on every operation: 400 and 429.

``vendor/spec/openapi.yaml`` declares a 400 and a 429 on every operation. The
429 comes from a rate limiter that answers ahead of every route, so any call
can hit it. It carries a ``Retry-After`` header and a ``RateLimitError`` body
that name the same wait. The client reports the wait on the exception and
leaves the decision to the caller: it never sleeps and never retries.

The stubs here are built from the contract's own example for the 429, so a
change to the declared header or body fails these tests instead of leaving the
client reading a shape the server no longer sends.
"""

from __future__ import annotations

from typing import Any, Callable, Dict

import httpx
import pytest
import respx
import yaml

from ezclassify_rest_client import (
    EzClassifyClient,
    EzClassifyClientError,
    connect_context,
)

from .test_request_schema_conformance import SPEC_PATH, OpenApiRequestSchemas

_BASE = "http://localhost:8080"
_CTX = f"{_BASE}/v1/ctx/acme/1.0.0"
_HTTP_METHODS = ("get", "post")


@pytest.fixture(scope="module")
def document() -> Dict[str, Any]:
    """The vendored contract, parsed once."""
    loaded: Dict[str, Any] = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    return loaded


def _rate_limited(
    document: Dict[str, Any],
    *,
    header: str | None = "declared",
    body: Dict[str, Any] | None = None,
) -> httpx.Response:
    """A 429 built from the contract's declared headers and example body.

    ``header`` is the ``Retry-After`` value: the declared example's wait by
    default, a given string, or ``None`` for no header. ``body`` replaces the
    example body.
    """
    declared = document["components"]["responses"]["TooManyRequests"]
    example = dict(declared["content"]["application/json"]["example"])
    assert (
        OpenApiRequestSchemas(document).validate_closed("RateLimitError", example) == []
    )
    assert "Retry-After" in declared["headers"]
    headers: Dict[str, str] = {}
    if header == "declared":
        headers["Retry-After"] = str(example["retryAfter"])
    elif header is not None:
        headers["Retry-After"] = header
    return httpx.Response(
        429, json=body if body is not None else example, headers=headers
    )


def _example_wait(document: Dict[str, Any]) -> float:
    declared = document["components"]["responses"]["TooManyRequests"]
    return float(declared["content"]["application/json"]["example"]["retryAfter"])


def _deployment_call() -> None:
    with EzClassifyClient(_BASE) as client:
        client.list_contexts()


def _context_read() -> None:
    with connect_context(_BASE, context_id="acme", context_version="1.0.0") as ctx:
        ctx.scheme.label_presets()


def _context_operation() -> None:
    with connect_context(_BASE, context_id="acme", context_version="1.0.0") as ctx:
        ctx.scheme.validate("H")


# One call per transport path: a deployment GET, a Context GET, a Context POST.
_ROUTES: Dict[str, tuple[str, str, Callable[[], None]]] = {
    "deployment-get": ("GET", f"{_BASE}/v1/ctx", _deployment_call),
    "context-get": ("GET", f"{_CTX}/scheme/label-presets", _context_read),
    "context-post": ("POST", f"{_CTX}/scheme/validate", _context_operation),
}


def _refused(route: str, response: httpx.Response) -> tuple[EzClassifyClientError, int]:
    """Make one call that the stub answers with ``response``.

    Returns the error the call raised and how many times the stub was asked.
    """
    method, url, call = _ROUTES[route]
    with respx.mock(assert_all_called=False) as router:
        # Connecting to a Context describes it first; that answer is not the
        # one under test.
        router.get(_CTX).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "acme",
                    "version": "1.0.0",
                    "scheme": {
                        "id": "s",
                        "version": "1.0.0",
                        "resolvedVersion": "1.0.0",
                    },
                    "schemes": [
                        {"selector": "s", "id": "s", "version": "1.0.0", "roles": []}
                    ],
                },
                headers={"X-EzClassify-Resolved-Version": "1.0.0"},
            )
        )
        refusal = router.route(method=method, url=url).mock(return_value=response)
        with pytest.raises(EzClassifyClientError) as raised:
            call()
    return raised.value, refusal.call_count


def test_the_contract_declares_400_and_429_on_every_operation(
    document: Dict[str, Any],
) -> None:
    missing = []
    for path, item in document["paths"].items():
        for method in _HTTP_METHODS:
            operation = item.get(method)
            if operation is None:
                continue
            for status, name in (("400", "BadRequest"), ("429", "TooManyRequests")):
                declared = operation["responses"].get(status, {}).get("$ref")
                if declared != f"#/components/responses/{name}":
                    missing.append(f"{method.upper()} {path} {status}")
    assert not missing, "operations without their declared refusal: " + ", ".join(
        missing
    )


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_a_429_reports_the_wait_on_every_transport_path(
    document: Dict[str, Any], route: str
) -> None:
    error, _ = _refused(route, _rate_limited(document))
    assert error.status_code == 429
    assert error.retry_after == _example_wait(document)
    assert error.payload == {"error": "Too Many Requests", "retryAfter": 42}


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_the_client_neither_retries_nor_waits_on_a_429(
    document: Dict[str, Any], route: str
) -> None:
    _, calls = _refused(route, _rate_limited(document))
    # Waiting and retrying are the caller's decision, so the stub is asked once.
    assert calls == 1


def test_the_header_is_preferred_over_the_body(document: Dict[str, Any]) -> None:
    error, _ = _refused("deployment-get", _rate_limited(document, header="7"))
    assert error.retry_after == 7.0


def test_the_body_answers_when_the_header_is_absent(document: Dict[str, Any]) -> None:
    error, _ = _refused("deployment-get", _rate_limited(document, header=None))
    assert error.retry_after == _example_wait(document)


@pytest.mark.parametrize("header", ["soon", "-3", "nan", "inf", ""])
def test_an_unusable_header_falls_back_to_the_body(
    document: Dict[str, Any], header: str
) -> None:
    error, _ = _refused("deployment-get", _rate_limited(document, header=header))
    assert error.retry_after == _example_wait(document)


def test_a_429_that_names_no_wait_has_none(document: Dict[str, Any]) -> None:
    bare = _rate_limited(document, header=None, body={"error": "Too Many Requests"})
    error, _ = _refused("deployment-get", bare)
    assert error.status_code == 429
    assert error.retry_after is None


def test_a_400_carries_its_code_and_detail_but_no_wait(
    document: Dict[str, Any],
) -> None:
    refusal = httpx.Response(
        400,
        json={
            "error": "invalid_input",
            "code": "INVALID_INPUT",
            "detail": "query: scheme",
        },
        headers={"Retry-After": "9"},
    )
    error, _ = _refused("context-get", refusal)
    assert (error.status_code, error.code, error.detail) == (
        400,
        "INVALID_INPUT",
        "query: scheme",
    )
    assert error.retry_after is None
