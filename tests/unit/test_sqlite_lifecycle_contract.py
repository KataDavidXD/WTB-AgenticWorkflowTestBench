"""Regression contracts for factory ownership and SQLite file lifecycle."""

from __future__ import annotations

import warnings
from unittest.mock import MagicMock

from sqlalchemy.pool import NullPool

from wtb.application.factories import ExecutionControllerFactory
from wtb.config import WTBConfig
from wtb.infrastructure.adapters.inmemory_state_adapter import InMemoryStateAdapter
from wtb.infrastructure.database.engine_cache import get_engine, normalize_db_url


def test_plain_postgres_url_selects_declared_psycopg3_driver() -> None:
    assert normalize_db_url("postgresql://user:pass@localhost/wtb") == (
        "postgresql+psycopg://user:pass@localhost/wtb"
    )


def test_legacy_postgres_url_selects_declared_psycopg3_driver() -> None:
    assert normalize_db_url("postgres://user:pass@localhost/wtb") == (
        "postgresql+psycopg://user:pass@localhost/wtb"
    )


def test_explicit_postgres_driver_is_preserved() -> None:
    url = "postgresql+psycopg://user:pass@localhost/wtb"
    assert normalize_db_url(url) == url


def test_file_sqlite_engine_does_not_retain_idle_file_handle(tmp_path) -> None:
    database_path = tmp_path / "lifecycle.db"
    engine = get_engine(f"sqlite:///{database_path}")

    assert isinstance(engine.pool, NullPool)

    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE lifecycle (id INTEGER)")

    renamed = database_path.with_suffix(".closed")
    database_path.rename(renamed)
    renamed.unlink()


def test_owned_controller_composition_does_not_emit_create_leak_warning() -> None:
    uow = MagicMock()
    uow.__enter__.return_value = uow

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ExecutionControllerFactory._create_controller(
            uow=uow,
            state_adapter=InMemoryStateAdapter(),
        )

    uow.__enter__.assert_called_once_with()
    assert not [
        warning
        for warning in caught
        if "leaks UoW" in str(warning.message)
    ]


def test_unmanaged_public_create_still_warns_once() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ExecutionControllerFactory.create(WTBConfig.for_testing())

    leak_warnings = [
        warning
        for warning in caught
        if "leaks UoW" in str(warning.message)
    ]
    assert len(leak_warnings) == 1
    assert leak_warnings[0].category is DeprecationWarning
