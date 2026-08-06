"""Focused regressions for synchronous resume state handling."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from wtb.application.services.execution_controller import ExecutionController
from wtb.domain.interfaces.node_executor import NodeExecutionResult
from wtb.domain.models import (
    Execution,
    ExecutionState,
    ExecutionStatus,
    TestWorkflow,
    WorkflowEdge,
    WorkflowNode,
)
from wtb.infrastructure.adapters.inmemory_state_adapter import InMemoryStateAdapter
from wtb.infrastructure.database.inmemory_unit_of_work import InMemoryUnitOfWork


def _paused_execution(workflow_id: str, variables: dict[str, Any]) -> Execution:
    return Execution(
        workflow_id=workflow_id,
        status=ExecutionStatus.PAUSED,
        state=ExecutionState(
            current_node_id="finish",
            workflow_variables=dict(variables),
        ),
    )


def _node_controller() -> tuple[ExecutionController, InMemoryUnitOfWork, Execution]:
    uow = InMemoryUnitOfWork()
    workflow = TestWorkflow(id="wf-resume", name="resume")
    workflow.add_node(WorkflowNode(id="finish", name="Finish", type="end"))
    workflow.entry_point = "finish"
    uow.workflows.add(workflow)

    adapter = InMemoryStateAdapter()
    execution = _paused_execution(workflow.id, {"original": True})
    execution.session_id = adapter.initialize_session(execution.id, execution.state)
    uow.executions.add(execution)

    controller = ExecutionController(
        execution_repository=uow.executions,
        workflow_repository=uow.workflows,
        state_adapter=adapter,
        unit_of_work=uow,
    )
    return controller, uow, execution


def test_resume_keeps_modified_state_in_node_executor_result() -> None:
    controller, _, execution = _node_controller()

    resumed = controller.resume(execution.id, {"injected": "kept"})

    assert resumed.status is ExecutionStatus.COMPLETED
    assert resumed.state.workflow_variables["injected"] == "kept"


class _GraphStateAdapter(InMemoryStateAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, Any] = {"original": True}
        self.updated_values: list[dict[str, Any]] = []

    def supports_graph_execution(self) -> bool:
        return True

    def has_graph(self) -> bool:
        return True

    def update_state(
        self,
        values: dict[str, Any],
        as_node: str | None = None,
    ) -> bool:
        self.updated_values.append(dict(values))
        self.values.update(values)
        return True

    def execute(self, initial_state: dict[str, Any] | None) -> dict[str, Any]:
        if initial_state:
            self.values.update(initial_state)
        return dict(self.values)


def test_resume_applies_modified_state_to_graph_checkpoint() -> None:
    uow = InMemoryUnitOfWork()
    adapter = _GraphStateAdapter()
    execution = _paused_execution("wf-graph", {"original": True})
    execution.session_id = adapter.initialize_session(execution.id, execution.state)
    uow.executions.add(execution)
    controller = ExecutionController(
        execution_repository=uow.executions,
        workflow_repository=uow.workflows,
        state_adapter=adapter,
        unit_of_work=uow,
    )

    resumed = controller.resume(execution.id, {"injected": "kept"})

    assert adapter.updated_values == [{"injected": "kept"}]
    assert resumed.state.workflow_variables["injected"] == "kept"


class _RejectedGraphSessionAdapter(_GraphStateAdapter):
    def set_current_session(
        self,
        session_id: str,
        execution_id: str | None = None,
    ) -> bool:
        return False


class _RejectedGraphUpdateAdapter(_GraphStateAdapter):
    def update_state(
        self,
        values: dict[str, Any],
        as_node: str | None = None,
    ) -> bool:
        return False


def _graph_controller(
    adapter: _GraphStateAdapter,
    *,
    status: ExecutionStatus = ExecutionStatus.PAUSED,
) -> tuple[ExecutionController, InMemoryUnitOfWork, Execution]:
    uow = InMemoryUnitOfWork()
    execution = _paused_execution("wf-graph", {"stable": "before"})
    execution.status = status
    if status is ExecutionStatus.PENDING:
        execution.metadata = {"fork_type": "checkpoint_fork"}
    execution.session_id = adapter.initialize_session(
        execution.id,
        execution.state,
    )
    uow.executions.add(execution)
    return (
        ExecutionController(
            execution_repository=uow.executions,
            workflow_repository=uow.workflows,
            state_adapter=adapter,
            unit_of_work=uow,
        ),
        uow,
        execution,
    )


def test_resume_rejects_graph_session_failure_without_pending_transition() -> None:
    controller, uow, execution = _graph_controller(
        _RejectedGraphSessionAdapter(),
        status=ExecutionStatus.PENDING,
    )

    with pytest.raises(RuntimeError, match="resume session"):
        controller.resume(execution.id, {"stable": "after"})

    stored = uow.executions.get(execution.id)
    assert stored is not None
    assert stored.status is ExecutionStatus.PENDING
    assert stored.state.workflow_variables["stable"] == "before"


def test_resume_rejects_graph_update_failure_without_domain_mutation() -> None:
    controller, uow, execution = _graph_controller(
        _RejectedGraphUpdateAdapter(),
    )

    with pytest.raises(RuntimeError, match="resume state"):
        controller.resume(execution.id, {"stable": "after"})

    stored = uow.executions.get(execution.id)
    assert stored is not None
    assert stored.status is ExecutionStatus.PAUSED
    assert stored.state.workflow_variables["stable"] == "before"


class _AliasingExecutionRepository:
    """A valid repository shape that returns its stored domain instance."""

    def __init__(self, execution: Execution) -> None:
        self.execution = execution
        self.update_calls = 0

    def get(self, execution_id: str) -> Execution | None:
        return self.execution if execution_id == self.execution.id else None

    def update(self, execution: Execution) -> Execution:
        self.update_calls += 1
        self.execution = execution
        return execution


class _CountingUnitOfWork:
    def __init__(self) -> None:
        self.commit_calls = 0

    def commit(self) -> None:
        self.commit_calls += 1


class _RejectedNodeSessionAdapter(InMemoryStateAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.activation_calls = 0

    def set_current_session(
        self,
        session_id: str,
        execution_id: str | None = None,
    ) -> bool:
        self.activation_calls += 1
        return False


class _FailingRestoreTracker:
    def is_available(self) -> bool:
        return True

    def get_commit_for_checkpoint(self, checkpoint_id: str) -> str:
        return "commit-present"

    def restore_from_checkpoint(self, checkpoint_id: str) -> None:
        raise RuntimeError("restore failed")


def test_resume_rejects_node_session_before_restore_or_domain_mutation() -> None:
    adapter = _RejectedNodeSessionAdapter()
    execution = _paused_execution(
        "wf-node-session",
        {
            "stable": "before",
            "_output_files": {"artifact.txt": "old"},
        },
    )
    execution.status = ExecutionStatus.PENDING
    execution.checkpoint_id = "checkpoint-before"
    execution.metadata = {
        "fork_type": "checkpoint_fork",
        "source_checkpoint_id": "checkpoint-source",
        "source_checkpoint_has_output_files": True,
    }
    execution.session_id = adapter.initialize_session(
        execution.id,
        execution.state,
    )

    repository = _AliasingExecutionRepository(execution)
    uow = _CountingUnitOfWork()
    workflow_uow = InMemoryUnitOfWork()
    workflow = TestWorkflow(id=execution.workflow_id, name="node session")
    workflow.add_node(WorkflowNode(id="finish", name="Finish", type="end"))
    workflow.entry_point = "finish"
    workflow_uow.workflows.add(workflow)
    controller = ExecutionController(
        execution_repository=repository,
        workflow_repository=workflow_uow.workflows,
        state_adapter=adapter,
        unit_of_work=uow,
        file_tracking_service=_FailingRestoreTracker(),
        output_dir="unused",
    )

    with patch.object(
        controller,
        "_restore_checkpoint_files",
        return_value={"restored_files": ["artifact.txt"]},
    ) as restore_mock:
        with pytest.raises(RuntimeError, match=r"activate .*session"):
            controller.resume(execution.id, {"stable": "after"})

    restore_mock.assert_not_called()
    assert adapter.activation_calls == 1
    assert repository.update_calls == 0
    assert uow.commit_calls == 0
    assert repository.execution is execution
    assert repository.execution.status is ExecutionStatus.PENDING
    assert repository.execution.checkpoint_id == "checkpoint-before"
    assert repository.execution.state.workflow_variables == {
        "stable": "before",
        "_output_files": {"artifact.txt": "old"},
    }


def test_resume_restore_failure_does_not_mutate_repository_entity() -> None:
    execution = _paused_execution(
        "wf-restore",
        {
            "stable": "before",
            "_output_files": {"artifact.txt": "old"},
        },
    )
    execution.metadata = {
        "fork_type": "checkpoint_fork",
        "source_checkpoint_id": "checkpoint-1",
        "source_checkpoint_has_output_files": True,
    }
    adapter = InMemoryStateAdapter()
    execution.session_id = adapter.initialize_session(
        execution.id,
        execution.state,
    )
    repository = _AliasingExecutionRepository(execution)
    controller = ExecutionController(
        execution_repository=repository,
        workflow_repository=InMemoryUnitOfWork().workflows,
        state_adapter=adapter,
        file_tracking_service=_FailingRestoreTracker(),
        output_dir="unused",
    )

    with pytest.raises(RuntimeError, match="restore failed"):
        controller.resume(execution.id, {"stable": "after"})

    assert repository.execution.state.workflow_variables["stable"] == "before"


class _RecordingNodeExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(
        self,
        node: WorkflowNode,
        context: dict[str, Any],
    ) -> NodeExecutionResult:
        self.calls.append(node.id)
        return NodeExecutionResult(
            success=True,
            output={f"{node.id}_runs": self.calls.count(node.id)},
        )


def _completed_two_node_execution() -> tuple[
    ExecutionController,
    InMemoryStateAdapter,
    _RecordingNodeExecutor,
    Execution,
]:
    uow = InMemoryUnitOfWork()
    workflow = TestWorkflow(id="wf-continuation", name="continuation")
    workflow.add_node(WorkflowNode(id="A", name="A", type="action"))
    workflow.add_node(WorkflowNode(id="B", name="B", type="end"))
    workflow.add_edge(WorkflowEdge(source_id="A", target_id="B"))
    workflow.entry_point = "A"
    uow.workflows.add(workflow)

    adapter = InMemoryStateAdapter()
    node_executor = _RecordingNodeExecutor()
    controller = ExecutionController(
        execution_repository=uow.executions,
        workflow_repository=uow.workflows,
        state_adapter=adapter,
        node_executor=node_executor,
        unit_of_work=uow,
    )
    execution = controller.create_execution(workflow)
    completed = controller.run(execution.id)
    assert completed.status is ExecutionStatus.COMPLETED
    assert node_executor.calls == ["A", "B"]
    return controller, adapter, node_executor, completed


def _exit_checkpoint_for(
    adapter: InMemoryStateAdapter,
    execution: Execution,
    node_id: str,
) -> str:
    boundary = adapter.get_node_boundary(execution.session_id or "", node_id)
    assert boundary is not None
    assert boundary.exit_checkpoint_id is not None
    return boundary.exit_checkpoint_id


def test_rollback_after_node_resumes_at_successor_without_replaying_node() -> None:
    controller, adapter, node_executor, completed = _completed_two_node_execution()
    after_a = _exit_checkpoint_for(adapter, completed, "A")

    controller.rollback(completed.id, after_a)
    resumed = controller.resume(completed.id)

    assert node_executor.calls == ["A", "B", "B"]
    assert resumed.state.execution_path == ["A", "B"]
    assert resumed.state.node_results["A"] == {"A_runs": 1}


def test_fork_after_node_resumes_at_successor_and_preserves_full_state() -> None:
    controller, adapter, node_executor, completed = _completed_two_node_execution()
    after_a = _exit_checkpoint_for(adapter, completed, "A")

    forked = controller.fork(completed.id, after_a)
    resumed = controller.resume(forked.id)

    assert node_executor.calls == ["A", "B", "B"]
    assert forked.state.current_node_id == "B"
    assert forked.state.execution_path == ["A"]
    assert forked.state.node_results["A"] == {"A_runs": 1}
    assert resumed.state.execution_path == ["A", "B"]


class _RejectedBoundaryCompletionAdapter(InMemoryStateAdapter):
    def mark_node_completed(self, node_id: str, exit_checkpoint_id: str) -> bool:
        return False


def test_node_run_fails_completed_node_when_boundary_claim_is_lost() -> None:
    uow = InMemoryUnitOfWork()
    workflow = TestWorkflow(id="wf-boundary-loser", name="boundary loser")
    workflow.add_node(WorkflowNode(id="A", name="A", type="action"))
    workflow.add_node(WorkflowNode(id="B", name="B", type="end"))
    workflow.add_edge(WorkflowEdge(source_id="A", target_id="B"))
    workflow.entry_point = "A"
    uow.workflows.add(workflow)

    node_executor = _RecordingNodeExecutor()
    controller = ExecutionController(
        execution_repository=uow.executions,
        workflow_repository=uow.workflows,
        state_adapter=_RejectedBoundaryCompletionAdapter(),
        node_executor=node_executor,
        unit_of_work=uow,
    )
    execution = controller.create_execution(workflow)

    failed = controller.run(execution.id)

    assert failed.status is ExecutionStatus.FAILED
    assert failed.error_node_id == "A"
    assert failed.state.current_node_id == "A"
    assert node_executor.calls == ["A"]
