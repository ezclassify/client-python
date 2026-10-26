"""The demo adapter must delegate exclusively to the published Context SDK."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from audit_builders import aggregate, audit_report, part
from client import EzClassifyClient, credentials_from_env
from ezclassify_rest_client import RestClientCredentials


def _envelope(result: object) -> MagicMock:
    envelope = MagicMock()
    envelope.result = result
    return envelope


@patch("client.connect_context")
def test_adapter_binds_context_and_delegates_every_operation(
    connect: MagicMock,
) -> None:
    context = connect.return_value
    report = audit_report([part("HIGH")], whole="HIGH")
    aggregated = aggregate("HIGH")
    context.document.audit.return_value = _envelope(report)
    context.scheme.aggregate.return_value = _envelope(aggregated)

    with EzClassifyClient("http://localhost:3000", "default", "1.0.0") as client:
        assert client.audit("HIGH") is report
        assert client.aggregate(["HIGH"]) is aggregated

    connect.assert_called_once_with(
        "http://localhost:3000",
        context_id="default",
        context_version="1.0.0",
        credentials=None,
    )
    context.document.audit.assert_called_once_with("HIGH")
    context.scheme.process.assert_not_called()
    context.scheme.aggregate.assert_called_once_with(["HIGH"])
    context.close.assert_called_once_with()


def test_credentials_come_from_the_environment_a_secured_runtime_expects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EZCLASSIFY_ACCESS_TOKEN", "caller-token")
    monkeypatch.setenv("EZCLASSIFY_CONTEXT_TOKEN", "context-token")

    credentials = credentials_from_env()

    assert isinstance(credentials, RestClientCredentials)
    assert credentials.access_token == "caller-token"
    assert credentials.context_token == "context-token"


def test_no_credentials_when_the_runtime_needs_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EZCLASSIFY_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("EZCLASSIFY_CONTEXT_TOKEN", raising=False)

    assert credentials_from_env() is None
