"""The wire contract decides what this client owes; this file checks it paid.

``vendor/spec/openapi.yaml`` is the authority for the REST surface and
``vendor/spec/wire/v1`` records a real exchange for every operation in it.
Without a check against both, a re-vendor could add a route or a recording and
the suite would stay green while the SDK quietly fell behind the wire.

Two rules hold here:

* Every operation the contract declares goes on the wire from a public method,
  or is listed in ``NOT_SENT`` with the reason the client never sends it.
* Every recorded case is driven through a public method, or is listed in
  ``wire_drivers.PRIVATE_SEAMS`` under one of its few reasons.

"Driven through a public method" is proven by ``test_wire_corpus_replay``: it
sends every case not in ``PRIVATE_SEAMS`` through the public call and matches
the exact request the recording holds, so a case counted here as public is one
a public method demonstrably sends.
"""

from __future__ import annotations

import pytest

from .openapi_members import vendored_spec
from .wire_corpus import WireCorpus
from .wire_drivers import (
    PRIVATE_SEAMS,
    UNBUILDABLE_REQUEST,
    UNDECLARED_ROUTE,
    UNSENT_OPERATION,
)

# operationId -> why this client deliberately does not send it.
NOT_SENT: dict[str, str] = {
    "getReady": (
        "Readiness probe for an orchestrator, not for an SDK. ping() sends "
        "getHealth, which carries the engine version the client needs for its "
        "compatibility handshake; readiness carries nothing a caller can use."
    ),
}


@pytest.fixture(scope="module")
def corpus() -> WireCorpus:
    return WireCorpus.vendored()


def _publicly_sent(corpus: WireCorpus) -> set[str]:
    return {
        case.operation_id
        for case in corpus.cases
        if case.id not in PRIVATE_SEAMS and case.operation_id is not None
    }


def test_every_operation_is_sent_by_a_public_method_or_explained(
    corpus: WireCorpus,
) -> None:
    """The gate. A route the contract declares is sent or refused on the record."""
    operations = vendored_spec().operations
    unsent = sorted(set(operations) - _publicly_sent(corpus) - set(NOT_SENT))
    assert not unsent, "\n".join(
        [
            "the vendored contract declares operations no public method sends:",
            *(
                f"  {name}  ({operations[name].method} {operations[name].path})"
                for name in unsent
            ),
            "",
            "Implement each one and drive its recordings publicly, or add it to "
            "NOT_SENT with the reason it is deliberately absent.",
        ]
    )


def test_not_sent_names_only_operations_the_client_really_does_not_send(
    corpus: WireCorpus,
) -> None:
    """A dropped route, or one the client started sending, must not leave a lie behind."""
    operations = vendored_spec().operations
    assert set(NOT_SENT) <= set(operations), (
        f"NOT_SENT names operations the contract does not declare: "
        f"{sorted(set(NOT_SENT) - set(operations))}"
    )
    sent = sorted(set(NOT_SENT) & _publicly_sent(corpus))
    assert not sent, f"NOT_SENT names operations a public method sends: {sent}"


@pytest.mark.parametrize("operation_id", sorted(NOT_SENT))
def test_each_unsent_operation_carries_a_real_reason(operation_id: str) -> None:
    """An allowlist without reasons documents nothing and grows without argument."""
    reason = NOT_SENT[operation_id]
    assert len(reason.split()) >= 12, f"{operation_id}: too short to be a reason"
    assert "not implemented" not in reason.lower(), (
        f"{operation_id} restates the fact instead of giving the reason"
    )


def test_every_private_seam_is_a_recorded_case_with_a_known_reason(
    corpus: WireCorpus,
) -> None:
    unknown = sorted(set(PRIVATE_SEAMS) - set(corpus.by_id))
    assert not unknown, f"PRIVATE_SEAMS names cases the corpus does not hold: {unknown}"
    reasons = {UNBUILDABLE_REQUEST, UNDECLARED_ROUTE, UNSENT_OPERATION}
    stray = sorted(c for c, reason in PRIVATE_SEAMS.items() if reason not in reasons)
    assert not stray, f"cases filed under an unnamed reason: {stray}"


@pytest.mark.parametrize("case_id", sorted(PRIVATE_SEAMS))
def test_each_private_seam_is_one_no_public_method_can_send(
    corpus: WireCorpus, case_id: str
) -> None:
    """The reason has to match the recording, or the table is just prose."""
    case = corpus.by_id[case_id]
    reason = PRIVATE_SEAMS[case_id]
    if reason is UNDECLARED_ROUTE:
        assert case.operation_id is None, (
            f"{case_id} is filed as an undeclared route but records {case.operation_id}"
        )
    elif reason is UNSENT_OPERATION:
        assert case.operation_id in NOT_SENT, (
            f"{case_id} is filed as never sent, but {case.operation_id} is not "
            "in NOT_SENT"
        )
    else:
        # A body or query no typed signature builds is one the runtime refuses.
        assert reason is UNBUILDABLE_REQUEST
        assert case.operation_id is not None, case_id
        assert 400 <= case.status < 500, (
            f"{case_id} is filed as unbuildable but the runtime accepted it "
            f"({case.status}), so a public method should send it"
        )
