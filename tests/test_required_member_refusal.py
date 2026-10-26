"""A success that breaks the contract's required members is refused, never kept.

Each recorded success in the wire corpus is walked against its operation's
response schema in the vendored OpenAPI document. Every member a ``required``
list names, wherever it sits in the recorded body, is taken away in turn, and
then, when the contract does not allow it to be null, set to null instead.
The public call that decoded the recording has to raise
``MalformedResponseError`` whose ``path`` names that member. Nothing here is a
list of members the client is known to check, so a member the client forgot,
or a model that fills one in with a default, fails the member it forgot.

The client runs with its cache on, so a cached operation is refused inside the
read that would file the answer; ``client/tests/test_context.py`` shows the
next call reaches the server again.
"""

from __future__ import annotations

import copy
import dataclasses
from typing import Any, Callable
from urllib.parse import urlsplit

import pytest

from ezclassify_rest_client import (
    EzClassifyClient,
    EzClassifyContext,
    MalformedResponseError,
)

from .openapi_members import Member, vendored_spec
from .test_spec_operation_coverage import NOT_SENT
from .wire_corpus import Replay, WireCase, WireCorpus
from .wire_drivers import (
    PRIVATE_SEAMS,
    answered_in_process,
    credentials,
    run_context,
    run_deployment,
)

BASE_URL = "http://replay.invalid"


@pytest.fixture(scope="module")
def corpus() -> WireCorpus:
    return WireCorpus.vendored()


def _successes() -> list[str]:
    corpus = WireCorpus.vendored()
    return sorted(
        case.id
        for case in corpus.cases
        if 200 <= case.status < 300 and case.id not in PRIVATE_SEAMS
    )


def _operation_of(case: WireCase) -> str:
    assert case.operation_id is not None, f"{case.id} names no operation"
    return case.operation_id


def _call(case: WireCase, corpus: WireCorpus) -> Callable[[], object]:
    """Run the public driver for ``case`` against a corpus holding only it."""

    def invoke() -> object:
        replay = Replay(corpus, [case.id])
        with answered_in_process(replay.answer):
            parts = urlsplit(case.path).path.split("/")
            if len(parts) >= 5 and parts[1:3] == ["v1", "ctx"]:
                with EzClassifyContext(
                    BASE_URL,
                    context_id=parts[3],
                    context_version=parts[4],
                    credentials=credentials(case),
                ) as ctx:
                    return run_context(case, ctx)
            with EzClassifyClient(BASE_URL, credentials=credentials(case)) as client:
                return run_deployment(case, client)

    return invoke


def _with_body(corpus: WireCorpus, case: WireCase, body: Any) -> WireCorpus:
    changed = dataclasses.replace(case, response_body=body)
    return WireCorpus([changed], corpus.manifest)


def _variants(member: Member) -> list[tuple[str, Callable[[dict[str, Any]], None]]]:
    def remove(parent: dict[str, Any]) -> None:
        del parent[member.name]

    def nullify(parent: dict[str, Any]) -> None:
        parent[member.name] = None

    variants: list[tuple[str, Callable[[dict[str, Any]], None]]] = [("absent", remove)]
    if not member.nullable:
        variants.append(("null", nullify))
    return variants


@pytest.mark.parametrize("case_id", _successes())
def test_every_required_member_is_refused_by_name(
    corpus: WireCorpus, case_id: str
) -> None:
    case = corpus.by_id[case_id]
    spec = vendored_spec()
    operation = spec.operation(_operation_of(case))
    assert operation.success is not None, f"{operation.id} declares no success body"
    # The recording itself decodes: the refusals below are about the change.
    _call(case, corpus)()

    members = spec.required_members(operation.success, case.response_body)
    failures: list[str] = []
    for member in members:
        for how, change in _variants(member):
            body = copy.deepcopy(case.response_body)
            change(member.parent(body))
            try:
                _call(case, _with_body(corpus, case, body))()
            except MalformedResponseError as error:
                if error.path != member.path:
                    failures.append(f"{how} {member.path}: refused as {error.path}")
            except Exception as error:  # noqa: BLE001  reported as the failure
                failures.append(f"{how} {member.path}: {type(error).__name__}: {error}")
            else:
                failures.append(f"{how} {member.path}: the call returned a result")
    assert not failures, (
        f"{case_id}: {len(failures)} required-member changes were not refused "
        "naming the member:\n" + "\n".join(failures[:25])
    )


def test_the_corpus_reaches_the_required_members_of_every_sent_operation(
    corpus: WireCorpus,
) -> None:
    """A recorded success for every operation the client sends, so none is untested."""
    covered = {_operation_of(corpus.by_id[case]) for case in _successes()}
    spec = vendored_spec()
    unreached = sorted(set(spec.operations) - covered - set(NOT_SENT))
    assert not unreached, f"no public success recording decodes: {unreached}"
