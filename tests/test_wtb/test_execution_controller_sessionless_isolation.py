"""Regression tests for executions that have no adapter session ID."""

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


def _state(value: str) -> ExecutionState:
    return ExecutionState(
        current_node_id="start",
        workflow_variables={"value": value},
        execution_path=[],
        node_results={},
    )


def _execution(*, status: ExecutionStatus = ExecutionStatus.PENDING) -> Execution:
    execution = Execution(
        id="execution-b",
        workflow_id="workflow",
        status=status,
        state=_state("b"),
    )
    execution.session_id = None
    return execution


def _workflow() -> WorkflowDefinition:
    workflow = WorkflowDefinition(
        id="workflow",
        name="workflow",
        entry_point="start",
    )
    workflow.add_node(WorkflowNode(id="start", name="Start", type="start"))
    return workflow


def _controller(execution: Execution, adapter) -> ExecutionController:
    execution_repository = MagicMock()
    execution_repository.get.return_value = execution
    workflow_repository = MagicMock()
    workflow_repository.get.return_value = _workflow()
    return ExecutionController(
        execution_repository=execution_repository,
        workflow_repository=workflow_repository,
        state_adapter=adapter,
        unit_of_work=MagicMock(),
    )


def _adapter_with_stale_history() -> tuple[InMemoryStateAdapter, str]:
    adapter = InMemoryStateAdapter()
    adapter.initialize_session("execution-a", _state("a"))
    checkpoint_id = adapter.save_checkpoint(
        _state("a"),
        node_id="node-a",
        trigger=CheckpointTrigger.AUTO,
    )
    return adapter, checkpoint_id


@pytest.mark.parametrize("use_langgraph", [True, False])
def test_run_without_session_id_fails_before_adapter_execution(use_langgraph):
    adapter = MagicMock()
    adapter.supports_graph_execution.return_value = use_langgraph
    adapter.has_graph.return_value = use_langgraph
    execution = _execution()
    controller = _controller(execution, adapter)

    with pytest.raises(RuntimeError, match="activate execution session"):
        controller.run(execution.id, graph=MagicMock() if use_langgraph else None)

    assert execution.status == ExecutionStatus.PENDING
    adapter.execute.assert_not_called()
    adapter.save_checkpoint.assert_not_called()


def test_pause_without_session_id_does_not_write_into_current_session():
    adapter, checkpoint_id = _adapter_with_stale_history()
    execution = _execution(status=ExecutionStatus.RUNNING)
    controller = _controller(execution, adapter)

    with pytest.raises(RuntimeError, match="activate execution session"):
        controller.pause(execution.id)

    assert execution.status == ExecutionStatus.RUNNING
    assert [
        item["checkpoint_id"] for item in adapter.get_checkpoint_history()
    ] == [checkpoint_id]


def test_checkpoint_history_without_session_id_returns_empty():
    adapter, _ = _adapter_with_stale_history()
    execution = _execution()
    controller = _controller(execution, adapter)

    assert controller.get_checkpoint_history(execution.id) == []


def test_update_state_without_session_id_fails_before_adapter_update():
    adapter = MagicMock()
    adapter.update_state.return_value = True
    execution = _execution()
    controller = _controller(execution, adapter)

    assert controller.update_execution_state(execution.id, {"value": "changed"}) is False
    adapter.update_state.assert_not_called()
    assert execution.state.workflow_variables == {"value": "b"}


def test_rollback_to_node_without_session_id_fails_before_history_read():
    adapter = MagicMock()
    adapter.supports_time_travel.return_value = True
    execution = _execution(status=ExecutionStatus.COMPLETED)
    controller = _controller(execution, adapter)

    with pytest.raises(RuntimeError, match="activate execution session"):
        controller.rollback_to_node(execution.id, "node-b")

    adapter.get_checkpoint_history.assert_not_called()
