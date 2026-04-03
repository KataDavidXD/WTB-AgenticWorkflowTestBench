"""
Unit tests for InMemoryStateAdapter.

Tests the in-memory implementation of IStateAdapter interface.
"""

import pytest
from datetime import datetime

from wtb.domain.models import ExecutionState
from wtb.domain.interfaces.state_adapter import CheckpointTrigger
from wtb.infrastructure.adapters.inmemory_state_adapter import InMemoryStateAdapter


class TestInMemoryStateAdapter:
    """Tests for InMemoryStateAdapter."""
    
    @pytest.fixture
    def adapter(self):
        """Create a fresh adapter for each test."""
        return InMemoryStateAdapter()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Session Management Tests
    # ═══════════════════════════════════════════════════════════════════════════
    
    def test_initialize_session(self, adapter):
        """Test initializing a new session."""
        state = ExecutionState(current_node_id="start")
        session_id = adapter.initialize_session("exec_1", state)
        
        # v1.6: Session IDs are now strings (format: "wtb-{execution_id}")
        assert session_id == "wtb-exec_1"
        assert adapter.get_current_session_id() == "wtb-exec_1"
        assert adapter.get_session_count() == 1
    
    def test_initialize_multiple_sessions(self, adapter):
        """Test initializing multiple sessions."""
        state = ExecutionState()
        
        session1 = adapter.initialize_session("exec_1", state)
        session2 = adapter.initialize_session("exec_2", state)
        
        # v1.6: Session IDs are now strings
        assert session1 == "wtb-exec_1"
        assert session2 == "wtb-exec_2"
        assert adapter.get_session_count() == 2
        assert adapter.get_current_session_id() == "wtb-exec_2"  # Last initialized is current
    
    def test_set_current_session(self, adapter):
        """Test switching between sessions."""
        state = ExecutionState()
        session1 = adapter.initialize_session("exec_1", state)
        session2 = adapter.initialize_session("exec_2", state)
        
        assert adapter.get_current_session_id() == session2
        
        result = adapter.set_current_session(session1)
        assert result is True
        assert adapter.get_current_session_id() == session1
    
    def test_set_invalid_session(self, adapter):
        """Test setting an invalid session."""
        # v1.6: Session IDs are strings
        result = adapter.set_current_session("non-existent-session")
        assert result is False
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Checkpoint Operations Tests
    # ═══════════════════════════════════════════════════════════════════════════
    
    def test_save_checkpoint(self, adapter):
        """Test saving a checkpoint."""
        state = ExecutionState(current_node_id="node1")
        adapter.initialize_session("exec_1", state)
        
        cp_id = adapter.save_checkpoint(
            state=state,
            node_id="node1",
            trigger=CheckpointTrigger.AUTO,
            name="test_checkpoint"
        )
        
        # v1.6: Checkpoint IDs are now UUID strings
        assert isinstance(cp_id, str)
        assert len(cp_id) == 36  # UUID format
        assert adapter.get_checkpoint_count() == 1
    
    def test_save_checkpoint_without_session_raises(self, adapter):
        """Test that saving without session raises error."""
        state = ExecutionState()
        
        with pytest.raises(RuntimeError, match="No active session"):
            adapter.save_checkpoint(
                state=state,
                node_id="node1",
                trigger=CheckpointTrigger.AUTO
            )
    
    def test_load_checkpoint(self, adapter):
        """Test loading a checkpoint."""
        state = ExecutionState(
            current_node_id="node1",
            workflow_variables={"x": 42}
        )
        adapter.initialize_session("exec_1", state)
        
        cp_id = adapter.save_checkpoint(
            state=state,
            node_id="node1",
            trigger=CheckpointTrigger.AUTO
        )
        
        loaded_state = adapter.load_checkpoint(cp_id)
        
        assert loaded_state.current_node_id == "node1"
        assert loaded_state.workflow_variables["x"] == 42
    
    def test_load_checkpoint_is_deep_copy(self, adapter):
        """Test that loaded checkpoint is a deep copy."""
        state = ExecutionState(workflow_variables={"x": 1})
        adapter.initialize_session("exec_1", state)
        
        cp_id = adapter.save_checkpoint(
            state=state,
            node_id="node1",
            trigger=CheckpointTrigger.AUTO
        )
        
        loaded = adapter.load_checkpoint(cp_id)
        loaded.workflow_variables["x"] = 999
        
        # Load again - should have original value
        loaded2 = adapter.load_checkpoint(cp_id)
        assert loaded2.workflow_variables["x"] == 1
    
    def test_load_invalid_checkpoint_raises(self, adapter):
        """Test loading invalid checkpoint raises error."""
        # v1.6: Checkpoint IDs are UUID strings
        with pytest.raises(ValueError, match="not found"):
            adapter.load_checkpoint("non-existent-checkpoint-id")
    
    # NOTE: link_file_commit was removed in v1.6 - AgentGit-specific file commit linking
    # Use file_processing module for file tracking instead
    
    def test_get_checkpoints(self, adapter):
        """Test getting checkpoints for a session."""
        state = ExecutionState()
        session_id = adapter.initialize_session("exec_1", state)
        
        adapter.save_checkpoint(state, "node1", CheckpointTrigger.AUTO)
        adapter.save_checkpoint(state, "node2", CheckpointTrigger.AUTO)
        adapter.save_checkpoint(state, "node1", CheckpointTrigger.AUTO)
        
        all_cps = adapter.get_checkpoints(session_id)
        assert len(all_cps) == 3
        
        node1_cps = adapter.get_checkpoints(session_id, node_id="node1")
        assert len(node1_cps) == 2
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Node Boundary Tests
    # ═══════════════════════════════════════════════════════════════════════════
    
    def test_mark_node_started(self, adapter):
        """Test marking a node as started."""
        state = ExecutionState()
        session_id = adapter.initialize_session("exec_1", state)
        
        cp_id = adapter.save_checkpoint(state, "node1", CheckpointTrigger.AUTO)
        boundary_id = adapter.mark_node_started("node1", cp_id)
        
        # v1.6: Boundary IDs are now UUID strings
        assert isinstance(boundary_id, str)
        assert len(boundary_id) == 36  # UUID format
        
        boundaries = adapter.get_node_boundaries(session_id)
        assert len(boundaries) == 1
        assert boundaries[0].node_id == "node1"
        assert boundaries[0].node_status == "started"
    
    def test_mark_node_completed(self, adapter):
        """Test marking a node as completed."""
        state = ExecutionState()
        session_id = adapter.initialize_session("exec_1", state)
        
        entry_cp = adapter.save_checkpoint(state, "node1", CheckpointTrigger.AUTO)
        adapter.mark_node_started("node1", entry_cp)
        
        exit_cp = adapter.save_checkpoint(state, "node1", CheckpointTrigger.AUTO)
        # v1.6: mark_node_completed no longer takes tool_count/checkpoint_count parameters
        result = adapter.mark_node_completed("node1", exit_cp)
        
        assert result is True
        
        boundary = adapter.get_node_boundary(session_id, "node1")
        assert boundary.node_status == "completed"
        assert boundary.exit_checkpoint_id == exit_cp
    
    def test_mark_node_failed(self, adapter):
        """Test marking a node as failed."""
        state = ExecutionState()
        session_id = adapter.initialize_session("exec_1", state)
        
        cp_id = adapter.save_checkpoint(state, "node1", CheckpointTrigger.AUTO)
        adapter.mark_node_started("node1", cp_id)
        
        result = adapter.mark_node_failed("node1", "Something went wrong")
        
        assert result is True
        
        boundary = adapter.get_node_boundary(session_id, "node1")
        assert boundary.node_status == "failed"
    
    def test_get_node_rollback_targets(self, adapter):
        """Test getting rollback targets."""
        state = ExecutionState()
        session_id = adapter.initialize_session("exec_1", state)
        
        # Complete node1
        cp1 = adapter.save_checkpoint(state, "node1", CheckpointTrigger.AUTO)
        adapter.mark_node_started("node1", cp1)
        exit_cp1 = adapter.save_checkpoint(state, "node1", CheckpointTrigger.AUTO)
        adapter.mark_node_completed("node1", exit_cp1)
        
        # Complete node2
        cp2 = adapter.save_checkpoint(state, "node2", CheckpointTrigger.AUTO)
        adapter.mark_node_started("node2", cp2)
        exit_cp2 = adapter.save_checkpoint(state, "node2", CheckpointTrigger.AUTO)
        adapter.mark_node_completed("node2", exit_cp2)
        
        # Start but don't complete node3
        cp3 = adapter.save_checkpoint(state, "node3", CheckpointTrigger.AUTO)
        adapter.mark_node_started("node3", cp3)
        
        targets = adapter.get_node_rollback_targets(session_id)
        
        assert len(targets) == 2
        node_ids = [t.node_id for t in targets]
        assert "node1" in node_ids
        assert "node2" in node_ids
        assert "node3" not in node_ids  # Not completed
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Rollback & Branching Tests
    # ═══════════════════════════════════════════════════════════════════════════
    
    def test_rollback(self, adapter):
        """Test rollback to a checkpoint."""
        state1 = ExecutionState(
            current_node_id="node1",
            workflow_variables={"x": 1}
        )
        adapter.initialize_session("exec_1", state1)
        
        cp1 = adapter.save_checkpoint(state1, "node1", CheckpointTrigger.AUTO)
        
        # Progress to another state
        state2 = ExecutionState(
            current_node_id="node2",
            workflow_variables={"x": 2}
        )
        adapter.save_checkpoint(state2, "node2", CheckpointTrigger.AUTO)
        
        # Rollback to first checkpoint
        restored = adapter.rollback(cp1)
        
        assert restored.current_node_id == "node1"
        assert restored.workflow_variables["x"] == 1
    
    def test_rollback_invalid_checkpoint(self, adapter):
        """Test rollback to invalid checkpoint."""
        # v1.6: Checkpoint IDs are UUID strings
        with pytest.raises(ValueError, match="not found"):
            adapter.rollback("non-existent-checkpoint-id")
    
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Cleanup Tests
    # ═══════════════════════════════════════════════════════════════════════════
    
    def test_cleanup(self, adapter):
        """Test cleanup of old checkpoints."""
        state = ExecutionState()
        session_id = adapter.initialize_session("exec_1", state)
        
        # Create 10 auto checkpoints
        for i in range(10):
            adapter.save_checkpoint(state, f"node{i}", CheckpointTrigger.AUTO)
        
        # Create 1 user checkpoint (should be preserved)
        adapter.save_checkpoint(state, "user_cp", CheckpointTrigger.USER_REQUEST)
        
        assert adapter.get_checkpoint_count() == 11
        
        deleted = adapter.cleanup(session_id, keep_latest=3)
        
        assert deleted == 7  # Kept 3 auto + 1 user = 4
        assert adapter.get_checkpoint_count() == 4
    
    def test_reset(self, adapter):
        """Test resetting all state."""
        state = ExecutionState()
        adapter.initialize_session("exec_1", state)
        adapter.save_checkpoint(state, "node1", CheckpointTrigger.AUTO)
        
        assert adapter.get_checkpoint_count() == 1
        assert adapter.get_session_count() == 1
        
        adapter.reset()
        
        assert adapter.get_checkpoint_count() == 0
        assert adapter.get_session_count() == 0
        assert adapter.get_current_session_id() is None

