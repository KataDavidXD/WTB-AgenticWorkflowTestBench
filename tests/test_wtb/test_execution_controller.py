"""
Unit tests for ExecutionController.

Tests the main workflow execution orchestration.

Quality Improvements (2026-01-16):
- Added contextual assertion helpers
- One-shot breakpoint semantic testing
- Better error messages for debugging
"""

import pytest
from typing import Optional, List, Dict, Any

from wtb.domain.models import (
    Execution,
    ExecutionStatus,
    ExecutionState,
    TestWorkflow,
    WorkflowNode,
    WorkflowEdge,
)
from wtb.domain.interfaces.repositories import IExecutionRepository, IWorkflowRepository
from wtb.infrastructure.adapters.inmemory_state_adapter import InMemoryStateAdapter
from wtb.application.services.execution_controller import ExecutionController, DefaultNodeExecutor

# Test helpers for better assertions
from tests.helpers.assertions import (
    assert_execution_completed,
    assert_execution_paused,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Mock Repositories
# ═══════════════════════════════════════════════════════════════════════════════

class MockExecutionRepository(IExecutionRepository):
    """In-memory execution repository for testing."""
    
    def __init__(self):
        self._storage: Dict[str, Execution] = {}
    
    def get(self, id: str) -> Optional[Execution]:
        return self._storage.get(id)
    
    def list(self, limit: int = 100, offset: int = 0) -> List[Execution]:
        items = list(self._storage.values())
        return items[offset:offset + limit]
    
    def add(self, entity: Execution) -> Execution:
        self._storage[entity.id] = entity
        return entity
    
    def update(self, entity: Execution) -> Execution:
        self._storage[entity.id] = entity
        return entity
    
    def delete(self, id: str) -> bool:
        if id in self._storage:
            del self._storage[id]
            return True
        return False
    
    def exists(self, id: str) -> bool:
        return id in self._storage
    
    def find_by_workflow(self, workflow_id: str, status: Optional[str] = None) -> List[Execution]:
        results = [e for e in self._storage.values() if e.workflow_id == workflow_id]
        if status:
            results = [e for e in results if e.status.value == status]
        return results
    
    def find_by_status(self, status: str) -> List[Execution]:
        return [e for e in self._storage.values() if e.status.value == status]
    
    def count_by_status(self) -> Dict[str, int]:
        counts = {}
        for e in self._storage.values():
            key = e.status.value
            counts[key] = counts.get(key, 0) + 1
        return counts
    
    def find_running(self) -> List[Execution]:
        return [e for e in self._storage.values() if e.status == ExecutionStatus.RUNNING]


class MockWorkflowRepository(IWorkflowRepository):
    """In-memory workflow repository for testing."""
    
    def __init__(self):
        self._storage: Dict[str, TestWorkflow] = {}
    
    def get(self, id: str) -> Optional[TestWorkflow]:
        return self._storage.get(id)
    
    def list(self, limit: int = 100, offset: int = 0) -> List[TestWorkflow]:
        items = list(self._storage.values())
        return items[offset:offset + limit]
    
    def add(self, entity: TestWorkflow) -> TestWorkflow:
        self._storage[entity.id] = entity
        return entity
    
    def update(self, entity: TestWorkflow) -> TestWorkflow:
        self._storage[entity.id] = entity
        return entity
    
    def delete(self, id: str) -> bool:
        if id in self._storage:
            del self._storage[id]
            return True
        return False
    
    def exists(self, id: str) -> bool:
        return id in self._storage
    
    def list_all(self) -> List[TestWorkflow]:
        """List all workflows without pagination."""
        return list(self._storage.values())
    
    def find_by_name(self, name: str) -> Optional[TestWorkflow]:
        for w in self._storage.values():
            if w.name == name:
                return w
        return None
    
    def find_by_version(self, name: str, version: str) -> Optional[TestWorkflow]:
        for w in self._storage.values():
            if w.name == name and w.version == version:
                return w
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════════════════════

def create_simple_workflow() -> TestWorkflow:
    """Create a simple start -> action -> end workflow."""
    workflow = TestWorkflow(name="Simple Workflow")
    
    start = WorkflowNode(id="start", name="Start", type="start")
    action = WorkflowNode(id="action", name="Action", type="action", config={"result": "done"})
    end = WorkflowNode(id="end", name="End", type="end")
    
    workflow.add_node(start)
    workflow.add_node(action)
    workflow.add_node(end)
    
    workflow.add_edge(WorkflowEdge(source_id="start", target_id="action"))
    workflow.add_edge(WorkflowEdge(source_id="action", target_id="end"))
    
    return workflow


def create_decision_workflow() -> TestWorkflow:
    """Create a workflow with a decision node."""
    workflow = TestWorkflow(name="Decision Workflow")
    
    start = WorkflowNode(id="start", name="Start", type="start")
    decision = WorkflowNode(
        id="decision",
        name="Check Value",
        type="decision",
        config={"condition": "x > 5"}
    )
    branch_true = WorkflowNode(id="branch_true", name="True Branch", type="action")
    branch_false = WorkflowNode(id="branch_false", name="False Branch", type="action")
    end = WorkflowNode(id="end", name="End", type="end")
    
    workflow.add_node(start)
    workflow.add_node(decision)
    workflow.add_node(branch_true)
    workflow.add_node(branch_false)
    workflow.add_node(end)
    
    workflow.add_edge(WorkflowEdge(source_id="start", target_id="decision"))
    workflow.add_edge(WorkflowEdge(source_id="decision", target_id="branch_true", condition="decision"))
    workflow.add_edge(WorkflowEdge(source_id="decision", target_id="branch_false", condition="not decision"))
    workflow.add_edge(WorkflowEdge(source_id="branch_true", target_id="end"))
    workflow.add_edge(WorkflowEdge(source_id="branch_false", target_id="end"))
    
    return workflow


# ═══════════════════════════════════════════════════════════════════════════════
# ExecutionController Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestExecutionController:
    """Tests for ExecutionController."""
    
    @pytest.fixture
    def controller(self):
        """Create controller with mock dependencies."""
        exec_repo = MockExecutionRepository()
        workflow_repo = MockWorkflowRepository()
        state_adapter = InMemoryStateAdapter()
        
        return ExecutionController(
            execution_repository=exec_repo,
            workflow_repository=workflow_repo,
            state_adapter=state_adapter,
        ), exec_repo, workflow_repo, state_adapter
    
    def test_create_execution(self, controller):
        """Test creating a new execution."""
        ctrl, exec_repo, workflow_repo, _ = controller
        
        workflow = create_simple_workflow()
        workflow_repo.add(workflow)
        
        execution = ctrl.create_execution(workflow)
        
        assert execution.workflow_id == workflow.id
        assert execution.status == ExecutionStatus.PENDING
        assert execution.state.current_node_id == "start"
        assert execution.session_id is not None
        assert exec_repo.exists(execution.id)
    
    def test_create_execution_with_initial_state(self, controller):
        """Test creating execution with initial variables."""
        ctrl, _, workflow_repo, _ = controller
        
        workflow = create_simple_workflow()
        workflow_repo.add(workflow)
        
        execution = ctrl.create_execution(
            workflow,
            initial_state={"x": 10, "y": 20}
        )
        
        assert execution.state.workflow_variables["x"] == 10
        assert execution.state.workflow_variables["y"] == 20
    
    def test_create_execution_with_breakpoints(self, controller):
        """Test creating execution with breakpoints."""
        ctrl, _, workflow_repo, _ = controller
        
        workflow = create_simple_workflow()
        workflow_repo.add(workflow)
        
        execution = ctrl.create_execution(
            workflow,
            breakpoints=["action"]
        )
        
        assert "action" in execution.breakpoints
    
    def test_create_execution_invalid_workflow(self, controller):
        """Test that invalid workflow raises error."""
        ctrl, _, _, _ = controller
        
        workflow = TestWorkflow(name="Invalid")  # No nodes or entry point
        
        with pytest.raises(ValueError, match="Invalid workflow"):
            ctrl.create_execution(workflow)
    
    def test_run_execution_completes(self, controller):
        """Test running a simple workflow to completion."""
        ctrl, _, workflow_repo, _ = controller
        
        workflow = create_simple_workflow()
        workflow_repo.add(workflow)
        
        execution = ctrl.create_execution(workflow)
        completed = ctrl.run(execution.id)
        
        assert completed.status == ExecutionStatus.COMPLETED
        assert "start" in completed.state.execution_path
        assert "action" in completed.state.execution_path
        assert "end" in completed.state.execution_path
    
    def test_run_execution_stops_at_breakpoint(self, controller):
        """Test that execution pauses at breakpoints."""
        ctrl, _, workflow_repo, _ = controller
        
        workflow = create_simple_workflow()
        workflow_repo.add(workflow)
        
        execution = ctrl.create_execution(workflow, breakpoints=["action"])
        paused = ctrl.run(execution.id)
        
        assert paused.status == ExecutionStatus.PAUSED
        assert paused.state.current_node_id == "action"
        assert "start" in paused.state.execution_path
        assert "action" not in paused.state.execution_path  # Not yet executed
    
    def test_resume_after_breakpoint(self, controller):
        """Test resuming execution after breakpoint."""
        ctrl, _, workflow_repo, _ = controller
        
        workflow = create_simple_workflow()
        workflow_repo.add(workflow)
        
        execution = ctrl.create_execution(workflow, breakpoints=["action"])
        paused = ctrl.run(execution.id)
        
        # Breakpoint is auto-removed after triggering (one-shot semantic)
        assert "action" not in paused.breakpoints, "Breakpoint should be removed after triggering"
        
        # Resume - breakpoint won't trigger again since it was already removed
        completed = ctrl.resume(paused.id)
        
        assert completed.status == ExecutionStatus.COMPLETED
    
    def test_pause_and_resume(self, controller):
        """Test manual pause and resume."""
        ctrl, exec_repo, workflow_repo, _ = controller
        
        workflow = create_simple_workflow()
        workflow_repo.add(workflow)
        
        execution = ctrl.create_execution(workflow)
        
        # Start and immediately pause (difficult to test mid-execution)
        # For this test, we'll verify pause/resume state transitions
        execution.start()
        execution.pause()
        exec_repo.update(execution)
        
        resumed = ctrl.resume(execution.id)
        
        assert resumed.status == ExecutionStatus.COMPLETED
    
    def test_stop_execution(self, controller):
        """Test stopping/canceling execution."""
        ctrl, _, workflow_repo, _ = controller
        
        workflow = create_simple_workflow()
        workflow_repo.add(workflow)
        
        execution = ctrl.create_execution(workflow)
        stopped = ctrl.stop(execution.id)
        
        assert stopped.status == ExecutionStatus.CANCELLED
        assert stopped.is_terminal()
    
    def test_get_state(self, controller):
        """Test getting current execution state."""
        ctrl, _, workflow_repo, _ = controller
        
        workflow = create_simple_workflow()
        workflow_repo.add(workflow)
        
        execution = ctrl.create_execution(workflow, initial_state={"key": "value"})
        
        state = ctrl.get_state(execution.id)
        
        assert state.current_node_id == "start"
        assert state.workflow_variables["key"] == "value"
    
    def test_get_status(self, controller):
        """Test getting execution status."""
        ctrl, _, workflow_repo, _ = controller
        
        workflow = create_simple_workflow()
        workflow_repo.add(workflow)
        
        execution = ctrl.create_execution(workflow)
        
        status = ctrl.get_status(execution.id)
        
        assert status.id == execution.id
        assert status.status == ExecutionStatus.PENDING
    
    def test_rollback(self, controller):
        """Test rolling back to a checkpoint."""
        ctrl, _, workflow_repo, state_adapter = controller
        
        workflow = create_simple_workflow()
        workflow_repo.add(workflow)
        
        execution = ctrl.create_execution(workflow, breakpoints=["action"])
        ctrl.run(execution.id)  # Pauses at action
        
        # Get checkpoint before action
        session_id = execution.session_id
        checkpoints = state_adapter.get_checkpoints(session_id)
        
        # Remove breakpoint and run to completion
        execution.breakpoints.clear()
        ctrl.run(execution.id)
        
        # Rollback to first checkpoint
        if checkpoints:
            restored = ctrl.rollback(execution.id, checkpoints[0].id)
            
            assert restored.status == ExecutionStatus.PAUSED
            assert restored.state.current_node_id == "start"
    
    def test_execution_not_found(self, controller):
        """Test handling missing execution."""
        ctrl, _, _, _ = controller
        
        with pytest.raises(ValueError, match="not found"):
            ctrl.run("nonexistent_id")
    
    def test_workflow_not_found(self, controller):
        """Test handling missing workflow."""
        ctrl, exec_repo, _, state_adapter = controller
        
        # Manually create an execution pointing to missing workflow
        execution = Execution(workflow_id="missing_workflow")
        session_id = state_adapter.initialize_session(
            execution.id,
            ExecutionState(current_node_id="start")
        )
        execution.session_id = session_id
        exec_repo.add(execution)
        
        with pytest.raises(ValueError, match="not found"):
            ctrl.run(execution.id)


class TestDefaultNodeExecutor:
    """Tests for DefaultNodeExecutor."""
    
    @pytest.fixture
    def executor(self):
        return DefaultNodeExecutor()
    
    def test_execute_start_node(self, executor):
        """Test executing a start node."""
        node = WorkflowNode(id="start", name="Start", type="start")
        
        result = executor.execute(node, {})
        
        assert result.success
        assert result.output["started"]
    
    def test_execute_end_node(self, executor):
        """Test executing an end node."""
        node = WorkflowNode(id="end", name="End", type="end")
        
        result = executor.execute(node, {})
        
        assert result.success
        assert result.output["completed"]
    
    def test_execute_action_node(self, executor):
        """Test executing an action node."""
        node = WorkflowNode(
            id="action",
            name="Process",
            type="action",
            tool_name="processor",
            config={"option": "fast"}
        )
        
        result = executor.execute(node, {"x": 10})
        
        assert result.success
        assert result.output["node_id"] == "action"
        assert result.output["option"] == "fast"
        assert result.tool_invocations == 1
    
    def test_execute_decision_node_true(self, executor):
        """Test executing a decision node with true condition."""
        node = WorkflowNode(
            id="decision",
            name="Check",
            type="decision",
            config={"condition": "x > 5"}
        )
        
        result = executor.execute(node, {"x": 10})
        
        assert result.success
        assert result.output["decision"] is True
    
    def test_execute_decision_node_false(self, executor):
        """Test executing a decision node with false condition."""
        node = WorkflowNode(
            id="decision",
            name="Check",
            type="decision",
            config={"condition": "x > 5"}
        )
        
        result = executor.execute(node, {"x": 2})
        
        assert result.success
        assert result.output["decision"] is False
    
    def test_can_execute(self, executor):
        """Test checking if executor supports node type."""
        action = WorkflowNode(id="a", name="A", type="action")
        unknown = WorkflowNode(id="u", name="U", type="custom_type")
        
        assert executor.can_execute(action)
        assert not executor.can_execute(unknown)
    
    def test_get_supported_types(self, executor):
        """Test getting supported node types."""
        types = executor.get_supported_node_types()
        
        assert "action" in types
        assert "start" in types
        assert "end" in types
        assert "decision" in types

