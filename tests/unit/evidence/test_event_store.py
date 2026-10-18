from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from itertools import permutations

import pytest

from wtb.domain.execution_evidence import AttemptIdentity, EvidenceError, EvidenceEvent
from wtb.domain.interfaces.workflow_runtime import RuntimeBindingEnvelope
from wtb.infrastructure.evidence.sqlite_store import SQLiteEvidenceStore


def identity(attempt="a1"):
    return AttemptIdentity(
        "e1",
        "runtime",
        "scope",
        RuntimeBindingEnvelope("actor", "store", "c" * 64, 2, 1, "d" * 64, attempt),
    )


def event(kind, id=None, body=None):
    return EvidenceEvent(id or kind, kind, body or {})


@pytest.fixture
def store_factory(tmp_path):
    return lambda: SQLiteEvidenceStore(tmp_path / "evidence.sqlite")


@pytest.fixture
def store(store_factory):
    return store_factory()


def test_exact_lookup_and_retry_cas(store):
    first = identity()
    store.register(first, expected_attempt=None)
    store.append("e1", "a1", event("completed", body={"receipt": "one"}))
    store.register(identity("a2"), expected_attempt="a1")
    store.append("e1", "a1", event("running"))
    assert store.current_attempt("e1") == "a2"
    assert store.get("e1", "a1").state == "completed"
    assert store.get("e1", "a2").state == "created"
    with pytest.raises(EvidenceError, match="attempt_changed"):
        store.register(identity("a3"), expected_attempt="a1")
    with pytest.raises(EvidenceError, match="binding_conflict"):
        store.register(replace(first, scope_id="other"), expected_attempt="a2")


@pytest.mark.parametrize(
    "order", list(permutations(["running", "unknown", "completed"]))
)
def test_order_independent_projection(store, order):
    store.register(identity(), expected_attempt=None)
    for kind in order:
        store.append("e1", "a1", event(kind))
    result = store.get("e1", "a1")
    assert result.state == "reconciled"
    assert result.outcome == "completed"


def test_duplicate_and_terminal_conflict_requires_explicit_reconciliation(store):
    store.register(identity(), expected_attempt=None)
    completed = event("completed", body={"receipt": "one"})
    store.append("e1", "a1", completed)
    revision = store.get("e1", "a1").revision
    store.append("e1", "a1", completed)
    assert store.get("e1", "a1").revision == revision
    store.append("e1", "a1", event("failed"))
    assert store.get("e1", "a1").state == "unknown"
    with pytest.raises(EvidenceError, match="evidence_changed"):
        store.resolve("e1", "a1", completed, expected_revision=revision)
    current = store.get("e1", "a1")
    store.resolve("e1", "a1", completed, expected_revision=current.revision)
    assert store.get("e1", "a1").state == "reconciled"
    # An old duplicate is harmless; a NEW contradictory fact invalidates resolution.
    store.append("e1", "a1", event("failed"))
    assert store.get("e1", "a1").state == "reconciled"
    store.append("e1", "a1", event("failed", "new-failure"))
    assert store.get("e1", "a1").state == "unknown"


def test_event_id_collision_is_persisted_as_conflict(store, store_factory):
    store.register(identity(), expected_attempt=None)
    store.append("e1", "a1", event("completed", "same", {"receipt": "one"}))
    store.append("e1", "a1", event("completed", "same", {"receipt": "two"}))
    assert store.get("e1", "a1").state == "unknown"
    reopened = store_factory()
    assert reopened.get("e1", "a1").state == "unknown"


def test_failure_never_becomes_success_and_data_is_detached(store):
    store.register(identity(), expected_attempt=None)
    body = {"nested": {"n": 1}}
    failed = event("failed", body=body)
    body["nested"]["n"] = 9
    store.append("e1", "a1", failed)
    store.append("e1", "a1", event("running"))
    result = store.get("e1", "a1")
    assert result.state == result.outcome == "failed"
    assert result.terminal.body["nested"]["n"] == 1
    result.terminal.body["nested"]["n"] = 7
    assert store.get("e1", "a1").terminal.body["nested"]["n"] == 1


def test_two_connections_cannot_both_replace_current_attempt(store, store_factory):
    store.register(identity(), expected_attempt=None)

    def replace_current(attempt):
        try:
            store_factory().register(identity(attempt), expected_attempt="a1")
            return True
        except EvidenceError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(replace_current, ["a2", "a3"])) == 1


def test_invalid_event_and_missing_identity_fail_closed(store):
    with pytest.raises(ValueError):
        event("success-ish")
    with pytest.raises(EvidenceError, match="attempt_missing"):
        store.append("e1", "a1", event("completed"))
