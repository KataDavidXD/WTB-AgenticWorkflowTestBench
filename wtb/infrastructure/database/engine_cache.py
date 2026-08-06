"""
Shared SQLAlchemy engines keyed by (db_url, echo).

Avoids creating a new Engine per UnitOfWork instance for the same database URL.
In-memory SQLite is excluded from caching because each caller expects its own DB.
"""

from functools import lru_cache

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool


def normalize_db_url(db_url: str) -> str:
    """Select the installed psycopg 3 SQLAlchemy dialect for PostgreSQL URLs."""
    normalized_url = db_url.lower()
    for prefix in ("postgresql://", "postgres://"):
        if normalized_url.startswith(prefix):
            return f"postgresql+psycopg://{db_url[len(prefix) :]}"
    return db_url


def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    """Enable SQLite foreign-key enforcement for each DBAPI connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def configure_sqlite_foreign_keys(engine: Engine) -> Engine:
    """Install SQLite connection initialization without affecting other DBs."""
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def _create_engine(db_url: str, echo: bool) -> Engine:
    db_url = normalize_db_url(db_url)
    normalized_url = db_url.lower()
    engine_options = {}
    if normalized_url.startswith("sqlite:") and ":memory:" not in normalized_url:
        # A cached QueuePool retains an idle sqlite3 connection after the UoW
        # session closes, which keeps the database file locked on Windows.
        # Keep caching the lightweight Engine/dialect, but close every physical
        # connection as soon as SQLAlchemy checks it back in.
        engine_options["poolclass"] = NullPool
    return configure_sqlite_foreign_keys(
        create_engine(db_url, echo=echo, **engine_options)
    )


@lru_cache(maxsize=32)
def _get_cached_engine(db_url: str, echo: bool) -> Engine:
    """Internal cached engine factory for file-backed databases."""
    return _create_engine(db_url, echo)


def get_engine(db_url: str, echo: bool = False) -> Engine:
    """Return an Engine for the given URL. In-memory SQLite bypasses cache."""
    if ":memory:" in db_url:
        return _create_engine(db_url, echo)
    return _get_cached_engine(db_url, echo)
