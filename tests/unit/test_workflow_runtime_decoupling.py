"""Contract tests for framework-neutral workflow runtimes."""

import copy
import threading
from types import MappingProxyType
from unittest.mock import MagicMock

import pytest

from wtb.application.services.execution_controller import ExecutionController
from wtb.application.services.outbox_controller_decorator import (
    OutboxExecutionControllerDecorator,
)
from wtb.domain.interfaces.workflow_runtime import (
    RuntimeAttemptPlan,
    RuntimeBindingEnvelope,
    RuntimeOutcomeReceipt,
    RuntimeOutcomeUnknownError,
    RuntimePreDispatchError,
    RuntimeSnapshot,
    RuntimeTerminalError,
    RuntimeTerminalOutcome,
)
from wtb.domain.models.outbox import OutboxEventType
from wtb.domain.runtime_identity import canonical_json_sha256
from wtb.domain.models.workflow import (
    Execution,
    ExecutionState,
    ExecutionStatus,
    TestWorkflow,
    WorkflowNode,
)
from wtb.infrastructure.adapters.langgraph_workflow_runtime import (
    LangGraphWorkflowRuntime,
)
from wtb.infrastructure.database.inmemory_unit_of_work import InMemoryUnitOfWork
from wtb.sdk.test_bench import WTBTestBench


_INPUT_SHA256 = canonical_json_sha256({"prompt": "hello"})
_FOREIGN_INPUT_SHA256 = "b" * 64
_CONFIG_SHA256 = "c" * 64


def _binding(
    *,
    execution_id: str = "exec-1",
    attempt_id: str = "attempt-1",
    input_sha256: str = _INPUT_SHA256,
    session_id: str | None = None,
) -> RuntimeBindingEnvelope:
    return RuntimeBindingEnvelope(
        exact_session_id=session_id or f"runtime-{execution_id}",
        persistence_instance_id="runtime-store-1",
        runtime_config_sha256=_CONFIG_SHA256,
        protocol_version=1,
        schema_version=1,
        input_sha256=input_sha256,
        attempt_id=attempt_id,
    )


def _runtime_attempt(
    *,
    execution_id: str = "exec-1",
    attempt_id: str = "attempt-1",
    input_sha256: str = _INPUT_SHA256,
    session_id: str | None = None,
) -> dict:
    binding = _binding(
        execution_id=execution_id,
        attempt_id=attempt_id,
        input_sha256=input_sha256,
        session_id=session_id,
    )
    return {
        "attempt_id": attempt_id,
        "attempt_identity_forwarded": True,
        "input_sha256": input_sha256,
        "session_id": binding.exact_session_id,
        "binding": binding.to_dict(),
    }


class RecordingRuntime:
    """Small non-LangGraph runtime used to prove the controller seam."""

    def __init__(self) -> None:
        self.bound_workflow = None
        self.invocations: list[dict] = []
        self.patches: list[dict] = []
        self._bound = False

    def is_bound(self) -> bool:
        return self._bound

    def supports_checkpoint_recovery(self) -> bool:
        return True

    def supports_forking(self) -> bool:
        return False

    def shares_checkpoint_store(self, state_adapter) -> bool:
        return True

    def bind(self, workflow, *, force_rebind: bool = True) -> None:
        self.bound_workflow = workflow
        self._bound = True

    def prepare_attempt(
        self,
        execution_id,
        attempt_id,
        *,
        input_sha256,
        checkpoint_id=None,
    ):
        return RuntimeAttemptPlan(
            execution_id=execution_id,
            attempt_id=attempt_id,
            session_id=f"runtime-{execution_id}",
            binding=_binding(
                execution_id=execution_id,
                attempt_id=attempt_id,
                input_sha256=input_sha256,
            ),
            metadata={"input_sha256": input_sha256},
        )

    def invoke(
        self,
        input_state,
        *,
        execution_id: str,
        checkpoint_id: str | None = None,
        attempt_id: str | None = None,
        binding: RuntimeBindingEnvelope | None = None,
    ):
        if attempt_id is not None:
            assert binding == _binding(
                execution_id=execution_id,
                attempt_id=attempt_id,
                input_sha256=binding.input_sha256,
            )
        self.invocations.append(
            {
                "input_state": input_state,
                "execution_id": execution_id,
                "checkpoint_id": checkpoint_id,
            }
        )
        return {"answer": "runtime-result", "input": input_state}

    def snapshot(self, execution_id: str, checkpoint_id: str | None = None):
        return RuntimeSnapshot(
            execution_id=execution_id,
            checkpoint_id=checkpoint_id or "runtime-cp-1",
            values={"answer": "runtime-result"},
            metadata={"runtime": "recording"},
        )

    def history(self, execution_id: str):
        snapshot = self.snapshot(execution_id)
        return [snapshot] if snapshot is not None else []

    def patch_checkpoint(
        self,
        execution_id,
        checkpoint_id,
        values,
        *,
        as_node=None,
    ):
        self.patches.append(
            {
                "execution_id": execution_id,
                "checkpoint_id": checkpoint_id,
                "values": dict(values),
                "as_node": as_node,
            }
        )
        return f"{checkpoint_id}-patched"


class EvidenceOnlyRuntime(RecordingRuntime):
    """Runtime whose receipts are observable but cannot be rewound."""

    def supports_checkpoint_recovery(self) -> bool:
        return False

    def snapshot(self, execution_id: str, checkpoint_id: str | None = None):
        if checkpoint_id is not None:
            return None
        return RuntimeSnapshot(
            execution_id=execution_id,
            checkpoint_id=None,
            values={"answer": "runtime-result"},
            metadata={"runtime": "evidence-only", "recoverable": False},
        )


def _execution(status: ExecutionStatus = ExecutionStatus.PENDING) -> Execution:
    return Execution(
        id="exec-1",
        workflow_id="wf-1",
        status=status,
        session_id="wtb-exec-1",
        state=ExecutionState(
            current_node_id="start",
            workflow_variables={"prompt": "hello"},
            execution_path=[],
            node_results={},
        ),
    )


def _controller(execution: Execution, runtime) -> tuple[ExecutionController, MagicMock]:
    state_adapter = MagicMock()
    state_adapter.supports_graph_execution.return_value = False
    state_adapter.set_current_session.return_value = True
    execution_repository = MagicMock()
    execution_repository.get.return_value = execution
    controller = ExecutionController(
        execution_repository=execution_repository,
        workflow_repository=MagicMock(),
        state_adapter=state_adapter,
        workflow_runtime=runtime,
        unit_of_work=MagicMock(),
    )
    return controller, state_adapter


def test_runtime_operator_contract_is_exported_from_public_interfaces():
    from wtb.domain import interfaces

    assert interfaces.RuntimeAttemptPlan is RuntimeAttemptPlan
    assert interfaces.RuntimeOutcomeReceipt is RuntimeOutcomeReceipt
    assert interfaces.RuntimePreDispatchError is RuntimePreDispatchError
    assert interfaces.RuntimeTerminalError is RuntimeTerminalError
    assert interfaces.RuntimeTerminalOutcome is RuntimeTerminalOutcome


def test_controller_executes_injected_runtime_without_graph_state_adapter():
    runtime = RecordingRuntime()
    execution = _execution()
    controller, state_adapter = _controller(execution, runtime)
    workflow = object()

    result = controller.run(execution.id, graph=workflow)

    assert result.status is ExecutionStatus.COMPLETED
    assert runtime.bound_workflow is workflow
    assert runtime.invocations == [
        {
            "input_state": {"prompt": "hello"},
            "execution_id": execution.id,
            "checkpoint_id": None,
        }
    ]
    assert result.checkpoint_id == "runtime-cp-1"
    assert result.metadata["last_runtime_attempt"]["session_id"] == (
        f"runtime-{execution.id}"
    )
    state_adapter.supports_graph_execution.assert_not_called()


@pytest.mark.parametrize(
    "invalid_state",
    [
        {"prompt": "hello", "bad": object()},
        {"prompt": "hello", "bad": float("nan")},
        {"prompt": "hello", "bad": float("inf")},
        {"prompt": "hello", "bad": float("-inf")},
        {"prompt": "hello", 7: "non-string-key"},
    ],
    ids=["object-repr", "nan", "positive-inf", "negative-inf", "non-string-key"],
)
def test_noncanonical_runtime_input_fails_before_claim_or_dispatch(invalid_state):
    runtime = RecordingRuntime()
    execution = _execution()
    execution.state.workflow_variables = invalid_state
    controller, _ = _controller(execution, runtime)

    with pytest.raises((TypeError, ValueError), match="JSON|finite|string key"):
        controller.run(execution.id, graph=object())

    assert execution.status is ExecutionStatus.PENDING
    assert execution.metadata == {}
    assert runtime.invocations == []
    controller._exec_repo.compare_and_swap.assert_not_called()


def test_controller_materializes_runtime_mapping_before_persistence_and_tracking():
    class ImmutableMappingRuntime(RecordingRuntime):
        def invoke(
            self,
            input_state,
            *,
            execution_id,
            checkpoint_id=None,
            attempt_id=None,
            binding=None,
        ):
            super().invoke(
                input_state,
                execution_id=execution_id,
                checkpoint_id=checkpoint_id,
                attempt_id=attempt_id,
                binding=binding,
            )
            return MappingProxyType({"answer": "immutable", "messages": ("done",)})

    runtime = ImmutableMappingRuntime()
    execution = _execution()
    controller, _ = _controller(execution, runtime)
    controller._file_tracking = MagicMock()
    controller._file_tracking.is_available.return_value = True
    controller._track_checkpoint_history_output_files = MagicMock()

    result = controller.run(execution.id, workflow=object())

    assert result.status is ExecutionStatus.COMPLETED
    assert type(result.state.workflow_variables) is dict
    assert result.state.workflow_variables["answer"] == "immutable"
    assert type(result.state.node_results["final"]) is dict
    tracked_state = controller._track_checkpoint_history_output_files.call_args.args[1]
    assert type(tracked_state) is dict


def test_unknown_runtime_outcome_requires_reconciliation_without_auto_retry():
    class UnknownOutcomeRuntime(RecordingRuntime):
        def invoke(
            self,
            input_state,
            *,
            execution_id,
            checkpoint_id=None,
            attempt_id=None,
            binding=None,
        ):
            self.invocations.append(
                {
                    "input_state": input_state,
                    "execution_id": execution_id,
                    "checkpoint_id": checkpoint_id,
                }
            )
            raise RuntimeOutcomeUnknownError(
                "response lost",
                session_id=binding.exact_session_id,
            )

    runtime = UnknownOutcomeRuntime()
    execution = _execution()
    controller, _ = _controller(execution, runtime)

    result = controller.run(execution.id, workflow=object())

    assert result.status is ExecutionStatus.RUNNING
    assert result.metadata["recovery_required"] is True
    assert result.metadata["recovery_reason"] == "runtime_outcome_unknown"
    assert result.metadata["runtime_session_id"] == f"runtime-{execution.id}"
    assert len(runtime.invocations) == 1

    with pytest.raises(RuntimeError, match="runtime outcome is unknown"):
        controller.run(execution.id)

    assert len(runtime.invocations) == 1


def test_committed_runtime_turn_is_not_redispatched_after_wtb_commit_failure():
    runtime = RecordingRuntime()
    execution = _execution()
    state_adapter = MagicMock()
    state_adapter.supports_graph_execution.return_value = False
    state_adapter.set_current_session.return_value = True
    uow = InMemoryUnitOfWork()
    uow.executions.add(execution)
    uow.commit()
    controller = ExecutionController(
        execution_repository=uow.executions,
        workflow_repository=uow.workflows,
        state_adapter=state_adapter,
        workflow_runtime=runtime,
        unit_of_work=uow,
    )
    decorated = OutboxExecutionControllerDecorator(
        controller,
        uow.outbox,
        commit_fn=MagicMock(side_effect=RuntimeError("final commit failed")),
        rollback_fn=uow.rollback,
    )

    with pytest.raises(RuntimeError, match="final commit failed"):
        decorated.run(execution.id, workflow=object())

    persisted = uow.executions.get(execution.id)
    assert persisted is not None
    assert persisted.status is ExecutionStatus.RUNNING
    assert persisted.metadata["recovery_required"] is True
    assert persisted.metadata["recovery_reason"] == "runtime_dispatch_inflight"
    attempt_id = persisted.metadata["runtime_attempt"]["attempt_id"]
    assert isinstance(attempt_id, str) and attempt_id
    assert persisted.metadata["runtime_session_id"] == f"runtime-{execution.id}"
    assert len(runtime.invocations) == 1
    assert any(
        event.event_type is OutboxEventType.EXECUTION_DISPATCH_PREPARED
        and event.payload["attempt_id"] == attempt_id
        for event in uow.outbox.list_all()
    )

    with pytest.raises(RuntimeError, match="reconcil"):
        controller.run(execution.id)
    assert len(runtime.invocations) == 1


def test_runtime_factory_persists_dispatch_fence_across_sqlite_reopen(tmp_path):
    data_dir = tmp_path / "durable-runtime"
    runtime = RecordingRuntime()
    bench = WTBTestBench.create_with_runtime(runtime, data_dir=str(data_dir))
    workflow = TestWorkflow(id="wf-durable", name="durable", entry_point="start")
    workflow.add_node(WorkflowNode(id="start", name="Start", type="start"))
    bench._project_service.register_workflow(workflow)
    created = bench._exec_ctrl.create_execution(
        workflow,
        initial_state={"prompt": "persist me"},
    )
    bench._exec_ctrl._commit_fn = MagicMock(
        side_effect=RuntimeError("final commit failed")
    )
    try:
        with pytest.raises(RuntimeError, match="final commit failed"):
            bench._exec_ctrl.run(created.id, workflow=object())
    finally:
        bench.close()

    reopened = WTBTestBench.create_with_runtime(
        RecordingRuntime(),
        data_dir=str(data_dir),
    )
    try:
        persisted = reopened.get_execution(created.id)
        assert persisted.status is ExecutionStatus.RUNNING
        assert persisted.metadata["recovery_reason"] == ("runtime_dispatch_inflight")
        assert persisted.metadata["runtime_session_id"] == (f"runtime-{created.id}")
    finally:
        reopened.close()


def test_two_independent_uows_cannot_both_claim_one_pending_runtime_attempt():
    shared_execution = _execution()
    shared_lock = threading.Lock()
    initial_reads = threading.Barrier(2)
    initial_read_count = 0
    dispatches = []

    class SharedRepository:
        def get(self, execution_id):
            nonlocal initial_read_count
            with shared_lock:
                loaded = copy.deepcopy(shared_execution)
                initial_read_count += 1
                should_wait = initial_read_count <= 2
            if should_wait:
                initial_reads.wait(timeout=2.0)
            return loaded

        def update(self, execution):
            nonlocal shared_execution
            with shared_lock:
                shared_execution = copy.deepcopy(execution)
            return execution

        def compare_and_swap(
            self,
            execution,
            *,
            expected_status,
            expected_metadata,
        ):
            nonlocal shared_execution
            with shared_lock:
                if (
                    shared_execution.status is not expected_status
                    or shared_execution.metadata != expected_metadata
                ):
                    return False
                shared_execution = copy.deepcopy(execution)
                return True

    class IndependentUoW:
        def __init__(self):
            self.outbox = MagicMock()

        def commit(self):
            return None

    class ConcurrentRuntime(RecordingRuntime):
        def invoke(
            self,
            input_state,
            *,
            execution_id,
            checkpoint_id=None,
            attempt_id=None,
            binding=None,
        ):
            assert binding is not None
            with shared_lock:
                dispatches.append(attempt_id)
            return {"answer": "ok"}

    controllers = []
    for _ in range(2):
        runtime = ConcurrentRuntime()
        runtime.bind(object())
        adapter = MagicMock()
        adapter.supports_graph_execution.return_value = False
        adapter.set_current_session.return_value = True
        controllers.append(
            ExecutionController(
                execution_repository=SharedRepository(),
                workflow_repository=MagicMock(),
                state_adapter=adapter,
                workflow_runtime=runtime,
                unit_of_work=IndependentUoW(),
            )
        )

    errors = []

    def run(controller):
        try:
            controller.run(shared_execution.id)
        except Exception as error:  # noqa: BLE001 - asserted below
            errors.append(error)

    workers = [threading.Thread(target=run, args=(item,)) for item in controllers]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=2.0)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert len(dispatches) == 1


def test_late_invoke_completion_cannot_overwrite_concurrent_cancel_winner():
    shared_execution = _execution()
    shared_lock = threading.Lock()
    invoke_started = threading.Event()
    release_invoke = threading.Event()
    repository_updates = []

    class SharedRepository:
        def get(self, execution_id):
            assert execution_id == shared_execution.id
            with shared_lock:
                return copy.deepcopy(shared_execution)

        def update(self, execution):
            repository_updates.append(copy.deepcopy(execution))
            raise AssertionError("runtime terminal transitions must use CAS")

        def compare_and_swap(
            self,
            execution,
            *,
            expected_status,
            expected_metadata,
        ):
            nonlocal shared_execution
            with shared_lock:
                if (
                    shared_execution.status is not expected_status
                    or shared_execution.metadata != expected_metadata
                ):
                    return False
                shared_execution = copy.deepcopy(execution)
                return True

    class IndependentUoW:
        def __init__(self):
            self.outbox = MagicMock()
            self.rollback_calls = 0

        def commit(self):
            return None

        def rollback(self):
            self.rollback_calls += 1

    class BlockingRuntime(RecordingRuntime):
        def invoke(
            self,
            input_state,
            *,
            execution_id,
            checkpoint_id=None,
            attempt_id=None,
            binding=None,
        ):
            del checkpoint_id, attempt_id
            assert binding is not None
            invoke_started.set()
            assert release_invoke.wait(timeout=3)
            return {"answer": "late success", "input": input_state}

    class CancelRuntime(RecordingRuntime):
        def cancel(
            self,
            execution_id,
            attempt_id,
            *,
            binding,
            input_sha256=None,
        ):
            return RuntimeOutcomeReceipt(
                execution_id=execution_id,
                attempt_id=attempt_id,
                input_sha256=input_sha256,
                outcome=RuntimeTerminalOutcome.CANCELLED,
                binding=binding,
                values={},
                receipt={"status": "terminal", "finish_reason": "canceled"},
                session_id=f"runtime-{execution_id}",
                detail="cancel won",
            )

    def controller(runtime, uow):
        adapter = MagicMock()
        adapter.supports_graph_execution.return_value = False
        adapter.set_current_session.return_value = True
        return ExecutionController(
            execution_repository=SharedRepository(),
            workflow_repository=MagicMock(),
            state_adapter=adapter,
            workflow_runtime=runtime,
            unit_of_work=uow,
        )

    blocking_runtime = BlockingRuntime()
    cancel_runtime = CancelRuntime()
    run_uow = IndependentUoW()
    run_controller = controller(blocking_runtime, run_uow)
    cancel_controller = controller(cancel_runtime, IndependentUoW())
    run_results = []
    run_errors = []

    def run() -> None:
        try:
            run_results.append(
                run_controller.run(shared_execution.id, graph=object())
            )
        except BaseException as error:  # noqa: BLE001 - asserted below
            run_errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    assert invoke_started.wait(timeout=2)

    cancelled = cancel_controller.stop(shared_execution.id)
    assert cancelled.status is ExecutionStatus.CANCELLED
    release_invoke.set()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert run_errors == []
    assert len(run_results) == 1
    assert run_results[0].status is ExecutionStatus.CANCELLED
    assert shared_execution.status is ExecutionStatus.CANCELLED
    assert shared_execution.state.workflow_variables == {"prompt": "hello"}
    assert repository_updates == []
    assert run_uow.rollback_calls == 1


def test_verified_runtime_receipt_reconciles_unknown_outcome_and_state():
    class ReconcilingRuntime(RecordingRuntime):
        def __init__(self):
            super().__init__()
            self.reconciliations = []

        def reconcile(
            self,
            execution_id,
            attempt_id,
            *,
            binding,
            input_state=None,
        ):
            del input_state
            self.reconciliations.append((execution_id, attempt_id))
            return RuntimeOutcomeReceipt(
                execution_id=execution_id,
                attempt_id=attempt_id,
                input_sha256=_INPUT_SHA256,
                outcome=RuntimeTerminalOutcome.COMPLETED,
                binding=binding,
                values={"answer": "reconciled"},
                receipt={"terminal_seq": 7},
                session_id="runtime-session-1",
            )

    runtime = ReconcilingRuntime()
    runtime.bind(object())
    execution = _execution(ExecutionStatus.RUNNING)
    execution.metadata = {
        "recovery_required": True,
        "recovery_reason": "runtime_outcome_unknown",
        "runtime_attempt": _runtime_attempt(session_id="runtime-session-1"),
        "runtime_session_id": "runtime-session-1",
    }
    controller, _ = _controller(execution, runtime)

    result = controller.reconcile_runtime_outcome(
        execution.id,
        attempt_id="attempt-1",
    )

    assert result.status is ExecutionStatus.COMPLETED
    assert result.state.workflow_variables == {"answer": "reconciled"}
    assert result.metadata.get("recovery_required") is None
    assert result.metadata.get("recovery_reason") is None
    assert result.metadata.get("runtime_attempt") is None
    assert result.metadata["last_runtime_reconciliation"] == {
        "attempt_id": "attempt-1",
        "input_sha256": _INPUT_SHA256,
        "outcome": "completed",
        "session_id": "runtime-session-1",
        "receipt": {"terminal_seq": 7},
        "runtime_binding": _binding(
            session_id="runtime-session-1",
        ).to_dict(),
    }
    assert runtime.reconciliations == [(execution.id, "attempt-1")]


def test_reconciliation_rejects_foreign_attempt_receipt_without_mutation():
    class ForeignReceiptRuntime(RecordingRuntime):
        def reconcile(
            self,
            execution_id,
            attempt_id,
            *,
            binding,
            input_state=None,
        ):
            del input_state
            return RuntimeOutcomeReceipt(
                execution_id=execution_id,
                attempt_id="attempt-other",
                input_sha256=_INPUT_SHA256,
                outcome=RuntimeTerminalOutcome.COMPLETED,
                binding=binding,
                values={"answer": "wrong"},
            )

    runtime = ForeignReceiptRuntime()
    runtime.bind(object())
    execution = _execution(ExecutionStatus.RUNNING)
    execution.metadata = {
        "recovery_required": True,
        "recovery_reason": "runtime_outcome_unknown",
        "runtime_attempt": _runtime_attempt(),
    }
    original_state = copy.deepcopy(execution.state)
    controller, _ = _controller(execution, runtime)

    with pytest.raises(RuntimeError, match="does not own"):
        controller.reconcile_runtime_outcome(execution.id)

    assert execution.status is ExecutionStatus.RUNNING
    assert execution.state == original_state
    assert execution.metadata["recovery_reason"] == "runtime_outcome_unknown"


@pytest.mark.parametrize(
    ("receipt_input_sha256", "receipt_session_id", "error_match"),
    [
        (_FOREIGN_INPUT_SHA256, "runtime-session-1", "input"),
        (_INPUT_SHA256, "runtime-session-foreign", "session"),
    ],
)
def test_reconciliation_rejects_foreign_fence_proof_without_mutation_or_outbox(
    receipt_input_sha256,
    receipt_session_id,
    error_match,
):
    class ForeignFenceRuntime(RecordingRuntime):
        def reconcile(
            self,
            execution_id,
            attempt_id,
            *,
            binding,
            input_state=None,
        ):
            del input_state
            return RuntimeOutcomeReceipt(
                execution_id=execution_id,
                attempt_id=attempt_id,
                input_sha256=receipt_input_sha256,
                outcome=RuntimeTerminalOutcome.COMPLETED,
                binding=binding,
                values={"answer": "must-not-apply"},
                receipt={"terminal_seq": 11},
                session_id=receipt_session_id,
            )

    runtime = ForeignFenceRuntime()
    runtime.bind(object())
    execution = _execution(ExecutionStatus.RUNNING)
    execution.metadata = {
        "recovery_required": True,
        "recovery_reason": "runtime_outcome_unknown",
        "runtime_session_id": "runtime-session-1",
        "runtime_attempt": _runtime_attempt(session_id="runtime-session-1"),
    }
    original = copy.deepcopy(execution)
    controller, _ = _controller(execution, runtime)
    outbox = MagicMock()
    commit = MagicMock()
    rollback = MagicMock()
    decorated = OutboxExecutionControllerDecorator(
        controller,
        outbox,
        commit_fn=commit,
        rollback_fn=rollback,
    )

    with pytest.raises(RuntimeError, match=error_match):
        decorated.reconcile_runtime_outcome(
            execution.id,
            attempt_id="attempt-1",
        )

    assert execution == original
    controller._exec_repo.update.assert_not_called()
    outbox.add.assert_not_called()
    commit.assert_not_called()
    rollback.assert_called_once_with()


def test_known_terminal_runtime_failure_is_not_outcome_unknown():
    class KnownFailureRuntime(RecordingRuntime):
        def invoke(
            self,
            input_state,
            *,
            execution_id,
            checkpoint_id=None,
            attempt_id=None,
            binding=None,
        ):
            assert binding is not None
            self.invocations.append({"execution_id": execution_id})
            raise RuntimeTerminalError(
                "token budget exhausted",
                session_id=f"runtime-{execution_id}",
                finish_reason="max-tokens",
                result_state={"partial": "trusted"},
                receipt={
                    "execution_id": execution_id,
                    "attempt_id": attempt_id,
                    "session_id": f"runtime-{execution_id}",
                    "terminal_seq": 9,
                },
            )

    runtime = KnownFailureRuntime()
    execution = _execution()
    controller, _ = _controller(execution, runtime)

    result = controller.run(execution.id, workflow=object())

    assert result.status is ExecutionStatus.FAILED
    assert result.state.workflow_variables == {"partial": "trusted"}
    assert result.metadata.get("recovery_required") is None
    assert result.metadata.get("recovery_reason") is None
    terminal = result.metadata["last_runtime_attempt"]
    assert terminal["outcome"] == "failed"
    assert terminal["finish_reason"] == "max-tokens"
    assert terminal["receipt"]["terminal_seq"] == 9
    assert terminal["receipt"]["attempt_id"] == terminal["attempt_id"]


def test_proven_runtime_startup_failure_is_known_failed_not_unknown():
    class StartupFailureRuntime(RecordingRuntime):
        def __init__(self):
            super().__init__()
            self.external_dispatches = 0

        def invoke(
            self,
            input_state,
            *,
            execution_id,
            checkpoint_id=None,
            attempt_id=None,
            binding=None,
        ):
            assert binding is not None
            raise RuntimePreDispatchError("runtime binary not found")

    runtime = StartupFailureRuntime()
    execution = _execution()
    controller, _ = _controller(execution, runtime)

    result = controller.run(execution.id, workflow=object())

    assert result.status is ExecutionStatus.FAILED
    assert result.error_message == "runtime binary not found"
    assert result.metadata.get("runtime_attempt") is None
    assert result.metadata.get("recovery_required") is None
    assert result.metadata["last_runtime_attempt"]["finish_reason"] == (
        "pre-dispatch-error"
    )
    assert runtime.external_dispatches == 0


def test_explicit_runtime_state_update_uses_owned_runtime_checkpoint():
    runtime = RecordingRuntime()
    runtime.bind(object())
    execution = _execution(ExecutionStatus.PAUSED)
    execution.checkpoint_id = "runtime-cp-1"
    controller, state_adapter = _controller(execution, runtime)

    updated = controller.update_execution_state(
        execution.id,
        {"temperature": 0.2},
    )

    assert updated is True
    assert runtime.patches == [
        {
            "execution_id": execution.id,
            "checkpoint_id": "runtime-cp-1",
            "values": {"temperature": 0.2},
            "as_node": None,
        }
    ]
    assert execution.checkpoint_id == "runtime-cp-1-patched"
    assert execution.metadata["resume_checkpoint_id"] == "runtime-cp-1-patched"
    assert execution.state.workflow_variables["temperature"] == 0.2
    state_adapter.update_state.assert_not_called()


def test_evidence_only_runtime_rejects_state_update_before_adapter_mutation():
    runtime = EvidenceOnlyRuntime()
    runtime.bind(object())
    execution = _execution(ExecutionStatus.PAUSED)
    controller, state_adapter = _controller(execution, runtime)

    with pytest.raises(RuntimeError, match="owned runtime checkpoint"):
        controller.update_execution_state(
            execution.id,
            {"temperature": 0.2},
        )

    assert execution.state.workflow_variables == {"prompt": "hello"}
    assert runtime.patches == []
    state_adapter.update_state.assert_not_called()


def test_compatibility_runtime_state_update_preserves_state_adapter_path():
    execution = _execution(ExecutionStatus.PAUSED)
    state_adapter = MagicMock()
    state_adapter.supports_graph_execution.return_value = True
    state_adapter.set_current_session.return_value = True
    state_adapter.update_state.return_value = True
    repository = MagicMock()
    repository.get.return_value = execution
    controller = ExecutionController(
        execution_repository=repository,
        workflow_repository=MagicMock(),
        state_adapter=state_adapter,
        unit_of_work=MagicMock(),
    )

    assert controller.update_execution_state(execution.id, {"value": 2}) is True
    state_adapter.update_state.assert_called_once_with({"value": 2})
    assert execution.state.workflow_variables["value"] == 2


def test_running_explicit_runtime_stop_requires_verified_cancel_ack():
    class CancellableRuntime(RecordingRuntime):
        def __init__(self):
            super().__init__()
            self.cancelled = []

        def cancel(
            self,
            execution_id,
            attempt_id,
            *,
            binding,
            input_state=None,
            input_sha256=None,
        ):
            assert input_sha256 == binding.input_sha256
            assert input_state == {"prompt": "hello"}
            self.cancelled.append((execution_id, attempt_id))
            return RuntimeOutcomeReceipt(
                execution_id=execution_id,
                attempt_id=attempt_id,
                input_sha256=_INPUT_SHA256,
                outcome=RuntimeTerminalOutcome.CANCELLED,
                binding=binding,
                receipt={"finish_reason": "aborted"},
                session_id=binding.exact_session_id,
            )

    runtime = CancellableRuntime()
    runtime.bind(object())
    execution = _execution(ExecutionStatus.RUNNING)
    execution.metadata = {
        "runtime_attempt": _runtime_attempt(),
        "recovery_required": True,
        "recovery_reason": "runtime_dispatch_inflight",
    }
    controller, _ = _controller(execution, runtime)

    result = controller.stop(execution.id)

    assert result.status is ExecutionStatus.CANCELLED
    assert runtime.cancelled == [(execution.id, "attempt-1")]
    assert result.metadata.get("runtime_attempt") is None
    assert result.metadata["last_runtime_reconciliation"]["outcome"] == "cancelled"
    assert (
        result.metadata["last_runtime_reconciliation"]["input_sha256"] == _INPUT_SHA256
    )


@pytest.mark.parametrize(
    ("receipt_input_sha256", "receipt_session_id", "error_match"),
    [
        (_FOREIGN_INPUT_SHA256, "runtime-session-1", "input"),
        (_INPUT_SHA256, "runtime-session-foreign", "session"),
    ],
)
def test_runtime_stop_rejects_foreign_cancel_proof_without_mutation_or_outbox(
    receipt_input_sha256,
    receipt_session_id,
    error_match,
):
    class ForeignCancelRuntime(RecordingRuntime):
        def cancel(
            self,
            execution_id,
            attempt_id,
            *,
            binding,
            input_sha256=None,
        ):
            del input_sha256
            return RuntimeOutcomeReceipt(
                execution_id=execution_id,
                attempt_id=attempt_id,
                input_sha256=receipt_input_sha256,
                outcome=RuntimeTerminalOutcome.CANCELLED,
                binding=binding,
                receipt={"finish_reason": "aborted"},
                session_id=receipt_session_id,
            )

    runtime = ForeignCancelRuntime()
    runtime.bind(object())
    execution = _execution(ExecutionStatus.RUNNING)
    execution.metadata = {
        "runtime_attempt": _runtime_attempt(session_id="runtime-session-1"),
        "runtime_session_id": "runtime-session-1",
        "recovery_required": True,
        "recovery_reason": "runtime_dispatch_inflight",
    }
    original = copy.deepcopy(execution)
    controller, _ = _controller(execution, runtime)
    outbox = MagicMock()
    commit = MagicMock()
    rollback = MagicMock()
    decorated = OutboxExecutionControllerDecorator(
        controller,
        outbox,
        commit_fn=commit,
        rollback_fn=rollback,
    )

    with pytest.raises(RuntimeError, match=error_match):
        decorated.stop(execution.id)

    assert execution == original
    controller._exec_repo.update.assert_not_called()
    outbox.add.assert_not_called()
    commit.assert_not_called()
    rollback.assert_called_once_with()


def test_running_explicit_runtime_stop_without_cancel_port_stays_running():
    runtime = RecordingRuntime()
    runtime.bind(object())
    runtime.cancel = None
    execution = _execution(ExecutionStatus.RUNNING)
    execution.metadata = {
        "runtime_attempt": _runtime_attempt(),
        "recovery_required": True,
        "recovery_reason": "runtime_dispatch_inflight",
    }
    controller, _ = _controller(execution, runtime)

    with pytest.raises(RuntimeError, match="cancel acknowledgement"):
        controller.stop(execution.id)

    assert execution.status is ExecutionStatus.RUNNING


def test_controller_passes_pinned_checkpoint_to_runtime_on_resume():
    runtime = RecordingRuntime()
    runtime.bind(object())
    execution = _execution(ExecutionStatus.PAUSED)
    execution.metadata = {"resume_checkpoint_id": "cp-source"}
    controller, _ = _controller(execution, runtime)

    result = controller.run(execution.id)

    assert result.status is ExecutionStatus.COMPLETED
    assert runtime.invocations == [
        {
            "input_state": None,
            "execution_id": execution.id,
            "checkpoint_id": "cp-source",
        }
    ]
    assert "resume_checkpoint_id" not in execution.metadata


def test_unbound_recoverable_runtime_binds_supplied_workflow_before_resume():
    runtime = RecordingRuntime()
    execution = _execution(ExecutionStatus.PAUSED)
    execution.metadata = {"resume_checkpoint_id": "cp-supplied"}
    controller, _ = _controller(execution, runtime)
    workflow = object()

    result = controller.run(execution.id, workflow=workflow)

    assert result.status is ExecutionStatus.COMPLETED
    assert runtime.bound_workflow is workflow
    assert runtime.invocations == [
        {
            "input_state": None,
            "execution_id": execution.id,
            "checkpoint_id": "cp-supplied",
        }
    ]


def test_post_dispatch_resume_failure_preserves_dispatched_checkpoint():
    class SnapshotFailureRuntime(RecordingRuntime):
        def snapshot(self, execution_id, checkpoint_id=None):
            if checkpoint_id is not None:
                return super().snapshot(execution_id, checkpoint_id)
            raise RuntimeError("snapshot receipt unavailable")

    runtime = SnapshotFailureRuntime()
    runtime.bind(object())
    execution = _execution(ExecutionStatus.PAUSED)
    execution.metadata = {"resume_checkpoint_id": "cp-patched"}
    controller, _ = _controller(execution, runtime)

    result = controller.run(execution.id)

    assert result.status is ExecutionStatus.RUNNING
    assert result.metadata["recovery_reason"] == "runtime_outcome_unknown"
    assert result.metadata["runtime_resume_checkpoint_id"] == "cp-patched"
    assert runtime.invocations[0]["checkpoint_id"] == "cp-patched"


def test_evidence_only_direct_run_rejects_resume_without_mutating_status():
    runtime = EvidenceOnlyRuntime()
    runtime.bind(object())
    execution = _execution(ExecutionStatus.PAUSED)
    execution.metadata = {"resume_checkpoint_id": "state-only-cp"}
    controller, _ = _controller(execution, runtime)

    try:
        controller.run(execution.id)
    except RuntimeError as error:
        assert "does not support resume" in str(error)
    else:
        raise AssertionError("direct resume must fail closed")

    assert execution.status is ExecutionStatus.PAUSED
    assert execution.error_message is None
    assert runtime.invocations == []


def test_controller_capability_requires_runtime_checkpoint_recovery():
    execution = _execution(ExecutionStatus.PAUSED)
    recoverable_controller, recoverable_adapter = _controller(
        execution,
        RecordingRuntime(),
    )
    evidence_controller, evidence_adapter = _controller(
        execution,
        EvidenceOnlyRuntime(),
    )
    recoverable_adapter.supports_time_travel.return_value = True
    evidence_adapter.supports_time_travel.return_value = True

    assert recoverable_controller.supports_time_travel() is True
    assert evidence_controller.supports_time_travel() is False


def test_generic_recoverable_runtime_does_not_claim_cross_execution_forking():
    execution = _execution(ExecutionStatus.PAUSED)
    controller, state_adapter = _controller(execution, RecordingRuntime())
    state_adapter.supports_time_travel.return_value = True

    assert controller.supports_time_travel() is True
    assert controller.supports_runtime_forking() is False


def test_generic_runtime_fork_rejected_before_adapter_or_uow_mutation():
    runtime = RecordingRuntime()
    runtime.bind(object())
    execution = _execution(ExecutionStatus.PAUSED)
    controller, state_adapter = _controller(execution, runtime)
    state_adapter.supports_time_travel.return_value = True

    try:
        controller.fork(execution.id, "cp-source")
    except RuntimeError as error:
        assert "does not support fork" in str(error)
    else:
        raise AssertionError("generic runtime fork must fail closed")

    state_adapter.set_current_session.assert_not_called()
    state_adapter.load_checkpoint.assert_not_called()
    controller._uow.rollback.assert_not_called()


def test_legacy_runtime_checkpoint_snapshot_remains_operation_compatible():
    """Old third-party runtimes can prove one checkpoint without advertising."""

    class LegacyRuntime:
        def is_bound(self):
            return True

        def snapshot(self, execution_id, checkpoint_id=None):
            return RuntimeSnapshot(
                execution_id=execution_id,
                checkpoint_id=checkpoint_id,
                values={},
            )

    execution = _execution(ExecutionStatus.PAUSED)
    controller, state_adapter = _controller(execution, LegacyRuntime())
    state_adapter.supports_time_travel.return_value = True

    controller._require_runtime_checkpoint(
        execution.id,
        "legacy-cp",
        operation="resume",
    )

    # Generic feature discovery stays fail-closed without an explicit method.
    assert controller.supports_time_travel() is False


def test_public_resume_rejects_evidence_runtime_after_selecting_correct_session():
    runtime = EvidenceOnlyRuntime()
    runtime.bind(object())
    execution = _execution(ExecutionStatus.PAUSED)
    execution.checkpoint_id = "state-only-cp"
    controller, state_adapter = _controller(execution, runtime)

    try:
        controller.resume(execution.id)
    except RuntimeError as error:
        assert "does not support resume" in str(error)
    else:
        raise AssertionError("resume must fail closed")

    assert execution.status is ExecutionStatus.PAUSED
    state_adapter.set_current_session.assert_called_once_with(
        execution.session_id,
        execution_id=execution.id,
    )
    assert runtime.invocations == []


def test_unbound_explicit_runtime_rejects_resume_before_any_mutation():
    runtime = EvidenceOnlyRuntime()
    execution = _execution(ExecutionStatus.PAUSED)
    execution.checkpoint_id = "state-only-cp"
    original_variables = dict(execution.state.workflow_variables)
    controller, state_adapter = _controller(execution, runtime)
    controller._node_executor = MagicMock()

    try:
        controller.resume(execution.id, {"temperature": 0.2})
    except RuntimeError as error:
        assert "not bound" in str(error)
    else:
        raise AssertionError("unbound explicit resume must fail closed")

    assert execution.status is ExecutionStatus.PAUSED
    assert execution.state.workflow_variables == original_variables
    state_adapter.set_current_session.assert_not_called()
    state_adapter.update_state.assert_not_called()
    state_adapter.save_checkpoint.assert_not_called()
    controller._node_executor.execute.assert_not_called()
    controller._exec_repo.update.assert_not_called()
    controller._uow.commit.assert_not_called()
    controller._uow.rollback.assert_not_called()


def test_unbound_explicit_runtime_rejects_rollback_before_any_mutation():
    runtime = EvidenceOnlyRuntime()
    execution = _execution(ExecutionStatus.COMPLETED)
    controller, state_adapter = _controller(execution, runtime)

    try:
        controller.rollback(execution.id, "state-only-cp")
    except RuntimeError as error:
        assert "not bound" in str(error)
    else:
        raise AssertionError("unbound explicit rollback must fail closed")

    state_adapter.set_current_session.assert_not_called()
    state_adapter.load_checkpoint.assert_not_called()
    state_adapter.rollback.assert_not_called()
    controller._exec_repo.update.assert_not_called()
    controller._uow.commit.assert_not_called()
    controller._uow.rollback.assert_not_called()


def test_runtime_checkpoint_rejects_snapshot_owned_by_another_execution():
    class CrossExecutionRuntime(RecordingRuntime):
        def snapshot(self, execution_id, checkpoint_id=None):
            return RuntimeSnapshot(
                execution_id="exec-other",
                checkpoint_id=checkpoint_id,
                values={},
            )

    runtime = CrossExecutionRuntime()
    runtime.bind(object())
    execution = _execution(ExecutionStatus.PAUSED)
    controller, _ = _controller(execution, runtime)

    try:
        controller._require_runtime_checkpoint(
            execution.id,
            "shared-cp",
            operation="resume",
        )
    except RuntimeError as error:
        assert "does not support resume" in str(error)
    else:
        raise AssertionError("cross-execution checkpoint must fail closed")


def test_resume_activates_execution_session_before_runtime_snapshot():
    events = []

    class OrderedRuntime(RecordingRuntime):
        def snapshot(self, execution_id, checkpoint_id=None):
            events.append(("snapshot", execution_id, checkpoint_id))
            return super().snapshot(execution_id, checkpoint_id)

    runtime = OrderedRuntime()
    runtime.bind(object())
    execution = _execution(ExecutionStatus.PAUSED)
    execution.checkpoint_id = "cp-active"
    controller, state_adapter = _controller(execution, runtime)

    def activate(session_id, *, execution_id=None):
        events.append(("activate", execution_id, session_id))
        return True

    state_adapter.set_current_session.side_effect = activate

    result = controller.resume(execution.id)

    assert result.status is ExecutionStatus.COMPLETED
    assert events[:2] == [
        ("activate", execution.id, execution.session_id),
        ("snapshot", execution.id, "cp-active"),
    ]


def test_resume_patches_generic_runtime_checkpoint_before_invocation():
    runtime = RecordingRuntime()
    runtime.bind(object())
    execution = _execution(ExecutionStatus.PAUSED)
    execution.checkpoint_id = "cp-patch"
    controller, state_adapter = _controller(execution, runtime)

    result = controller.resume(execution.id, {"temperature": 0.2})

    assert result.status is ExecutionStatus.COMPLETED
    assert runtime.patches == [
        {
            "execution_id": execution.id,
            "checkpoint_id": "cp-patch",
            "values": {"temperature": 0.2},
            "as_node": None,
        }
    ]
    assert runtime.invocations[0] == {
        "input_state": None,
        "execution_id": execution.id,
        "checkpoint_id": "cp-patch-patched",
    }
    state_adapter.update_state.assert_not_called()


def test_resume_modified_state_fails_closed_when_runtime_lacks_patch_port():
    runtime = RecordingRuntime()
    runtime.bind(object())
    runtime.patch_checkpoint = None
    execution = _execution(ExecutionStatus.PAUSED)
    execution.checkpoint_id = "cp-patch"
    controller, _ = _controller(execution, runtime)

    try:
        controller.resume(execution.id, {"temperature": 0.2})
    except RuntimeError as error:
        assert "cannot apply resume state" in str(error)
    else:
        raise AssertionError("missing patch port must fail closed")

    assert execution.status is ExecutionStatus.PAUSED
    assert execution.state.workflow_variables == {"prompt": "hello"}
    assert runtime.invocations == []


def test_evidence_only_runtime_rejects_rollback_before_state_mutation():
    runtime = EvidenceOnlyRuntime()
    runtime.bind(object())
    execution = _execution(ExecutionStatus.COMPLETED)
    controller, state_adapter = _controller(execution, runtime)

    try:
        controller.rollback(execution.id, "state-only-cp")
    except RuntimeError as error:
        assert "does not support rollback" in str(error)
    else:
        raise AssertionError("rollback must fail closed")

    state_adapter.load_checkpoint.assert_not_called()
    state_adapter.rollback.assert_not_called()


def test_evidence_only_runtime_rejects_fork_before_checkpoint_read():
    runtime = EvidenceOnlyRuntime()
    runtime.bind(object())
    execution = _execution(ExecutionStatus.PAUSED)
    controller, state_adapter = _controller(execution, runtime)
    controller._workflow_repo.get.return_value = object()

    try:
        controller.fork(execution.id, "state-only-cp")
    except RuntimeError as error:
        assert "does not support fork" in str(error)
    else:
        raise AssertionError("fork must fail closed")

    state_adapter.load_checkpoint.assert_not_called()


def test_evidence_only_runtime_rejects_pause_before_state_checkpoint_mutation():
    runtime = EvidenceOnlyRuntime()
    runtime.bind(object())
    execution = _execution(ExecutionStatus.RUNNING)
    controller, state_adapter = _controller(execution, runtime)

    try:
        controller.pause(execution.id)
    except RuntimeError as error:
        assert "does not support pause" in str(error)
    else:
        raise AssertionError("evidence-only pause must fail closed")

    assert execution.status is ExecutionStatus.RUNNING
    assert execution.checkpoint_id is None
    state_adapter.set_current_session.assert_not_called()
    state_adapter.save_checkpoint.assert_not_called()
    controller._exec_repo.update.assert_not_called()
    controller._uow.commit.assert_not_called()


def test_claimed_pause_recovery_without_owned_snapshot_is_rejected():
    class UnprovenPauseRuntime(RecordingRuntime):
        def shares_checkpoint_store(self, state_adapter):
            return True

        def snapshot(self, execution_id, checkpoint_id=None):
            return None

    runtime = UnprovenPauseRuntime()
    runtime.bind(object())
    execution = _execution(ExecutionStatus.RUNNING)
    controller, state_adapter = _controller(execution, runtime)

    try:
        controller.pause(execution.id)
    except RuntimeError as error:
        assert "owned pause checkpoint" in str(error)
    else:
        raise AssertionError("unproven pause receipt must fail closed")

    assert execution.status is ExecutionStatus.RUNNING
    assert execution.checkpoint_id is None
    state_adapter.save_checkpoint.assert_not_called()
    controller._exec_repo.update.assert_not_called()
    controller._uow.commit.assert_not_called()


def test_claimed_pause_recovery_with_foreign_snapshot_is_rejected():
    class ForeignPauseRuntime(RecordingRuntime):
        def shares_checkpoint_store(self, state_adapter):
            return True

        def snapshot(self, execution_id, checkpoint_id=None):
            return RuntimeSnapshot(
                execution_id="exec-other",
                checkpoint_id="cp-foreign",
                values={},
            )

    runtime = ForeignPauseRuntime()
    runtime.bind(object())
    execution = _execution(ExecutionStatus.RUNNING)
    controller, state_adapter = _controller(execution, runtime)

    try:
        controller.pause(execution.id)
    except RuntimeError as error:
        assert "owned pause checkpoint" in str(error)
    else:
        raise AssertionError("foreign pause receipt must fail closed")

    assert execution.status is ExecutionStatus.RUNNING
    assert execution.checkpoint_id is None
    state_adapter.save_checkpoint.assert_not_called()
    controller._exec_repo.update.assert_not_called()
    controller._uow.commit.assert_not_called()


def test_langgraph_pause_reuses_owned_runtime_checkpoint():
    state_adapter = MagicMock()
    state_adapter.supports_graph_execution.return_value = True
    state_adapter.set_current_session.return_value = True
    state_adapter.get_current_session_id.return_value = "wtb-exec-1"
    state_adapter.get_checkpoint_history.return_value = [
        {"checkpoint_id": "cp-runtime", "values": {"answer": "ready"}}
    ]
    state_adapter.has_graph.return_value = True
    runtime = LangGraphWorkflowRuntime(state_adapter)
    execution = _execution(ExecutionStatus.RUNNING)
    execution_repository = MagicMock()
    execution_repository.get.return_value = execution
    controller = ExecutionController(
        execution_repository=execution_repository,
        workflow_repository=MagicMock(),
        state_adapter=state_adapter,
        workflow_runtime=runtime,
        unit_of_work=MagicMock(),
    )

    result = controller.pause(execution.id)

    assert result.status is ExecutionStatus.PAUSED
    assert result.checkpoint_id == "cp-runtime"
    assert result.metadata["resume_checkpoint_id"] == "cp-runtime"
    state_adapter.save_checkpoint.assert_not_called()


def test_unknown_runtime_outcome_blocks_pause_and_stop_before_mutation():
    runtime = RecordingRuntime()
    runtime.bind(object())
    execution = _execution(ExecutionStatus.RUNNING)
    execution.metadata = {
        "recovery_required": True,
        "recovery_reason": "runtime_outcome_unknown",
    }
    controller, state_adapter = _controller(execution, runtime)

    for operation in (
        lambda: controller.pause(execution.id),
        lambda: controller.stop(execution.id),
    ):
        try:
            operation()
        except RuntimeError as error:
            assert "runtime outcome is unknown" in str(error)
        else:
            raise AssertionError("unknown outcome must block pause and stop")

    assert execution.status is ExecutionStatus.RUNNING
    state_adapter.set_current_session.assert_not_called()
    state_adapter.save_checkpoint.assert_not_called()
    controller._exec_repo.update.assert_not_called()
    controller._uow.commit.assert_not_called()


def test_post_dispatch_snapshot_failure_requires_reconciliation():
    class SnapshotFailureRuntime(RecordingRuntime):
        def snapshot(self, execution_id, checkpoint_id=None):
            raise RuntimeError("snapshot receipt unavailable")

    runtime = SnapshotFailureRuntime()
    execution = _execution()
    controller, _ = _controller(execution, runtime)

    result = controller.run(execution.id, workflow=object())

    assert result.status is ExecutionStatus.RUNNING
    assert result.metadata["recovery_required"] is True
    assert result.metadata["recovery_reason"] == "runtime_outcome_unknown"
    assert "snapshot receipt unavailable" in (result.error_message or "")
    assert len(runtime.invocations) == 1


def test_post_dispatch_cross_execution_snapshot_requires_reconciliation():
    class CrossExecutionRuntime(RecordingRuntime):
        def snapshot(self, execution_id, checkpoint_id=None):
            return RuntimeSnapshot(
                execution_id="exec-other",
                checkpoint_id="cp-other",
                values={"answer": "wrong-owner"},
            )

    runtime = CrossExecutionRuntime()
    execution = _execution()
    controller, _ = _controller(execution, runtime)

    result = controller.run(execution.id, workflow=object())

    assert result.status is ExecutionStatus.RUNNING
    assert result.metadata["recovery_reason"] == "runtime_outcome_unknown"
    assert execution.checkpoint_id is None
    assert len(runtime.invocations) == 1

    try:
        controller.run(execution.id)
    except RuntimeError as error:
        assert "runtime outcome is unknown" in str(error)
    else:
        raise AssertionError("post-dispatch failure must block retry")

    assert len(runtime.invocations) == 1


def test_post_dispatch_file_link_failure_requires_reconciliation():
    runtime = RecordingRuntime()
    execution = _execution()
    controller, _ = _controller(execution, runtime)
    controller._file_tracking = MagicMock()
    controller._file_tracking.is_available.return_value = True
    controller._track_checkpoint_history_output_files = MagicMock(
        side_effect=RuntimeError("file link receipt unavailable")
    )

    result = controller.run(execution.id, workflow=object())

    assert result.status is ExecutionStatus.RUNNING
    assert result.metadata["recovery_reason"] == "runtime_outcome_unknown"
    assert "file link receipt unavailable" in (result.error_message or "")
    assert len(runtime.invocations) == 1


def test_evidence_only_runtime_does_not_link_files_to_state_checkpoint():
    runtime = EvidenceOnlyRuntime()
    execution = _execution()
    execution.checkpoint_id = "state-only-cp"
    file_tracking = MagicMock()
    file_tracking.is_available.return_value = True
    state_adapter = MagicMock()
    state_adapter.supports_graph_execution.return_value = False
    state_adapter.set_current_session.return_value = True
    execution_repository = MagicMock()
    execution_repository.get.return_value = execution
    controller = ExecutionController(
        execution_repository=execution_repository,
        workflow_repository=MagicMock(),
        state_adapter=state_adapter,
        workflow_runtime=runtime,
        unit_of_work=MagicMock(),
        file_tracking_service=file_tracking,
    )

    try:
        controller.run(execution.id, workflow=object())
    except RuntimeError as error:
        assert "file tracking" in str(error)
    else:
        raise AssertionError("evidence-only file tracking must fail preflight")

    assert execution.status is ExecutionStatus.PENDING
    assert runtime.invocations == []
    file_tracking.track_and_link.assert_not_called()


def test_unknown_runtime_outcome_blocks_recovery_operations_before_mutation():
    runtime = RecordingRuntime()
    runtime.bind(object())
    execution = _execution(ExecutionStatus.PAUSED)
    execution.metadata = {
        "recovery_required": True,
        "recovery_reason": "runtime_outcome_unknown",
    }
    execution.checkpoint_id = "cp-unknown"
    controller, state_adapter = _controller(execution, runtime)
    controller._workflow_repo.get.return_value = object()

    for operation in (
        lambda: controller.resume(execution.id),
        lambda: controller.rollback(execution.id, "cp-unknown"),
        lambda: controller.fork(execution.id, "cp-unknown"),
        lambda: controller.rollback_to_node(execution.id, "node-a"),
        lambda: controller.update_execution_state(execution.id, {"value": 2}),
    ):
        try:
            operation()
        except RuntimeError as error:
            assert "runtime outcome is unknown" in str(error)
        else:
            raise AssertionError("unknown outcome must block recovery")

    state_adapter.set_current_session.assert_not_called()
    state_adapter.load_checkpoint.assert_not_called()
    state_adapter.rollback.assert_not_called()
    state_adapter.get_checkpoint_history.assert_not_called()
    state_adapter.update_state.assert_not_called()
    assert runtime.invocations == []


def test_langgraph_runtime_preserves_legacy_adapter_contract():
    state_adapter = MagicMock()
    state_adapter.has_graph.return_value = True
    state_adapter.execute.return_value = {"answer": "ok"}
    state_adapter.get_checkpoint_history.return_value = [
        {
            "checkpoint_id": "cp-7",
            "values": {"answer": "ok"},
            "step": 7,
        }
    ]
    state_adapter.get_current_session_id.return_value = "wtb-exec-7"
    runtime = LangGraphWorkflowRuntime(state_adapter)
    graph = object()

    runtime.bind(graph)
    result = runtime.invoke(
        {"prompt": "hello"},
        execution_id="exec-7",
    )
    snapshot = runtime.snapshot("exec-7")

    state_adapter.set_workflow_graph.assert_called_once_with(
        graph,
        force_recompile=True,
    )
    state_adapter.execute.assert_called_once_with({"prompt": "hello"})
    assert result == {"answer": "ok"}
    assert snapshot is not None
    assert snapshot.checkpoint_id == "cp-7"
    assert snapshot.metadata["step"] == 7
    assert runtime.supports_checkpoint_recovery() is True
    assert runtime.supports_forking() is True


def test_langgraph_runtime_reconciliation_receipt_owns_prepared_input_hash():
    state_adapter = MagicMock()
    state_adapter.has_graph.return_value = True
    state_adapter.execute.return_value = {"answer": "ok"}
    state_adapter.get_current_session_id.return_value = "wtb-exec-hash"
    state_adapter.get_checkpoint_history.return_value = [
        {"checkpoint_id": "cp-hash", "values": {"answer": "ok"}}
    ]
    runtime = LangGraphWorkflowRuntime(state_adapter)
    plan = runtime.prepare_attempt(
        "exec-hash",
        "attempt-hash",
        input_sha256=_INPUT_SHA256,
    )

    runtime.invoke(
        {"prompt": "hello"},
        execution_id="exec-hash",
        attempt_id="attempt-hash",
        binding=plan.binding,
    )
    receipt = runtime.reconcile(
        "exec-hash",
        "attempt-hash",
        binding=plan.binding,
    )

    assert receipt is not None
    assert receipt.input_sha256 == _INPUT_SHA256
    assert receipt.session_id == "wtb-exec-hash"


def test_langgraph_runtime_does_not_relabel_active_session_history():
    state_adapter = MagicMock()
    state_adapter.get_current_session_id.return_value = "wtb-exec-a"
    state_adapter.get_checkpoint_history.return_value = [
        {"checkpoint_id": "shared-cp", "values": {"owner": "a"}}
    ]
    runtime = LangGraphWorkflowRuntime(state_adapter)

    assert runtime.snapshot("exec-b", "shared-cp") is None
    snapshot = runtime.snapshot("exec-a", "shared-cp")

    assert snapshot is not None
    assert snapshot.execution_id == "exec-a"
    assert snapshot.values == {"owner": "a"}


def test_langgraph_runtime_pins_resume_checkpoint_without_controller_private_access():
    state_adapter = MagicMock()
    state_adapter.execute.return_value = {"answer": "resumed"}
    runtime = LangGraphWorkflowRuntime(state_adapter)

    runtime.invoke(
        None,
        execution_id="exec-8",
        checkpoint_id="cp-8",
    )

    state_adapter.set_resume_checkpoint.assert_called_once_with("cp-8")
    state_adapter.execute.assert_called_once_with(None)


def test_langgraph_patch_returns_new_checkpoint_and_invoke_pins_it():
    state_adapter = MagicMock()
    state_adapter.get_current_session_id.return_value = "wtb-exec-8"
    state_adapter.get_checkpoint_history.return_value = [
        {"checkpoint_id": "cp-source", "values": {"value": 1}}
    ]
    state_adapter.update_checkpoint_state.return_value = "cp-updated"
    state_adapter.execute.return_value = {"value": 2}
    runtime = LangGraphWorkflowRuntime(state_adapter)

    continuation_id = runtime.patch_checkpoint(
        "exec-8",
        "cp-source",
        {"value": 2},
        as_node="review",
    )
    runtime.invoke(
        None,
        execution_id="exec-8",
        checkpoint_id=continuation_id,
    )

    assert continuation_id == "cp-updated"
    state_adapter.update_checkpoint_state.assert_called_once_with(
        "cp-source",
        {"value": 2},
        as_node="review",
    )
    state_adapter.set_resume_checkpoint.assert_called_once_with("cp-updated")


def test_langgraph_adapter_patch_uses_source_config_and_returns_new_id():
    from wtb.infrastructure.adapters.langgraph_state_adapter import (
        LangGraphStateAdapter,
    )

    adapter = object.__new__(LangGraphStateAdapter)
    adapter._compiled_graph = MagicMock()
    source_config = {
        "configurable": {
            "thread_id": "wtb-exec-9",
            "checkpoint_id": "cp-source",
        }
    }
    adapter.get_config = MagicMock(return_value=source_config)
    adapter._compiled_graph.update_state.return_value = {
        "configurable": {
            "thread_id": "wtb-exec-9",
            "checkpoint_id": "cp-updated",
        }
    }

    continuation_id = adapter.update_checkpoint_state(
        "cp-source",
        {"approved": True},
        as_node="review",
    )

    assert continuation_id == "cp-updated"
    adapter.get_config.assert_called_once_with("cp-source")
    adapter._compiled_graph.update_state.assert_called_once_with(
        source_config,
        {"approved": True},
        as_node="review",
    )
