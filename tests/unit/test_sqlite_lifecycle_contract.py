"""Regression contracts for factory ownership and SQLite file lifecycle."""

from __future__ import annotations

import warnings
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from sqlalchemy.pool import NullPool

from wtb.application.factories import ExecutionControllerFactory
from wtb.application.errors import PostCommitCleanupError
from wtb.config import WTBConfig
from wtb.infrastructure.adapters.inmemory_state_adapter import InMemoryStateAdapter
from wtb.infrastructure.database.engine_cache import get_engine, normalize_db_url


class _ClosableRuntime:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FailingCloseRuntime(_ClosableRuntime):
    def close(self) -> None:
        self.closed = True
        raise RuntimeError("runtime close failed")


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


def test_runtime_builder_creates_distinct_owned_runtime_per_isolated_controller():
    runtimes = []

    def runtime_factory():
        runtime = _ClosableRuntime()
        runtimes.append(runtime)
        return runtime

    factory = ExecutionControllerFactory(
        WTBConfig.for_testing()
    ).with_workflow_runtime(runtime_factory)
    first = factory.create_isolated()
    second = factory.create_isolated()
    try:
        assert first.controller._workflow_runtime is runtimes[0]
        assert second.controller._workflow_runtime is runtimes[1]
        assert runtimes[0] is not runtimes[1]
    finally:
        first.__exit__(None, None, None)
        second.__exit__(None, None, None)

    assert [runtime.closed for runtime in runtimes] == [True, True]
    assert factory._active_workflow_runtimes == {}


def test_runtime_builder_allows_100_sequential_fresh_owned_runtimes():
    created = 0

    def runtime_factory():
        nonlocal created
        created += 1
        return _ClosableRuntime()

    factory = ExecutionControllerFactory(
        WTBConfig.for_testing()
    ).with_workflow_runtime(runtime_factory)

    for _ in range(100):
        managed = factory.create_isolated()
        runtime = managed.controller._workflow_runtime
        managed.__exit__(None, None, None)
        assert runtime.closed is True
        assert factory._active_workflow_runtimes == {}

    assert created == 100


def test_runtime_builder_rejects_factory_that_reuses_mutable_runtime():
    runtime = _ClosableRuntime()
    factory = ExecutionControllerFactory(
        WTBConfig.for_testing()
    ).with_workflow_runtime(lambda: runtime)
    first = factory.create_isolated()
    try:
        with pytest.raises(RuntimeError, match="fresh workflow runtime"):
            factory.create_isolated()
        # The failed duplicate is borrowed by the still-live first controller.
        # Its cleanup must not close that controller's owned runtime.
        assert runtime.closed is False
        assert factory._active_workflow_runtimes == {id(runtime): runtime}
    finally:
        first.__exit__(None, None, None)

    assert runtime.closed is True
    assert factory._active_workflow_runtimes == {}


def test_runtime_builder_serializes_factory_identity_registration():
    runtime = _ClosableRuntime()
    factory = ExecutionControllerFactory(
        WTBConfig.for_testing()
    ).with_workflow_runtime(lambda: runtime)

    def make_uow(*_args, **_kwargs):
        uow = MagicMock()
        uow.__enter__.return_value = uow
        return uow

    with (
        patch(
            "wtb.application.factories.UnitOfWorkFactory.create",
            side_effect=make_uow,
        ),
        patch.object(
            factory,
            "_create_state_adapter_instance",
            side_effect=lambda: MagicMock(),
        ),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        futures = [pool.submit(factory.create_isolated) for _ in range(2)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=5))
            except RuntimeError as error:
                outcomes.append(error)

    managed = [item for item in outcomes if not isinstance(item, Exception)]
    errors = [item for item in outcomes if isinstance(item, Exception)]
    try:
        assert len(managed) == 1
        assert len(errors) == 1
        assert "fresh workflow runtime" in str(errors[0])
    finally:
        for item in managed:
            item.__exit__(None, None, None)

    assert runtime.closed is True
    assert factory._active_workflow_runtimes == {}


def test_owned_runtime_close_failure_is_visible_after_all_cleanup():
    runtime = _FailingCloseRuntime()
    factory = ExecutionControllerFactory(
        WTBConfig.for_testing()
    ).with_workflow_runtime(lambda: runtime)
    managed = factory.create_isolated()

    with pytest.raises(PostCommitCleanupError, match="runtime close failed"):
        managed.__exit__(None, None, None)

    assert runtime.closed is True
    assert factory._active_workflow_runtimes == {}


def test_runtime_close_failure_does_not_mask_active_body_error():
    runtime = _FailingCloseRuntime()
    factory = ExecutionControllerFactory(
        WTBConfig.for_testing()
    ).with_workflow_runtime(lambda: runtime)
    primary = ValueError("body failed")

    with pytest.raises(ValueError, match="body failed") as caught:
        with factory.create_isolated():
            raise primary

    assert caught.value is primary
    assert any("runtime close failed" in note for note in caught.value.__notes__)
    assert factory._active_workflow_runtimes == {}


def test_rollback_failure_does_not_mask_active_body_error():
    factory = ExecutionControllerFactory(WTBConfig.for_testing())
    managed = factory.create_isolated()
    managed.uow.rollback = MagicMock(side_effect=RuntimeError("rollback failed"))
    primary = ValueError("body failed")

    with pytest.raises(ValueError, match="body failed") as caught:
        with managed:
            raise primary

    assert caught.value is primary
    assert any("rollback failed" in note for note in caught.value.__notes__)
