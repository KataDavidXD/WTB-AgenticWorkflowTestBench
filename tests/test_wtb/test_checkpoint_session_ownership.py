"""Checkpoint ownership regressions for the in-memory state adapter."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from wtb.application.services.execution_controller import ExecutionController
from wtb.domain.interfaces.state_adapter import CheckpointTrigger
from wtb.domain.models import (
    Execution,
    ExecutionState,
    ExecutionStatus,
    WorkflowNode,
)
from wtb.domain.models import (
    TestWorkflow as WorkflowDefinition,
)
from wtb.infrastructure.adapters.inmemory_state_adapter import InMemoryStateAdapter
from wtb.infrastructure.database.inmemory_unit_of_work import InMemoryUnitOfWork


def _state(owner: str) -> ExecutionState:
    return ExecutionState(
        current_node_id="start",
        workflow_variables={"owner": owner},
        execution_path=[],
        node_results={},
    )


def _adapter_fingerprint(adapter: InMemoryStateAdapter) -> dict[str, Any]:
    return {
        "current_session_id": adapter._current_session_id,
        "current_execution_id": adapter._current_execution_id,
        "step_counter": adapter._step_counter,
        "sessions": {
            session_id: {
                "execution_id": session["execution_id"],
                "initial_state": session["initial_state"].to_dict(),
                "created_at": session["created_at"],
            }
            for session_id, session in adapter._sessions.items()
        },
        "checkpoints": {
            checkpoint_id: {
                "session_id": checkpoint.session_id,
                "state": checkpoint.state.to_dict(),
                "node_id": checkpoint.node_id,
                "trigger": checkpoint.trigger,
                "name": checkpoint.name,
                "metadata": dict(checkpoint.metadata),
                "step": checkpoint.step,
                "created_at": checkpoint.created_at,
            }
            for checkpoint_id, checkpoint in adapter._checkpoints.items()
        },
        "boundaries": {
            boundary_id: dict(boundary.__dict__)
            for boundary_id, boundary in adapter._boundaries.items()
        },
    }


def _repository_fingerprint(uow: InMemoryUnitOfWork) -> dict[str, Execution]:
    return {
        execution.id: deepcopy(execution)
        for execution in uow.executions.list()
    }


def _two_session_adapter() -> tuple[InMemoryStateAdapter, str, str, str, str]:
    adapter = InMemoryStateAdapter()
    session_a = adapter.initialize_session("execution-a", _state("a"))
    checkpoint_a = adapter.save_checkpoint(
        _state("a"),
        node_id="node-a",
        trigger=CheckpointTrigger.AUTO,
    )
    session_b = adapter.initialize_session("execution-b", _state("b"))
    checkpoint_b = adapter.save_checkpoint(
        _state("b"),
        node_id="node-b",
        trigger=CheckpointTrigger.AUTO,
    )
    assert adapter.set_current_session(session_a, execution_id="execution-a") is True
    return adapter, session_a, checkpoint_a, session_b, checkpoint_b


def _controller_setup() -> tuple[
    ExecutionController,
    InMemoryUnitOfWork,
    InMemoryStateAdapter,
    Execution,
    Execution,
    str,
]:
    adapter, session_a, _, session_b, checkpoint_b = _two_session_adapter()
    uow = InMemoryUnitOfWork()
    workflow = WorkflowDefinition(
        id="workflow",
        name="workflow",
        entry_point="start",
    )
    workflow.add_node(WorkflowNode(id="start", name="Start", type="start"))
    uow.workflows.add(workflow)

    execution_a = Execution(
        id="execution-a",
        workflow_id=workflow.id,
        status=ExecutionStatus.COMPLETED,
        state=_state("a"),
        session_id=session_a,
    )
    execution_b = Execution(
        id="execution-b",
        workflow_id=workflow.id,
        status=ExecutionStatus.COMPLETED,
        state=_state("b"),
        session_id=session_b,
    )
    uow.executions.add(execution_a)
    uow.executions.add(execution_b)
    uow.commit()

    controller = ExecutionController(
        execution_repository=uow.executions,
        workflow_repository=uow.workflows,
        state_adapter=adapter,
        unit_of_work=uow,
    )
    return controller, uow, adapter, execution_a, execution_b, checkpoint_b


@pytest.mark.parametrize("operation", ["load_checkpoint", "rollback"])
def test_checkpoint_access_requires_an_active_session(operation: str) -> None:
    adapter, _, checkpoint_a, _, _ = _two_session_adapter()
    adapter._current_session_id = None
    adapter._current_execution_id = None
    before = _adapter_fingerprint(adapter)

    with pytest.raises(RuntimeError, match="active session"):
        getattr(adapter, operation)(checkpoint_a)

    assert _adapter_fingerprint(adapter) == before


@pytest.mark.parametrize("operation", ["load_checkpoint", "rollback"])
def test_checkpoint_access_rejects_foreign_session(operation: str) -> None:
    adapter, session_a, _, _, checkpoint_b = _two_session_adapter()
    before = _adapter_fingerprint(adapter)

    with pytest.raises(ValueError, match="does not belong to active session"):
        getattr(adapter, operation)(checkpoint_b)

    assert adapter.get_current_session_id() == session_a
    assert _adapter_fingerprint(adapter) == before


def test_controller_rollback_rejects_foreign_checkpoint_without_mutation() -> None:
    controller, uow, adapter, execution_a, _, checkpoint_b = _controller_setup()
    entity_before = deepcopy(execution_a)
    repository_before = _repository_fingerprint(uow)
    adapter_before = _adapter_fingerprint(adapter)

    with pytest.raises(ValueError, match="does not belong to active session"):
        controller.rollback(execution_a.id, checkpoint_b)

    assert execution_a == entity_before
    assert _repository_fingerprint(uow) == repository_before
    assert _adapter_fingerprint(adapter) == adapter_before


def test_controller_fork_rejects_foreign_checkpoint_without_mutation() -> None:
    controller, uow, adapter, execution_a, _, checkpoint_b = _controller_setup()
    entity_before = deepcopy(execution_a)
    repository_before = _repository_fingerprint(uow)
    adapter_before = _adapter_fingerprint(adapter)

    with pytest.raises(ValueError, match="does not belong to active session"):
        controller.fork(execution_a.id, checkpoint_b)

    assert execution_a == entity_before
    assert _repository_fingerprint(uow) == repository_before
    assert _adapter_fingerprint(adapter) == adapter_before
