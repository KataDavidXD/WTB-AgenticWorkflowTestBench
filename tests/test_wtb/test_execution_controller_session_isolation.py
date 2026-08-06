"""Regression tests for fail-closed execution session activation."""

from unittest.mock import MagicMock

import pytest

from wtb.application.services.execution_controller import ExecutionController
from wtb.domain.interfaces.state_adapter import CheckpointTrigger
from wtb.domain.models.workflow import (
    Execution,
    ExecutionState,
    ExecutionStatus,
    TestWorkflow as WorkflowDefinition,
    WorkflowNode,
)
from wtb.infrastructure.adapters.inmemory_state_adapter import InMemoryStateAdapter
from wtb.sdk import BatchTestResult, WTBTestBench


def _state(*, value: str) -> ExecutionState:
    return ExecutionState(
        current_node_id="start",
        workflow_variables={"value": value},
        execution_path=[],
        node_results={},
    )


def _execution(
    execution_id: str,
    *,
    session_id: str,
    status: ExecutionStatus = ExecutionStatus.PENDING,
) -> Execution:
    execution = Execution(
        id=execution_id,
        workflow_id="workflow",
        status=status,
        state=_state(value=execution_id),
    )
    execution.session_id = session_id
    return execution


def _controller(
    execution: Execution,
    adapter,
    *,
    workflow: WorkflowDefinition | None = None,
) -> ExecutionController:
    execution_repository = MagicMock()
    execution_repository.get.side_effect = (
        lambda execution_id: execution if execution_id == execution.id else None
    )
    workflow_repository = MagicMock()
    workflow_repository.get.return_value = workflow
    return ExecutionController(
        execution_repository=execution_repository,
        workflow_repository=workflow_repository,
        state_adapter=adapter,
        unit_of_work=MagicMock(),
    )


def _adapter_with_stale_checkpoint() -> tuple[InMemoryStateAdapter, str, str]:
    adapter = InMemoryStateAdapter()
    stale_session = adapter.initialize_session("execution-a", _state(value="a"))
    checkpoint_id = adapter.save_checkpoint(
        _state(value="a"),
        node_id="node-a",
        trigger=CheckpointTrigger.AUTO,
    )
    return adapter, stale_session, checkpoint_id


def _single_node_workflow() -> WorkflowDefinition:
    workflow = WorkflowDefinition(
        id="workflow",
        name="workflow",
        entry_point="start",
    )
    workflow.add_node(WorkflowNode(id="start", name="Start", type="start"))
    return workflow


def test_checkpoint_history_missing_session_does_not_return_previous_execution():
    adapter, stale_session, stale_checkpoint_id = _adapter_with_stale_checkpoint()
    execution = _execution("execution-b", session_id="missing-session")
    controller = _controller(execution, adapter)

    assert controller.get_checkpoint_history(execution.id) == []
    assert adapter.get_current_session_id() == stale_session
    assert [
        item["checkpoint_id"] for item in adapter.get_checkpoint_history()
    ] == [stale_checkpoint_id]


def test_sdk_batch_checkpoint_failure_is_not_hidden_by_fallback():
    adapter, _, _ = _adapter_with_stale_checkpoint()
    execution = _execution("execution-b", session_id="missing-session")
    controller = _controller(execution, adapter)
    coordinator = MagicMock()
    coordinator.get_checkpoints.side_effect = RuntimeError("actor unavailable")
    batch_runner = MagicMock()
    batch_runner.create_rollback_coordinator.return_value = coordinator
    bench = WTBTestBench(
        project_service=MagicMock(),
        variant_service=MagicMock(),
        execution_controller=controller,
        batch_runner=batch_runner,
    )

    with pytest.raises(RuntimeError, match="actor unavailable"):
        bench.get_batch_result_checkpoints(
            BatchTestResult(
                combination_name="variant-b",
                execution_id=execution.id,
                success=True,
            )
        )

    coordinator.get_checkpoints.assert_called_once_with(execution.id, graph=None)


def test_langgraph_run_rejects_missing_session_before_execute():
    adapter = MagicMock()
    adapter.supports_graph_execution.return_value = True
    adapter.has_graph.return_value = True
    adapter.set_current_session.return_value = False
    execution = _execution("execution-b", session_id="missing-session")
    controller = _controller(execution, adapter)

    with pytest.raises(RuntimeError, match="activate execution session"):
        controller.run(execution.id, graph=MagicMock())

    assert execution.status == ExecutionStatus.PENDING
    adapter.execute.assert_not_called()


def test_node_executor_run_rejects_missing_session_without_stale_writes():
    adapter, stale_session, stale_checkpoint_id = _adapter_with_stale_checkpoint()
    execution = _execution("execution-b", session_id="missing-session")
    controller = _controller(execution, adapter, workflow=_single_node_workflow())

    with pytest.raises(RuntimeError, match="activate execution session"):
        controller.run(execution.id)

    assert execution.status == ExecutionStatus.PENDING
    assert adapter.get_current_session_id() == stale_session
    assert [
        item["checkpoint_id"] for item in adapter.get_checkpoint_history()
    ] == [stale_checkpoint_id]


def test_update_state_rejects_missing_session_before_adapter_update():
    adapter = MagicMock()
    adapter.set_current_session.return_value = False
    execution = _execution("execution-b", session_id="missing-session")
    controller = _controller(execution, adapter)

    assert controller.update_execution_state(execution.id, {"value": "changed"}) is False
    assert execution.state.workflow_variables == {"value": execution.id}
    adapter.update_state.assert_not_called()


def test_rollback_to_node_rejects_missing_session_before_history_read():
    adapter = MagicMock()
    adapter.supports_time_travel.return_value = True
    adapter.set_current_session.return_value = False
    execution = _execution(
        "execution-b",
        session_id="missing-session",
        status=ExecutionStatus.COMPLETED,
    )
    controller = _controller(execution, adapter)

    with pytest.raises(RuntimeError, match="activate execution session"):
        controller.rollback_to_node(execution.id, "node-b")

    adapter.get_checkpoint_history.assert_not_called()


def test_pause_switches_to_target_session_before_checkpoint_write():
    adapter = InMemoryStateAdapter()
    session_a = adapter.initialize_session("execution-a", _state(value="a"))
    checkpoint_a = adapter.save_checkpoint(
        _state(value="a"),
        node_id="node-a",
        trigger=CheckpointTrigger.AUTO,
    )
    session_b = adapter.initialize_session("execution-b", _state(value="b"))
    assert adapter.set_current_session(session_a, execution_id="execution-a") is True
    execution = _execution(
        "execution-b",
        session_id=session_b,
        status=ExecutionStatus.RUNNING,
    )
    controller = _controller(execution, adapter)

    controller.pause(execution.id)

    assert execution.status == ExecutionStatus.PAUSED
    assert adapter.get_current_session_id() == session_b
    assert [item.id for item in adapter.get_checkpoints(session_a)] == [checkpoint_a]
    checkpoints_b = adapter.get_checkpoints(session_b)
    assert len(checkpoints_b) == 1
    assert checkpoints_b[0].node_id == "start"


def test_pause_missing_session_fails_before_domain_or_checkpoint_mutation():
    adapter, stale_session, stale_checkpoint_id = _adapter_with_stale_checkpoint()
    execution = _execution(
        "execution-b",
        session_id="missing-session",
        status=ExecutionStatus.RUNNING,
    )
    controller = _controller(execution, adapter)

    with pytest.raises(RuntimeError, match="activate execution session"):
        controller.pause(execution.id)

    assert execution.status == ExecutionStatus.RUNNING
    assert adapter.get_current_session_id() == stale_session
    assert [
        item["checkpoint_id"] for item in adapter.get_checkpoint_history()
    ] == [stale_checkpoint_id]
