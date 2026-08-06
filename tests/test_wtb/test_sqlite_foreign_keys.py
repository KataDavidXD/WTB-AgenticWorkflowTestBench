"""Regression tests for SQLite foreign-key enforcement and migration safety."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from wtb.infrastructure.database.async_unit_of_work import (
    AsyncSQLAlchemyUnitOfWork,
)
from wtb.infrastructure.database.engine_cache import get_engine

MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "wtb"
    / "infrastructure"
    / "database"
    / "migrations"
    / "006_checkpoint_file_links_string_ids.sql"
)


@pytest.mark.parametrize("storage", ["file", "memory"])
def test_sync_sqlite_engines_enable_foreign_keys_on_every_new_connection(
    tmp_path: Path,
    storage: str,
) -> None:
    """Cached file engines and uncached memory engines enforce SQLite FKs."""
    db_url = (
        f"sqlite:///{tmp_path / 'foreign-keys.db'}"
        if storage == "file"
        else "sqlite:///:memory:"
    )
    first_engine = get_engine(db_url)
    second_engine = get_engine(db_url)

    try:
        if storage == "file":
            assert second_engine is first_engine
        else:
            assert second_engine is not first_engine

        with first_engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1

        # Disposing forces the cached Engine to open another physical connection.
        first_engine.dispose()
        with second_engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    finally:
        first_engine.dispose()
        if second_engine is not first_engine:
            second_engine.dispose()


@pytest.mark.asyncio
async def test_async_sqlite_engine_enables_foreign_keys_on_new_connections(
    tmp_path: Path,
) -> None:
    """The aiosqlite engine installs the listener on its sync engine."""
    uow = AsyncSQLAlchemyUnitOfWork(f"sqlite:///{tmp_path / 'async-fks.db'}")
    engine = uow.get_engine(uow._db_url)

    try:
        async with engine.connect() as connection:
            result = await connection.exec_driver_sql("PRAGMA foreign_keys")
            assert result.scalar_one() == 1

        await engine.dispose()
        async with engine.connect() as connection:
            result = await connection.exec_driver_sql("PRAGMA foreign_keys")
            assert result.scalar_one() == 1
    finally:
        await engine.dispose()
        AsyncSQLAlchemyUnitOfWork._engine_pool.pop(uow._db_url, None)


def _create_legacy_checkpoint_schema(
    connection: sqlite3.Connection,
    *,
    commit_exists: bool,
) -> None:
    connection.executescript(
        """
        CREATE TABLE file_commits (
            commit_id VARCHAR(64) PRIMARY KEY
        );
        CREATE TABLE checkpoint_file_links (
            checkpoint_id INTEGER PRIMARY KEY,
            commit_id VARCHAR(64) NOT NULL,
            linked_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            file_count INTEGER NOT NULL,
            total_size_bytes BIGINT NOT NULL
        );
        """
    )
    if commit_exists:
        connection.execute(
            "INSERT INTO file_commits (commit_id) VALUES (?)",
            ("commit-1",),
        )
    connection.execute(
        """
        INSERT INTO checkpoint_file_links (
            checkpoint_id, commit_id, file_count, total_size_bytes
        ) VALUES (?, ?, ?, ?)
        """,
        (7, "commit-1", 1, 4),
    )
    connection.commit()


def test_sqlite_006_migrates_valid_links_and_restores_fk_enforcement(
    tmp_path: Path,
) -> None:
    connection = sqlite3.connect(tmp_path / "valid-links.db")
    try:
        _create_legacy_checkpoint_schema(connection, commit_exists=True)

        connection.executescript(MIGRATION_PATH.read_text(encoding="utf-8"))

        checkpoint_id, storage_type = connection.execute(
            "SELECT checkpoint_id, typeof(checkpoint_id) FROM checkpoint_file_links"
        ).fetchone()
        assert checkpoint_id == "7"
        assert storage_type == "text"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute(
            "PRAGMA foreign_key_check(checkpoint_file_links)"
        ).fetchall() == []
    finally:
        connection.close()


def test_sqlite_006_rolls_back_before_replacing_table_when_link_is_orphaned(
    tmp_path: Path,
) -> None:
    connection = sqlite3.connect(tmp_path / "orphaned-link.db")
    try:
        _create_legacy_checkpoint_schema(connection, commit_exists=False)

        with pytest.raises(sqlite3.IntegrityError):
            connection.executescript(MIGRATION_PATH.read_text(encoding="utf-8"))

        assert connection.in_transaction is False
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "checkpoint_file_links" in tables
        assert "checkpoint_file_links_new" not in tables
        assert connection.execute(
            "SELECT checkpoint_id, commit_id FROM checkpoint_file_links"
        ).fetchall() == [(7, "commit-1")]
        checkpoint_type = next(
            row[2]
            for row in connection.execute("PRAGMA table_info(checkpoint_file_links)")
            if row[1] == "checkpoint_id"
        )
        assert checkpoint_type == "INTEGER"
    finally:
        connection.close()
