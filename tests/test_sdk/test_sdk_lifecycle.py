"""Focused SDK resource-ownership and lifecycle regression tests."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
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


@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("run", ("project", {})),
        ("pause", ("exec-1",)),
        ("resume", ("exec-1",)),
        ("stop", ("exec-1",)),
        ("reconcile_runtime_outcome", ("exec-1",)),
        ("rollback", ("exec-1", "cp-1")),
        ("rollback_to_node", ("exec-1", "node-1")),
        ("fork", ("exec-1", "cp-1")),
        ("get_checkpoints", ("exec-1",)),
        ("supports_time_travel", ()),
        ("supports_streaming", ()),
        ("supports_forking", ()),
        ("register_project", (object(),)),
        ("get_project", ("project",)),
        ("list_projects", ()),
        ("unregister_project", ("project",)),
        ("register_variant", ("project", "node", "variant", lambda: None)),
        ("get_state", ("exec-1",)),
        ("get_execution", ("exec-1",)),
        ("get_batch_coordinator", ()),
        ("rollback_batch_result", (object(),)),
        ("fork_batch_result", (object(),)),
        ("get_batch_result_checkpoints", (object(),)),
        ("run_batch_test", ("project", [], [])),
    ],
)
def test_closed_bench_rejects_serialized_public_methods(method_name, args):
    controller = MagicMock()
    bench = _bench(controller)
    bench.close()

    with pytest.raises(RuntimeError, match="closed"):
        getattr(bench, method_name)(*args)

    assert not controller.method_calls


def test_cleanup_failed_bench_rejects_serialized_reuse_until_close_retry():
    runner = MagicMock()
    runner.shutdown.side_effect = [RuntimeError("cleanup failed"), None]
    controller = MagicMock()
    bench = _bench(controller, runner, owns_batch_runner=True)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        bench.close()

    with pytest.raises(RuntimeError, match="retry close"):
        bench.stop("exec-1")
    controller.stop.assert_not_called()

    bench.close()
    assert bench._closed is True


def test_explicit_runtime_stop_is_not_blocked_by_long_running_adapter_lock():
    run_started = threading.Event()
    release_run = threading.Event()
    stop_called = threading.Event()

    class _ExplicitController(_Controller):
        _workflow_runtime_explicit = True

        def create_execution(self, **kwargs):
            return SimpleNamespace(id="exec-1")

        def run(self, execution_id, graph=None):
            run_started.set()
            assert release_run.wait(timeout=2.0)
            return SimpleNamespace(id=execution_id)

        def stop(self, execution_id):
            stop_called.set()
            release_run.set()
            return SimpleNamespace(id=execution_id)

    project_service = MagicMock()
    project_service.get_workflow_by_name.return_value = object()
    bench = WTBTestBench(
        project_service=project_service,
        variant_service=object(),
        execution_controller=_ExplicitController(),
    )
    project = MagicMock()
    project.build_graph.return_value = object()
    bench._project_cache["project"] = project
    bench._workflow_cache["project"] = object()
    run_thread = threading.Thread(target=bench.run, args=("project", {}))
    stop_thread = threading.Thread(target=bench.stop, args=("exec-1",))

    run_thread.start()
    assert run_started.wait(timeout=2.0)
    stop_thread.start()
    assert stop_called.wait(timeout=0.5)
    stop_thread.join(timeout=2.0)
    run_thread.join(timeout=2.0)

    assert not stop_thread.is_alive()
    assert not run_thread.is_alive()


def test_close_waits_for_explicit_runtime_stop_before_disposing_uow():
    stop_started = threading.Event()
    release_stop = threading.Event()
    stop_finished = threading.Event()
    uow_exit_called = threading.Event()
    call_order = []
    stop_errors = []
    close_errors = []

    uow = MagicMock()

    def exit_uow(*_args):
        call_order.append("uow-exit")
        uow_exit_called.set()

    def dispose_uow():
        call_order.append("uow-dispose")

    uow.__exit__.side_effect = exit_uow
    uow.dispose.side_effect = dispose_uow

    class _BlockingStopController(_Controller):
        _workflow_runtime_explicit = True

        def __init__(self):
            super().__init__(uow=uow)
            self.stop_calls = []

        def stop(self, execution_id):
            self.stop_calls.append(execution_id)
            stop_started.set()
            assert release_stop.wait(timeout=2.0)
            call_order.append("stop-finished")
            stop_finished.set()
            return SimpleNamespace(id=execution_id)

    controller = _BlockingStopController()
    bench = _bench(controller, owns_execution_resources=True)

    def stop():
        try:
            bench.stop("exec-1")
        except BaseException as error:  # noqa: BLE001 - asserted below
            stop_errors.append(error)

    def close():
        try:
            bench.close()
        except BaseException as error:  # noqa: BLE001 - asserted below
            close_errors.append(error)

    stop_thread = threading.Thread(target=stop)
    close_thread = threading.Thread(target=close)
    stop_thread.start()
    assert stop_started.wait(timeout=2.0)
    close_thread.start()

    deadline = time.monotonic() + 1.0
    while not bench._closing and time.monotonic() < deadline:
        time.sleep(0.001)
    assert bench._closing is True
    assert not uow_exit_called.wait(timeout=0.05)

    with pytest.raises(RuntimeError, match="closing"):
        bench.stop("exec-2")
    assert controller.stop_calls == ["exec-1"]

    release_stop.set()
    stop_thread.join(timeout=2.0)
    close_thread.join(timeout=2.0)

    assert not stop_thread.is_alive()
    assert not close_thread.is_alive()
    assert not stop_errors
    assert not close_errors
    assert stop_finished.is_set()
    assert call_order == ["stop-finished", "uow-exit", "uow-dispose"]
    assert bench._closed is True


def test_close_of_uninterruptible_explicit_runtime_stop_fails_within_bound():
    stop_started = threading.Event()
    release_stop = threading.Event()
    uow = MagicMock()

    class _UninterruptibleStopController(_Controller):
        _workflow_runtime_explicit = True

        def stop(self, execution_id):
            stop_started.set()
            assert release_stop.wait(timeout=2.0)
            return SimpleNamespace(id=execution_id)

    bench = _bench(
        _UninterruptibleStopController(uow=uow),
        owns_execution_resources=True,
    )
    bench._runtime_drain_timeout_seconds = 0.05
    stop_thread = threading.Thread(target=bench.stop, args=("exec-1",))
    stop_thread.start()
    assert stop_started.wait(timeout=2.0)

    started_at = time.monotonic()
    with pytest.raises(TimeoutError, match="Timed out"):
        bench.close()
    elapsed = time.monotonic() - started_at

    assert elapsed < 1.0
    assert bench._cleanup_failed is True
    assert bench._closing is False
    uow.__exit__.assert_not_called()
    uow.dispose.assert_not_called()

    release_stop.set()
    stop_thread.join(timeout=2.0)
    assert not stop_thread.is_alive()
    bench.close()
    uow.__exit__.assert_called_once_with(None, None, None)
    uow.dispose.assert_called_once_with()


def test_close_interrupts_explicit_runtime_run_before_releasing_uow():
    run_started = threading.Event()
    release_run = threading.Event()
    run_finished = threading.Event()
    runtime = MagicMock()
    runtime.close.side_effect = release_run.set
    uow = MagicMock()
    uow.__exit__.side_effect = lambda *_: run_finished.is_set() or (
        (_ for _ in ()).throw(AssertionError("UoW closed before run drained"))
    )

    class _BlockingExplicitController(_Controller):
        _workflow_runtime_explicit = True

        def __init__(self):
            super().__init__(adapter=MagicMock(), uow=uow)
            self._workflow_runtime = runtime

        def create_execution(self, **kwargs):
            return SimpleNamespace(id="exec-1")

        def run(self, execution_id, graph=None):
            run_started.set()
            assert release_run.wait(timeout=2.0)
            run_finished.set()
            return SimpleNamespace(id=execution_id)

    project_service = MagicMock()
    project_service.get_workflow_by_name.return_value = object()
    bench = WTBTestBench(
        project_service=project_service,
        variant_service=object(),
        execution_controller=_BlockingExplicitController(),
        owns_execution_resources=True,
    )
    project = MagicMock()
    project.build_graph.return_value = object()
    bench._project_cache["project"] = project
    bench._workflow_cache["project"] = object()
    run_errors = []

    def invoke():
        try:
            bench.run("project", {})
        except BaseException as error:  # noqa: BLE001 - asserted below
            run_errors.append(error)

    run_thread = threading.Thread(target=invoke)
    close_thread = threading.Thread(target=bench.close)
    run_thread.start()
    assert run_started.wait(timeout=2.0)
    close_thread.start()
    close_thread.join(timeout=2.0)
    run_thread.join(timeout=2.0)

    assert not close_thread.is_alive()
    assert not run_thread.is_alive()
    assert not run_errors
    runtime.close.assert_called_once_with()
    uow.__exit__.assert_called_once_with(None, None, None)


def test_close_of_uninterruptible_explicit_runtime_fails_within_bound():
    run_started = threading.Event()
    release_run = threading.Event()
    runtime = MagicMock()
    uow = MagicMock()

    class _UninterruptibleController(_Controller):
        _workflow_runtime_explicit = True

        def __init__(self):
            super().__init__(adapter=MagicMock(), uow=uow)
            self._workflow_runtime = runtime

        def create_execution(self, **kwargs):
            return SimpleNamespace(id="exec-1")

        def run(self, execution_id, graph=None):
            run_started.set()
            assert release_run.wait(timeout=2.0)
            return SimpleNamespace(id=execution_id)

    project_service = MagicMock()
    project_service.get_workflow_by_name.return_value = object()
    bench = WTBTestBench(
        project_service=project_service,
        variant_service=object(),
        execution_controller=_UninterruptibleController(),
        owns_execution_resources=True,
    )
    bench._runtime_drain_timeout_seconds = 0.05
    project = MagicMock()
    project.build_graph.return_value = object()
    bench._project_cache["project"] = project
    bench._workflow_cache["project"] = object()
    run_thread = threading.Thread(target=bench.run, args=("project", {}))
    run_thread.start()
    assert run_started.wait(timeout=2.0)

    started_at = time.monotonic()
    with pytest.raises(TimeoutError, match="Timed out"):
        bench.close()
    elapsed = time.monotonic() - started_at

    assert elapsed < 1.0
    uow.__exit__.assert_not_called()
    release_run.set()
    run_thread.join(timeout=2.0)
    assert not run_thread.is_alive()
    bench.close()


def test_sdk_exposes_verified_runtime_reconciliation():
    controller = MagicMock()
    expected = object()
    controller.reconcile_runtime_outcome.return_value = expected
    bench = _bench(controller)

    result = bench.reconcile_runtime_outcome(
        "exec-1",
        attempt_id="attempt-1",
    )

    assert result is expected
    controller.reconcile_runtime_outcome.assert_called_once_with(
        "exec-1",
        attempt_id="attempt-1",
    )


def test_close_interrupts_explicit_runtime_reconcile_before_releasing_uow():
    reconcile_started = threading.Event()
    runtime_closed = threading.Event()
    allow_reconcile_finish = threading.Event()
    reconcile_finished = threading.Event()
    uow_exit_called = threading.Event()
    call_order = []
    reconcile_errors = []
    close_errors = []

    runtime = MagicMock()
    runtime.close.side_effect = runtime_closed.set
    uow = MagicMock()

    def exit_uow(*_args):
        assert reconcile_finished.is_set()
        call_order.append("uow-exit")
        uow_exit_called.set()

    uow.__exit__.side_effect = exit_uow

    class _BlockingReconcileController(_Controller):
        _workflow_runtime_explicit = True

        def __init__(self):
            super().__init__(uow=uow)
            self._workflow_runtime = runtime
            self.reconcile_calls = []

        def reconcile_runtime_outcome(self, execution_id, **kwargs):
            self.reconcile_calls.append((execution_id, kwargs))
            reconcile_started.set()
            assert runtime_closed.wait(timeout=2.0)
            assert allow_reconcile_finish.wait(timeout=2.0)
            call_order.append("reconcile-finished")
            reconcile_finished.set()
            return SimpleNamespace(id=execution_id)

    controller = _BlockingReconcileController()
    bench = _bench(controller, owns_execution_resources=True)

    def reconcile():
        try:
            bench.reconcile_runtime_outcome("exec-1", attempt_id="attempt-1")
        except BaseException as error:  # noqa: BLE001 - asserted below
            reconcile_errors.append(error)

    def close():
        try:
            bench.close()
        except BaseException as error:  # noqa: BLE001 - asserted below
            close_errors.append(error)

    reconcile_thread = threading.Thread(target=reconcile)
    close_thread = threading.Thread(target=close)
    reconcile_thread.start()
    assert reconcile_started.wait(timeout=2.0)
    close_thread.start()

    try:
        assert runtime_closed.wait(timeout=0.5)
        deadline = time.monotonic() + 1.0
        while not bench._closing and time.monotonic() < deadline:
            time.sleep(0.001)
        assert bench._closing is True
        assert not uow_exit_called.wait(timeout=0.05)

        with pytest.raises(RuntimeError, match="closing"):
            bench.reconcile_runtime_outcome("exec-2", attempt_id="attempt-2")
        assert controller.reconcile_calls == [
            ("exec-1", {"attempt_id": "attempt-1"})
        ]
    finally:
        runtime_closed.set()
        allow_reconcile_finish.set()
        reconcile_thread.join(timeout=2.0)
        close_thread.join(timeout=2.0)

    assert not reconcile_thread.is_alive()
    assert not close_thread.is_alive()
    assert not reconcile_errors
    assert not close_errors
    assert call_order == ["reconcile-finished", "uow-exit"]
    runtime.close.assert_called_once_with()
    uow.__exit__.assert_called_once_with(None, None, None)


def test_explicit_runtime_reconcile_preserves_body_error_and_drains_admission():
    runtime = MagicMock()
    uow = MagicMock()

    class _FailingReconcileController(_Controller):
        _workflow_runtime_explicit = True

        def __init__(self):
            super().__init__(uow=uow)
            self._workflow_runtime = runtime

        def reconcile_runtime_outcome(self, execution_id, **kwargs):
            del execution_id, kwargs
            raise LookupError("reconcile body failed")

    bench = _bench(
        _FailingReconcileController(),
        owns_execution_resources=True,
    )

    with pytest.raises(LookupError, match="reconcile body failed"):
        bench.reconcile_runtime_outcome("exec-1", attempt_id="attempt-1")

    assert bench._active_runtime_runs == 0
    bench.close()
    runtime.close.assert_called_once_with()
    uow.__exit__.assert_called_once_with(None, None, None)


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
