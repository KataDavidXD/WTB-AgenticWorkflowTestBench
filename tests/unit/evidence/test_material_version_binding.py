"""Material generation is distinct from payload and execution-evidence identity."""

import pytest

from tests.unit.evidence.test_publication_sink import setup_sink
from wtb.domain.execution_evidence import EvidenceError
from wtb.infrastructure.evidence.external_delivery import ExternalDelivery
from wtb.infrastructure.evidence.publication_sink import SQLitePublicationSink
from wtb.infrastructure.evidence.sqlite_store import SQLiteEvidenceStore


@pytest.fixture
def sink(tmp_path):
    return setup_sink(tmp_path / "publication.sqlite")


@pytest.fixture
def reopen_sink(sink):
    return lambda: SQLitePublicationSink(SQLiteEvidenceStore(sink.store.path))


def advance_identical_material(sink):
    with sink.store.transaction() as db:
        payload, mutations, version = sink.material(db, "artifact.txt")
    return sink.record_material(
        "artifact.txt", payload, mutations, expected_version=version
    )


@pytest.mark.parametrize("reopen", [False, True])
def test_identical_material_generation_invalidates_unconsumed_approval(sink, reopen_sink, reopen):
    approval = sink.approve("artifact.txt")
    assert advance_identical_material(sink) == 2
    if reopen:
        sink = reopen_sink()
    result = sink.publish(approval)
    assert not result.allowed and not result.new_effect
    assert result.reason == "material_changed"
    with sink.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM publication_effects").fetchone()[0] == 0
    assert sink.publish(sink.approve("artifact.txt")).new_effect


def test_identical_material_generation_denies_external_reservation(sink):
    approval = sink.approve("artifact.txt")
    advance_identical_material(sink)
    delivery = ExternalDelivery(sink)
    with pytest.raises(EvidenceError, match="material_changed"):
        delivery.reserve(approval, "receiver")
    with sink.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM publication_deliveries").fetchone()[0] == 0


def test_committed_effect_replay_survives_material_generation_change(sink, reopen_sink):
    approval = sink.approve("artifact.txt")
    assert sink.publish(approval).new_effect
    advance_identical_material(sink)
    reopened = reopen_sink()
    replay = reopened.publish(approval)
    assert replay.allowed and not replay.new_effect
    with reopened.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM publication_effects").fetchone()[0] == 1


@pytest.mark.parametrize("committed", [False, True])
def test_upgrade_never_infers_missing_approval_version(sink, reopen_sink, committed):
    approval = sink.approve("artifact.txt")
    if committed:
        assert sink.publish(approval).new_effect
    with sink.store.transaction() as db:
        # Reconstruct the old schema: it stored no material-version binding.
        db.execute("DROP TABLE publication_approval_material_versions")
    reopened = reopen_sink()
    result = reopened.publish(approval)
    assert not result.new_effect
    assert result.allowed is committed
    if not committed:
        assert result.reason == "approval_version_missing"
        assert reopened.publish(reopened.approve("artifact.txt")).new_effect


def test_approval_version_is_written_by_library_in_same_transaction(sink):
    approval = sink.approve("artifact.txt")
    with sink.store.transaction() as db:
        bound = db.execute(
            "SELECT target,version FROM publication_approval_material_versions "
            "WHERE approval=?", (approval,),
        ).fetchone()
        assert (bound["target"], bound["version"]) == ("artifact.txt", 1)
    advance_identical_material(sink)
    with sink.store.transaction() as db:
        bound = db.execute(
            "SELECT version FROM publication_approval_material_versions "
            "WHERE approval=?", (approval,),
        ).fetchone()
        assert bound["version"] == 1


def test_authoritative_absence_cannot_redrive_old_material_generation(sink):
    from tests.unit.evidence.test_external_delivery import Provider

    provider = Provider()
    delivery = ExternalDelivery(sink)
    approval = sink.approve("artifact.txt")
    delivery.reserve(approval, provider.sink_id)
    advance_identical_material(sink)
    result = delivery.retry_absent(approval, provider)
    assert result.status == "rejected" and result.reason == "material_changed"
    assert provider.calls == 0 and provider.effects == {}


def test_unknown_committed_delivery_is_reconciled_after_version_change(sink):
    from tests.unit.evidence.test_external_delivery import Provider

    provider = Provider()
    provider.lose_ack = True
    delivery = ExternalDelivery(sink)
    approval = sink.approve("artifact.txt")
    assert delivery.dispatch(approval, provider).status == "unknown"
    advance_identical_material(sink)
    assert delivery.dispatch(approval, provider).status == "applied"
    assert provider.calls == 1


def test_unrelated_material_version_does_not_invalidate_approval(sink):
    from dataclasses import replace

    approval = sink.approve("artifact.txt")
    with sink.store.transaction() as db:
        payload, mutations, _ = sink.material(db, "artifact.txt")
    sink.record_material("other.txt", payload,
                         tuple(replace(m, target_path="other.txt") for m in mutations),
                         expected_version=0)
    assert sink.publish(approval).new_effect
