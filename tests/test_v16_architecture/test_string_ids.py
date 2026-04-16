"""
Unit Tests for v1.6 Architecture Refactoring.

Tests verify:
- All IDs are strings (UUIDs)
- Session concept preserved (maps to thread_id)
- IStateAdapter interface compliance
- Execution model field renaming (session_id, checkpoint_id)
"""

import pytest
import uuid
from datetime import datetime

from wtb.domain.interfaces.state_adapter import (
    IStateAdapter,
    CheckpointTrigger,
    CheckpointInfo,
    NodeBoundaryInfo,
)
from wtb.domain.models.workflow import (
    Execution,
    ExecutionState,
    ExecutionStatus,
    TestWorkflow,
    WorkflowNode,
)
from wtb.infrastructure.adapters import InMemoryStateAdapter


class TestExecutionModelStringIDs:
    """Test that Execution model uses string IDs."""
    
    def test_execution_has_string_session_id(self):
        """Execution should have session_id as Optional[str]."""
        execution = Execution()
        execution.session_id = "wtb-test-session"
        
        assert execution.session_id == "wtb-test-session"
        assert isinstance(execution.session_id, str)
    
    def test_execution_has_string_checkpoint_id(self):
        """Execution should have checkpoint_id as Optional[str]."""
        execution = Execution()
        checkpoint_uuid = str(uuid.uuid4())
        execution.checkpoint_id = checkpoint_uuid
        
        assert execution.checkpoint_id == checkpoint_uuid
        assert isinstance(execution.checkpoint_id, str)
    
    def test_execution_to_dict_uses_new_field_names(self):
        """Execution.to_dict() should use session_id and checkpoint_id."""
        execution = Execution(
            session_id="wtb-test-123",
            checkpoint_id="abc-def-ghi",
        )
        
        data = execution.to_dict()
        
        assert "session_id" in data
        assert "checkpoint_id" in data
        assert data["session_id"] == "wtb-test-123"
        assert data["checkpoint_id"] == "abc-def-ghi"
        
        # Old field names should NOT be present
        assert "agentgit_session_id" not in data
        assert "agentgit_checkpoint_id" not in data
    
    def test_execution_from_dict_supports_new_field_names(self):
        """Execution.from_dict() should parse new field names."""
        data = {
            "id": str(uuid.uuid4()),
            "workflow_id": "test-workflow",
            "status": "pending",
            "session_id": "wtb-session-456",
            "checkpoint_id": "checkpoint-uuid-789",
        }
        
        execution = Execution.from_dict(data)
        
        assert execution.session_id == "wtb-session-456"
        assert execution.checkpoint_id == "checkpoint-uuid-789"
    
    def test_execution_from_dict_supports_legacy_field_names(self):
        """Execution.from_dict() should handle legacy agentgit_* fields."""
        data = {
            "id": str(uuid.uuid4()),
            "workflow_id": "test-workflow",
            "status": "pending",
            "agentgit_session_id": 12345,  # Legacy int
            "agentgit_checkpoint_id": 67890,  # Legacy int
        }
        
        execution = Execution.from_dict(data)
        
        # Should convert to string
        assert execution.session_id == "12345"
        assert execution.checkpoint_id == "67890"


class TestInMemoryStateAdapterStringIDs:
    """Test InMemoryStateAdapter uses string IDs."""
    
    @pytest.fixture
    def adapter(self):
        """Create fresh adapter for each test."""
        return InMemoryStateAdapter()
    
    def test_initialize_session_returns_string(self, adapter):
        """initialize_session() should return string session_id."""
        initial_state = ExecutionState()
        
        session_id = adapter.initialize_session("exec-123", initial_state)
        
        assert isinstance(session_id, str)
        assert "wtb-exec-123" == session_id  # Format: wtb-{execution_id}
    
    def test_get_current_session_id_returns_string(self, adapter):
        """get_current_session_id() should return string."""
        adapter.initialize_session("exec-456", ExecutionState())
        
        session_id = adapter.get_current_session_id()
        
        assert isinstance(session_id, str)
    
    def test_save_checkpoint_returns_string(self, adapter):
        """save_checkpoint() should return string checkpoint_id (UUID)."""
        adapter.initialize_session("exec-789", ExecutionState())
        
        checkpoint_id = adapter.save_checkpoint(
            state=ExecutionState(),
            node_id="test_node",
            trigger=CheckpointTrigger.AUTO,
        )
        
        assert isinstance(checkpoint_id, str)
        # Should be valid UUID
        uuid.UUID(checkpoint_id)  # Raises if invalid
    
    def test_load_checkpoint_accepts_string(self, adapter):
        """load_checkpoint() should accept string checkpoint_id."""
        adapter.initialize_session("exec-abc", ExecutionState())
        
        checkpoint_id = adapter.save_checkpoint(
            state=ExecutionState(current_node_id="node_1"),
            node_id="node_1",
            trigger=CheckpointTrigger.AUTO,
        )
        
        state = adapter.load_checkpoint(checkpoint_id)
        
        assert state.current_node_id == "node_1"
    
    def test_rollback_accepts_string(self, adapter):
        """rollback() should accept string checkpoint_id."""
        adapter.initialize_session("exec-def", ExecutionState())
        
        checkpoint_id = adapter.save_checkpoint(
            state=ExecutionState(current_node_id="node_A"),
            node_id="node_A",
            trigger=CheckpointTrigger.AUTO,
        )
        
        # Advance state
        adapter.save_checkpoint(
            state=ExecutionState(current_node_id="node_B"),
            node_id="node_B",
            trigger=CheckpointTrigger.AUTO,
        )
        
        # Rollback
        state = adapter.rollback(checkpoint_id)
        
        assert state.current_node_id == "node_A"
    
    def test_mark_node_started_accepts_string_returns_string(self, adapter):
        """mark_node_started() should accept/return string IDs."""
        adapter.initialize_session("exec-ghi", ExecutionState())
        
        checkpoint_id = adapter.save_checkpoint(
            state=ExecutionState(),
            node_id="node_1",
            trigger=CheckpointTrigger.AUTO,
        )
        
        boundary_id = adapter.mark_node_started("node_1", checkpoint_id)
        
        assert isinstance(boundary_id, str)
    
    def test_mark_node_completed_accepts_string(self, adapter):
        """mark_node_completed() should accept string checkpoint_id."""
        adapter.initialize_session("exec-jkl", ExecutionState())
        
        entry_cp = adapter.save_checkpoint(
            state=ExecutionState(),
            node_id="node_1",
            trigger=CheckpointTrigger.NODE_START,
        )
        adapter.mark_node_started("node_1", entry_cp)
        
        exit_cp = adapter.save_checkpoint(
            state=ExecutionState(),
            node_id="node_1",
            trigger=CheckpointTrigger.NODE_END,
        )
        
        result = adapter.mark_node_completed("node_1", exit_cp)
        
        assert result is True
    
    def test_get_checkpoints_uses_string_session_id(self, adapter):
        """get_checkpoints() should accept string session_id."""
        session_id = adapter.initialize_session("exec-mno", ExecutionState())
        
        adapter.save_checkpoint(
            state=ExecutionState(),
            node_id="node_1",
            trigger=CheckpointTrigger.AUTO,
        )
        
        checkpoints = adapter.get_checkpoints(session_id)
        
        assert len(checkpoints) == 1
        assert isinstance(checkpoints[0].id, str)
    
    def test_get_node_boundaries_uses_string_session_id(self, adapter):
        """get_node_boundaries() should accept string session_id."""
        session_id = adapter.initialize_session("exec-pqr", ExecutionState())
        
        cp_id = adapter.save_checkpoint(
            state=ExecutionState(),
            node_id="node_1",
            trigger=CheckpointTrigger.AUTO,
        )
        adapter.mark_node_started("node_1", cp_id)
        
        boundaries = adapter.get_node_boundaries(session_id)
        
        assert len(boundaries) == 1
        assert isinstance(boundaries[0].id, str)
        assert isinstance(boundaries[0].entry_checkpoint_id, str)


class TestCheckpointInfoStringIDs:
    """Test CheckpointInfo dataclass uses string IDs."""
    
    def test_checkpoint_info_has_string_id(self):
        """CheckpointInfo.id should be str."""
        info = CheckpointInfo(
            id="uuid-checkpoint-id",
            name="Test Checkpoint",
            node_id="node_1",
            step=1,
            trigger_type=CheckpointTrigger.AUTO,
            created_at=datetime.now().isoformat(),
            is_auto=True,
        )
        
        assert isinstance(info.id, str)
        assert info.id == "uuid-checkpoint-id"


class TestNodeBoundaryInfoStringIDs:
    """Test NodeBoundaryInfo dataclass uses string IDs."""
    
    def test_node_boundary_info_has_string_ids(self):
        """NodeBoundaryInfo should have string IDs."""
        info = NodeBoundaryInfo(
            id="boundary-uuid",
            node_id="node_1",
            entry_checkpoint_id="entry-uuid",
            exit_checkpoint_id="exit-uuid",
            node_status="completed",
            started_at=datetime.now().isoformat(),
            completed_at=datetime.now().isoformat(),
        )
        
        assert isinstance(info.id, str)
        assert isinstance(info.entry_checkpoint_id, str)
        assert isinstance(info.exit_checkpoint_id, str)


class TestSessionConcept:
    """Test that Session domain concept is preserved."""
    
    def test_session_maps_to_thread_id_format(self):
        """Session ID should follow wtb-{execution_id} format."""
        adapter = InMemoryStateAdapter()
        execution_id = "my-execution-123"
        
        session_id = adapter.initialize_session(execution_id, ExecutionState())
        
        # Session ID = thread_id in LangGraph format
        assert session_id == f"wtb-{execution_id}"
    
    def test_session_can_be_reconstructed_from_execution_id(self):
        """Session should be reconstructable from execution_id within same adapter.
        
        Note: Cross-instance reconstruction requires a persistent adapter 
        (e.g., LangGraphStateAdapter with SQLite). InMemoryStateAdapter only
        supports within-instance reconstruction.
        """
        adapter = InMemoryStateAdapter()
        execution_id = "reconstruct-test"
        
        # Initialize session
        session_id = adapter.initialize_session(execution_id, ExecutionState())
        expected_session = f"wtb-{execution_id}"
        assert session_id == expected_session
        
        # Clear current session (simulating a different code path accessing the adapter)
        adapter._current_session_id = None
        adapter._current_execution_id = None
        
        # Should be able to restore session using execution_id within same adapter
        result = adapter.set_current_session(
            session_id=session_id,
            execution_id=execution_id,
        )
        
        assert result is True
        assert adapter.get_current_session_id() == expected_session
