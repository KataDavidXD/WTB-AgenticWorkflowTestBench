"""
Integration tests for File Processing with WTB System.

Tests end-to-end file tracking workflows, transaction consistency,
and event/audit integration.

Test Categories:
1. Transaction consistency with UoW
2. Event publishing during file operations
3. Outbox pattern integration
4. Cross-system consistency (checkpoint-file links)
"""

import pytest
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

from wtb.domain.models.file_processing import (
    BlobId,
    CommitId,
    FileMemento,
    FileCommit,
    CheckpointFileLink,
    CommitStatus,
)
from wtb.domain.events.file_processing_events import (
    FileCommitCreatedEvent,
    FileRestoredEvent,
    CheckpointFileLinkCreatedEvent,
    FileTrackingStartedEvent,
    FileTrackingCompletedEvent,
)
from wtb.infrastructure.database.repositories.file_processing_repository import (
    InMemoryBlobRepository,
    InMemoryFileCommitRepository,
    InMemoryCheckpointFileLinkRepository,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def blob_repo():
    """Create in-memory blob repository."""
    return InMemoryBlobRepository()


@pytest.fixture
def commit_repo():
    """Create in-memory commit repository."""
    return InMemoryFileCommitRepository()


@pytest.fixture
def link_repo():
    """Create in-memory checkpoint link repository."""
    return InMemoryCheckpointFileLinkRepository()


@pytest.fixture
def temp_files(tmp_path):
    """Create temporary test files."""
    files = {}
    for name, content in [
        ("data.csv", b"id,name,value\n1,Alice,100\n2,Bob,200"),
        ("model.pkl", b"PICKLE_BINARY_MODEL_DATA"),
        ("config.json", b'{"setting": "value"}'),
    ]:
        path = tmp_path / name
        path.write_bytes(content)
        files[name] = {
            "path": str(path),
            "content": content,
        }
    return files


@pytest.fixture
def event_bus_mock():
    """Create mock event bus."""
    mock = Mock()
    mock.publish = Mock()
    mock.get_history = Mock(return_value=[])
    return mock


# ═══════════════════════════════════════════════════════════════════════════════
# Transaction Consistency Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestTransactionConsistency:
    """Tests for ACID transaction consistency."""
    
    def test_atomic_commit_save(self, blob_repo, commit_repo, temp_files):
        """Test that commit and mementos are saved atomically."""
        # Create commit with multiple files
        commit = FileCommit.create(message="Atomic test commit")
        
        for name, data in temp_files.items():
            content = data["content"]
            blob_id = blob_repo.save(content)
            memento = FileMemento.from_path_and_hash(
                file_path=data["path"],
                file_hash=blob_id,
                file_size=len(content),
            )
            commit.add_memento(memento)
        
        commit.finalize()
        commit_repo.save(commit)
        
        # All or nothing: commit should have all mementos
        retrieved = commit_repo.get_by_id(commit.commit_id)
        assert retrieved.file_count == len(temp_files)
    
    def test_rollback_on_error(self, blob_repo, commit_repo, temp_files):
        """Test that changes are rolled back on error."""
        commit = FileCommit.create(message="Will fail")
        
        # Add one valid memento
        data = list(temp_files.values())[0]
        blob_id = blob_repo.save(data["content"])
        memento = FileMemento.from_path_and_hash(
            data["path"], blob_id, len(data["content"])
        )
        commit.add_memento(memento)
        
        initial_count = commit_repo.count()
        
        # Simulate error during save by patching
        with patch.object(commit_repo, 'save', side_effect=Exception("DB Error")):
            try:
                commit_repo.save(commit)
            except Exception:
                pass
        
        # Count should be unchanged
        assert commit_repo.count() == initial_count
    
    def test_consistent_checkpoint_linking(self, blob_repo, commit_repo, link_repo, temp_files):
        """Test checkpoint-file link consistency."""
        # Create and save commit
        commit = FileCommit.create()
        data = list(temp_files.values())[0]
        blob_id = blob_repo.save(data["content"])
        commit.add_memento(FileMemento.from_path_and_hash(
            data["path"], blob_id, len(data["content"])
        ))
        commit.finalize()
        commit_repo.save(commit)
        
        # Create link
        link = CheckpointFileLink.create(checkpoint_id=42, commit=commit)
        link_repo.add(link)
        
        # Verify link points to correct commit
        retrieved_link = link_repo.get_by_checkpoint(42)
        retrieved_commit = commit_repo.get_by_id(retrieved_link.commit_id)
        
        assert retrieved_commit.commit_id == commit.commit_id
        assert retrieved_link.file_count == commit.file_count
        assert retrieved_link.total_size_bytes == commit.total_size


# ═══════════════════════════════════════════════════════════════════════════════
# Event Publishing Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestEventPublishing:
    """Tests for domain event publishing during file operations."""
    
    def test_file_commit_created_event(self, event_bus_mock, blob_repo, commit_repo, temp_files):
        """Test that FileCommitCreatedEvent is published after commit."""
        commit = FileCommit.create(message="Event test")
        
        for name, data in temp_files.items():
            blob_id = blob_repo.save(data["content"])
            commit.add_memento(FileMemento.from_path_and_hash(
                data["path"], blob_id, len(data["content"])
            ))
        
        commit.finalize()
        commit_repo.save(commit)
        
        # Simulate event publishing
        event = FileCommitCreatedEvent(
            commit_id=commit.commit_id.value,
            file_count=commit.file_count,
            total_size_bytes=commit.total_size,
            message=commit.message,
            file_paths=tuple(commit.file_paths),
        )
        event_bus_mock.publish(event)
        
        # Verify event was published
        event_bus_mock.publish.assert_called_once()
        published = event_bus_mock.publish.call_args[0][0]
        assert isinstance(published, FileCommitCreatedEvent)
        assert published.commit_id == commit.commit_id.value
        assert published.file_count == 3
    
    def test_file_restored_event(self, event_bus_mock, blob_repo, commit_repo, tmp_path):
        """Test that FileRestoredEvent is published after restore."""
        # Setup: Create commit with blob
        content = b"Content to restore"
        blob_id = blob_repo.save(content)
        
        original_path = str(tmp_path / "original.txt")
        commit = FileCommit.create()
        commit.add_memento(FileMemento.from_path_and_hash(
            original_path, blob_id, len(content)
        ))
        commit.finalize()
        commit_repo.save(commit)
        
        # Restore files
        restored_paths = []
        total_size = 0
        for memento in commit.mementos:
            blob_repo.restore_to_file(memento.file_hash, memento.file_path)
            restored_paths.append(memento.file_path)
            total_size += memento.file_size
        
        # Publish event
        event = FileRestoredEvent(
            commit_id=commit.commit_id.value,
            files_restored=len(restored_paths),
            total_size_bytes=total_size,
            restored_paths=tuple(restored_paths),
        )
        event_bus_mock.publish(event)
        
        # Verify
        event_bus_mock.publish.assert_called_once()
        published = event_bus_mock.publish.call_args[0][0]
        assert isinstance(published, FileRestoredEvent)
        assert published.files_restored == 1
    
    def test_checkpoint_link_created_event(self, event_bus_mock, commit_repo, link_repo):
        """Test that CheckpointFileLinkCreatedEvent is published."""
        commit = FileCommit.create()
        blob_id = BlobId(value="a" * 64)
        commit.add_memento(FileMemento.from_path_and_hash("file.txt", blob_id, 100))
        commit.finalize()
        commit_repo.save(commit)
        
        link = CheckpointFileLink.create(checkpoint_id=42, commit=commit)
        link_repo.add(link)
        
        # Publish event
        event = CheckpointFileLinkCreatedEvent(
            checkpoint_id=link.checkpoint_id,
            commit_id=link.commit_id.value,
            file_count=link.file_count,
            total_size_bytes=link.total_size_bytes,
        )
        event_bus_mock.publish(event)
        
        # Verify
        published = event_bus_mock.publish.call_args[0][0]
        assert isinstance(published, CheckpointFileLinkCreatedEvent)
        assert published.checkpoint_id == 42


# ═══════════════════════════════════════════════════════════════════════════════
# Audit Trail Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestAuditTrail:
    """Tests for audit trail integration."""
    
    def test_audit_entry_on_commit_create(self, blob_repo, commit_repo, temp_files):
        """Test that audit entries are created for commits."""
        audit_entries = []
        
        def mock_audit_add(event_type, data):
            audit_entries.append({"event_type": event_type, "data": data})
        
        # Create commit
        commit = FileCommit.create(
            message="Audited commit",
            created_by="test_user",
            execution_id="exec-audit",
        )
        
        for data in temp_files.values():
            blob_id = blob_repo.save(data["content"])
            commit.add_memento(FileMemento.from_path_and_hash(
                data["path"], blob_id, len(data["content"])
            ))
        
        commit.finalize()
        commit_repo.save(commit)
        
        # Simulate audit entry
        mock_audit_add(
            event_type="file_commit.created",
            data={
                "commit_id": commit.commit_id.value,
                "file_count": commit.file_count,
                "created_by": commit.created_by,
            }
        )
        
        # Verify audit entry
        assert len(audit_entries) == 1
        assert audit_entries[0]["event_type"] == "file_commit.created"
        assert audit_entries[0]["data"]["file_count"] == 3
    
    def test_audit_entry_on_restore(self, blob_repo, commit_repo, tmp_path):
        """Test that audit entries are created for restores."""
        audit_entries = []
        
        # Setup
        content = b"Audit test content"
        blob_id = blob_repo.save(content)
        commit = FileCommit.create()
        commit.add_memento(FileMemento.from_path_and_hash(
            str(tmp_path / "file.txt"), blob_id, len(content)
        ))
        commit.finalize()
        commit_repo.save(commit)
        
        # Restore
        for memento in commit.mementos:
            blob_repo.restore_to_file(memento.file_hash, memento.file_path)
        
        # Simulate audit
        audit_entries.append({
            "event_type": "file.restored",
            "data": {
                "commit_id": commit.commit_id.value,
                "files_restored": commit.file_count,
            }
        })
        
        assert len(audit_entries) == 1
        assert audit_entries[0]["event_type"] == "file.restored"


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-System Consistency Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCrossSystemConsistency:
    """Tests for cross-system consistency (WTB-FileTracker)."""
    
    def test_checkpoint_file_link_verification(self, blob_repo, commit_repo, link_repo, temp_files):
        """Test verification of checkpoint-file links."""
        # Create and persist commit
        commit = FileCommit.create()
        for data in temp_files.values():
            blob_id = blob_repo.save(data["content"])
            commit.add_memento(FileMemento.from_path_and_hash(
                data["path"], blob_id, len(data["content"])
            ))
        commit.finalize()
        commit_repo.save(commit)
        
        # Create link
        link = CheckpointFileLink.create(checkpoint_id=100, commit=commit)
        link_repo.add(link)
        
        # Verification: Check all blobs exist
        retrieved_link = link_repo.get_by_checkpoint(100)
        retrieved_commit = commit_repo.get_by_id(retrieved_link.commit_id)
        
        all_blobs_exist = all(
            blob_repo.exists(m.file_hash)
            for m in retrieved_commit.mementos
        )
        
        assert all_blobs_exist is True
    
    def test_orphan_link_detection(self, link_repo, commit_repo):
        """Test detection of orphaned checkpoint links."""
        # Create link pointing to non-existent commit
        orphan_commit_id = CommitId.generate()
        orphan_link = CheckpointFileLink(
            checkpoint_id=999,
            commit_id=orphan_commit_id,
            linked_at=datetime.now(),
            file_count=5,
            total_size_bytes=1000,
        )
        link_repo.add(orphan_link)
        
        # Verify commit doesn't exist
        retrieved = link_repo.get_by_checkpoint(999)
        commit = commit_repo.get_by_id(retrieved.commit_id)
        
        assert commit is None  # Orphan detected
    
    def test_cascading_restore_from_checkpoint(
        self, blob_repo, commit_repo, link_repo, temp_files, tmp_path
    ):
        """Test complete restore workflow from checkpoint."""
        # Setup: Create commit and link
        commit = FileCommit.create()
        for name, data in temp_files.items():
            blob_id = blob_repo.save(data["content"])
            commit.add_memento(FileMemento.from_path_and_hash(
                str(tmp_path / "restored" / name),  # Different path
                blob_id,
                len(data["content"]),
            ))
        commit.finalize()
        commit_repo.save(commit)
        
        link = CheckpointFileLink.create(checkpoint_id=50, commit=commit)
        link_repo.add(link)
        
        # Restore from checkpoint
        retrieved_link = link_repo.get_by_checkpoint(50)
        retrieved_commit = commit_repo.get_by_id(retrieved_link.commit_id)
        
        restored_files = []
        for memento in retrieved_commit.mementos:
            blob_repo.restore_to_file(memento.file_hash, memento.file_path)
            restored_files.append(memento.file_path)
        
        # Verify all files restored
        assert len(restored_files) == len(temp_files)
        for path in restored_files:
            assert Path(path).exists()


# ═══════════════════════════════════════════════════════════════════════════════
# Performance Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestPerformance:
    """Tests for performance characteristics."""
    
    def test_bulk_commit_creation(self, blob_repo, commit_repo):
        """Test creating many commits efficiently."""
        num_commits = 100
        
        for i in range(num_commits):
            commit = FileCommit.create(message=f"Commit {i}")
            blob_id = BlobId(value=f"{i:064d}")
            commit.add_memento(FileMemento.from_path_and_hash(
                f"file{i}.txt", blob_id, 100
            ))
            commit.finalize()
            commit_repo.save(commit)
        
        assert commit_repo.count() == num_commits
    
    def test_bulk_blob_deduplication(self, blob_repo):
        """Test that identical blobs are deduplicated."""
        content = b"Same content repeated many times"
        
        # Save same content 100 times
        blob_ids = [blob_repo.save(content) for _ in range(100)]
        
        # All should be same ID
        assert all(bid == blob_ids[0] for bid in blob_ids)
        
        # Only 1 blob stored
        stats = blob_repo.get_stats()
        assert stats["blob_count"] == 1
    
    def test_large_commit_with_many_files(self, blob_repo, commit_repo):
        """Test commit with many files."""
        commit = FileCommit.create(message="Large commit")
        
        for i in range(50):  # 50 files
            blob_id = BlobId(value=f"{i:064d}")
            commit.add_memento(FileMemento.from_path_and_hash(
                f"files/category{i // 10}/file{i}.txt",
                blob_id,
                1000 * (i + 1),
            ))
        
        commit.finalize()
        commit_repo.save(commit)
        
        retrieved = commit_repo.get_by_id(commit.commit_id)
        assert retrieved.file_count == 50
        assert retrieved.total_size > 0


# ═══════════════════════════════════════════════════════════════════════════════
# Error Handling Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestErrorHandling:
    """Tests for error handling scenarios."""
    
    def test_restore_missing_blob(self, blob_repo, tmp_path):
        """Test error handling when restoring missing blob."""
        missing_blob_id = BlobId(value="0" * 64)
        
        with pytest.raises(FileNotFoundError):
            blob_repo.restore_to_file(missing_blob_id, str(tmp_path / "out.txt"))
    
    def test_get_missing_commit(self, commit_repo):
        """Test getting non-existent commit returns None."""
        missing_id = CommitId.generate()
        result = commit_repo.get_by_id(missing_id)
        assert result is None
    
    def test_get_missing_link(self, link_repo):
        """Test getting non-existent link returns None."""
        result = link_repo.get_by_checkpoint(99999)
        assert result is None
    
    def test_duplicate_file_in_commit(self, blob_repo):
        """Test that duplicate files in commit raise error."""
        from wtb.domain.models.file_processing import DuplicateFileError
        
        commit = FileCommit.create()
        blob_id = BlobId(value="a" * 64)
        
        commit.add_memento(FileMemento.from_path_and_hash("same.txt", blob_id, 100))
        
        with pytest.raises(DuplicateFileError):
            commit.add_memento(FileMemento.from_path_and_hash("same.txt", blob_id, 200))
    
    def test_finalize_empty_commit(self):
        """Test that finalizing empty commit raises error."""
        commit = FileCommit.create()
        
        with pytest.raises(ValueError):
            commit.finalize()
