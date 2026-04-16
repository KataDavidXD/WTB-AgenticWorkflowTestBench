"""
Integration Tests for ExecutionController.fork() method.

Tests verify:
- Fork creates new Execution with new session
- Fork preserves checkpoint state
- Fork is ACID compliant
- Fork metadata tracks source
"""

import pytest
import uuid

from wtb.domain.models.workflow import (
    Execution,
    ExecutionState,
    ExecutionStatus,
    TestWorkflow,
    WorkflowNode,
    WorkflowEdge,
)
from wtb.infrastructure.adapters import InMemoryStateAdapter
from wtb.infrastructure.database import InMemoryUnitOfWork
from wtb.application.services.execution_controller import ExecutionController


class TestExecutionControllerFork:
    """Test ExecutionController.fork() method."""
    
    @pytest.fixture
    def controller(self):
        """Create ExecutionController with in-memory dependencies."""
        uow = InMemoryUnitOfWork()
        uow.__enter__()
        adapter = InMemoryStateAdapter()
        
        controller = ExecutionController(
            execution_repository=uow.executions,
            workflow_repository=uow.workflows,
            state_adapter=adapter,
            unit_of_work=uow,
        )
        
        # Register a test workflow
        workflow = TestWorkflow(
            id="test-workflow-1",
            name="Test Workflow",
        )
        workflow.add_node(WorkflowNode(id="start", name="Start", type="start"))
        workflow.add_node(WorkflowNode(id="process", name="Process", type="action"))
        workflow.add_node(WorkflowNode(id="end", name="End", type="end"))
        workflow.add_edge(WorkflowEdge(source_id="start", target_id="process"))
        workflow.add_edge(WorkflowEdge(source_id="process", target_id="end"))
        workflow.entry_point = "start"
        
        uow.workflows.add(workflow)
        uow.commit()
        
        return controller
    
    def test_fork_creates_new_execution(self, controller):
        """Fork should create a new Execution with unique ID."""
        # Create source execution
        workflow = controller._workflow_repo.get("test-workflow-1")
        source_exec = controller.create_execution(
            workflow=workflow,
            initial_state={"key": "value"},
        )
        
        # Save a checkpoint
        checkpoint_id = controller._state_adapter.save_checkpoint(
            state=source_exec.state,
            node_id="start",
            trigger=controller._state_adapter.__class__.__bases__[0].__subclasses__()[0] if hasattr(controller._state_adapter.__class__, '__bases__') else None,
        )
        # Use the checkpoint trigger properly
        from wtb.domain.interfaces.state_adapter import CheckpointTrigger
        checkpoint_id = controller._state_adapter.save_checkpoint(
            state=source_exec.state,
            node_id="start",
            trigger=CheckpointTrigger.AUTO,
        )
        
        # Fork
        forked_exec = controller.fork(source_exec.id, checkpoint_id)
        
        # Verify new execution
        assert forked_exec.id != source_exec.id
        assert isinstance(forked_exec.id, str)
        assert len(forked_exec.id) == 36  # UUID length
    
    def test_fork_preserves_workflow_id(self, controller):
        """Forked execution should have same workflow_id."""
        workflow = controller._workflow_repo.get("test-workflow-1")
        source_exec = controller.create_execution(workflow=workflow)
        
        from wtb.domain.interfaces.state_adapter import CheckpointTrigger
        checkpoint_id = controller._state_adapter.save_checkpoint(
            state=source_exec.state,
            node_id="start",
            trigger=CheckpointTrigger.AUTO,
        )
        
        forked_exec = controller.fork(source_exec.id, checkpoint_id)
        
        assert forked_exec.workflow_id == source_exec.workflow_id
    
    def test_fork_has_pending_status(self, controller):
        """Forked execution should start in PENDING status."""
        workflow = controller._workflow_repo.get("test-workflow-1")
        source_exec = controller.create_execution(workflow=workflow)
        
        from wtb.domain.interfaces.state_adapter import CheckpointTrigger
        checkpoint_id = controller._state_adapter.save_checkpoint(
            state=source_exec.state,
            node_id="start",
            trigger=CheckpointTrigger.AUTO,
        )
        
        forked_exec = controller.fork(source_exec.id, checkpoint_id)
        
        assert forked_exec.status == ExecutionStatus.PENDING
    
    def test_fork_has_new_session_id(self, controller):
        """Forked execution should have new session_id."""
        workflow = controller._workflow_repo.get("test-workflow-1")
        source_exec = controller.create_execution(workflow=workflow)
        
        from wtb.domain.interfaces.state_adapter import CheckpointTrigger
        checkpoint_id = controller._state_adapter.save_checkpoint(
            state=source_exec.state,
            node_id="start",
            trigger=CheckpointTrigger.AUTO,
        )
        
        forked_exec = controller.fork(source_exec.id, checkpoint_id)
        
        assert forked_exec.session_id is not None
        assert forked_exec.session_id != source_exec.session_id
    
    def test_fork_preserves_state_from_checkpoint(self, controller):
        """Forked execution should have state from checkpoint."""
        workflow = controller._workflow_repo.get("test-workflow-1")
        source_exec = controller.create_execution(
            workflow=workflow,
            initial_state={"original_key": "original_value"},
        )
        
        from wtb.domain.interfaces.state_adapter import CheckpointTrigger
        checkpoint_id = controller._state_adapter.save_checkpoint(
            state=source_exec.state,
            node_id="start",
            trigger=CheckpointTrigger.AUTO,
        )
        
        forked_exec = controller.fork(source_exec.id, checkpoint_id)
        
        assert "original_key" in forked_exec.state.workflow_variables
        assert forked_exec.state.workflow_variables["original_key"] == "original_value"
    
    def test_fork_merges_new_initial_state(self, controller):
        """Fork with new_initial_state should merge with checkpoint state."""
        workflow = controller._workflow_repo.get("test-workflow-1")
        source_exec = controller.create_execution(
            workflow=workflow,
            initial_state={"base_key": "base_value"},
        )
        
        from wtb.domain.interfaces.state_adapter import CheckpointTrigger
        checkpoint_id = controller._state_adapter.save_checkpoint(
            state=source_exec.state,
            node_id="start",
            trigger=CheckpointTrigger.AUTO,
        )
        
        forked_exec = controller.fork(
            source_exec.id, 
            checkpoint_id,
            new_initial_state={"new_key": "new_value"},
        )
        
        # Should have both base and new keys
        assert "base_key" in forked_exec.state.workflow_variables
        assert "new_key" in forked_exec.state.workflow_variables
    
    def test_fork_tracks_source_in_metadata(self, controller):
        """Forked execution should track source in metadata."""
        workflow = controller._workflow_repo.get("test-workflow-1")
        source_exec = controller.create_execution(workflow=workflow)
        
        from wtb.domain.interfaces.state_adapter import CheckpointTrigger
        checkpoint_id = controller._state_adapter.save_checkpoint(
            state=source_exec.state,
            node_id="start",
            trigger=CheckpointTrigger.AUTO,
        )
        
        forked_exec = controller.fork(source_exec.id, checkpoint_id)
        
        assert forked_exec.metadata.get("forked_from") == source_exec.id
        assert forked_exec.metadata.get("source_checkpoint_id") == checkpoint_id
        assert forked_exec.metadata.get("fork_type") == "checkpoint_fork"
    
    def test_fork_is_persisted(self, controller):
        """Forked execution should be persisted to repository."""
        workflow = controller._workflow_repo.get("test-workflow-1")
        source_exec = controller.create_execution(workflow=workflow)
        
        from wtb.domain.interfaces.state_adapter import CheckpointTrigger
        checkpoint_id = controller._state_adapter.save_checkpoint(
            state=source_exec.state,
            node_id="start",
            trigger=CheckpointTrigger.AUTO,
        )
        
        forked_exec = controller.fork(source_exec.id, checkpoint_id)
        
        # Should be retrievable
        retrieved = controller._exec_repo.get(forked_exec.id)
        assert retrieved is not None
        assert retrieved.id == forked_exec.id
    
    def test_fork_raises_for_nonexistent_execution(self, controller):
        """Fork should raise ValueError for nonexistent source."""
        with pytest.raises(ValueError, match="not found"):
            controller.fork("nonexistent-id", "some-checkpoint")
    
    def test_fork_raises_for_nonexistent_checkpoint(self, controller):
        """Fork should raise ValueError for nonexistent checkpoint."""
        workflow = controller._workflow_repo.get("test-workflow-1")
        source_exec = controller.create_execution(workflow=workflow)
        
        with pytest.raises(ValueError, match="not found"):
            controller.fork(source_exec.id, "nonexistent-checkpoint")


class TestForkACIDCompliance:
    """Test fork() ACID compliance."""
    
    @pytest.fixture
    def controller(self):
        """Create controller for ACID tests."""
        uow = InMemoryUnitOfWork()
        uow.__enter__()
        adapter = InMemoryStateAdapter()
        
        controller = ExecutionController(
            execution_repository=uow.executions,
            workflow_repository=uow.workflows,
            state_adapter=adapter,
            unit_of_work=uow,
        )
        
        workflow = TestWorkflow(id="acid-workflow", name="ACID Test")
        workflow.add_node(WorkflowNode(id="start", name="Start", type="start"))
        workflow.entry_point = "start"
        uow.workflows.add(workflow)
        uow.commit()
        
        return controller
    
    def test_fork_atomicity_success(self, controller):
        """Successful fork should be atomic (all or nothing)."""
        workflow = controller._workflow_repo.get("acid-workflow")
        source = controller.create_execution(workflow=workflow)
        
        from wtb.domain.interfaces.state_adapter import CheckpointTrigger
        cp_id = controller._state_adapter.save_checkpoint(
            state=source.state,
            node_id="start",
            trigger=CheckpointTrigger.AUTO,
        )
        
        forked = controller.fork(source.id, cp_id)
        
        # Both execution and session should exist
        assert controller._exec_repo.get(forked.id) is not None
        assert forked.session_id is not None
    
    def test_fork_isolation_independent_sessions(self, controller):
        """Forked execution should have isolated session."""
        workflow = controller._workflow_repo.get("acid-workflow")
        source = controller.create_execution(
            workflow=workflow,
            initial_state={"counter": 0},
        )
        
        from wtb.domain.interfaces.state_adapter import CheckpointTrigger
        cp_id = controller._state_adapter.save_checkpoint(
            state=source.state,
            node_id="start",
            trigger=CheckpointTrigger.AUTO,
        )
        
        forked = controller.fork(source.id, cp_id)
        
        # Modify forked state
        forked.state.workflow_variables["counter"] = 100
        
        # Source should be unchanged
        original = controller._exec_repo.get(source.id)
        assert original.state.workflow_variables.get("counter", 0) == 0
