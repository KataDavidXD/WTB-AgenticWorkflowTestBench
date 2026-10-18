import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.unit.evidence.test_collector import receipt
from tests.unit.evidence.test_event_store import event, identity
from wtb.application.services.evidence_collector import EvidenceCollector
from wtb.domain.execution_evidence import EvidenceError
from wtb.domain.publication_authority import ScopeMutationEvidence
from wtb.infrastructure.evidence.publication_sink import SQLitePublicationSink
from wtb.infrastructure.evidence.sqlite_store import SQLiteEvidenceStore


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def setup_sink(path=None, *, store=None, sink_type=SQLitePublicationSink):
    store = store if store is not None else SQLiteEvidenceStore(path)
    store.register(identity(), expected_attempt=None)
    collector = EvidenceCollector(store)
    collector.record(identity(), receipt(), native_reference="native-message-93")
    source = store.get("e1", "a1").terminal.digest
    mutation = ScopeMutationEvidence(
        "scope",
        "actor",
        "e1",
        "a1",
        "d" * 64,
        source,
        "mutation",
        1,
        "artifact.txt",
        "d" * 64,
        digest(b""),
        digest(b"payload"),
        digest(b""),
        digest(b"payload"),
    )
    sink = sink_type(store)
    sink.update_policy("scope", "actor", expected_generation=0)
    sink.record_material("artifact.txt", b"payload", (mutation,), expected_version=0)
    return sink


@pytest.fixture
def sink(tmp_path):
    return setup_sink(tmp_path / "publication.sqlite")


@pytest.fixture
def reopen_sink(sink):
    return lambda: SQLitePublicationSink(SQLiteEvidenceStore(sink.store.path))


def test_atomic_effect_and_reopen_replay(sink, reopen_sink):
    approval = sink.approve("artifact.txt")
    first = sink.publish(approval)
    assert first.new_effect and first.allowed
    reopened = reopen_sink()
    second = reopened.publish(approval)
    assert second.allowed and not second.new_effect
    with sink.store.transaction() as db:
        assert (
            db.execute("SELECT payload FROM publication_effects").fetchone()[0]
            == b"payload"
        )


@pytest.mark.parametrize("change", ["policy", "aba", "retry", "conflict", "material"])
def test_relevant_changes_deny_stale_approval(sink, change):
    approval = sink.approve("artifact.txt")
    if change == "policy":
        sink.update_policy("scope", "actor", expected_generation=1)
    elif change == "aba":
        sink.update_policy("scope", "other", expected_generation=1)
        sink.update_policy("scope", "actor", expected_generation=2)
    elif change == "retry":
        sink.store.register(identity("a2"), expected_attempt="a1")
    elif change == "conflict":
        sink.store.append("e1", "a1", event("failed"))
    else:
        with sink.store.transaction() as db:
            # A corrupted backing store is outside the trust model, but byte
            # mismatch should still fail closed before writing any effect.
            db.execute("UPDATE publication_material SET payload=?", (b"changed",))
    result = sink.publish(approval)
    assert not result.allowed and not result.new_effect
    with sink.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM publication_effects").fetchone()[0] == 0


def test_unrelated_update_does_not_invalidate_approval(sink):
    approval = sink.approve("artifact.txt")
    sink.update_policy("other-scope", "other", expected_generation=0)
    assert sink.publish(approval).new_effect


def test_concurrent_policy_cas_and_replay(sink):
    approval = sink.approve("artifact.txt")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: sink.publish(approval), range(2)))
    assert all(r.allowed for r in results)
    assert sum(r.new_effect for r in results) == 1

    def update(_):
        try:
            sink.update_policy("scope", "actor", expected_generation=1)
            return True
        except EvidenceError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(update, range(2))) == 1


def test_record_material_requires_exact_version_and_payload(sink):
    with sink.store.transaction() as db:
        _, mutations, version = sink.material(db, "artifact.txt")
    with pytest.raises(EvidenceError, match="material_changed"):
        sink.record_material("artifact.txt", b"payload", mutations, expected_version=0)
    with pytest.raises(EvidenceError, match="material_mismatch"):
        sink.record_material(
            "artifact.txt", b"bad", mutations, expected_version=version
        )


def test_failure_can_be_reconciled_but_not_automatically_permitted(sink):
    approval = sink.approve("artifact.txt")
    completed = sink.store.get("e1", "a1").terminal
    sink.store.append("e1", "a1", event("failed"))
    assert not sink.publish(approval).allowed
    state = sink.store.get("e1", "a1")
    sink.store.resolve("e1", "a1", completed, expected_revision=state.revision)
    assert sink.publish(approval).allowed


def test_snapshot_reuse_checks_current_versions_before_cache(sink, monkeypatch):
    from wtb.domain.publication_authority import PublicationAuthorityOperator

    original = PublicationAuthorityOperator.snapshot_from_executions
    calls = []

    def counted(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(
        PublicationAuthorityOperator, "snapshot_from_executions", counted
    )
    with sink.store.transaction() as db:
        first, _ = sink.snapshot(db, "artifact.txt")
        second, _ = sink.snapshot(db, "artifact.txt")
    assert first == second
    assert len(calls) == 1
    sink.update_policy("other", "actor", expected_generation=0)
    with sink.store.transaction() as db:
        sink.snapshot(db, "artifact.txt")
    assert len(calls) == 1
    sink.update_policy("scope", "actor", expected_generation=1)
    with sink.store.transaction() as db:
        changed, _ = sink.snapshot(db, "artifact.txt")
    assert len(calls) == 2 and changed.snapshot_sha256 != first.snapshot_sha256


def test_redelivery_id_does_not_change_receipt_or_approval(sink):
    from wtb.domain.execution_evidence import EvidenceEvent

    approval = sink.approve("artifact.txt")
    original = sink.store.get("e1", "a1").terminal
    # Exercise both digest orders, retaining every transport event for audit.
    for index in range(20):
        sink.store.append(
            "e1",
            "a1",
            EvidenceEvent(f"redelivery-{index}", original.kind, original.body),
        )
        assert sink.store.get("e1", "a1").terminal.digest == original.digest
    assert sink.publish(approval).new_effect


@pytest.mark.parametrize(
    "field,value", [("mutation_sequence", 2), ("target_path", "other.txt")]
)
def test_invalid_material_chain_rejected_without_changing_state(sink, field, value):
    from dataclasses import replace

    approval = sink.approve("artifact.txt")
    with sink.store.transaction() as db:
        payload, mutations, version = sink.material(db, "artifact.txt")
        count = db.execute(
            "SELECT COUNT(*) FROM publication_material_history"
        ).fetchone()[0]
    with pytest.raises(EvidenceError) as error:
        sink.record_material(
            "artifact.txt",
            payload,
            (replace(mutations[0], **{field: value}),),
            expected_version=version,
        )
    assert error.value.recovery == "bind_current_material"
    with sink.store.transaction() as db:
        assert sink.material(db, "artifact.txt") == (payload, mutations, version)
        assert (
            db.execute("SELECT COUNT(*) FROM publication_material_history").fetchone()[
                0
            ]
            == count
        )
    assert sink.publish(approval).new_effect


def test_existing_receipt_binding_survives_store_upgrade(sink, reopen_sink):
    from wtb.domain.execution_evidence import EvidenceEvent

    approval = sink.approve("artifact.txt")
    original = sink.store.get("e1", "a1").terminal
    with sink.store.transaction() as db:
        db.execute("DROP TABLE evidence_terminal_bindings")
    reopened = reopen_sink()
    assert reopened.store.get("e1", "a1").terminal.digest == original.digest
    reopened.store.append(
        "e1", "a1", EvidenceEvent("new-delivery", original.kind, original.body)
    )
    assert reopened.publish(approval).new_effect


@pytest.mark.parametrize("broken", ["material", "workspace", "scope"])
def test_discontinuous_chain_does_not_replace_material(sink, broken):
    from dataclasses import replace

    with sink.store.transaction() as db:
        payload, mutations, version = sink.material(db, "artifact.txt")
    first = mutations[0]
    second = replace(
        first,
        scope_id="scope-two",
        logical_mutation_id="second",
        mutation_sequence=2,
        before_material_sha256=first.after_material_sha256,
        pre_workspace_digest=first.post_workspace_digest,
    )
    if broken == "material":
        second = replace(second, before_material_sha256="f" * 64)
    elif broken == "workspace":
        second = replace(second, pre_workspace_digest="f" * 64)
    else:
        second = replace(second, scope_id=first.scope_id)
    with pytest.raises(EvidenceError, match="invalid_material_chain"):
        sink.record_material(
            "artifact.txt", payload, (first, second), expected_version=version
        )
    with sink.store.transaction() as db:
        assert sink.material(db, "artifact.txt") == (payload, mutations, version)
