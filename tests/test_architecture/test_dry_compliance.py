"""
DRY Compliance Tests - Verify shared mappers are used consistently.

Created: 2026-01-28
Reference: ARCHITECTURE_REVIEW_2026_01_28.md - ISSUE-FS-001, ISSUE-FS-002

These tests verify:
1. BlobStorageCore is used by both sync and async repositories
2. OutboxMapper is used by both sync and async repositories
3. FileCommitMapper/CheckpointFileLinkMapper are used consistently
4. No duplicate conversion logic exists

SOLID Compliance:
- SRP: Each mapper has single responsibility
- DIP: Repositories depend on mapper abstractions
"""

import pytest
import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch


# ═══════════════════════════════════════════════════════════════════════════════
# BlobStorageCore Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBlobStorageCore:
    """Tests for BlobStorageCore shared logic."""
    
    def test_compute_blob_id_sha256(self):
        """Verify SHA-256 hash computation."""
        from wtb.infrastructure.database.mappers import BlobStorageCore
        
        content = b"Hello, World!"
        expected = hashlib.sha256(content).hexdigest()
        
        result = BlobStorageCore.compute_blob_id(content)
        
        assert result == expected
        assert len(result) == 64  # SHA-256 produces 64 hex chars
    
    def test_compute_blob_id_idempotent(self):
        """Same content always produces same ID."""
        from wtb.infrastructure.database.mappers import BlobStorageCore
        
        content = b"test content"
        
        id1 = BlobStorageCore.compute_blob_id(content)
        id2 = BlobStorageCore.compute_blob_id(content)
        
        assert id1 == id2
    
    def test_compute_storage_path_git_like(self):
        """Verify Git-like directory sharding."""
        from wtb.infrastructure.database.mappers import BlobStorageCore
        
        blob_id = "abcdef1234567890" + "0" * 48  # 64 chars
        objects_path = Path("/data/blobs/objects")
        
        result = BlobStorageCore.compute_storage_path(blob_id, objects_path)
        
        # Should be objects/ab/cdef1234...
        assert result == objects_path / "ab" / (blob_id[2:])
    
    def test_compute_directory_path(self):
        """Verify directory path computation."""
        from wtb.infrastructure.database.mappers import BlobStorageCore
        
        blob_id = "xy" + "1" * 62
        objects_path = Path("/data/objects")
        
        result = BlobStorageCore.compute_directory_path(blob_id, objects_path)
        
        assert result == objects_path / "xy"
    
    def test_compute_temp_path_with_suffix(self):
        """Verify temp path with unique suffix."""
        from wtb.infrastructure.database.mappers import BlobStorageCore
        
        storage_path = Path("/data/objects/ab/cdef1234")
        
        result = BlobStorageCore.compute_temp_path(storage_path, "unique123")
        
        assert ".tmp.unique123" in str(result)
    
    def test_validate_blob_id_valid(self):
        """Valid SHA-256 hashes pass validation."""
        from wtb.infrastructure.database.mappers import BlobStorageCore
        
        valid_hash = "a" * 64
        
        assert BlobStorageCore.validate_blob_id(valid_hash) is True
    
    def test_validate_blob_id_invalid_length(self):
        """Invalid length fails validation."""
        from wtb.infrastructure.database.mappers import BlobStorageCore
        
        short_hash = "abc123"
        
        assert BlobStorageCore.validate_blob_id(short_hash) is False
    
    def test_validate_blob_id_invalid_chars(self):
        """Non-hex chars fail validation."""
        from wtb.infrastructure.database.mappers import BlobStorageCore
        
        invalid_hash = "g" * 64  # 'g' is not hex
        
        assert BlobStorageCore.validate_blob_id(invalid_hash) is False
    
    def test_get_short_id(self):
        """Verify short ID extraction."""
        from wtb.infrastructure.database.mappers import BlobStorageCore
        
        blob_id = "abcdef1234567890" + "0" * 48
        
        result = BlobStorageCore.get_short_id(blob_id, 8)
        
        assert result == "abcdef12"
    
    def test_verify_content_matches_id_true(self):
        """Content matching ID returns True."""
        from wtb.infrastructure.database.mappers import BlobStorageCore
        
        content = b"matching content"
        blob_id = BlobStorageCore.compute_blob_id(content)
        
        assert BlobStorageCore.verify_content_matches_id(content, blob_id) is True
    
    def test_verify_content_matches_id_false(self):
        """Mismatched content returns False."""
        from wtb.infrastructure.database.mappers import BlobStorageCore
        
        content = b"actual content"
        wrong_id = BlobStorageCore.compute_blob_id(b"different content")
        
        assert BlobStorageCore.verify_content_matches_id(content, wrong_id) is False
    
    def test_create_orm_dict(self):
        """Verify ORM dict creation."""
        from wtb.infrastructure.database.mappers import BlobStorageCore
        
        result = BlobStorageCore.create_orm_dict(
            blob_id="abc123" + "0" * 58,
            storage_location="/data/objects/ab/c123",
            content_size=1024,
            reference_count=2,
        )
        
        assert result["content_hash"] == "abc123" + "0" * 58
        assert result["storage_location"] == "/data/objects/ab/c123"
        assert result["size"] == 1024
        assert result["reference_count"] == 2
        assert "created_at" in result
    
    def test_compute_stats(self):
        """Verify stats computation."""
        from wtb.infrastructure.database.mappers import BlobStorageCore
        
        result = BlobStorageCore.compute_stats(100, 1048576)  # 1 MB
        
        assert result["blob_count"] == 100
        assert result["total_size_bytes"] == 1048576
        assert result["total_size_mb"] == 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# OutboxMapper Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestOutboxMapper:
    """Tests for OutboxMapper shared logic."""
    
    def test_to_domain_parses_payload(self):
        """Verify payload JSON parsing."""
        from wtb.infrastructure.database.mappers import OutboxMapper
        from wtb.domain.models.outbox import OutboxEventType, OutboxStatus
        from datetime import datetime
        
        # Create mock ORM object
        mock_orm = MagicMock()
        mock_orm.id = 1
        mock_orm.event_id = "event-123"
        mock_orm.event_type = OutboxEventType.CHECKPOINT_CREATE.value
        mock_orm.aggregate_type = "Execution"
        mock_orm.aggregate_id = "exec-456"
        mock_orm.payload = '{"key": "value"}'
        mock_orm.status = OutboxStatus.PENDING.value
        mock_orm.retry_count = 0
        mock_orm.max_retries = 5
        mock_orm.created_at = datetime.now()
        mock_orm.processed_at = None
        mock_orm.last_error = None
        
        result = OutboxMapper.to_domain(mock_orm)
        
        assert result.event_id == "event-123"
        assert result.payload == {"key": "value"}
        assert result.aggregate_type == "Execution"
    
    def test_to_orm_dict_serializes_payload(self):
        """Verify payload JSON serialization."""
        from wtb.infrastructure.database.mappers import OutboxMapper
        from wtb.domain.models.outbox import OutboxEvent, OutboxEventType
        
        event = OutboxEvent(
            event_type=OutboxEventType.CHECKPOINT_CREATE,
            aggregate_type="Workflow",
            aggregate_id="wf-789",
            payload={"nested": {"data": 123}},
        )
        event.event_id = "event-001"
        
        result = OutboxMapper.to_orm_dict(event)
        
        assert result["event_id"] == "event-001"
        assert '"nested"' in result["payload"]  # JSON serialized
        assert result["aggregate_id"] == "wf-789"
    
    def test_update_orm_from_event(self):
        """Verify ORM update from event."""
        from wtb.infrastructure.database.mappers import OutboxMapper
        from wtb.domain.models.outbox import OutboxEvent, OutboxEventType, OutboxStatus
        from datetime import datetime
        
        mock_orm = MagicMock()
        
        event = OutboxEvent(
            event_type=OutboxEventType.CHECKPOINT_CREATE,
            aggregate_type="Test",
            aggregate_id="test-1",
        )
        event.status = OutboxStatus.PROCESSED
        event.retry_count = 3
        event.processed_at = datetime.now()
        event.last_error = "Test error"
        
        OutboxMapper.update_orm_from_event(mock_orm, event)
        
        assert mock_orm.status == OutboxStatus.PROCESSED.value
        assert mock_orm.retry_count == 3
        assert mock_orm.last_error == "Test error"


# ═══════════════════════════════════════════════════════════════════════════════
# Repository Integration Tests - Verify mappers are actually used
# ═══════════════════════════════════════════════════════════════════════════════


class TestRepositoryUsesMappers:
    """Verify repositories actually use the shared mappers."""
    
    def test_sync_blob_repo_uses_blob_storage_core(self):
        """Sync blob repository uses BlobStorageCore."""
        import inspect
        from wtb.infrastructure.database.repositories.file_processing_repository import (
            SQLAlchemyBlobRepository,
        )
        
        source = inspect.getsource(SQLAlchemyBlobRepository)
        
        # Verify BlobStorageCore is used
        assert "BlobStorageCore" in source
        assert "BlobStorageCore.create_orm_dict" in source or "BlobStorageCore.compute" in source
    
    def test_async_blob_repo_uses_blob_storage_core(self):
        """Async blob repository uses BlobStorageCore."""
        import inspect
        from wtb.infrastructure.database.async_repositories.async_file_processing_repository import (
            AsyncSQLAlchemyBlobRepository,
        )
        
        source = inspect.getsource(AsyncSQLAlchemyBlobRepository)
        
        # Verify BlobStorageCore is used
        assert "BlobStorageCore" in source
        assert "BlobStorageCore.create_orm_dict" in source or "BlobStorageCore.compute" in source
    
    def test_sync_outbox_repo_uses_outbox_mapper(self):
        """Sync outbox repository uses OutboxMapper."""
        import inspect
        from wtb.infrastructure.database.repositories.outbox_repository import (
            SQLAlchemyOutboxRepository,
        )
        
        source = inspect.getsource(SQLAlchemyOutboxRepository)
        
        assert "OutboxMapper" in source
        assert "OutboxMapper.to_domain" in source
    
    def test_async_outbox_repo_uses_outbox_mapper(self):
        """Async outbox repository uses OutboxMapper."""
        import inspect
        from wtb.infrastructure.database.async_repositories.async_outbox_repository import (
            AsyncOutboxRepository,
        )
        
        source = inspect.getsource(AsyncOutboxRepository)
        
        assert "OutboxMapper" in source
        assert "OutboxMapper.to_domain" in source
    
    def test_sync_file_commit_repo_uses_mapper(self):
        """Sync file commit repository uses FileCommitMapper."""
        import inspect
        from wtb.infrastructure.database.repositories.file_processing_repository import (
            SQLAlchemyFileCommitRepository,
        )
        
        source = inspect.getsource(SQLAlchemyFileCommitRepository)
        
        assert "FileCommitMapper" in source
    
    def test_async_file_commit_repo_uses_mapper(self):
        """Async file commit repository uses FileCommitMapper."""
        import inspect
        from wtb.infrastructure.database.async_repositories.async_file_processing_repository import (
            AsyncSQLAlchemyFileCommitRepository,
        )
        
        source = inspect.getsource(AsyncSQLAlchemyFileCommitRepository)
        
        assert "FileCommitMapper" in source


# ═══════════════════════════════════════════════════════════════════════════════
# SOLID Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSOLIDCompliance:
    """Tests verifying SOLID principle compliance."""
    
    def test_srp_mappers_have_single_responsibility(self):
        """Each mapper handles only one domain model."""
        from wtb.infrastructure.database.mappers import (
            BlobStorageCore,
            OutboxMapper,
            FileCommitMapper,
            CheckpointFileLinkMapper,
        )
        
        # BlobStorageCore: only blob storage logic
        assert hasattr(BlobStorageCore, 'compute_blob_id')
        assert not hasattr(BlobStorageCore, 'to_domain')  # No ORM conversion
        
        # OutboxMapper: only OutboxEvent conversion
        assert hasattr(OutboxMapper, 'to_domain')
        assert hasattr(OutboxMapper, 'to_orm_dict')
        
        # FileCommitMapper: only FileCommit conversion  
        assert hasattr(FileCommitMapper, 'orm_to_domain')
        
        # CheckpointFileLinkMapper: only CheckpointFileLink conversion
        assert hasattr(CheckpointFileLinkMapper, 'orm_to_domain')
    
    def test_ocp_mappers_can_be_extended(self):
        """Mappers use static methods that can be overridden."""
        from wtb.infrastructure.database.mappers import BlobStorageCore
        
        # Can create a custom subclass with different algorithm
        class CustomBlobStorageCore(BlobStorageCore):
            HASH_ALGORITHM = "sha512"
            
            @staticmethod
            def compute_blob_id(content: bytes) -> str:
                import hashlib
                return hashlib.sha512(content).hexdigest()
        
        # Verify it works differently
        content = b"test"
        original = BlobStorageCore.compute_blob_id(content)
        custom = CustomBlobStorageCore.compute_blob_id(content)
        
        assert original != custom
        assert len(custom) == 128  # SHA-512 produces 128 hex chars
    
    def test_dip_repositories_depend_on_mappers(self):
        """Repositories depend on mapper abstractions, not inline logic."""
        import inspect
        from wtb.infrastructure.database.repositories.file_processing_repository import (
            SQLAlchemyBlobRepository,
            SQLAlchemyFileCommitRepository,
        )
        
        blob_source = inspect.getsource(SQLAlchemyBlobRepository)
        commit_source = inspect.getsource(SQLAlchemyFileCommitRepository)
        
        # Should NOT have inline hashlib usage
        assert "hashlib.sha256" not in blob_source
        
        # Should NOT have inline FileCommit.reconstitute
        assert "FileCommit.reconstitute" not in commit_source


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
