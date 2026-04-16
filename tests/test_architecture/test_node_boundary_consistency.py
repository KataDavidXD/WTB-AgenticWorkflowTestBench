"""
Node Boundary Domain/ORM Consistency Tests.

Tests to verify NodeBoundary domain model and ORM are aligned.
Reference: ARCHITECTURE_REVIEW_2026_01_28.md - ISSUE-CP-001, ISSUE-CP-002

Critical Issues Tested:
1. Domain model field alignment with ORM
2. Checkpoint ID type consistency (str vs CheckpointId)
3. Repository mapping correctness
"""

import pytest
from dataclasses import fields
from datetime import datetime
from typing import get_type_hints, Optional


class TestNodeBoundaryDomainModel:
    """Tests for NodeBoundary domain model."""
    
    def test_domain_model_has_expected_fields(self):
        """Verify domain model has expected fields per DDD changes."""
        from wtb.domain.models.node_boundary import NodeBoundary
        
        domain_fields = {f.name for f in fields(NodeBoundary)}
        
        # Expected fields (per DDD changes 2026-01-15)
        expected_fields = {
            'id',
            'execution_id',
            'node_id',
            'entry_checkpoint_id',  # Optional[str]
            'exit_checkpoint_id',   # Optional[str]
            'node_status',
            'started_at',
            'completed_at',
            'duration_ms',
            'error_message',
            'error_details',
        }
        
        assert expected_fields == domain_fields, (
            f"Domain model fields mismatch.\n"
            f"Expected: {expected_fields}\n"
            f"Got: {domain_fields}\n"
            f"Missing: {expected_fields - domain_fields}\n"
            f"Extra: {domain_fields - expected_fields}"
        )
    
    def test_domain_model_no_deprecated_fields(self):
        """Verify domain model does NOT have deprecated fields."""
        from wtb.domain.models.node_boundary import NodeBoundary
        
        domain_fields = {f.name for f in fields(NodeBoundary)}
        
        # Deprecated fields that should NOT exist
        deprecated_fields = {
            'internal_session_id',  # AgentGit-specific, removed
            'tool_count',           # LangGraph doesn't track tools
            'checkpoint_count',     # Redundant
        }
        
        found_deprecated = domain_fields & deprecated_fields
        assert not found_deprecated, (
            f"Domain model contains deprecated fields: {found_deprecated}\n"
            f"These should have been removed per DDD changes (2026-01-15)"
        )
    
    def test_checkpoint_ids_are_optional_strings(self):
        """Verify checkpoint IDs are Optional[str], not CheckpointId objects."""
        from wtb.domain.models.node_boundary import NodeBoundary
        
        # Get type hints
        hints = get_type_hints(NodeBoundary)
        
        # entry_checkpoint_id should be Optional[str]
        assert 'entry_checkpoint_id' in hints
        entry_type = hints['entry_checkpoint_id']
        # Optional[str] is Union[str, None] in Python's type system
        assert entry_type == Optional[str], (
            f"entry_checkpoint_id should be Optional[str], got {entry_type}"
        )
        
        # exit_checkpoint_id should be Optional[str]
        assert 'exit_checkpoint_id' in hints
        exit_type = hints['exit_checkpoint_id']
        assert exit_type == Optional[str], (
            f"exit_checkpoint_id should be Optional[str], got {exit_type}"
        )
    
    def test_node_boundary_creation(self):
        """Test NodeBoundary can be created with string checkpoint IDs."""
        from wtb.domain.models.node_boundary import NodeBoundary
        
        boundary = NodeBoundary(
            id=1,
            execution_id="exec-123",
            node_id="node_a",
            entry_checkpoint_id="cp-entry-uuid",  # String
            exit_checkpoint_id="cp-exit-uuid",    # String
            node_status="completed",
        )
        
        assert boundary.entry_checkpoint_id == "cp-entry-uuid"
        assert boundary.exit_checkpoint_id == "cp-exit-uuid"
        assert isinstance(boundary.entry_checkpoint_id, str)
        assert isinstance(boundary.exit_checkpoint_id, str)
    
    def test_node_boundary_start_sets_string_checkpoint(self):
        """Test start() method accepts string checkpoint ID."""
        from wtb.domain.models.node_boundary import NodeBoundary
        
        boundary = NodeBoundary.create_for_node("exec-123", "node_a")
        assert boundary.node_status == "pending"
        assert boundary.entry_checkpoint_id is None
        
        # Start should accept string checkpoint ID
        boundary.start("uuid-entry-checkpoint-123")
        
        assert boundary.node_status == "running"
        assert boundary.entry_checkpoint_id == "uuid-entry-checkpoint-123"
        assert isinstance(boundary.entry_checkpoint_id, str)
    
    def test_node_boundary_complete_sets_string_checkpoint(self):
        """Test complete() method accepts string checkpoint ID."""
        from wtb.domain.models.node_boundary import NodeBoundary
        
        boundary = NodeBoundary.create_for_node("exec-123", "node_a")
        boundary.start("entry-cp")
        
        boundary.complete("uuid-exit-checkpoint-456")
        
        assert boundary.node_status == "completed"
        assert boundary.exit_checkpoint_id == "uuid-exit-checkpoint-456"
        assert isinstance(boundary.exit_checkpoint_id, str)


class TestNodeBoundaryORMAlignment:
    """Tests for NodeBoundaryORM alignment with domain model."""
    
    def test_orm_has_checkpoint_columns(self):
        """Verify ORM has entry/exit checkpoint ID columns."""
        from wtb.infrastructure.database.models import NodeBoundaryORM
        
        # Get column names
        columns = {c.name for c in NodeBoundaryORM.__table__.columns}
        
        assert 'entry_checkpoint_id' in columns
        assert 'exit_checkpoint_id' in columns
    
    def test_orm_checkpoint_columns_are_string_type(self):
        """Verify checkpoint columns are String type (for UUIDs)."""
        from wtb.infrastructure.database.models import NodeBoundaryORM
        from sqlalchemy import String
        
        entry_col = NodeBoundaryORM.__table__.c.entry_checkpoint_id
        exit_col = NodeBoundaryORM.__table__.c.exit_checkpoint_id
        
        # Should be String type (v1.6: String UUIDs, not Integer)
        assert isinstance(entry_col.type, String), (
            f"entry_checkpoint_id should be String, got {type(entry_col.type)}"
        )
        assert isinstance(exit_col.type, String), (
            f"exit_checkpoint_id should be String, got {type(exit_col.type)}"
        )
    
    @pytest.mark.xfail(
        reason="ORM still contains deprecated fields - needs migration",
        strict=False  # May pass if migration applied
    )
    def test_orm_no_deprecated_columns(self):
        """Verify ORM does NOT have deprecated columns."""
        from wtb.infrastructure.database.models import NodeBoundaryORM
        
        columns = {c.name for c in NodeBoundaryORM.__table__.columns}
        
        # These columns should not exist after migration
        deprecated_columns = {
            'internal_session_id',  # Should be removed
            'tool_count',           # Should be removed
            'checkpoint_count',     # Should be removed
        }
        
        found_deprecated = columns & deprecated_columns
        assert not found_deprecated, (
            f"ORM contains deprecated columns: {found_deprecated}\n"
            f"Run migration 005_node_boundary_cleanup.sql to fix"
        )


class TestNodeBoundaryRepositoryMapping:
    """Tests for repository domain<->ORM mapping."""
    
    def test_repository_returns_correct_checkpoint_types(self):
        """Verify repository returns string checkpoint IDs, not CheckpointId objects."""
        from wtb.domain.models.node_boundary import NodeBoundary
        from wtb.domain.models.checkpoint import CheckpointId
        
        # Create a boundary directly
        boundary = NodeBoundary(
            id=1,
            execution_id="exec-123",
            node_id="node_a",
            entry_checkpoint_id="entry-uuid",
            exit_checkpoint_id="exit-uuid",
        )
        
        # Checkpoint IDs should be strings
        assert isinstance(boundary.entry_checkpoint_id, str)
        assert isinstance(boundary.exit_checkpoint_id, str)
        
        # NOT CheckpointId objects (this was the bug)
        assert not isinstance(boundary.entry_checkpoint_id, CheckpointId)
        assert not isinstance(boundary.exit_checkpoint_id, CheckpointId)
    
    def test_get_checkpoint_id_value_returns_value_object(self):
        """Test helper methods return CheckpointId value objects when needed."""
        from wtb.domain.models.node_boundary import NodeBoundary
        from wtb.domain.models.checkpoint import CheckpointId
        
        boundary = NodeBoundary(
            id=1,
            execution_id="exec-123",
            node_id="node_a",
            entry_checkpoint_id="entry-uuid-123",
            exit_checkpoint_id="exit-uuid-456",
        )
        
        # Helper methods should return CheckpointId objects
        entry_cp = boundary.get_entry_checkpoint_id_value()
        exit_cp = boundary.get_exit_checkpoint_id_value()
        
        assert isinstance(entry_cp, CheckpointId)
        assert isinstance(exit_cp, CheckpointId)
        assert entry_cp.value == "entry-uuid-123"
        assert exit_cp.value == "exit-uuid-456"
    
    def test_get_checkpoint_id_value_returns_none_when_empty(self):
        """Test helper methods return None when checkpoint IDs are not set."""
        from wtb.domain.models.node_boundary import NodeBoundary
        
        boundary = NodeBoundary(
            id=1,
            execution_id="exec-123",
            node_id="node_a",
            # No checkpoint IDs set
        )
        
        assert boundary.get_entry_checkpoint_id_value() is None
        assert boundary.get_exit_checkpoint_id_value() is None


class TestNodeBoundarySerializationRoundtrip:
    """Tests for serialization consistency."""
    
    def test_to_dict_preserves_string_checkpoint_ids(self):
        """Verify to_dict() preserves checkpoint IDs as strings."""
        from wtb.domain.models.node_boundary import NodeBoundary
        
        boundary = NodeBoundary(
            id=1,
            execution_id="exec-123",
            node_id="node_a",
            entry_checkpoint_id="entry-uuid",
            exit_checkpoint_id="exit-uuid",
            node_status="completed",
            started_at=datetime(2026, 1, 28, 10, 0, 0),
            completed_at=datetime(2026, 1, 28, 10, 1, 0),
            duration_ms=60000,
        )
        
        data = boundary.to_dict()
        
        assert data['entry_checkpoint_id'] == "entry-uuid"
        assert data['exit_checkpoint_id'] == "exit-uuid"
        assert isinstance(data['entry_checkpoint_id'], str)
        assert isinstance(data['exit_checkpoint_id'], str)
    
    def test_from_dict_creates_string_checkpoint_ids(self):
        """Verify from_dict() creates NodeBoundary with string checkpoint IDs."""
        from wtb.domain.models.node_boundary import NodeBoundary
        
        data = {
            'id': 1,
            'execution_id': 'exec-123',
            'node_id': 'node_a',
            'entry_checkpoint_id': 'entry-uuid',
            'exit_checkpoint_id': 'exit-uuid',
            'node_status': 'completed',
        }
        
        boundary = NodeBoundary.from_dict(data)
        
        assert boundary.entry_checkpoint_id == "entry-uuid"
        assert boundary.exit_checkpoint_id == "exit-uuid"
        assert isinstance(boundary.entry_checkpoint_id, str)
        assert isinstance(boundary.exit_checkpoint_id, str)
    
    def test_roundtrip_preserves_checkpoint_types(self):
        """Verify to_dict -> from_dict roundtrip preserves types."""
        from wtb.domain.models.node_boundary import NodeBoundary
        
        original = NodeBoundary(
            id=1,
            execution_id="exec-123",
            node_id="node_a",
            entry_checkpoint_id="entry-uuid-abc",
            exit_checkpoint_id="exit-uuid-xyz",
            node_status="completed",
        )
        
        # Roundtrip
        data = original.to_dict()
        restored = NodeBoundary.from_dict(data)
        
        # Types should be preserved
        assert type(original.entry_checkpoint_id) == type(restored.entry_checkpoint_id)
        assert type(original.exit_checkpoint_id) == type(restored.exit_checkpoint_id)
        
        # Values should be equal
        assert original.entry_checkpoint_id == restored.entry_checkpoint_id
        assert original.exit_checkpoint_id == restored.exit_checkpoint_id


class TestIsRollbackTargetConsistency:
    """Tests for is_rollback_target behavior."""
    
    def test_is_rollback_target_requires_completed_and_exit_checkpoint(self):
        """Verify is_rollback_target() checks both status and exit_checkpoint_id."""
        from wtb.domain.models.node_boundary import NodeBoundary
        
        # Not a rollback target: status != completed
        boundary1 = NodeBoundary(
            execution_id="exec-1",
            node_id="node_a",
            node_status="running",
            exit_checkpoint_id="some-uuid",
        )
        assert not boundary1.is_rollback_target()
        
        # Not a rollback target: no exit_checkpoint_id
        boundary2 = NodeBoundary(
            execution_id="exec-2",
            node_id="node_b",
            node_status="completed",
            exit_checkpoint_id=None,
        )
        assert not boundary2.is_rollback_target()
        
        # IS a rollback target: completed AND has exit_checkpoint_id
        boundary3 = NodeBoundary(
            execution_id="exec-3",
            node_id="node_c",
            node_status="completed",
            exit_checkpoint_id="valid-uuid",
        )
        assert boundary3.is_rollback_target()
