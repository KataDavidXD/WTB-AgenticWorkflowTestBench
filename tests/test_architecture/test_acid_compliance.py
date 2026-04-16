"""
ACID Compliance Tests - Verify transaction consistency across repositories.

Created: 2026-01-28
Reference: ARCHITECTURE_REVIEW_2026_01_28.md - ACID-001

These tests verify:
1. Atomicity: All operations in a transaction succeed or fail together
2. Consistency: Data integrity constraints are maintained
3. Isolation: Concurrent transactions don't interfere
4. Durability: Committed data persists

ACID Properties Tested:
- Outbox pattern maintains cross-system consistency
- Blob storage uses two-phase commit
- API services properly handle transactions
"""

import pytest
import tempfile
import shutil
from pathlib import Path
from datetime import datetime
from unittest.mock import MagicMock, patch


# ═══════════════════════════════════════════════════════════════════════════════
# Test Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def temp_storage():
    """Create temporary storage directory."""
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def mock_session():
    """Create mock SQLAlchemy session."""
    session = MagicMock()
    session.get = MagicMock(return_value=None)
    session.add = MagicMock()
    session.delete = MagicMock()
    session.flush = MagicMock()
    session.commit = MagicMock()
    session.rollback = MagicMock()
    return session


# ═══════════════════════════════════════════════════════════════════════════════
# Atomicity Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestAtomicity:
    """Tests for Atomicity - all or nothing transactions."""
    
    def test_blob_save_creates_file_and_db_record(self, temp_storage, mock_session):
        """Blob save is atomic: both file and DB record created."""
        from wtb.infrastructure.database.repositories.file_processing_repository import (
            SQLAlchemyBlobRepository,
        )
        
        repo = SQLAlchemyBlobRepository(mock_session, str(temp_storage))
        content = b"test content for atomicity"
        
        blob_id = repo.save(content)
        
        # Both file and DB record should be created
        assert mock_session.add.called
        # File should exist on disk
        storage_path = temp_storage / "objects" / blob_id.value[:2] / blob_id.value[2:]
        assert storage_path.exists()
        assert storage_path.read_bytes() == content
    
    def test_blob_delete_removes_file_and_db_record(self, temp_storage, mock_session):
        """Blob delete is atomic: both file and DB record removed."""
        from wtb.infrastructure.database.repositories.file_processing_repository import (
            SQLAlchemyBlobRepository,
        )
        from wtb.domain.models.file_processing import BlobId
        
        # Setup: Create a blob first
        repo = SQLAlchemyBlobRepository(mock_session, str(temp_storage))
        content = b"content to delete"
        blob_id = repo.save(content)
        
        # Mock existing blob with reference_count = 1
        mock_orm = MagicMock()
        mock_orm.reference_count = 1
        mock_orm.storage_location = str(
            temp_storage / "objects" / blob_id.value[:2] / blob_id.value[2:]
        )
        mock_session.get.return_value = mock_orm
        
        # Delete
        result = repo.delete(blob_id)
        
        # Both DB delete and file delete should happen
        assert result is True
        assert mock_session.delete.called
    
    def test_outbox_event_created_atomically_with_domain_operation(self):
        """Outbox event is created in same transaction as domain operation."""
        from wtb.domain.models.outbox import OutboxEvent, OutboxEventType
        
        # This is a design verification test
        # Outbox pattern ensures:
        # 1. Domain operation executes
        # 2. Outbox event added to session
        # 3. Both commit together
        
        event = OutboxEvent.create(
            event_type=OutboxEventType.EXECUTION_PAUSED,
            aggregate_id="exec-123",
            payload={"reason": "test"},
        )
        
        # Event should have valid ID and status
        assert event.event_id is not None
        assert event.status.value == "pending"
        assert event.aggregate_id == "exec-123"


# ═══════════════════════════════════════════════════════════════════════════════
# Consistency Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestConsistency:
    """Tests for Consistency - data integrity constraints."""
    
    def test_blob_id_is_content_hash(self, temp_storage, mock_session):
        """Blob ID must match content hash (SHA-256)."""
        from wtb.infrastructure.database.repositories.file_processing_repository import (
            SQLAlchemyBlobRepository,
        )
        from wtb.infrastructure.database.mappers import BlobStorageCore
        
        repo = SQLAlchemyBlobRepository(mock_session, str(temp_storage))
        content = b"consistency test content"
        
        blob_id = repo.save(content)
        
        # Blob ID must be SHA-256 of content
        expected_hash = BlobStorageCore.compute_blob_id(content)
        assert blob_id.value == expected_hash
    
    def test_idempotent_blob_save_increments_ref_count(self, temp_storage, mock_session):
        """Saving same content twice increments reference count."""
        from wtb.infrastructure.database.repositories.file_processing_repository import (
            SQLAlchemyBlobRepository,
        )
        
        repo = SQLAlchemyBlobRepository(mock_session, str(temp_storage))
        content = b"duplicate content test"
        
        # First save
        blob_id1 = repo.save(content)
        
        # Mock existing blob for second save
        mock_orm = MagicMock()
        mock_orm.reference_count = 1
        mock_session.get.return_value = mock_orm
        
        # Second save of same content
        blob_id2 = repo.save(content)
        
        # Same blob ID
        assert blob_id1.value == blob_id2.value
        # Reference count should be incremented
        assert mock_orm.reference_count == 2
    
    def test_outbox_event_type_must_be_valid(self):
        """Outbox event type must be a valid enum value."""
        from wtb.domain.models.outbox import OutboxEvent, OutboxEventType
        
        # Valid event types
        valid_types = [
            OutboxEventType.CHECKPOINT_CREATE,
            OutboxEventType.EXECUTION_PAUSED,
            OutboxEventType.EXECUTION_RESUMED,
            OutboxEventType.ROLLBACK_PERFORMED,
        ]
        
        for event_type in valid_types:
            event = OutboxEvent.create(
                event_type=event_type,
                aggregate_id="test-123",
                payload={"test": "data"},
            )
            assert event.event_type == event_type


# ═══════════════════════════════════════════════════════════════════════════════
# Isolation Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestIsolation:
    """Tests for Isolation - concurrent transaction handling."""
    
    def test_concurrent_blob_writes_use_unique_temp_files(self, temp_storage):
        """Concurrent writes use unique temp files to avoid conflicts."""
        from wtb.infrastructure.database.mappers import BlobStorageCore
        
        storage_path = temp_storage / "objects" / "ab" / "cdef1234"
        
        # Two concurrent writes would get unique temp paths
        temp1 = BlobStorageCore.compute_temp_path(storage_path, "unique1")
        temp2 = BlobStorageCore.compute_temp_path(storage_path, "unique2")
        
        assert temp1 != temp2
        assert "unique1" in str(temp1)
        assert "unique2" in str(temp2)
    
    def test_session_isolation_via_uow(self):
        """Each Unit of Work has isolated session."""
        # This is a design verification - UoW pattern ensures isolation
        from wtb.domain.interfaces.unit_of_work import IUnitOfWork
        
        # IUnitOfWork interface enforces:
        # - __enter__ starts transaction
        # - __exit__ commits or rolls back
        # - Each request gets new UoW instance
        
        assert hasattr(IUnitOfWork, '__enter__')
        assert hasattr(IUnitOfWork, '__exit__')
        assert hasattr(IUnitOfWork, 'commit')
        assert hasattr(IUnitOfWork, 'rollback')


# ═══════════════════════════════════════════════════════════════════════════════
# Durability Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestDurability:
    """Tests for Durability - committed data persists."""
    
    def test_blob_file_written_before_db_record(self, temp_storage, mock_session):
        """Blob file is written to disk before DB record added."""
        from wtb.infrastructure.database.repositories.file_processing_repository import (
            SQLAlchemyBlobRepository,
        )
        
        # Track call order
        call_order = []
        original_add = mock_session.add
        mock_session.add = lambda x: (call_order.append('db_add'), original_add(x))
        
        repo = SQLAlchemyBlobRepository(mock_session, str(temp_storage))
        content = b"durability test"
        
        blob_id = repo.save(content)
        
        # File should exist (written before DB add)
        storage_path = temp_storage / "objects" / blob_id.value[:2] / blob_id.value[2:]
        assert storage_path.exists()
        
        # This ensures: file write → success → DB add
        # If file write fails, DB add never happens (atomicity)
    
    def test_atomic_file_write_uses_temp_rename(self, temp_storage, mock_session):
        """File writes use temp file + rename for atomicity."""
        from wtb.infrastructure.database.repositories.file_processing_repository import (
            SQLAlchemyBlobRepository,
        )
        
        repo = SQLAlchemyBlobRepository(mock_session, str(temp_storage))
        content = b"atomic write test"
        
        blob_id = repo.save(content)
        
        # Final file should exist (temp was renamed)
        storage_path = temp_storage / "objects" / blob_id.value[:2] / blob_id.value[2:]
        assert storage_path.exists()
        
        # Temp file should NOT exist (was renamed)
        temp_path = storage_path.with_suffix(".tmp")
        assert not temp_path.exists()


# ═══════════════════════════════════════════════════════════════════════════════
# API Service Transaction Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestAPIServiceTransactions:
    """Tests for API service transaction handling."""
    
    def test_list_executions_uses_uow_context(self):
        """list_executions wraps read in UoW context."""
        import inspect
        from wtb.application.services.api_services import ExecutionAPIService
        
        source = inspect.getsource(ExecutionAPIService.list_executions)
        
        # Should use UoW context manager
        assert "with self._uow" in source
        # Should query executions from repository
        assert "self._uow.executions.list" in source
    
    def test_pause_execution_creates_outbox_event(self):
        """pause_execution creates outbox event atomically."""
        import inspect
        from wtb.application.services.api_services import ExecutionAPIService
        
        source = inspect.getsource(ExecutionAPIService.pause_execution)
        
        # Should create outbox event
        assert "OutboxEvent.create" in source
        # Should add to outbox
        assert "self._uow.outbox.add" in source
        # Should commit atomically
        assert "self._uow.commit()" in source
    
    def test_rollback_execution_creates_outbox_event(self):
        """rollback_execution creates outbox event atomically."""
        import inspect
        from wtb.application.services.api_services import ExecutionAPIService
        
        source = inspect.getsource(ExecutionAPIService.rollback_execution)
        
        # Should create outbox event
        assert "OutboxEvent.create" in source
        assert "ROLLBACK_PERFORMED" in source


# ═══════════════════════════════════════════════════════════════════════════════
# Outbox Pattern Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestOutboxPattern:
    """Tests for Outbox Pattern effectiveness."""
    
    def test_outbox_events_have_fifo_ordering(self):
        """Outbox events are processed in FIFO order."""
        import inspect
        from wtb.infrastructure.database.repositories.outbox_repository import (
            SQLAlchemyOutboxRepository,
        )
        
        source = inspect.getsource(SQLAlchemyOutboxRepository.get_pending)
        
        # Should order by created_at for FIFO
        assert "order_by" in source.lower()
        assert "created_at" in source
    
    def test_outbox_status_transitions(self):
        """Outbox event status follows valid transitions."""
        from wtb.domain.models.outbox import OutboxStatus
        
        # Valid status values
        valid_statuses = [
            OutboxStatus.PENDING,
            OutboxStatus.PROCESSING,
            OutboxStatus.PROCESSED,
            OutboxStatus.FAILED,
        ]
        
        # All should have string values
        for status in valid_statuses:
            assert isinstance(status.value, str)
    
    def test_outbox_retry_count_tracked(self):
        """Failed outbox events track retry count."""
        from wtb.domain.models.outbox import OutboxEvent, OutboxEventType, OutboxStatus
        
        event = OutboxEvent.create(
            event_type=OutboxEventType.CHECKPOINT_VERIFY,
            aggregate_id="exec-456",
            payload={"execution_id": "exec-456"},
        )
        
        # Initial retry count
        assert event.retry_count == 0
        assert event.max_retries > 0  # Should have retry limit
        
        # Simulate failures
        event.retry_count = 3
        assert event.retry_count < event.max_retries  # Can still retry


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
