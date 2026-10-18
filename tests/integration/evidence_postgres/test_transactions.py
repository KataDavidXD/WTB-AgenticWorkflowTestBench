import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.unit.evidence.test_event_store import event, identity


def test_waiting_publisher_observes_committed_revocation(sink, pg_namespace):
    import psycopg

    approval = sink.approve("artifact.txt")
    dsn, _ = pg_namespace
    with ThreadPoolExecutor(max_workers=1) as pool:
        with sink.store.transaction() as db:
            db.execute("UPDATE publication_policies SET generation=generation+1")
            future = pool.submit(sink.publish, approval)
            # Prove this publisher actually waited for our transaction, rather
            # than relying on scheduling or a fixed sleep.
            with psycopg.connect(dsn, autocommit=True) as observer:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    waiting = observer.execute(
                        "SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
                        "AND NOT granted AND classid::bigint=%s AND objid::bigint=%s",
                        (
                            (sink.store._lock_id >> 32) & 0xFFFFFFFF,
                            sink.store._lock_id & 0xFFFFFFFF,
                        ),
                    ).fetchone()[0]
                    if waiting:
                        break
                    time.sleep(0.02)
                else:
                    pytest.fail("Publisher never reached the transaction lock")
            assert not future.done()
        result = future.result(timeout=10)
    assert not result.allowed and not result.new_effect
    with sink.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM publication_effects").fetchone()[0] == 0


def test_lock_timeout_fails_closed_then_recovers(sink, store_factory):
    import psycopg
    from wtb.infrastructure.evidence.postgres_store import (
        PostgresPublicationSink,
    )

    other = PostgresPublicationSink(store_factory(lock_timeout_ms=100))
    approval = sink.approve("artifact.txt")
    with sink.store.transaction():
        with pytest.raises(psycopg.errors.LockNotAvailable):
            other.publish(approval)
    assert other.publish(approval).new_effect
    assert not sink.publish(approval).new_effect


@pytest.mark.parametrize("committed", [False, True])
def test_process_death_respects_commit_boundary(sink, pg_namespace, committed):
    dsn, schema = pg_namespace
    approval = sink.approve("artifact.txt")
    code = """
import os, sys
from wtb.infrastructure.evidence.postgres_store import PostgresEvidenceStore, PostgresPublicationSink
store = PostgresEvidenceStore(sys.argv[1], schema=sys.argv[2])
sink = PostgresPublicationSink(store)
if sys.argv[4] == "True":
    assert sink.publish(sys.argv[3]).new_effect
    os._exit(73)
original = sink.verify
def die_before_commit(db, approval):
    decision, payload = original(db, approval)
    db.execute("INSERT INTO publication_effects VALUES (?,?,?)", (approval, payload, "uncommitted"))
    db.execute("UPDATE publication_approvals SET consumed=1 WHERE id=?", (approval,))
    os._exit(73)
sink.verify = die_before_commit
sink.publish(sys.argv[3])
"""
    child = subprocess.run(
        [sys.executable, "-c", code, dsn, schema, approval, str(committed)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 73, child.stderr
    result = sink.publish(approval)
    assert result.allowed and result.new_effect is not committed
    with sink.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM publication_effects").fetchone()[0] == 1
        assert (
            db.execute("SELECT payload FROM publication_effects").fetchone()[0]
            == b"payload"
        )


def test_concurrent_bootstrap_and_bound_json(store_factory):
    with ThreadPoolExecutor(max_workers=4) as pool:
        stores = list(pool.map(lambda _: store_factory(), range(4)))
    stores[0].register(identity(), expected_attempt=None)
    body = {"receipt": "产物 ' ? % ; DROP TABLE evidence_events; --"}
    stores[0].append("e1", "a1", event("completed", body=body))
    assert stores[1].get("e1", "a1").terminal.body == body
    with stores[2].transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM evidence_schema").fetchone()[0] == 1


def test_generation_exceeds_32_bits(sink):
    with sink.store.transaction() as db:
        db.execute("UPDATE publication_policies SET generation=?", (2**31,))
    assert sink.update_policy("scope", "actor", expected_generation=2**31) == 2**31 + 1


def test_schema_version_and_exception_rollback(store):
    from wtb.domain.execution_evidence import EvidenceError

    with pytest.raises(RuntimeError):
        with store.transaction() as db:
            db.execute("UPDATE evidence_schema SET version=999")
            raise RuntimeError("abort")
    store._initialize_schema()
    with store.transaction() as db:
        db.execute("UPDATE evidence_schema SET version=999")
    with pytest.raises(EvidenceError, match="unsupported_evidence_schema"):
        store._initialize_schema()


def test_two_processes_consume_once(sink, pg_namespace):
    dsn, schema = pg_namespace
    approval = sink.approve("artifact.txt")
    worker = """
import sys
from wtb.infrastructure.evidence.postgres_store import PostgresEvidenceStore, PostgresPublicationSink
sink = PostgresPublicationSink(PostgresEvidenceStore(sys.argv[1], schema=sys.argv[2]))
print("ready", flush=True)
sys.stdin.readline()
result = sink.publish(sys.argv[3])
assert result.allowed
print(int(result.new_effect), flush=True)
"""
    children = []
    try:
        for _ in range(2):
            children.append(
                subprocess.Popen(
                    [sys.executable, "-c", worker, dsn, schema, approval],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        # Initialization may itself acquire the lock; wait before holding it.
        for child in children:
            assert child.stdout.readline().strip() == "ready"
        with sink.store.transaction():
            for child in children:
                child.stdin.write("publish\n")
                child.stdin.flush()
        results = []
        for child in children:
            output, error = child.communicate(timeout=20)
            assert child.returncode == 0, error
            results.append(int(output.strip()))
        assert sorted(results) == [0, 1]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
