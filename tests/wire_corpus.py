"""Strict loading and request matching for the owner-vendored wire recordings."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import httpx


@dataclass(frozen=True)
class WireCase:
    """One immutable recording; credential values are deliberately absent."""

    id: str
    method: str
    path: str
    body: object
    has_body: bool
    request_headers: dict[str, str]
    credential: str
    status: int
    response_body: object
    response_headers: dict[str, str]
    operation_id: str | None = None


def target_key(target: str) -> str:
    """Sort raw query pairs without decoding encoded path or query bytes."""
    path, _, query = target.partition("?")
    if not path:
        path = "/"
    elif len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    if not query:
        return path
    pairs = []
    for pair in query.split("&"):
        if pair:
            name, _, value = pair.partition("=")
            pairs.append((name, value))
    pairs.sort()
    query = "&".join(name + ("=" + value if value else "") for name, value in pairs)
    return path + "?" + query


def request_key(
    method: str, target: str, body: object, has_body: bool
) -> tuple[str, str, str]:
    """Object order is immaterial; array order and absent bodies remain exact."""
    encoded = (
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if has_body
        else "(no body)"
    )
    return method.upper(), target_key(target), encoded


class WireCorpus:
    """Load both provenance and recordings, refusing missing or mismatched inputs."""

    def __init__(self, cases: list[WireCase], manifest: dict[str, object]) -> None:
        self.cases = cases
        self.manifest = manifest
        self.by_id = {case.id: case for case in cases}
        assert len(self.by_id) == len(cases), "duplicate wire case id"
        assert cases, "wire corpus contains no cases"

    @classmethod
    def load(cls, directory: Path, openapi: Path) -> WireCorpus:
        """Bind recorded cases to the exact YAML used by the generated SDK."""
        document = json.loads((directory / "corpus.json").read_text())
        manifest = json.loads((directory / "manifest.json").read_text())
        assert document["corpusVersion"] == manifest["corpusVersion"] == "v1"
        assert (
            manifest["spec"]["digest"]
            == "sha256:" + hashlib.sha256(openapi.read_bytes()).hexdigest()
        ), "wire/OpenAPI provenance mismatch"
        assert manifest["contractDivergences"] == [], (
            "unresolved recorded contract divergence"
        )
        assert manifest["coverage"]["cases"] == len(document["cases"]), (
            "manifest case count mismatch"
        )
        cases = []
        for entry in document["cases"]:
            request, response = entry["request"], entry["response"]
            cases.append(
                WireCase(
                    entry["id"],
                    request["method"],
                    request["path"],
                    request.get("body"),
                    "body" in request,
                    request["headers"],
                    entry["auth"]["credential"],
                    response["status"],
                    response["body"],
                    response["headers"],
                    entry.get("operationId"),
                )
            )
        return cls(cases, manifest)

    @classmethod
    def vendored(cls) -> WireCorpus:
        """No sibling-repository or package fallback can hide a stale mirror."""
        vendor = Path(__file__).resolve().parents[1] / "vendor/spec"
        return cls.load(vendor / "wire/v1", vendor / "openapi.yaml")


class Replay:
    """Answer only declared, matching requests once; no engine computations."""

    def __init__(self, corpus: WireCorpus, ids: list[str]) -> None:
        assert len(ids) == len(set(ids)), "driver repeats a case id"
        assert set(ids) <= set(corpus.by_id), "driver names unknown case ids"
        self.pending = {case_id: corpus.by_id[case_id] for case_id in ids}
        self.hits: Counter[str] = Counter()
        self.requests: list[httpx.Request] = []
        self.responses: list[httpx.Response] = []

    def answer(self, request: httpx.Request) -> httpx.Response:
        """A mismatch raises, including changes to any body field or credential."""
        has_body = bool(request.content)
        body = json.loads(request.content) if has_body else None
        key = request_key(
            request.method, request.url.raw_path.decode("ascii"), body, has_body
        )
        candidates = [
            case
            for case in self.pending.values()
            if request_key(case.method, case.path, case.body, case.has_body) == key
        ]
        assert len(candidates) == 1, f"unrecorded/ambiguous request: {key}"
        case = candidates[0]
        for name, value in case.request_headers.items():
            assert request.headers.get(name) == value, f"{case.id}: changed {name}"
        carrier = request.headers.get("x-ezclassify-context-token")
        bearer = request.headers.get("authorization")
        if case.credential == "context-token":
            assert carrier == "fixture-context-carrier", (
                f"{case.id}: missing Context carrier"
            )
            assert bearer is None, f"{case.id}: unexpected bearer"
        elif case.credential == "invalid-bearer":
            assert (
                bearer == "Bearer fixture-refused-bearer"
                and carrier == "fixture-context-carrier"
            ), case.id
        else:
            assert case.credential == "none", (
                f"unsupported credential class {case.credential}"
            )
            assert bearer is None and carrier is None, f"{case.id}: leaked credential"
        del self.pending[case.id]
        self.hits[case.id] += 1
        self.requests.append(request)
        response = httpx.Response(
            case.status,
            content=json.dumps(case.response_body).encode(),
            headers=case.response_headers,
            request=request,
        )
        self.responses.append(response)
        return response

    def assert_complete(self) -> None:
        """Neither omissions nor unexpected extra requests are a successful replay."""
        assert not self.pending, f"unreplayed cases: {sorted(self.pending)}"
        assert all(count == 1 for count in self.hits.values())
