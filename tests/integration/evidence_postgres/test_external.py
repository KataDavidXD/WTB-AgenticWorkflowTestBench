"""Identical external-delivery contract against the PostgreSQL ledger."""

from tests.unit.evidence.test_external_delivery import (
    test_absent_unknown_does_not_blindly_redrive as test_absent_unknown_does_not_blindly_redrive,
    test_explicit_retry_revalidates_and_keeps_key as test_explicit_retry_revalidates_and_keeps_key,
    test_lost_ack_reconciles_without_another_effect as test_lost_ack_reconciles_without_another_effect,
    test_provider_rejection_is_persisted as test_provider_rejection_is_persisted,
)


def test_reopened_ledger_preserves_provider_binding(sink, reopen_sink):
    from tests.unit.evidence.test_external_delivery import Provider
    from wtb.infrastructure.evidence.external_delivery import ExternalDelivery

    approval = sink.approve("artifact.txt")
    first = Provider()
    first.lose_ack = True
    assert ExternalDelivery(sink).dispatch(approval, first).status == "unknown"
    other = Provider()
    other.sink_id = "different-provider"
    reopened = ExternalDelivery(reopen_sink())
    assert reopened.dispatch(approval, other).status == "rejected"
    assert other.calls == 0
    assert reopened.dispatch(approval, first).status == "applied"
    assert first.calls == 1


def test_external_effect_survives_process_death(
    sink, reopen_sink, pg_namespace, tmp_path
):
    import sqlite3
    import subprocess
    import sys

    from tests.integration.test_external_delivery_crash import DurableProvider
    from wtb.infrastructure.evidence.external_delivery import ExternalDelivery

    dsn, schema = pg_namespace
    approval = sink.approve("artifact.txt")
    provider = DurableProvider(tmp_path / "external.sqlite")
    delivery = ExternalDelivery(sink)
    delivery.reserve(approval, provider.sink_id)
    request, _ = delivery._request(approval, provider.sink_id)
    provider.set_authority(request.authority_snapshot_sha256)
    worker = """
import os, sys
from tests.integration.test_external_delivery_crash import DurableProvider
from wtb.infrastructure.evidence.postgres_store import PostgresEvidenceStore, PostgresPublicationSink
from wtb.infrastructure.evidence.external_delivery import ExternalDelivery
class CrashAfterEffect(DurableProvider):
    def apply(self, request):
        assert super().apply(request).status == "applied"
        os._exit(73)
delivery = ExternalDelivery(PostgresPublicationSink(PostgresEvidenceStore(sys.argv[1], schema=sys.argv[2])))
delivery.retry_absent(sys.argv[3], CrashAfterEffect(sys.argv[4]))
"""
    child = subprocess.run(
        [sys.executable, "-c", worker, dsn, schema, approval, str(provider.path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 73, child.stderr
    reopened = ExternalDelivery(reopen_sink())
    assert reopened.dispatch(approval, provider).status == "applied"
    assert reopened.dispatch(approval, provider).status == "applied"
    assert not sink.publish(approval).allowed
    with sqlite3.connect(provider.path) as db:
        assert db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 1


def test_external_provider_guard_rejects_changed_authority(sink, tmp_path):
    from tests.integration.test_external_delivery_crash import DurableProvider
    from wtb.infrastructure.evidence.external_delivery import ExternalDelivery

    approval = sink.approve("artifact.txt")
    provider = DurableProvider(tmp_path / "external.sqlite")
    delivery = ExternalDelivery(sink)
    delivery.reserve(approval, provider.sink_id)
    request, _ = delivery._request(approval, provider.sink_id)
    provider.set_authority("new-authority")
    assert delivery.retry_absent(approval, provider).status == "rejected"
    assert provider.lookup(request).status == "absent"


from tests.unit.evidence.test_external_delivery import (
    test_local_rejection_preserves_recovery as test_local_rejection_preserves_recovery,
)
