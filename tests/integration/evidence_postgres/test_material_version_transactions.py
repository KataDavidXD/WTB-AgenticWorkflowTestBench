"""Check the material-version boundary under real PostgreSQL contention."""

from concurrent.futures import ThreadPoolExecutor
import time

import pytest


def wait_for_transaction_lock(sink, pg_namespace):
    import psycopg

    dsn, _ = pg_namespace
    with psycopg.connect(dsn, autocommit=True) as observer:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            waiting = observer.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
                "AND NOT granted AND classid::bigint=%s AND objid::bigint=%s",
                ((sink.store._lock_id >> 32) & 0xFFFFFFFF,
                 sink.store._lock_id & 0xFFFFFFFF),
            ).fetchone()[0]
            if waiting:
                return
            time.sleep(0.02)
    pytest.fail("Contending operation never reached the transaction lock")


def advance_material_in_open_transaction(db):
    # Arrange the trusted material writer's identical-byte SQL transition while
    # holding its commit open. Calling record_material() here would open another
    # pooled connection and self-deadlock; public API tests cover that method.
    version = db.execute(
        "SELECT version FROM publication_material WHERE target=?", ("artifact.txt",)
    ).fetchone()[0]
    db.execute(
        "INSERT INTO publication_material_history (target,version,payload,mutations) "
        "SELECT target,version+1,payload,mutations FROM publication_material WHERE target=?",
        ("artifact.txt",),
    )
    db.execute(
        "UPDATE publication_material SET version=version+1 WHERE target=?",
        ("artifact.txt",),
    )
    return version + 1


@pytest.mark.parametrize("operation", ["approve", "publish"])
def test_material_generation_commit_serializes_with_authority(sink, pg_namespace, operation):
    old_approval = sink.approve("artifact.txt")
    with ThreadPoolExecutor(max_workers=1) as pool:
        with sink.store.transaction() as db:
            new_version = advance_material_in_open_transaction(db)
            if operation == "approve":
                future = pool.submit(sink.approve, "artifact.txt")
            else:
                future = pool.submit(sink.publish, old_approval)
            wait_for_transaction_lock(sink, pg_namespace)
            assert not future.done()
        result = future.result(timeout=10)

    if operation == "approve":
        with sink.store.transaction() as db:
            bound = db.execute(
                "SELECT version FROM publication_approval_material_versions WHERE approval=?",
                (result,),
            ).fetchone()[0]
        assert bound == new_version
        assert sink.publish(result).new_effect
    else:
        assert not result.allowed and not result.new_effect
        assert result.reason == "material_changed"
        with sink.store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM publication_effects").fetchone()[0] == 0


def test_rollback_does_not_advance_material_or_approval_binding(sink):
    approval = sink.approve("artifact.txt")
    with pytest.raises(RuntimeError, match="abort material transaction"):
        with sink.store.transaction() as db:
            advance_material_in_open_transaction(db)
            raise RuntimeError("abort material transaction")
    assert sink.publish(approval).new_effect
