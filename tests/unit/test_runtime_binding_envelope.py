"""Durable runtime binding and operator transaction contracts."""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock

import pytest

from wtb.application.services.execution_controller import ExecutionController
from wtb.domain.interfaces.workflow_runtime import (
    RuntimeAttemptPlan,
    RuntimeBindingEnvelope,
    RuntimeOutcomeReceipt,
    RuntimeOutcomeUnknownError,
    RuntimeTerminalOutcome,
)
from wtb.domain.models.workflow import (
    Execution,
    ExecutionState,
    ExecutionStatus,
)
from wtb.domain.models.outbox import OutboxEventType
from wtb.infrastructure.database.unit_of_work import SQLAlchemyUnitOfWork
from wtb.sdk.test_bench import WTBTestBench
from wtb.sdk.workflow_project import WorkflowProject


_INPUT_SHA256 = "a" * 64
_CONFIG_SHA256 = "b" * 64


def _binding(
    *,
    exact_session_id: str = "session-exact",
    persistence_instance_id: str = "store-01HX",
    runtime_config_sha256: str = _CONFIG_SHA256,
    input_sha256: str = _INPUT_SHA256,
    attempt_id: str = "attempt-1",
) -> RuntimeBindingEnvelope:
    return RuntimeBindingEnvelope(
        exact_session_id=exact_session_id,
        persistence_instance_id=persistence_instance_id,
        runtime_config_sha256=runtime_config_sha256,
        protocol_version=1,
        schema_version=1,
        input_sha256=input_sha256,
        attempt_id=attempt_id,
    )


def _execution() -> Execution:
    return Execution(
        id="exec-1",
        workflow_id="wf-1",
        status=ExecutionStatus.PENDING,
        session_id="wtb-exec-1",
        state=ExecutionState(
            current_node_id="start",
            workflow_variables={"prompt": "hello"},
            execution_path=[],
            node_results={},
        ),
    )


def _controller(execution: Execution, runtime: object) -> ExecutionController:
    state_adapter = MagicMock()
    state_adapter.supports_graph_execution.return_value = False
    state_adapter.set_current_session.return_value = True
    execution_repository = MagicMock()
    execution_repository.get.return_value = execution
    execution_repository.compare_and_swap.side_effect = (
        lambda candidate, **_kwargs: _replace_execution(execution, candidate)
    )
    unit_of_work = MagicMock()
    unit_of_work.outbox = MagicMock()
    return ExecutionController(
        execution_repository=execution_repository,
        workflow_repository=MagicMock(),
        state_adapter=state_adapter,
        workflow_runtime=runtime,
        unit_of_work=unit_of_work,
    )


def _replace_execution(target: Execution, source: Execution) -> bool:
    target.__dict__.update(copy.deepcopy(source.__dict__))
    return True


def test_runtime_binding_envelope_is_strict_immutable_and_round_trips() -> None:
    binding = _binding()

    assert RuntimeBindingEnvelope.from_mapping(binding.to_dict()) == binding
    assert binding.to_dict() == {
        "exact_session_id": "session-exact",
        "persistence_instance_id": "store-01HX",
        "runtime_config_sha256": _CONFIG_SHA256,
        "protocol_version": 1,
        "schema_version": 1,
        "input_sha256": _INPUT_SHA256,
        "attempt_id": "attempt-1",
    }
    with pytest.raises(FrozenInstanceError):
        binding.attempt_id = "mutated"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("exact_session_id", ""),
        ("exact_session_id", "C:\\sessions\\tenant"),
        ("exact_session_id", "s" * 257),
        ("persistence_instance_id", ""),
        ("persistence_instance_id", "/srv/dsh/sessions"),
        ("persistence_instance_id", "p" * 129),
        ("runtime_config_sha256", "B" * 64),
        ("runtime_config_sha256", "short"),
        ("protocol_version", 0),
        ("protocol_version", True),
        ("schema_version", 0),
        ("input_sha256", "A" * 64),
        ("attempt_id", ""),
        ("attempt_id", "a" * 129),
    ],
)
def test_runtime_binding_envelope_rejects_ambiguous_identity(field, value) -> None:
    kwargs = _binding().to_dict()
    kwargs[field] = value

    with pytest.raises((TypeError, ValueError)):
        RuntimeBindingEnvelope(**kwargs)


def test_runtime_attempt_plan_requires_exact_binding_consistency() -> None:
    binding = _binding()

    plan = RuntimeAttemptPlan(
        execution_id="exec-1",
        attempt_id="attempt-1",
        session_id="session-exact",
        binding=binding,
    )

    assert plan.binding is binding
    with pytest.raises(ValueError, match="session"):
        RuntimeAttemptPlan(
            execution_id="exec-1",
            attempt_id="attempt-1",
            session_id="session-other",
            binding=binding,
        )
    with pytest.raises(ValueError, match="attempt"):
        RuntimeAttemptPlan(
            execution_id="exec-1",
            attempt_id="attempt-other",
            session_id="session-exact",
            binding=binding,
        )


def test_controller_persists_binding_and_passes_same_envelope_to_reconcile() -> None:
    binding = _binding()
    planned_metadata = {
        "operator_config_sha256": "c" * 64,
        "root_config_sha256": "d" * 64,
        "process_generation": "generation-fenced",
    }

    class Runtime:
        def is_bound(self):
            return True

        def prepare_attempt(self, execution_id, attempt_id, **kwargs):
            assert execution_id == "exec-1"
            input_sha256 = kwargs["input_sha256"]
            return RuntimeAttemptPlan(
                execution_id=execution_id,
                attempt_id=attempt_id,
                session_id=binding.exact_session_id,
                binding=RuntimeBindingEnvelope(
                    **{
                        **binding.to_dict(),
                        "attempt_id": attempt_id,
                        "input_sha256": input_sha256,
                    }
                ),
                metadata=planned_metadata,
            )

        def invoke(self, _state, **kwargs):
            assert kwargs["attempt_metadata"] == planned_metadata
            assert kwargs["attempt_metadata"] is not planned_metadata
            assert kwargs["attempt_metadata"] == (
                execution.metadata["runtime_attempt"]["runtime_metadata"]
            )
            raise RuntimeOutcomeUnknownError(
                "lost after dispatch",
                session_id=kwargs["binding"].exact_session_id,
            )

        def reconcile(self, execution_id, attempt_id, *, binding, input_state):
            assert binding == RuntimeBindingEnvelope.from_mapping(
                execution.metadata["runtime_attempt"]["binding"]
            )
            return RuntimeOutcomeReceipt(
                execution_id=execution_id,
                attempt_id=attempt_id,
                input_sha256=binding.input_sha256,
                outcome=RuntimeTerminalOutcome.COMPLETED,
                binding=binding,
                values={"answer": "reconciled"},
                session_id=binding.exact_session_id,
            )

    runtime = Runtime()
    execution = _execution()
    controller = _controller(execution, runtime)

    fenced = controller.run(execution.id)
    active = fenced.metadata["runtime_attempt"]

    assert active["binding"]["persistence_instance_id"] == "store-01HX"
    assert active["binding"]["exact_session_id"] == active["session_id"]
    assert active["runtime_metadata"] == planned_metadata
    prepared_event = controller._uow.outbox.add.call_args.args[0]
    assert prepared_event.event_type is OutboxEventType.EXECUTION_DISPATCH_PREPARED
    assert prepared_event.payload["runtime_metadata"] == planned_metadata
    assert prepared_event.payload["runtime_metadata"] is not planned_metadata
    reconciled = controller.reconcile_runtime_outcome(execution.id)
    assert reconciled.status is ExecutionStatus.COMPLETED
    assert reconciled.state.workflow_variables["answer"] == "reconciled"
    assert reconciled.metadata["last_runtime_reconciliation"][
        "runtime_binding"
    ] == RuntimeBindingEnvelope.from_mapping(active["binding"]).to_dict()


def test_controller_passes_same_envelope_to_cancel_before_external_call() -> None:
    binding = _binding()
    planned_metadata = {
        "operator_config_sha256": "c" * 64,
        "root_config_sha256": "d" * 64,
        "process_generation": "generation-fenced",
    }
    execution = _execution()
    execution.status = ExecutionStatus.RUNNING
    execution.metadata = {
        "runtime_attempt": {
            "attempt_id": binding.attempt_id,
            "attempt_identity_forwarded": True,
            "input_sha256": binding.input_sha256,
            "session_id": binding.exact_session_id,
            "binding": binding.to_dict(),
            "runtime_metadata": planned_metadata,
        },
        "runtime_session_id": binding.exact_session_id,
        "recovery_required": True,
        "recovery_reason": "runtime_outcome_unknown",
    }

    class Runtime:
        def is_bound(self):
            return True

        def cancel(
            self,
            execution_id,
            attempt_id,
            *,
            binding: RuntimeBindingEnvelope,
            input_sha256,
            attempt_metadata,
        ):
            assert binding == _binding()
            assert input_sha256 == binding.input_sha256
            assert attempt_metadata == planned_metadata
            assert attempt_metadata is not planned_metadata
            return RuntimeOutcomeReceipt(
                execution_id=execution_id,
                attempt_id=attempt_id,
                input_sha256=binding.input_sha256,
                outcome=RuntimeTerminalOutcome.CANCELLED,
                binding=binding,
                session_id=binding.exact_session_id,
            )

    controller = _controller(execution, Runtime())
    result = controller.stop(execution.id)

    assert result.status is ExecutionStatus.CANCELLED


def test_controller_queued_cold_stop_keeps_recovery_when_generation_is_foreign() -> None:
    binding = _binding()
    persisted_metadata = {
        "operator_config_sha256": "c" * 64,
        "root_config_sha256": "d" * 64,
        "process_generation": "generation-before-restart",
    }
    execution = _execution()
    execution.status = ExecutionStatus.RUNNING
    execution.metadata = {
        "runtime_attempt": {
            "attempt_id": binding.attempt_id,
            "attempt_identity_forwarded": True,
            "input_sha256": binding.input_sha256,
            "session_id": binding.exact_session_id,
            "binding": binding.to_dict(),
            "runtime_metadata": persisted_metadata,
            "durable_status": "queued",
        },
        "runtime_session_id": binding.exact_session_id,
        "recovery_required": True,
        "recovery_reason": "runtime_outcome_unknown",
    }
    cancel_io: list[tuple[str, str]] = []

    class RestartedRuntime:
        def is_bound(self):
            return True

        def cancel(
            self,
            execution_id,
            attempt_id,
            *,
            binding,
            input_sha256,
            attempt_metadata,
        ):
            assert input_sha256 == binding.input_sha256
            if attempt_metadata["process_generation"] != "generation-after-restart":
                raise RuntimeOutcomeUnknownError(
                    "DeepSeek Harness process generation changed before cancel",
                    session_id=binding.exact_session_id,
                )
            cancel_io.append((execution_id, attempt_id))
            raise AssertionError("foreign generation must fail before cancel I/O")

    controller = _controller(execution, RestartedRuntime())

    with pytest.raises(RuntimeOutcomeUnknownError, match="process generation"):
        controller.stop(execution.id)

    assert execution.status is ExecutionStatus.RUNNING
    assert execution.metadata["recovery_required"] is True
    assert execution.metadata["recovery_reason"] == "runtime_outcome_unknown"
    assert cancel_io == []
    assert controller._uow.outbox.add.call_count == 0


class _BlockingOperatorRuntime:
    """Shared transport with deterministic concurrent terminal contenders."""

    def __init__(self) -> None:
        self.bound_workflow = None
        self.invoke_started = threading.Event()
        self.release_invoke = threading.Event()
        self.execution_id: str | None = None
        self.binding: RuntimeBindingEnvelope | None = None

    def supports_checkpoint_recovery(self):
        return False

    def supports_forking(self):
        return False

    def shares_checkpoint_store(self, _state_adapter):
        return False

    def is_bound(self):
        return self.bound_workflow is not None

    def bind(self, workflow, *, force_rebind=True):
        del force_rebind
        self.bound_workflow = workflow

    def prepare_attempt(
        self,
        execution_id,
        attempt_id,
        *,
        input_sha256,
        checkpoint_id=None,
    ):
        assert checkpoint_id is None
        self.execution_id = execution_id
        self.binding = _binding(
            exact_session_id=f"session-{execution_id}",
            input_sha256=input_sha256,
            attempt_id=attempt_id,
        )
        return RuntimeAttemptPlan(
            execution_id=execution_id,
            attempt_id=attempt_id,
            session_id=self.binding.exact_session_id,
            binding=self.binding,
        )

    def invoke(
        self,
        input_state,
        *,
        execution_id,
        checkpoint_id=None,
        attempt_id,
        binding,
    ):
        del checkpoint_id
        assert execution_id == self.execution_id
        assert attempt_id == binding.attempt_id
        assert binding == self.binding
        self.invoke_started.set()
        assert self.release_invoke.wait(timeout=3)
        # Give the operator call a deterministic opportunity to win its CAS.
        time.sleep(0.05)
        return {**dict(input_state or {}), "answer": "late invoke"}

    def cancel(self, execution_id, attempt_id, *, binding, input_sha256=None):
        assert binding == self.binding
        assert input_sha256 == binding.input_sha256
        self.release_invoke.set()
        return RuntimeOutcomeReceipt(
            execution_id=execution_id,
            attempt_id=attempt_id,
            input_sha256=binding.input_sha256,
            outcome=RuntimeTerminalOutcome.CANCELLED,
            binding=binding,
            session_id=binding.exact_session_id,
        )

    def reconcile(self, execution_id, attempt_id, *, binding, input_state=None):
        del input_state
        assert binding == self.binding
        self.release_invoke.set()
        return RuntimeOutcomeReceipt(
            execution_id=execution_id,
            attempt_id=attempt_id,
            input_sha256=binding.input_sha256,
            outcome=RuntimeTerminalOutcome.COMPLETED,
            binding=binding,
            values={"prompt": "hello", "answer": "reconciled"},
            session_id=binding.exact_session_id,
        )

    def snapshot(self, _execution_id, checkpoint_id=None):
        del checkpoint_id
        return None

    def history(self, _execution_id):
        return ()

    def close(self):
        self.release_invoke.set()


def _project(name: str = "operator") -> WorkflowProject:
    return WorkflowProject(name=name, graph_factory=lambda: object())


def _run_in_thread(bench, project, results, errors) -> threading.Thread:
    def run() -> None:
        try:
            results.append(
                bench.run(project=project.name, initial_state={"prompt": "hello"})
            )
        except BaseException as error:  # noqa: BLE001 - asserted by tests
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    return worker


def test_sqlite_public_run_and_stop_use_operation_scoped_sessions(
    tmp_path,
    monkeypatch,
) -> None:
    opened_sessions: list[int] = []
    original_enter = SQLAlchemyUnitOfWork.__enter__

    def tracked_enter(uow):
        entered = original_enter(uow)
        opened_sessions.append(id(entered.session))
        return entered

    monkeypatch.setattr(SQLAlchemyUnitOfWork, "__enter__", tracked_enter)
    runtime = _BlockingOperatorRuntime()
    project = _project("operator-stop")
    bench = WTBTestBench.create_with_runtime(runtime, data_dir=str(tmp_path))
    bench.register_project(project)
    results: list[Execution] = []
    errors: list[BaseException] = []

    try:
        worker = _run_in_thread(bench, project, results, errors)
        assert runtime.invoke_started.wait(timeout=2)
        stopped = bench.stop(runtime.execution_id)
        worker.join(timeout=3)

        assert not worker.is_alive()
        assert errors == []
        assert stopped.status is ExecutionStatus.CANCELLED
        assert results[0].status is ExecutionStatus.CANCELLED
        assert bench.get_execution(runtime.execution_id).status is (
            ExecutionStatus.CANCELLED
        )
        assert len(set(opened_sessions)) >= 3
    finally:
        runtime.release_invoke.set()
        bench.close()


def test_sqlite_public_run_and_reconcile_use_operation_scoped_sessions(
    tmp_path,
    monkeypatch,
) -> None:
    opened_sessions: list[int] = []
    original_enter = SQLAlchemyUnitOfWork.__enter__

    def tracked_enter(uow):
        entered = original_enter(uow)
        opened_sessions.append(id(entered.session))
        return entered

    monkeypatch.setattr(SQLAlchemyUnitOfWork, "__enter__", tracked_enter)
    runtime = _BlockingOperatorRuntime()
    project = _project("operator-reconcile")
    bench = WTBTestBench.create_with_runtime(runtime, data_dir=str(tmp_path))
    bench.register_project(project)
    results: list[Execution] = []
    errors: list[BaseException] = []

    try:
        worker = _run_in_thread(bench, project, results, errors)
        assert runtime.invoke_started.wait(timeout=2)
        reconciled = bench.reconcile_runtime_outcome(runtime.execution_id)
        worker.join(timeout=3)

        assert not worker.is_alive()
        assert errors == []
        assert reconciled.status is ExecutionStatus.COMPLETED
        assert reconciled.state.workflow_variables["answer"] == "reconciled"
        assert results[0].state.workflow_variables["answer"] == "reconciled"
        assert len(set(opened_sessions)) >= 3
    finally:
        runtime.release_invoke.set()
        bench.close()


@pytest.mark.parametrize("stop_after_reconcile", [False, True])
def test_sqlite_reconcile_winner_suppresses_stop_loser_outbox(tmp_path, stop_after_reconcile) -> None:
    database = tmp_path / "wtb.db"
    db_url = f"sqlite:///{database.as_posix()}"
    reconcile_claimed = threading.Event()
    reconcile_finished = threading.Event()

    class OrderedRuntimeUoW(SQLAlchemyUnitOfWork):
        def __init__(self):
            super().__init__(db_url)

        def __enter__(self):
            entered = super().__enter__()
            compare_and_swap = self.executions.compare_and_swap

            def ordered_compare_and_swap(*args, **kwargs):
                if threading.current_thread().name == "stop-loser":
                    assert reconcile_claimed.wait(timeout=3)
                claimed = compare_and_swap(*args, **kwargs)
                if (
                    threading.current_thread().name == "reconcile-winner"
                    and claimed
                ):
                    reconcile_claimed.set()
                return claimed

            self.executions.compare_and_swap = ordered_compare_and_swap
            return entered

    runtime = _BlockingOperatorRuntime()
    project = _project("operator-reconcile-stop-race")
    bench = WTBTestBench.create_with_runtime(
        runtime,
        data_dir=str(tmp_path),
        operation_uow_factory=OrderedRuntimeUoW,
    )
    bench.attach_project(project)
    observed: dict[str, Execution] = {}
    errors: list[BaseException] = []

    def invoke():
        try:
            observed["run"] = bench.run(
                project=project.name,
                initial_state={"prompt": "hello"},
            )
        except BaseException as error:
            errors.append(error)

    def reconcile():
        try:
            observed["reconcile"] = bench.reconcile_runtime_outcome(
                runtime.execution_id,
            )
            reconcile_finished.set()
        except BaseException as error:
            errors.append(error)

    def stop():
        try:
            if stop_after_reconcile:
                assert reconcile_finished.wait(timeout=3)
            observed["stop"] = bench.stop(runtime.execution_id)
        except BaseException as error:
            errors.append(error)

    run_worker = threading.Thread(target=invoke, name="run-owner")
    reconcile_worker = threading.Thread(
        target=reconcile,
        name="reconcile-winner",
    )
    stop_worker = threading.Thread(target=stop, name="stop-loser")
    try:
        run_worker.start()
        assert runtime.invoke_started.wait(timeout=3)
        reconcile_worker.start()
        stop_worker.start()
        for worker in (run_worker, reconcile_worker, stop_worker):
            worker.join(timeout=5)

        assert all(
            not worker.is_alive()
            for worker in (run_worker, reconcile_worker, stop_worker)
        )
        assert errors == []
        assert reconcile_claimed.is_set()
        assert all(
            result.status is ExecutionStatus.COMPLETED
            for result in observed.values()
        )
        events = bench.list_outbox_events(execution_id=runtime.execution_id)
        assert not any(
            event.event_type is OutboxEventType.EXECUTION_STOPPED
            for event in events
        )
        assert sum(
            event.event_type is OutboxEventType.EXECUTION_RECONCILED
            for event in events
        ) == 1
        assert sum(
            event.event_type is OutboxEventType.EXECUTION_COMPLETED
            for event in events
        ) == 1
    finally:
        runtime.release_invoke.set()
        for worker in (run_worker, reconcile_worker, stop_worker):
            worker.join(timeout=2)
        bench.close()


def test_public_runtime_workflow_binding_and_project_attach_survive_reopen(
    tmp_path,
) -> None:
    project = _project("operator-restart")
    first_workflow = object()
    first_runtime = _BlockingOperatorRuntime()
    first = WTBTestBench.create_with_runtime(
        first_runtime,
        data_dir=str(tmp_path),
        workflow=first_workflow,
    )
    try:
        first.attach_project(project)
        first.attach_project(project)
        assert first_runtime.bound_workflow is first_workflow
    finally:
        first.close()

    restarted_workflow = object()
    restarted_runtime = _BlockingOperatorRuntime()
    restarted = WTBTestBench.create_with_runtime(
        restarted_runtime,
        data_dir=str(tmp_path),
        workflow=restarted_workflow,
    )
    try:
        restarted.attach_project(project)
        assert restarted_runtime.bound_workflow is restarted_workflow
        assert restarted.get_project(project.name) is project
    finally:
        restarted.close()


def test_public_uow_fault_seam_can_fail_only_terminal_commit_and_query_fence(
    tmp_path,
) -> None:
    database = tmp_path / "wtb.db"
    db_url = f"sqlite:///{database.as_posix()}"
    opened = []

    class FailTerminalCommitUoW(SQLAlchemyUnitOfWork):
        def __init__(self):
            super().__init__(db_url)
            self.commit_calls = 0

        def commit(self):
            self.commit_calls += 1
            # Public bench.run has three durable boundaries in one operation:
            # create, pre-dispatch fence, then terminal + outbox.
            if self.commit_calls == 3:
                self.rollback()
                raise RuntimeError("injected terminal commit failure")
            return super().commit()

    def operation_uow_factory():
        uow = FailTerminalCommitUoW()
        opened.append(uow)
        return uow

    runtime = _BlockingOperatorRuntime()
    runtime.release_invoke.set()
    project = _project("operator-final-commit")
    bench = WTBTestBench.create_with_runtime(
        runtime,
        data_dir=str(tmp_path),
        operation_uow_factory=operation_uow_factory,
    )
    bench.register_project(project)

    try:
        with pytest.raises(
            RuntimeError,
            match="injected terminal commit failure",
        ) as caught:
            bench.run(project=project.name, initial_state={"prompt": "hello"})

        execution_id = caught.value.wtb_execution_id
        fenced = bench.get_execution(execution_id)
        events = bench.list_outbox_events(execution_id=execution_id)

        assert opened[0].commit_calls == 3
        assert fenced.status is ExecutionStatus.RUNNING
        assert fenced.metadata["recovery_reason"] == "runtime_dispatch_inflight"
        assert {event.event_type for event in events} == {
            OutboxEventType.EXECUTION_CREATED,
            OutboxEventType.EXECUTION_DISPATCH_PREPARED,
        }
        assert len(events) == 2

        reconciled = bench.reconcile_runtime_outcome(execution_id)
        assert reconciled.status is ExecutionStatus.COMPLETED
        assert sum(
            event.event_type is OutboxEventType.EXECUTION_RECONCILED
            for event in bench.list_outbox_events(execution_id=execution_id)
        ) == 1
    finally:
        bench.close()


def test_operation_uow_construction_failure_closes_partially_entered_uow(
    tmp_path,
) -> None:
    lifecycle = {"exit_calls": 0}

    class BrokenUoW:
        def __enter__(self):
            raise RuntimeError("repository construction failed")

        def __exit__(self, exc_type, exc_value, traceback):
            del exc_type, exc_value, traceback
            lifecycle["exit_calls"] += 1

    runtime = _BlockingOperatorRuntime()
    bench = WTBTestBench.create_with_runtime(
        runtime,
        data_dir=str(tmp_path),
        operation_uow_factory=BrokenUoW,
    )
    try:
        with pytest.raises(RuntimeError, match="repository construction failed"):
            bench.get_execution("missing")
        assert lifecycle["exit_calls"] == 1
    finally:
        bench.close()


def test_public_run_never_reads_lifetime_project_session_from_worker_thread(
    tmp_path,
) -> None:
    runtime = _BlockingOperatorRuntime()
    runtime.release_invoke.set()
    project = _project("operation-project-cache")
    bench = WTBTestBench.create_with_runtime(
        runtime,
        data_dir=str(tmp_path),
    )
    bench.attach_project(project)
    owner_thread = threading.get_ident()
    original_lookup = bench._project_service.get_workflow_by_name

    def owner_thread_only(name):
        assert threading.get_ident() == owner_thread, (
            "lifetime ProjectService SQLAlchemy session crossed threads"
        )
        return original_lookup(name)

    bench._project_service.get_workflow_by_name = owner_thread_only
    observed = {}

    def run():
        try:
            observed["result"] = bench.run(
                project=project.name,
                initial_state={"prompt": "hello"},
            )
        except BaseException as error:
            observed["error"] = error

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(timeout=10)
    try:
        assert not worker.is_alive()
        assert "error" not in observed
        assert observed["result"].status is ExecutionStatus.COMPLETED
    finally:
        bench.close()
