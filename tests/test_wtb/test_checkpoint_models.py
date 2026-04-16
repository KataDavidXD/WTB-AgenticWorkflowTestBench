"""
Unit tests for Domain Checkpoint Models.

Tests for:
- CheckpointId value object
- Checkpoint entity
- ExecutionHistory aggregate root

These tests verify DDD compliance and domain logic correctness.
"""

import pytest
from datetime import datetime, timedelta
from typing import Dict, Any

from wtb.domain.models.checkpoint import (
    CheckpointId,
    Checkpoint,
    ExecutionHistory,
    CheckpointNotFoundError,
    InvalidRollbackTargetError,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Test Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def sample_checkpoint_id() -> CheckpointId:
    """Sample CheckpointId for testing."""
    return CheckpointId("cp-001")


@pytest.fixture
def sample_checkpoint() -> Checkpoint:
    """Sample Checkpoint for testing."""
    return Checkpoint(
        id=CheckpointId("cp-001"),
        execution_id="exec-001",
        step=1,
        node_writes={"node_a": {"result": "success"}},
        next_nodes=["node_b"],
        state_values={"value": 42},
        created_at=datetime.now(),
    )


@pytest.fixture
def sample_history() -> ExecutionHistory:
    """Sample ExecutionHistory with multiple checkpoints."""
    now = datetime.now()
    
    checkpoints = [
        Checkpoint(
            id=CheckpointId("cp-001"),
            execution_id="exec-001",
            step=0,
            node_writes={},  # Initial checkpoint
            next_nodes=["node_a"],
            state_values={"input": "data"},
            created_at=now,
        ),
        Checkpoint(
            id=CheckpointId("cp-002"),
            execution_id="exec-001",
            step=1,
            node_writes={"node_a": {"result": "processed"}},
            next_nodes=["node_b"],
            state_values={"input": "data", "node_a_output": "processed"},
            created_at=now + timedelta(seconds=1),
        ),
        Checkpoint(
            id=CheckpointId("cp-003"),
            execution_id="exec-001",
            step=2,
            node_writes={"node_b": {"result": "analyzed"}},
            next_nodes=["node_c"],
            state_values={"input": "data", "node_a_output": "processed", "node_b_output": "analyzed"},
            created_at=now + timedelta(seconds=2),
        ),
        Checkpoint(
            id=CheckpointId("cp-004"),
            execution_id="exec-001",
            step=3,
            node_writes={"node_c": {"result": "final"}},
            next_nodes=[],  # Terminal
            state_values={"input": "data", "final_result": "complete"},
            created_at=now + timedelta(seconds=3),
        ),
    ]
    
    return ExecutionHistory(execution_id="exec-001", checkpoints=checkpoints)


# ═══════════════════════════════════════════════════════════════════════════════
# CheckpointId Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckpointId:
    """Tests for CheckpointId value object."""
    
    def test_creation(self):
        """Test creating a CheckpointId."""
        cp_id = CheckpointId("test-id")
        assert cp_id.value == "test-id"
    
    def test_string_representation(self):
        """Test string representation."""
        cp_id = CheckpointId("test-id")
        assert str(cp_id) == "test-id"
    
    def test_equality_same_value(self):
        """Test equality with same value."""
        cp_id1 = CheckpointId("test-id")
        cp_id2 = CheckpointId("test-id")
        assert cp_id1 == cp_id2
    
    def test_equality_different_value(self):
        """Test inequality with different values."""
        cp_id1 = CheckpointId("test-id-1")
        cp_id2 = CheckpointId("test-id-2")
        assert cp_id1 != cp_id2
    
    def test_equality_with_string(self):
        """Test equality comparison with string."""
        cp_id = CheckpointId("test-id")
        assert cp_id == "test-id"
    
    def test_hash_consistency(self):
        """Test that same values produce same hash."""
        cp_id1 = CheckpointId("test-id")
        cp_id2 = CheckpointId("test-id")
        assert hash(cp_id1) == hash(cp_id2)
    
    def test_usable_as_dict_key(self):
        """Test that CheckpointId can be used as dict key."""
        cp_id = CheckpointId("test-id")
        data = {cp_id: "value"}
        assert data[cp_id] == "value"
        assert data[CheckpointId("test-id")] == "value"
    
    def test_immutability(self):
        """Test that CheckpointId is immutable."""
        cp_id = CheckpointId("test-id")
        with pytest.raises(AttributeError):
            cp_id.value = "new-id"
    
    def test_empty_value_raises(self):
        """Test that empty value raises ValueError."""
        with pytest.raises(ValueError):
            CheckpointId("")


# ═══════════════════════════════════════════════════════════════════════════════
# Checkpoint Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckpoint:
    """Tests for Checkpoint entity."""
    
    def test_creation(self, sample_checkpoint: Checkpoint):
        """Test creating a Checkpoint."""
        cp = sample_checkpoint
        assert cp.id == CheckpointId("cp-001")
        assert cp.execution_id == "exec-001"
        assert cp.step == 1
        assert "node_a" in cp.node_writes
    
    def test_completed_node(self, sample_checkpoint: Checkpoint):
        """Test completed_node property."""
        cp = sample_checkpoint
        assert cp.completed_node == "node_a"
    
    def test_completed_node_empty_writes(self):
        """Test completed_node with empty writes."""
        cp = Checkpoint(
            id=CheckpointId("cp-001"),
            execution_id="exec-001",
            step=0,
            node_writes={},
            next_nodes=["node_a"],
            state_values={},
            created_at=datetime.now(),
        )
        assert cp.completed_node is None
    
    def test_is_terminal_true(self):
        """Test is_terminal when no next nodes."""
        cp = Checkpoint(
            id=CheckpointId("cp-001"),
            execution_id="exec-001",
            step=3,
            node_writes={"final": {}},
            next_nodes=[],
            state_values={},
            created_at=datetime.now(),
        )
        assert cp.is_terminal is True
    
    def test_is_terminal_false(self, sample_checkpoint: Checkpoint):
        """Test is_terminal when there are next nodes."""
        assert sample_checkpoint.is_terminal is False
    
    def test_is_node_completion_true(self, sample_checkpoint: Checkpoint):
        """Test is_node_completion with writes."""
        assert sample_checkpoint.is_node_completion is True
    
    def test_is_node_completion_false(self):
        """Test is_node_completion without writes."""
        cp = Checkpoint(
            id=CheckpointId("cp-001"),
            execution_id="exec-001",
            step=0,
            node_writes={},
            next_nodes=["node_a"],
            state_values={},
            created_at=datetime.now(),
        )
        assert cp.is_node_completion is False
    
    def test_has_node_in_next(self, sample_checkpoint: Checkpoint):
        """Test has_node_in_next."""
        assert sample_checkpoint.has_node_in_next("node_b") is True
        assert sample_checkpoint.has_node_in_next("node_a") is False
    
    def test_has_node_in_writes(self, sample_checkpoint: Checkpoint):
        """Test has_node_in_writes."""
        assert sample_checkpoint.has_node_in_writes("node_a") is True
        assert sample_checkpoint.has_node_in_writes("node_b") is False
    
    def test_get_node_output(self, sample_checkpoint: Checkpoint):
        """Test get_node_output."""
        output = sample_checkpoint.get_node_output("node_a")
        assert output == {"result": "success"}
        assert sample_checkpoint.get_node_output("node_b") is None
    
    def test_to_dict(self, sample_checkpoint: Checkpoint):
        """Test serialization to dict."""
        data = sample_checkpoint.to_dict()
        assert data["id"] == "cp-001"
        assert data["execution_id"] == "exec-001"
        assert data["step"] == 1
        assert "node_writes" in data
    
    def test_from_dict(self, sample_checkpoint: Checkpoint):
        """Test deserialization from dict."""
        data = sample_checkpoint.to_dict()
        restored = Checkpoint.from_dict(data)
        
        assert restored.id == sample_checkpoint.id
        assert restored.execution_id == sample_checkpoint.execution_id
        assert restored.step == sample_checkpoint.step
    
    def test_immutability(self, sample_checkpoint: Checkpoint):
        """Test that Checkpoint is immutable."""
        with pytest.raises(AttributeError):
            sample_checkpoint.step = 999


# ═══════════════════════════════════════════════════════════════════════════════
# ExecutionHistory Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestExecutionHistory:
    """Tests for ExecutionHistory aggregate root."""
    
    def test_creation(self, sample_history: ExecutionHistory):
        """Test creating ExecutionHistory."""
        assert sample_history.execution_id == "exec-001"
        assert len(sample_history.checkpoints) == 4
    
    def test_get_checkpoint(self, sample_history: ExecutionHistory):
        """Test getting checkpoint by ID."""
        cp = sample_history.get_checkpoint(CheckpointId("cp-002"))
        assert cp is not None
        assert cp.step == 1
    
    def test_get_checkpoint_not_found(self, sample_history: ExecutionHistory):
        """Test getting non-existent checkpoint."""
        cp = sample_history.get_checkpoint(CheckpointId("nonexistent"))
        assert cp is None
    
    def test_get_checkpoint_by_id_string(self, sample_history: ExecutionHistory):
        """Test getting checkpoint by string ID."""
        cp = sample_history.get_checkpoint_by_id("cp-002")
        assert cp is not None
        assert cp.step == 1
    
    def test_get_latest_checkpoint(self, sample_history: ExecutionHistory):
        """Test getting latest checkpoint."""
        cp = sample_history.get_latest_checkpoint()
        assert cp is not None
        assert cp.id == CheckpointId("cp-004")
        assert cp.step == 3
    
    def test_get_latest_checkpoint_empty(self):
        """Test getting latest from empty history."""
        history = ExecutionHistory(execution_id="exec-001", checkpoints=[])
        assert history.get_latest_checkpoint() is None
    
    def test_get_checkpoint_for_node(self, sample_history: ExecutionHistory):
        """Test getting checkpoint where node completed."""
        cp = sample_history.get_checkpoint_for_node("node_b")
        assert cp is not None
        assert cp.id == CheckpointId("cp-003")
    
    def test_get_checkpoint_for_node_not_found(self, sample_history: ExecutionHistory):
        """Test getting checkpoint for non-executed node."""
        cp = sample_history.get_checkpoint_for_node("nonexistent")
        assert cp is None
    
    def test_get_completed_nodes(self, sample_history: ExecutionHistory):
        """Test getting list of completed nodes."""
        nodes = sample_history.get_completed_nodes()
        assert nodes == ["node_a", "node_b", "node_c"]
    
    def test_get_unique_nodes(self, sample_history: ExecutionHistory):
        """Test getting all unique nodes."""
        nodes = set(sample_history.get_unique_nodes())
        assert "node_a" in nodes
        assert "node_b" in nodes
        assert "node_c" in nodes
    
    def test_is_node_completed_true(self, sample_history: ExecutionHistory):
        """Test checking if node is completed."""
        assert sample_history.is_node_completed("node_a") is True
        assert sample_history.is_node_completed("node_b") is True
    
    def test_is_node_completed_false(self, sample_history: ExecutionHistory):
        """Test checking if node is not completed."""
        assert sample_history.is_node_completed("nonexistent") is False
    
    def test_get_rollback_target(self, sample_history: ExecutionHistory):
        """Test getting rollback target for node."""
        target = sample_history.get_rollback_target("node_a")
        assert target is not None
        assert target.id == CheckpointId("cp-002")
    
    def test_get_rollback_targets(self, sample_history: ExecutionHistory):
        """Test getting all rollback targets."""
        targets = sample_history.get_rollback_targets()
        # Should be 3 node completions (not including initial checkpoint)
        assert len(targets) == 3
        # Most recent first
        assert targets[0].id == CheckpointId("cp-004")
    
    def test_can_rollback_to_valid(self, sample_history: ExecutionHistory):
        """Test can_rollback_to for valid target."""
        assert sample_history.can_rollback_to(CheckpointId("cp-002")) is True
    
    def test_can_rollback_to_invalid(self, sample_history: ExecutionHistory):
        """Test can_rollback_to for initial checkpoint (no writes)."""
        assert sample_history.can_rollback_to(CheckpointId("cp-001")) is False
    
    def test_get_nodes_to_revert(self, sample_history: ExecutionHistory):
        """Test getting nodes that would be reverted."""
        from_cp = CheckpointId("cp-004")
        to_cp = CheckpointId("cp-002")
        
        reverted = sample_history.get_nodes_to_revert(from_cp, to_cp)
        assert "node_b" in reverted
        assert "node_c" in reverted
        assert "node_a" not in reverted
    
    def test_get_execution_path(self, sample_history: ExecutionHistory):
        """Test getting execution path."""
        path = sample_history.get_execution_path()
        assert path == ["node_a", "node_b", "node_c"]
    
    def test_iteration(self, sample_history: ExecutionHistory):
        """Test iterating over history."""
        steps = [cp.step for cp in sample_history]
        assert steps == [0, 1, 2, 3]  # Should be in order
    
    def test_len(self, sample_history: ExecutionHistory):
        """Test length of history."""
        assert len(sample_history) == 4
    
    def test_bool_with_checkpoints(self, sample_history: ExecutionHistory):
        """Test truthiness with checkpoints."""
        assert bool(sample_history) is True
    
    def test_bool_empty(self):
        """Test truthiness when empty."""
        history = ExecutionHistory(execution_id="exec-001", checkpoints=[])
        assert bool(history) is False
    
    def test_add_checkpoint(self, sample_history: ExecutionHistory):
        """Test adding a checkpoint."""
        new_cp = Checkpoint(
            id=CheckpointId("cp-005"),
            execution_id="exec-001",
            step=4,
            node_writes={"final_node": {}},
            next_nodes=[],
            state_values={},
            created_at=datetime.now(),
        )
        
        sample_history.add_checkpoint(new_cp)
        assert len(sample_history) == 5
    
    def test_add_checkpoint_wrong_execution(self, sample_history: ExecutionHistory):
        """Test adding checkpoint with wrong execution_id."""
        wrong_cp = Checkpoint(
            id=CheckpointId("cp-999"),
            execution_id="wrong-exec",
            step=99,
            node_writes={},
            next_nodes=[],
            state_values={},
            created_at=datetime.now(),
        )
        
        with pytest.raises(ValueError):
            sample_history.add_checkpoint(wrong_cp)
    
    def test_to_dict(self, sample_history: ExecutionHistory):
        """Test serialization to dict."""
        data = sample_history.to_dict()
        assert data["execution_id"] == "exec-001"
        assert len(data["checkpoints"]) == 4
        assert data["completed_nodes"] == ["node_a", "node_b", "node_c"]
    
    def test_from_dict(self, sample_history: ExecutionHistory):
        """Test deserialization from dict."""
        data = sample_history.to_dict()
        restored = ExecutionHistory.from_dict(data)
        
        assert restored.execution_id == sample_history.execution_id
        assert len(restored.checkpoints) == len(sample_history.checkpoints)
    
    def test_get_node_entry_checkpoint(self, sample_history: ExecutionHistory):
        """Test getting node entry checkpoint."""
        entry = sample_history.get_node_entry_checkpoint("node_a")
        assert entry is not None
        assert entry.id == CheckpointId("cp-001")  # First checkpoint has node_a in next
    
    def test_get_checkpoints_by_step_range(self, sample_history: ExecutionHistory):
        """Test getting checkpoints in step range."""
        cps = sample_history.get_checkpoints_by_step_range(1, 2)
        assert len(cps) == 2
        assert cps[0].step == 1
        assert cps[1].step == 2


# ═══════════════════════════════════════════════════════════════════════════════
# Domain Exception Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestDomainExceptions:
    """Tests for domain exceptions."""
    
    def test_checkpoint_not_found_error(self):
        """Test CheckpointNotFoundError."""
        cp_id = CheckpointId("missing-id")
        error = CheckpointNotFoundError(cp_id)
        
        assert error.checkpoint_id == cp_id
        assert "missing-id" in str(error)
    
    def test_invalid_rollback_target_error(self):
        """Test InvalidRollbackTargetError."""
        cp_id = CheckpointId("invalid-target")
        error = InvalidRollbackTargetError(cp_id, "No node writes")
        
        assert error.checkpoint_id == cp_id
        assert error.reason == "No node writes"
        assert "invalid-target" in str(error)
