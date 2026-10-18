"""Real backend contracts for pooled transactions and eager initialization."""

import pytest

from wtb.infrastructure.evidence.postgres_store import PostgresEvidenceStore


def test_connection_reused_but_transactions_are_independent(pg_namespace):
    dsn, schema = pg_namespace
    with PostgresEvidenceStore(dsn, schema=schema, pool_max_size=1) as store:
        with store.transaction() as db:
            first = db.execute("SELECT pg_backend_pid(), txid_current()").fetchone()
        with store.transaction() as db:
            second = db.execute("SELECT pg_backend_pid(), txid_current()").fetchone()
        assert first[0] == second[0]
        assert first[1] != second[1]
    store.close()  # Idempotent shutdown.
    from psycopg_pool import PoolClosed

    with pytest.raises(PoolClosed):
        with store.transaction():
            pytest.fail("closed store yielded a connection")


@pytest.mark.parametrize("failure", ["python", "sql", "base_exception"])
def test_rollback_and_transaction_state_clean_before_reuse(pg_namespace, failure):
    import psycopg

    dsn, schema = pg_namespace
    with PostgresEvidenceStore(dsn, schema=schema, pool_max_size=1) as store:
        with store.transaction() as db:
            pid = db.execute("SELECT pg_backend_pid()").fetchone()[0]

        class Abort(BaseException):
            pass

        error = {
            "python": RuntimeError,
            "sql": psycopg.errors.DivisionByZero,
            "base_exception": Abort,
        }[failure]
        with pytest.raises(error):
            with store.transaction() as db:
                db.execute("UPDATE evidence_schema SET version=999")
                db.execute("SET LOCAL application_name='temporary-operation'")
                if failure == "sql":
                    db.execute("SELECT 1/0")
                raise error("abort")
        with store._pool.connection() as connection:
            assert (
                connection.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
            )
            assert (
                connection.execute("SHOW application_name").fetchone()[0]
                != "temporary-operation"
            )
        with store.transaction() as db:
            assert db.execute("SELECT pg_backend_pid()").fetchone()[0] == pid
            assert db.execute("SELECT version FROM evidence_schema").fetchone()[0] == 1


def test_hot_operations_do_not_create_or_check_schema(pg_namespace, monkeypatch):
    import psycopg
    from tests.unit.evidence.test_event_store import identity

    dsn, schema = pg_namespace
    with PostgresEvidenceStore(dsn, schema=schema, pool_max_size=1) as store:
        statements = []
        original = psycopg.Connection.execute

        def execute(connection, query, *args, **kwargs):
            statements.append(
                query.as_string(connection)
                if hasattr(query, "as_string")
                else str(query)
            )
            return original(connection, query, *args, **kwargs)

        monkeypatch.setattr(psycopg.Connection, "execute", execute)
        store.register(identity(), expected_attempt=None)
        assert store.get("e1", "a1").state == "created"
        assert not any(
            "CREATE " in s.upper() or "EVIDENCE_SCHEMA" in s.upper() for s in statements
        )


def test_incompatible_schema_closes_failed_constructor_pool(pg_namespace, monkeypatch):
    import psycopg_pool
    from wtb.domain.execution_evidence import EvidenceError

    dsn, schema = pg_namespace
    with PostgresEvidenceStore(dsn, schema=schema) as old:
        with old.transaction() as db:
            db.execute("UPDATE evidence_schema SET version=999")
    created = []
    original = psycopg_pool.ConnectionPool

    def track(*args, **kwargs):
        pool = original(*args, **kwargs)
        created.append(pool)
        return pool

    monkeypatch.setattr(psycopg_pool, "ConnectionPool", track)
    with pytest.raises(EvidenceError, match="unsupported_evidence_schema"):
        PostgresEvidenceStore(dsn, schema=schema)
    assert len(created) == 1 and created[0].closed


def test_pool_exhaustion_is_bounded_and_recovers(pg_namespace):
    from psycopg_pool import PoolTimeout

    dsn, schema = pg_namespace
    with PostgresEvidenceStore(
        dsn, schema=schema, pool_max_size=1, pool_timeout=0.1
    ) as store:
        with store.transaction():
            with pytest.raises(PoolTimeout):
                with store.transaction():
                    pytest.fail("pool exceeded its configured size")
        with store.transaction() as db:
            assert db.execute("SELECT version FROM evidence_schema").fetchone()[0] == 1


def test_broken_connection_is_discarded(pg_namespace):
    import psycopg

    dsn, schema = pg_namespace
    with PostgresEvidenceStore(dsn, schema=schema, pool_max_size=1) as store:
        with pytest.raises(psycopg.Error):
            with store.transaction() as db:
                old_pid = db.execute("SELECT pg_backend_pid()").fetchone()[0]
                db.execute("UPDATE evidence_schema SET version=999")
                db.connection.close()
                db.execute("SELECT 1")
        with store.transaction() as db:
            assert db.execute("SELECT pg_backend_pid()").fetchone()[0] != old_pid
            assert db.execute("SELECT version FROM evidence_schema").fetchone()[0] == 1


def test_close_rejects_waiters_but_allows_active_transaction_to_finish(pg_namespace):
    import time
    from concurrent.futures import ThreadPoolExecutor
    import psycopg
    from psycopg import sql
    from psycopg_pool import PoolClosed

    dsn, schema = pg_namespace
    with PostgresEvidenceStore(dsn, schema=schema, pool_max_size=1) as store:

        def borrow():
            with store.transaction():
                pytest.fail("closed pool admitted its waiter")

        with ThreadPoolExecutor(max_workers=1) as executor:
            with store.transaction() as db:
                connection = db.connection
                db.execute("UPDATE evidence_schema SET version=2")
                waiter = executor.submit(borrow)
                deadline = time.monotonic() + 5
                while store._pool.get_stats().get("requests_waiting", 0) != 1:
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                store.close()
                with pytest.raises(PoolClosed):
                    waiter.result(timeout=5)
                assert not connection.closed
            assert connection.closed
    with psycopg.connect(dsn) as observer:
        assert (
            observer.execute(
                sql.SQL("SELECT version FROM {}.evidence_schema").format(
                    sql.Identifier(schema)
                )
            ).fetchone()[0]
            == 2
        )


def test_failed_bootstrap_rolls_back_schema_and_closes_pool(pg_namespace, monkeypatch):
    import psycopg
    import psycopg_pool
    from wtb.infrastructure.evidence.postgres_store import _Connection

    dsn, schema = pg_namespace
    created = []
    original_pool = psycopg_pool.ConnectionPool

    def pool(*args, **kwargs):
        result = original_pool(*args, **kwargs)
        created.append(result)
        return result

    original_execute = _Connection.execute

    def execute(connection, statement, parameters=()):
        if "CREATE TABLE IF NOT EXISTS evidence_heads" in statement:
            return original_execute(connection, "SELECT 1/0")
        return original_execute(connection, statement, parameters)

    monkeypatch.setattr(psycopg_pool, "ConnectionPool", pool)
    monkeypatch.setattr(_Connection, "execute", execute)
    with pytest.raises(psycopg.errors.DivisionByZero):
        PostgresEvidenceStore(dsn, schema=schema)
    assert len(created) == 1 and created[0].closed
    with psycopg.connect(dsn) as observer:
        assert (
            observer.execute(
                "SELECT count(*) FROM pg_namespace WHERE nspname=%s", (schema,)
            ).fetchone()[0]
            == 0
        )
