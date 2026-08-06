"""Focused SDK resource-ownership and lifecycle regression tests."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from wtb.application.factories import WTBTestBenchFactory
from wtb.sdk.test_bench import WTBTestBench, WTBTestBenchBuilder


class _Controller:
    """Small controller double whose resources do not auto-materialize."""

    def __init__(self, adapter=None, file_tracking=None, uow=None):
        self._state_adapter = adapter
        self._file_tracking = file_tracking
        self._uow = uow


def _bench(
    controller: _Controller,
    runner=None,
    **ownership,
) -> WTBTestBench:
    return WTBTestBench(
        project_service=object(),
        variant_service=object(),
        execution_controller=controller,
        batch_runner=runner,
        **ownership,
    )


def test_close_does_not_release_borrowed_dependencies():
    adapter = MagicMock()
    file_tracking = MagicMock()
    uow = MagicMock()
    runner = MagicMock()
    bench = _bench(_Controller(adapter, file_tracking, uow), runner)

    bench.close()

    runner.shutdown.assert_not_called()
    adapter.close.assert_not_called()
    file_tracking.close.assert_not_called()
    uow.__exit__.assert_not_called()


def test_close_releases_owned_resources_once_and_deduplicates_aliases():
    shared_resource = MagicMock()
    uow = MagicMock()
    runner = MagicMock()
    bench = _bench(
        _Controller(shared_resource, shared_resource, uow),
        runner,
        owns_batch_runner=True,
        owns_execution_resources=True,
    )

    bench.close()
    bench.close()

    runner.shutdown.assert_called_once_with()
    shared_resource.close.assert_called_once_with()
    uow.__exit__.assert_called_once_with(None, None, None)


def test_closed_bench_rejects_lazy_coordinator_creation():
    bench = _bench(_Controller())
    create_coordinator = MagicMock()
    bench._create_batch_coordinator = create_coordinator

    bench.close()

    with pytest.raises(RuntimeError, match="closed"):
        bench.get_batch_coordinator()
    create_coordinator.assert_not_called()


def test_close_and_get_coordinator_are_serialized():
    shutdown_started = threading.Event()
    allow_shutdown = threading.Event()

    class _BlockingRunner:
        def shutdown(self):
            shutdown_started.set()
            assert allow_shutdown.wait(timeout=2.0)

    bench = _bench(
        _Controller(),
        _BlockingRunner(),
        owns_batch_runner=True,
    )
    create_coordinator = MagicMock(return_value=object())
    bench._create_batch_coordinator = create_coordinator
    getter_errors = []

    close_thread = threading.Thread(target=bench.close)
    close_thread.start()
    assert shutdown_started.wait(timeout=2.0)

    def get_coordinator():
        try:
            bench.get_batch_coordinator()
        except Exception as error:  # noqa: BLE001 - asserted below
            getter_errors.append(error)

    getter_thread = threading.Thread(target=get_coordinator)
    getter_thread.start()
    allow_shutdown.set()
    close_thread.join(timeout=2.0)
    getter_thread.join(timeout=2.0)

    assert not close_thread.is_alive()
    assert not getter_thread.is_alive()
    assert len(getter_errors) == 1
    assert isinstance(getter_errors[0], RuntimeError)
    assert "closed" in str(getter_errors[0])
    create_coordinator.assert_not_called()


def test_builder_partial_runner_override_releases_old_and_borrows_custom():
    old_runner = MagicMock()
    custom_runner = MagicMock()
    factory_bench = _bench(
        _Controller(),
        old_runner,
        owns_batch_runner=True,
    )

    with patch.object(
        WTBTestBenchFactory,
        "create_for_testing",
        return_value=factory_bench,
    ):
        bench = (
            WTBTestBenchBuilder()
            .for_testing()
            .with_batch_runner(custom_runner)
            .build()
        )

    old_runner.shutdown.assert_called_once_with()
    bench.close()
    custom_runner.shutdown.assert_not_called()


def test_builder_full_custom_dependencies_remain_borrowed():
    adapter = MagicMock()
    file_tracking = MagicMock()
    uow = MagicMock()
    runner = MagicMock()
    controller = _Controller(adapter, file_tracking, uow)

    bench = (
        WTBTestBenchBuilder()
        .with_project_service(object())
        .with_variant_service(object())
        .with_execution_controller(controller)
        .with_batch_runner(runner)
        .build()
    )
    bench.close()

    runner.shutdown.assert_not_called()
    adapter.close.assert_not_called()
    file_tracking.close.assert_not_called()
    uow.__exit__.assert_not_called()


def test_builder_partial_controller_override_releases_old_and_borrows_custom():
    old_adapter = MagicMock()
    old_file_tracking = MagicMock()
    old_uow = MagicMock()
    custom_adapter = MagicMock()
    custom_file_tracking = MagicMock()
    custom_uow = MagicMock()
    custom_controller = _Controller(
        custom_adapter,
        custom_file_tracking,
        custom_uow,
    )
    factory_bench = _bench(
        _Controller(old_adapter, old_file_tracking, old_uow),
        owns_execution_resources=True,
    )

    with patch.object(
        WTBTestBenchFactory,
        "create_for_testing",
        return_value=factory_bench,
    ):
        bench = (
            WTBTestBenchBuilder()
            .for_testing()
            .with_execution_controller(custom_controller)
            .build()
        )

    assert bench._exec_ctrl is custom_controller
    old_adapter.close.assert_called_once_with()
    old_file_tracking.close.assert_called_once_with()
    old_uow.__exit__.assert_called_once_with(None, None, None)
    bench.close()
    custom_adapter.close.assert_not_called()
    custom_file_tracking.close.assert_not_called()
    custom_uow.__exit__.assert_not_called()


def test_application_factory_marks_created_dependencies_owned():
    bench = WTBTestBenchFactory.create_for_testing()
    try:
        assert bench._owns_batch_runner is True
        assert bench._owns_execution_resources is True
    finally:
        bench.close()
