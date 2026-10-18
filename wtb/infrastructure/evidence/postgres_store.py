"""Optional PostgreSQL backend with a schema-wide transaction consistency boundary.

All evidence, policy and publication writers must use this port. Direct SQL
writes bypass its advisory lock. Serialization favors correctness over throughput.
"""

from contextlib import contextmanager
from hashlib import sha256
import math
import re

from wtb.infrastructure.evidence.sql_store import SQLEvidenceStore
from wtb.infrastructure.evidence.publication_sink import SQLPublicationSink


class _Row(dict):
    def __getitem__(self, key):
        return (
            tuple(self.values())[key]
            if isinstance(key, int)
            else super().__getitem__(key)
        )


def _row_factory(cursor):
    names = [column.name for column in cursor.description or ()]
    return lambda values: _Row(zip(names, values, strict=True))


class _Connection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement, parameters=()):
        # Internal fixed SQL only: question marks in SQL literals are unsupported.
        # User content is always bound separately, never interpolated into SQL.
        if statement.count("?") != len(parameters):
            raise ValueError("SQL parameter count mismatch")
        if not parameters:
            return self.connection.execute(statement)
        return self.connection.execute(
            statement.replace("%", "%%").replace("?", "%s"), parameters
        )


class PostgresEvidenceStore(SQLEvidenceStore):
    blob_type = "BYTEA"

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "wtb_evidence",
        lock_timeout_ms: int = 30000,
        connect_timeout: int = 10,
        pool_min_size: int = 1,
        pool_max_size: int = 4,
        pool_timeout: float = 30.0,
    ):
        if (
            not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema)
            or schema.startswith("pg_")
            or schema in {"public", "information_schema"}
        ):
            raise ValueError("A dedicated lowercase PostgreSQL schema is required")
        for value in (lock_timeout_ms, connect_timeout):
            if type(value) is not int or value <= 0:
                raise ValueError("Timeouts must be positive integers")
        if (
            type(pool_min_size) is not int
            or type(pool_max_size) is not int
            or not 1 <= pool_min_size <= pool_max_size
        ):
            raise ValueError("Pool sizes must be positive integers with min <= max")
        if (
            isinstance(pool_timeout, bool)
            or not isinstance(pool_timeout, (int, float))
            or not math.isfinite(pool_timeout)
            or pool_timeout <= 0
        ):
            raise ValueError("Pool timeout must be positive and finite")
        try:
            import psycopg
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise ImportError("Install WTB's postgres-evidence extra") from exc
        self._driver = psycopg
        self._dsn = dsn
        self.schema = schema
        self.lock_timeout_ms = lock_timeout_ms
        self.connect_timeout = connect_timeout
        self._lock_id = int.from_bytes(
            sha256(("wtb.evidence.v1:" + schema).encode()).digest()[:8],
            "big",
            signed=True,
        )
        self._pool = ConnectionPool(
            dsn,
            kwargs={"row_factory": _row_factory, "connect_timeout": connect_timeout},
            min_size=pool_min_size,
            max_size=pool_max_size,
            timeout=pool_timeout,
            open=False,
        )
        try:
            self._pool.open(wait=True, timeout=connect_timeout)
            self._initialize_schema()
        except BaseException:
            self._pool.close()
            raise

    def close(self) -> None:
        """Stop new borrows and close idle connections; active ones close on return.

        Idempotent. Let application workers finish before calling this method
        when shutdown must also wait for their transactions to complete.
        """
        self._pool.close()

    def __enter__(self):
        if self._pool.closed:
            from psycopg_pool import PoolClosed

            raise PoolClosed("PostgreSQL evidence store is closed")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def _initialize_schema(self, connection=None) -> None:
        # Namespace and version/table bootstrap share the normal serialization
        # lock and one transaction. No partially initialized store is published.
        with self.transaction() as db:
            db.connection.execute(
                self._driver.sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    self._driver.sql.Identifier(self.schema)
                )
            )
            super()._initialize_schema(db)

    @contextmanager
    def transaction(self):
        driver = self._driver
        with self._pool.connection() as connection:
            connection.isolation_level = driver.IsolationLevel.READ_COMMITTED
            with connection.transaction():
                connection.execute(
                    "SELECT pg_catalog.set_config('lock_timeout', %s, true)",
                    (f"{self.lock_timeout_ms}ms",),
                )
                connection.execute(
                    "SELECT pg_catalog.pg_advisory_xact_lock(%s)", (self._lock_id,)
                )
                connection.execute(
                    driver.sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                        driver.sql.Identifier(self.schema)
                    )
                )
                yield _Connection(connection)


class PostgresPublicationSink(SQLPublicationSink):
    def __init__(self, store: PostgresEvidenceStore):
        super().__init__(store)
