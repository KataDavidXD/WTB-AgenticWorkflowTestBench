"""External provider model with its own durable guard/effect transaction.

This exercises two distinct databases and process loss, not a claim about any
unmodified third-party API. The provider must own its authoritative guard state.
"""

import sqlite3
import subprocess
import sys
from pathlib import Path

from tests.unit.evidence.test_publication_sink import setup_sink
from wtb.domain.interfaces.publication_sink import ExternalOutcome
from wtb.infrastructure.evidence.external_delivery import ExternalDelivery


class DurableProvider:
    sink_id = "separate-provider-store"

    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS authority(token TEXT)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS effects(key TEXT PRIMARY KEY,payload BLOB,token TEXT)"
            )

    def set_authority(self, token):
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM authority")
            db.execute("INSERT INTO authority VALUES (?)", (token,))

    def lookup(self, request):
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT payload,token FROM effects WHERE key=?",
                (request.idempotency_key,),
            ).fetchone()
        if row is None:
            return ExternalOutcome("absent")
        assert row == (request.payload, request.authority_snapshot_sha256)
        return ExternalOutcome("applied", "receipt-" + request.idempotency_key)

    def apply(self, request):
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload,token FROM effects WHERE key=?",
                (request.idempotency_key,),
            ).fetchone()
            if row is None:
                authority = db.execute("SELECT token FROM authority").fetchone()
                if (
                    authority is None
                    or authority[0] != request.authority_snapshot_sha256
                ):
                    return ExternalOutcome("rejected")
                db.execute(
                    "INSERT INTO effects VALUES (?,?,?)",
                    (
                        request.idempotency_key,
                        request.payload,
                        request.authority_snapshot_sha256,
                    ),
                )
            else:
                assert row == (request.payload, request.authority_snapshot_sha256)
        return ExternalOutcome("applied", "receipt-" + request.idempotency_key)


def test_external_effect_then_process_death_reconciles(tmp_path):
    sink = setup_sink(tmp_path / "wtb.sqlite")
    approval = sink.approve("artifact.txt")
    provider = DurableProvider(tmp_path / "remote.sqlite")
    delivery = ExternalDelivery(sink)
    delivery.reserve(approval, provider.sink_id)
    request, _ = delivery._request(approval, provider.sink_id)
    provider.set_authority(request.authority_snapshot_sha256)
    worker = """
import os,sys
from tests.integration.test_external_delivery_crash import DurableProvider
from wtb.infrastructure.evidence.external_delivery import ExternalDelivery
from wtb.infrastructure.evidence.sqlite_store import SQLiteEvidenceStore
from wtb.infrastructure.evidence.publication_sink import SQLitePublicationSink
class CrashAfterEffect(DurableProvider):
    def apply(self, request):
        result = super().apply(request)
        assert result.status == 'applied'
        os._exit(73)
delivery = ExternalDelivery(SQLitePublicationSink(SQLiteEvidenceStore(sys.argv[1])))
delivery.retry_absent(sys.argv[2], CrashAfterEffect(sys.argv[3]))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            worker,
            str(sink.store.path),
            approval,
            str(provider.path),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 73, result.stderr.decode()
    assert delivery.dispatch(approval, provider).status == "applied"
    assert delivery.dispatch(approval, provider).status == "applied"
    with sqlite3.connect(provider.path) as db:
        assert db.execute("SELECT count(*) FROM effects").fetchone()[0] == 1


def test_provider_checks_authority_at_actual_effect_boundary(tmp_path):
    sink = setup_sink(tmp_path / "wtb.sqlite")
    approval = sink.approve("artifact.txt")
    provider = DurableProvider(tmp_path / "remote.sqlite")
    delivery = ExternalDelivery(sink)
    delivery.reserve(approval, provider.sink_id)
    request, _ = delivery._request(approval, provider.sink_id)
    provider.set_authority("new-provider-authority")
    # Local WTB still permits the old token; the remote guard must reject it.
    assert delivery.retry_absent(approval, provider).status == "rejected"
    assert provider.lookup(request).status == "absent"
