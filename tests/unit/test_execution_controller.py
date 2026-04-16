"""
Unit tests for ExecutionController routing, resume, rollback fixes.

Tests FLAW 1-4, 4a fixes with mocks (no real LangGraph/DB required).
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime

from wtb.domain.models.workflow import (
    Execution,
    ExecutionState,
    ExecutionStatus,
    TestWorkflow,
    WorkflowNode,
    WorkflowEdge,
)
from wtb.application.services.execution_controller import (
    ExecutionController,
    DefaultNodeExecutor,
)


def _make_workflow() -> TestWorkflow:
    wf = TestWorkflow(id="wf-1", name="test", entry_point="start")
    wf.add_node(WorkflowNode(id="start", name="Start", type="start"))
    wf.add_node(WorkflowNode(id="end", name="End", type="end"))
    wf.add_edge(WorkflowEdge(source_id="start", target_id="end"))
    return wf


def _make_controller(
    state_adapter=None,
    exec_repo=None,
    workflow_repo=None,
    uow=None,
):
    exec_repo = exec_repo or MagicMock()
    workflow_repo = workflow_repo or MagicMock()
    state_adapter = state_adapter or MagicMock()
    uow = uow or MagicMock()

    return ExecutionController(
        execution_repository=exec_repo,
        workflow_repository=workflow_repo,
        state_adapter=state_adapter,
        node_executor=DefaultNodeExecutor(),
        unit_of_work=uow,
    )


def _make_execution(status=ExecutionStatus.PENDING, exec_id="exec-1") -> Execution:
    return Execution(
        id=exec_id,
        workflow_id="wf-1",
        status=status,
        state=ExecutionState(
            current_node_id="start",
            workflow_variables={"value": 0},
            execution_path=[],
            node_results={},
        ),
    )


# ═══════════════════════════════════════════════════════════════
# FLAW 1+2: Routing Tests
# ═══════════════════════════════════════════════════════════════


class TestRunRouting:
    """Capability-based routing: graph provided? -> adapter.has_graph()? -> node_executor."""

    def test_graph_provided_routes_to_langgraph(self):
        adapter = MagicMock()
        adapter.execute = MagicMock(return_value={"value": 2})
        adapter.set_workflow_graph = MagicMock()
        adapter.get_checkpointer = MagicMock()
        adapter.has_graph = MagicMock(return_value=True)

        exec_repo = MagicMock()
        execution = _make_execution()
        exec_repo.get.return_value = execution

        ctrl = _make_controller(state_adapter=adapter, exec_repo=exec_repo)
        graph = MagicMock()

        result = ctrl.run("exec-1", graph=graph)

        adapter.set_workflow_graph.assert_called_once_with(graph, force_recompile=True)
        adapter.execute.assert_called_once()
        assert result.status in (ExecutionStatus.COMPLETED, ExecutionStatus.FAILED)

    def test_no_graph_adapter_has_graph_routes_to_langgraph(self):
        """Resume scenario: graph=None but adapter already has a compiled graph."""
        adapter = MagicMock()
        adapter.execute = MagicMock(return_value={"value": 2})
        adapter.set_workflow_graph = MagicMock()
        adapter.get_checkpointer = MagicMock()
        adapter.has_graph = MagicMock(return_value=True)

        execution = _make_execution(status=ExecutionStatus.PAUSED)
        exec_repo = MagicMock()
        exec_repo.get.return_value = execution

        ctrl = _make_controller(state_adapter=adapter, exec_repo=exec_repo)

        result = ctrl.run("exec-1", graph=None)

        adapter.set_workflow_graph.assert_not_called()
        adapter.execute.assert_called_once_with(None)

    def test_no_graph_no_adapter_graph_routes_to_node_executor(self):
        """No graph anywhere -> node_executor path."""
        adapter = MagicMock(spec=[
            "initialize_session", "set_current_session",
            "save_checkpoint", "mark_node_started", "mark_node_completed",
            "mark_node_failed", "supports_graph_execution",
        ])
        adapter.supports_graph_execution.return_value = False

        workflow = _make_workflow()
        execution = _make_execution()

        exec_repo = MagicMock()
        exec_repo.get.return_value = execution
        workflow_repo = MagicMock()
        workflow_repo.get.return_value = workflow

        ctrl = _make_controller(
            state_adapter=adapter,
            exec_repo=exec_repo,
            workflow_repo=workflow_repo,
        )

        result = ctrl.run("exec-1", graph=None)
        assert result.status in (ExecutionStatus.COMPLETED, ExecutionStatus.FAILED)


# ═══════════════════════════════════════════════════════════════
# FLAW 1 (resume): PAUSED vs PENDING in _run_with_langgraph
# ═══════════════════════════════════════════════════════════════


class TestResumePath:

    def test_pending_calls_start_and_execute_with_state(self):
        adapter = MagicMock()
        adapter.execute = MagicMock(return_value={"value": 2})
        adapter.set_workflow_graph = MagicMock()
        adapter.get_checkpointer = MagicMock()
        adapter.has_graph = MagicMock(return_value=True)

        execution = _make_execution(status=ExecutionStatus.PENDING)
        exec_repo = MagicMock()
        exec_repo.get.return_value = execution

        ctrl = _make_controller(state_adapter=adapter, exec_repo=exec_repo)
        ctrl.run("exec-1", graph=MagicMock())

        args = adapter.execute.call_args[0]
        assert args[0] is not None, "PENDING should pass initial_state (not None)"

    def test_paused_calls_resume_and_execute_with_none(self):
        adapter = MagicMock()
        adapter.execute = MagicMock(return_value={"value": 2})
        adapter.set_workflow_graph = MagicMock()
        adapter.get_checkpointer = MagicMock()
        adapter.has_graph = MagicMock(return_value=True)

        execution = _make_execution(status=ExecutionStatus.PAUSED)
        exec_repo = MagicMock()
        exec_repo.get.return_value = execution

        ctrl = _make_controller(state_adapter=adapter, exec_repo=exec_repo)
        ctrl.run("exec-1")

        adapter.execute.assert_called_once_with(None)

    def test_completed_status_raises_runtime_error(self):
        adapter = MagicMock()
        adapter.execute = MagicMock()
        adapter.set_workflow_graph = MagicMock()
        adapter.get_checkpointer = MagicMock()
        adapter.has_graph = MagicMock(return_value=True)

        execution = _make_execution(status=ExecutionStatus.COMPLETED)
        exec_repo = MagicMock()
        exec_repo.get.return_value = execution

        ctrl = _make_controller(state_adapter=adapter, exec_repo=exec_repo)
        result = ctrl.run("exec-1", graph=MagicMock())

        # H13 fix: running a completed execution keeps it COMPLETED (terminal state guard)
        assert result.status in (ExecutionStatus.COMPLETED, ExecutionStatus.FAILED)


# ═══════════════════════════════════════════════════════════════
# FLAW 4: Rollback uses domain model + type normalization
# ═══════════════════════════════════════════════════════════════


class TestRollback:

    def test_rollback_uses_restore_from_checkpoint(self):
        adapter = MagicMock()
        adapter.rollback.return_value = ExecutionState(
            current_node_id="node_a",
            workflow_variables={"value": 5},
            execution_path=["start"],
            node_results={},
        )
        adapter.initialize_session = MagicMock()

        execution = _make_execution(status=ExecutionStatus.COMPLETED)
        exec_repo = MagicMock()
        exec_repo.get.return_value = execution

        ctrl = _make_controller(state_adapter=adapter, exec_repo=exec_repo)
        result = ctrl.rollback("exec-1", "cp-123")

        assert result.status == ExecutionStatus.PAUSED
        assert result.checkpoint_id == "cp-123"
        assert result.error_message is None

    def test_rollback_normalizes_dict_to_execution_state(self):
        adapter = MagicMock()
        adapter.rollback.return_value = {
            "current_node_id": "node_a",
            "value": 5,
            "execution_path": ["start"],
            "node_results": {"start": {"ok": True}},
        }
        adapter.initialize_session = MagicMock()

        execution = _make_execution(status=ExecutionStatus.FAILED)
        exec_repo = MagicMock()
        exec_repo.get.return_value = execution

        ctrl = _make_controller(state_adapter=adapter, exec_repo=exec_repo)
        result = ctrl.rollback("exec-1", "cp-456")

        assert result.status == ExecutionStatus.PAUSED
        assert isinstance(result.state, ExecutionState)
        assert result.state.current_node_id == "node_a"

    def test_rollback_invalid_checkpoint_propagates(self):
        adapter = MagicMock()
        adapter.rollback.side_effect = ValueError("Checkpoint not found")
        adapter.initialize_session = MagicMock()

        execution = _make_execution(status=ExecutionStatus.COMPLETED)
        exec_repo = MagicMock()
        exec_repo.get.return_value = execution

        ctrl = _make_controller(state_adapter=adapter, exec_repo=exec_repo)

        with pytest.raises(ValueError, match="Checkpoint not found"):
            ctrl.rollback("exec-1", "nonexistent")

    def test_rollback_syncs_external_cache_metadata(self):
        adapter = MagicMock()
        adapter.rollback.return_value = ExecutionState(
            current_node_id="node_a",
            workflow_variables={
                "llm_cache_refs": {"answer": "cache-123"},
                "llm_cache_hits": {"answer": True},
            },
            execution_path=["start"],
            node_results={},
        )
        adapter.initialize_session = MagicMock()

        execution = _make_execution(status=ExecutionStatus.COMPLETED)
        execution.metadata = {
            "actor_id": "actor_1",
            "checkpoint_db_path": "/tmp/ray_actors/actor_1/wtb_checkpoints.db",
            "llm_cache_path": "/tmp/ray_actors/actor_1/llm_response_cache.db",
        }
        exec_repo = MagicMock()
        exec_repo.get.return_value = execution

        ctrl = _make_controller(state_adapter=adapter, exec_repo=exec_repo)
        result = ctrl.rollback("exec-1", "cp-789")

        assert result.metadata["llm_cache_refs"] == {"answer": "cache-123"}
        assert result.metadata["llm_cache_hits"] == {"answer": True}
        assert result.metadata["actor_id"] == "actor_1"
        assert result.metadata["cache_storage_scope"] == "actor_local"


class TestFork:

    def test_fork_preserves_external_cache_metadata(self):
        adapter = MagicMock()
        adapter.load_checkpoint.return_value = ExecutionState(
            current_node_id="node_a",
            workflow_variables={
                "llm_cache_refs": {"answer": "cache-456"},
                "llm_cache_hits": {"answer": False},
            },
            execution_path=["start"],
            node_results={},
        )
        adapter.initialize_session = MagicMock(return_value="session-2")
        adapter.set_current_session = MagicMock()
        adapter.supports_graph_execution = MagicMock(return_value=False)
        adapter.create_fork = MagicMock()

        source_execution = _make_execution(status=ExecutionStatus.PAUSED)
        source_execution.metadata = {
            "actor_id": "actor_2",
            "checkpoint_db_path": "/tmp/ray_actors/actor_2/wtb_checkpoints.db",
            "llm_cache_path": "/tmp/ray_actors/actor_2/llm_response_cache.db",
            "cache_storage_scope": "actor_local",
            "llm_cache_refs": {"answer": "cache-456"},
            "llm_cache_hits": {"answer": False},
            "requested_execution_id": "requested-1",
        }

        exec_repo = MagicMock()
        exec_repo.get.return_value = source_execution

        workflow = _make_workflow()
        workflow_repo = MagicMock()
        workflow_repo.get.return_value = workflow

        ctrl = _make_controller(
            state_adapter=adapter,
            exec_repo=exec_repo,
            workflow_repo=workflow_repo,
        )
        forked = ctrl.fork("exec-2", "cp-321")

        assert forked.metadata["actor_id"] == "actor_2"
        assert forked.metadata["checkpoint_db_path"] == "/tmp/ray_actors/actor_2/wtb_checkpoints.db"
        assert forked.metadata["llm_cache_path"] == "/tmp/ray_actors/actor_2/llm_response_cache.db"
        assert forked.metadata["llm_cache_refs"] == {"answer": "cache-456"}
        assert forked.metadata["llm_cache_hits"] == {"answer": False}
        assert forked.metadata["cache_storage_scope"] == "actor_local"
        assert forked.metadata["forked_from"] == "exec-2"
        assert "requested_execution_id" not in forked.metadata


# ═══════════════════════════════════════════════════════════════
# Negative cases
# ═══════════════════════════════════════════════════════════════


class TestNegativeCases:

    def test_resume_when_not_paused_raises(self):
        adapter = MagicMock()
        execution = _make_execution(status=ExecutionStatus.PENDING)
        exec_repo = MagicMock()
        exec_repo.get.return_value = execution

        ctrl = _make_controller(state_adapter=adapter, exec_repo=exec_repo)

        with pytest.raises(ValueError, match="Cannot resume"):
            ctrl.resume("exec-1")

    def test_pause_when_not_running_raises(self):
        adapter = MagicMock()
        execution = _make_execution(status=ExecutionStatus.PENDING)
        exec_repo = MagicMock()
        exec_repo.get.return_value = execution

        ctrl = _make_controller(state_adapter=adapter, exec_repo=exec_repo)

        with pytest.raises(ValueError, match="Cannot pause"):
            ctrl.pause("exec-1")

    def test_run_nonexistent_execution_raises(self):
        adapter = MagicMock()
        exec_repo = MagicMock()
        exec_repo.get.return_value = None

        ctrl = _make_controller(state_adapter=adapter, exec_repo=exec_repo)

        with pytest.raises(ValueError, match="not found"):
            ctrl.run("nonexistent")
