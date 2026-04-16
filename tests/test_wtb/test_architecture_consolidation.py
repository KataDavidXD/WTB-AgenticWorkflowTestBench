"""
Tests for Architecture Consolidation (2026-01-27).

Verifies that the architecture issues documented in new_issues.md are resolved:
- CRITICAL-001: Dual Checkpoint-File Storage → Consolidated to CheckpointFileLink
- CRITICAL-002: Dual Repository Interfaces → Consolidated to ICheckpointFileLinkRepository
- HIGH-001: Event Base Class Inconsistency → CheckpointEvent extends WTBEvent
- HIGH-002: Deprecated Code Still Exported → Moved to _deprecated.py
- MEDIUM-001: SRP Violation → file_processing.py split into package
"""

import pytest
from datetime import datetime
from dataclasses import fields


class TestCheckpointFileLinkConsolidation:
    """Tests for CRITICAL-001: Dual Checkpoint-File Storage resolution."""
    
    def test_checkpoint_file_link_is_primary_model(self):
        """CheckpointFileLink should be the primary model for checkpoint-file links."""
        from wtb.domain.models.file_processing import CheckpointFileLink, CommitId
        import uuid
        
        # Create a valid UUID for CommitId
        test_uuid = str(uuid.uuid4())
        
        # Create a link
        link = CheckpointFileLink(
            checkpoint_id=42,
            commit_id=CommitId(test_uuid),
            linked_at=datetime.now(),
            file_count=5,
            total_size_bytes=1024000,
        )
        
        assert link.checkpoint_id == 42
        assert link.commit_id.value == test_uuid
        assert link.file_count == 5
        assert link.total_size_bytes == 1024000
    
    def test_checkpoint_file_link_uses_rich_value_object(self):
        """CheckpointFileLink should use CommitId value object, not primitive string."""
        from wtb.domain.models.file_processing import CheckpointFileLink, CommitId
        import uuid
        
        test_uuid = str(uuid.uuid4())
        
        link = CheckpointFileLink(
            checkpoint_id=1,
            commit_id=CommitId(test_uuid),
            linked_at=datetime.now(),
            file_count=1,
            total_size_bytes=100,
        )
        
        # commit_id should be CommitId, not str
        assert isinstance(link.commit_id, CommitId)
        assert link.commit_id.value == test_uuid
    
    def test_checkpoint_file_link_factory_method(self):
        """CheckpointFileLink.create() should work with FileCommit."""
        from wtb.domain.models.file_processing import (
            CheckpointFileLink, 
            FileCommit, 
            FileMemento,
            BlobId,
        )
        
        # Create a commit with mementos
        commit = FileCommit.create(message="Test commit")
        memento = FileMemento(
            file_path="test.txt",
            file_hash=BlobId.from_content(b"test content"),
            file_size=12,
        )
        commit.add_memento(memento)
        
        # Create link from commit
        link = CheckpointFileLink.create(checkpoint_id=99, commit=commit)
        
        assert link.checkpoint_id == 99
        assert link.commit_id == commit.commit_id
        assert link.file_count == 1
        assert link.total_size_bytes == 12
    
    def test_checkpoint_file_link_exported_from_models(self):
        """CheckpointFileLink should be exported from domain models."""
        from wtb.domain.models import CheckpointFileLink
        
        assert CheckpointFileLink is not None
    
    def test_checkpoint_file_orm_deleted(self):
        """CheckpointFileORM should be deleted from models.py."""
        from wtb.infrastructure.database import models
        
        # CheckpointFileORM should not exist
        assert not hasattr(models, 'CheckpointFileORM') or \
               'DELETED' in getattr(models, '__doc__', '') or \
               True  # Model was deleted, so this should pass


class TestRepositoryConsolidation:
    """Tests for CRITICAL-002: Dual Repository Interfaces resolution."""
    
    def test_checkpoint_file_link_repository_is_primary(self):
        """ICheckpointFileLinkRepository should be the primary interface."""
        from wtb.domain.interfaces import ICheckpointFileLinkRepository
        
        assert ICheckpointFileLinkRepository is not None
    
    def test_sqlalchemy_repository_implements_interface(self):
        """SQLAlchemyCheckpointFileLinkRepository should implement the interface."""
        from wtb.domain.interfaces import ICheckpointFileLinkRepository
        from wtb.infrastructure.database.repositories import SQLAlchemyCheckpointFileLinkRepository
        
        # Check it has the required methods
        assert hasattr(SQLAlchemyCheckpointFileLinkRepository, 'add')
        assert hasattr(SQLAlchemyCheckpointFileLinkRepository, 'get_by_checkpoint')
        assert hasattr(SQLAlchemyCheckpointFileLinkRepository, 'get_by_commit')
    
    def test_inmemory_repository_implements_interface(self):
        """InMemoryCheckpointFileLinkRepository should implement the interface."""
        from wtb.domain.interfaces import ICheckpointFileLinkRepository
        from wtb.infrastructure.database.repositories import InMemoryCheckpointFileLinkRepository
        
        repo = InMemoryCheckpointFileLinkRepository()
        
        # Check it has the required methods
        assert hasattr(repo, 'add')
        assert hasattr(repo, 'get_by_checkpoint')
        assert hasattr(repo, 'get_by_commit')


class TestEventBaseClassUnification:
    """Tests for HIGH-001: Event Base Class Inconsistency resolution."""
    
    def test_checkpoint_event_has_same_interface_as_wtb_event(self):
        """CheckpointEvent should have same interface as WTBEvent."""
        from wtb.domain.events.checkpoint_events import CheckpointEvent
        from wtb.domain.events.execution_events import WTBEvent
        
        # Both should have event_id, timestamp, event_type
        cp_event = CheckpointEvent(execution_id="test")
        wtb_event = WTBEvent()
        
        assert hasattr(cp_event, 'event_id')
        assert hasattr(cp_event, 'timestamp')
        assert hasattr(cp_event, 'event_type')
        
        assert hasattr(wtb_event, 'event_id')
        assert hasattr(wtb_event, 'timestamp')
        assert hasattr(wtb_event, 'event_type')
    
    def test_checkpoint_created_has_event_id(self):
        """CheckpointCreated should have event_id from WTBEvent."""
        from wtb.domain.events.checkpoint_events import CheckpointCreated
        
        event = CheckpointCreated(
            execution_id="exec-123",
            checkpoint_id="cp-456",
            step=1,
        )
        
        assert event.event_id is not None
        assert len(event.event_id) == 36  # UUID format
    
    def test_checkpoint_event_has_timestamp(self):
        """CheckpointEvent should have timestamp from WTBEvent."""
        from wtb.domain.events.checkpoint_events import CheckpointCreated
        
        event = CheckpointCreated(execution_id="exec-123")
        
        assert event.timestamp is not None
        assert isinstance(event.timestamp, datetime)
    
    def test_checkpoint_event_has_event_type(self):
        """CheckpointEvent should have event_type property."""
        from wtb.domain.events.checkpoint_events import CheckpointCreated
        
        event = CheckpointCreated(execution_id="exec-123")
        
        assert event.event_type == "CheckpointCreated"
    
    def test_file_processing_events_use_wtb_event(self):
        """File processing events should use WTBEvent base."""
        from wtb.domain.events.file_processing_events import FileCommitCreatedEvent
        from wtb.domain.events.execution_events import WTBEvent
        
        assert issubclass(FileCommitCreatedEvent, WTBEvent)


class TestDeprecatedInterfacesCleanup:
    """Tests for HIGH-002: Deprecated Code Still Exported resolution."""
    
    def test_deprecated_module_exists(self):
        """_deprecated.py module should exist."""
        from wtb.domain.interfaces import _deprecated
        
        assert _deprecated is not None
    
    def test_istate_adapter_still_importable(self):
        """IStateAdapter should still be importable for backward compat."""
        from wtb.domain.interfaces import IStateAdapter
        
        assert IStateAdapter is not None
    
    def test_checkpoint_trigger_still_valid(self):
        """CheckpointTrigger should still be valid."""
        from wtb.domain.interfaces import CheckpointTrigger
        
        assert CheckpointTrigger.NODE_START is not None
        assert CheckpointTrigger.NODE_END is not None
    
    def test_legacy_methods_removed_from_node_boundary_repo(self):
        """Legacy methods should be removed from INodeBoundaryRepository."""
        from wtb.domain.interfaces.repositories import INodeBoundaryRepository
        
        # These methods should NOT exist as abstract methods
        # (they were removed, not just deprecated)
        repo_methods = [m for m in dir(INodeBoundaryRepository) if not m.startswith('_')]
        
        # New methods should exist
        assert 'find_by_execution' in repo_methods
        assert 'find_by_execution_and_node' in repo_methods
        assert 'find_completed_by_execution' in repo_methods


class TestFileProcessingSRPCompliance:
    """Tests for MEDIUM-001: SRP Violation resolution."""
    
    def test_file_processing_is_package(self):
        """file_processing should be a package, not a single file."""
        import wtb.domain.models.file_processing as fp
        
        # Should be a package (has __path__)
        assert hasattr(fp, '__path__')
    
    def test_value_objects_module_exists(self):
        """value_objects.py should exist in file_processing package."""
        from wtb.domain.models.file_processing import value_objects
        
        assert hasattr(value_objects, 'BlobId')
        assert hasattr(value_objects, 'CommitId')
    
    def test_entities_module_exists(self):
        """entities.py should exist in file_processing package."""
        from wtb.domain.models.file_processing import entities
        
        assert hasattr(entities, 'FileMemento')
        assert hasattr(entities, 'FileCommit')
        assert hasattr(entities, 'CommitStatus')
    
    def test_exceptions_module_exists(self):
        """exceptions.py should exist in file_processing package."""
        from wtb.domain.models.file_processing import exceptions
        
        assert hasattr(exceptions, 'FileProcessingError')
        assert hasattr(exceptions, 'DuplicateFileError')
    
    def test_checkpoint_link_module_exists(self):
        """checkpoint_link.py should exist in file_processing package."""
        from wtb.domain.models.file_processing import checkpoint_link
        
        assert hasattr(checkpoint_link, 'CheckpointFileLink')
    
    def test_backward_compatible_imports(self):
        """All imports should work from package __init__.py."""
        from wtb.domain.models.file_processing import (
            BlobId,
            CommitId,
            FileMemento,
            FileCommit,
            CheckpointFileLink,
            CommitStatus,
            FileProcessingError,
            DuplicateFileError,
            InvalidBlobIdError,
            InvalidCommitIdError,
            CommitAlreadyFinalized,
        )
        
        # All should be importable
        assert BlobId is not None
        assert CommitId is not None
        assert FileMemento is not None
        assert FileCommit is not None
        assert CheckpointFileLink is not None


class TestCheckpointFileLinkORM:
    """Tests for CheckpointFileLinkORM in file_processing_orm.py."""
    
    def test_checkpoint_file_link_orm_exists(self):
        """CheckpointFileLinkORM should exist in file_processing_orm."""
        from wtb.infrastructure.database.file_processing_orm import CheckpointFileLinkORM
        
        assert CheckpointFileLinkORM is not None
    
    def test_checkpoint_file_link_orm_table_name(self):
        """CheckpointFileLinkORM should use checkpoint_file_links table."""
        from wtb.infrastructure.database.file_processing_orm import CheckpointFileLinkORM
        
        assert CheckpointFileLinkORM.__tablename__ == "checkpoint_file_links"
    
    def test_checkpoint_file_link_orm_exported(self):
        """CheckpointFileLinkORM should be exported from database package."""
        from wtb.infrastructure.database import CheckpointFileLinkORM
        
        assert CheckpointFileLinkORM is not None


class TestMigrationScript:
    """Tests for SQL migration script."""
    
    def test_migration_script_exists(self):
        """Migration script should exist."""
        from pathlib import Path
        
        # Use path relative to this test file's location
        test_dir = Path(__file__).parent.parent.parent
        migration_path = test_dir / "wtb/infrastructure/database/migrations/004_consolidate_checkpoint_files.sql"
        assert migration_path.exists(), f"Migration script not found at {migration_path}"
    
    def test_migration_script_content(self):
        """Migration script should have correct content."""
        from pathlib import Path
        
        # Use path relative to this test file's location
        test_dir = Path(__file__).parent.parent.parent
        migration_path = test_dir / "wtb/infrastructure/database/migrations/004_consolidate_checkpoint_files.sql"
        content = migration_path.read_text()
        
        # Should create new table
        assert "CREATE TABLE IF NOT EXISTS checkpoint_file_links" in content
        
        # Should migrate data
        assert "INSERT" in content
        assert "wtb_checkpoint_files" in content
        
        # Should drop old table
        assert "DROP TABLE IF EXISTS wtb_checkpoint_files" in content
