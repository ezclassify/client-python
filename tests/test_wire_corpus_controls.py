"""Adversarial controls for the recording-only loader, matcher and coverage gate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from .wire_corpus import Replay, WireCase, WireCorpus, request_key, target_key
from .wire_drivers import answered_in_process


def _case() -> WireCase:
    return WireCase(
        "sample",
        "POST",
        "/v1/ctx/example/1.0.0/scheme/process",
        {"input": "OPEN", "outputs": ["first", "second"]},
        True,
        {},
        "context-token",
        400,
        {"code": "INVALID_INPUT", "detail": "fixture"},
        {"content-type": "application/json"},
    )


_CARRIER = "fixture-context-carrier"


def _request(
    body: object, token: str | None = _CARRIER, path: str | None = None
) -> httpx.Request:
    headers = {"x-ezclassify-context-token": token} if token is not None else {}
    return httpx.Request(
        "POST",
        "http://replay.invalid" + (path or _case().path),
        json=body,
        headers=headers,
    )


@pytest.mark.parametrize(
    "body",
    [
        {"input": "OPEN"},
        {"input": "OPEN", "outputs": ["second", "first"]},
        {"input": "OPEN", "outputs": ["first", "second"], "fix": False},
    ],
)
def test_changed_request_body_is_not_answered(body: object) -> None:
    replay = Replay(WireCorpus([_case()], {}), ["sample"])
    with pytest.raises(AssertionError, match="unrecorded"):
        replay.answer(_request(body))
    assert not replay.hits and list(replay.pending) == ["sample"]


@pytest.mark.parametrize("token", [None, "different-carrier"])
def test_missing_or_changed_carrier_is_not_answered(token: str | None) -> None:
    replay = Replay(WireCorpus([_case()], {}), ["sample"])
    with pytest.raises(AssertionError, match="Context carrier"):
        replay.answer(_request(_case().body, token))
    assert not replay.hits


def test_missing_case_and_extra_request_both_fail() -> None:
    replay = Replay(WireCorpus([_case()], {}), ["sample"])
    with pytest.raises(AssertionError, match="unreplayed"):
        replay.assert_complete()
    response = replay.answer(_request(_case().body))
    assert response.status_code == 400 and response.json() == _case().response_body
    replay.assert_complete()
    with pytest.raises(AssertionError, match="unrecorded"):
        replay.answer(_request(_case().body))


@pytest.mark.parametrize("ids", [["unknown"], ["sample", "sample"]])
def test_unknown_or_duplicate_driver_ids_fail(ids: list[str]) -> None:
    with pytest.raises(AssertionError):
        Replay(WireCorpus([_case()], {}), ids)


def test_duplicate_recording_ids_fail() -> None:
    with pytest.raises(AssertionError, match="duplicate"):
        WireCorpus([_case(), _case()], {})


def test_encoding_and_absent_body_remain_distinct() -> None:
    assert target_key("/a%2Fb?z=2&a=1") == "/a%2Fb?a=1&z=2"
    assert target_key("/a%2Fb") != target_key("/a/b")
    assert request_key("POST", "/path", None, False) != request_key(
        "POST", "/path", None, True
    )
    assert request_key("POST", "/path", {}, True) != request_key(
        "POST", "/path", None, False
    )


def _inputs(directory: Path) -> Path:
    spec = directory / "openapi.yaml"
    spec.write_text("fixture contract bytes\n")
    case = _case()
    (directory / "corpus.json").write_text(
        json.dumps(
            {
                "corpusVersion": "v1",
                "cases": [
                    {
                        "id": case.id,
                        "auth": {"credential": case.credential},
                        "request": {
                            "method": case.method,
                            "path": case.path,
                            "body": case.body,
                            "headers": {},
                        },
                        "response": {
                            "status": case.status,
                            "body": case.response_body,
                            "headers": case.response_headers,
                        },
                    }
                ],
            }
        )
    )
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "corpusVersion": "v1",
                "spec": {
                    "digest": "sha256:" + hashlib.sha256(spec.read_bytes()).hexdigest()
                },
                "contractDivergences": [],
                "coverage": {"cases": 1},
            }
        )
    )
    return spec


@pytest.mark.parametrize("mutation", ["digest", "divergence", "count", "missing"])
def test_stale_or_missing_provenance_fails(tmp_path: Path, mutation: str) -> None:
    spec = _inputs(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if mutation == "missing":
        manifest_path.unlink()
    else:
        if mutation == "digest":
            manifest["spec"]["digest"] = "sha256:changed"
        if mutation == "divergence":
            manifest["contractDivergences"] = [{"caseId": "sample"}]
        if mutation == "count":
            manifest["coverage"]["cases"] = 2
        manifest_path.write_text(json.dumps(manifest))
    with pytest.raises((AssertionError, FileNotFoundError)):
        WireCorpus.load(tmp_path, spec)


def test_matching_provenance_loads(tmp_path: Path) -> None:
    corpus = WireCorpus.load(tmp_path, _inputs(tmp_path))
    assert len(corpus.cases) == 1 and list(corpus.by_id) == ["sample"]


@pytest.mark.parametrize("mutation", ["path", "method", "bearer"])
def test_route_or_bearer_mutations_are_not_answered(mutation: str) -> None:
    replay = Replay(WireCorpus([_case()], {}), ["sample"])
    request = _request(_case().body)
    if mutation == "path":
        request = _request(_case().body, path="/v1/ctx/other/1.0.0/scheme/process")
    elif mutation == "method":
        request = httpx.Request(
            "GET", request.url, headers=request.headers, content=request.content
        )
    else:
        request.headers["authorization"] = "Bearer fixture-unexpected"
    with pytest.raises(AssertionError):
        replay.answer(request)
    assert not replay.hits


@pytest.mark.parametrize("mutation", ["payload", "status", "header"])
def test_changed_response_cannot_pass_the_adapter_gate(mutation: str) -> None:
    from .test_wire_corpus_replay import run_case

    case = WireCase(
        "ctx-scheme-process.undeclared-format",
        "POST",
        "/v1/ctx/example/1.0.0/scheme/process",
        {"input": "OPEN", "outputFormat": "not-declared"},
        True,
        {},
        "context-token",
        400,
        {"code": "INVALID_INPUT", "detail": "outputFormat: not-declared"},
        {"content-type": "application/json"},
    )
    replay = Replay(WireCorpus([case], {}), [case.id])

    def altered_response(request: httpx.Request) -> httpx.Response:
        response = replay.answer(request)
        if mutation == "payload":
            response = httpx.Response(
                400,
                json={"code": "INVALID_INPUT", "detail": "silently-changed"},
                headers=case.response_headers,
                request=request,
            )
        elif mutation == "status":
            response.status_code = 401
        else:
            response.headers["content-type"] = "text/plain"
        return response

    with (
        answered_in_process(altered_response),
        pytest.raises(AssertionError),
    ):
        run_case(case, replay, "http://replay.invalid")


@pytest.mark.parametrize(
    ("target", "canonical"),
    [
        ("", "/"),
        ("/", "/"),
        ("/path/", "/path"),
        ("/path//", "/path/"),
        ("/path///", "/path//"),
        ("/x?key=", "/x?key"),
        ("/x?a-=1&a=2", "/x?a=2&a-=1"),
        ("/x?name=2&name=1", "/x?name=1&name=2"),
        ("/x?b=&a=1&&b", "/x?a=1&b&b"),
        ("/a%2Fb/?v=%2F&v=/", "/a%2Fb?v=%2F&v=/"),
    ],
)
def test_target_matches_owner_canonical_rules(target: str, canonical: str) -> None:
    assert target_key(target) == canonical


def test_double_trailing_slash_is_not_answered() -> None:
    replay = Replay(WireCorpus([_case()], {}), ["sample"])
    with pytest.raises(AssertionError, match="unrecorded"):
        replay.answer(_request(_case().body, path=_case().path + "//"))
    assert not replay.hits and list(replay.pending) == ["sample"]


def test_one_trailing_slash_still_matches_owner_route() -> None:
    replay = Replay(WireCorpus([_case()], {}), ["sample"])
    response = replay.answer(_request(_case().body, path=_case().path + "/"))
    assert response.status_code == _case().status
    replay.assert_complete()
