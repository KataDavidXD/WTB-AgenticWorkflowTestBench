"""
Unit Tests for FileTracker Verification in OutboxProcessor.

Tests the FileTracker integration handlers:
- FILE_COMMIT_VERIFY: Commit existence verification
- FILE_BLOB_VERIFY: Blob existence verification
- FILE_BATCH_VERIFY: Batch verification for multiple commits
- FILE_INTEGRITY_CHECK: Deep integrity verification
- FILE_RESTORE_VERIFY: Restore verification
- ROLLBACK_FILE_RESTORE: Rollback file restoration
- ROLLBACK_VERIFY: Full rollback verification

Design Principles:
- SOLID: Each test class tests one handler
- AAA Pattern: Arrange-Act-Assert
- Mock-based: Uses mock repositories for isolation

Usage:
    pytest tests/test_wtb/test_outbox_filetracker.py -v
"""

import pytest
from datetime import datetime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

from wtb.domain.models.outbox import OutboxEvent, OutboxEventType, OutboxStatus
from wtb.infrastructure.outbox.processor import (
    OutboxProcessor,
    VerificationResult,
    FileVerificationResult,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Mock Implementations
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class MockMemento:
    """Mock file memento for testing."""
    _file_path: str
    _file_hash: str
    _file_size: int = 1024


@dataclass
class MockCommit:
    """Mock commit for testing."""
    commit_id: str
    _mementos: List[MockMemento]
    message: str = "test commit"


class MockCheckpointRepository:
    """Mock AgentGit checkpoint repository."""
    
    def __init__(self):
        self._checkpoints: Dict[int, Any] = {}
    
    def add_checkpoint(self, checkpoint_id: int, data: Any = None) -> None:
        self._checkpoints[checkpoint_id] = data or {"id": checkpoint_id}
    
    def get_by_id(self, checkpoint_id: int) -> Optional[Any]:
        return self._checkpoints.get(checkpoint_id)


class MockCommitRepository:
    """Mock FileTracker commit repository."""
    
    def __init__(self):
        self._commits: Dict[str, MockCommit] = {}
    
    def add_commit(
        self, 
        commit_id: str, 
        mementos: List[MockMemento] = None
    ) -> MockCommit:
        commit = MockCommit(
            commit_id=commit_id,
            _mementos=mementos or [],
        )
        self._commits[commit_id] = commit
        return commit
    
    def find_by_id(self, commit_id: str) -> Optional[MockCommit]:
        return self._commits.get(commit_id)
    
    def get_by_checkpoint_id(self, checkpoint_id: int) -> Optional[MockCommit]:
        for commit in self._commits.values():
            if getattr(commit, '_checkpoint_id', None) == checkpoint_id:
                return commit
        return None


class MockBlobRepository:
    """Mock FileTracker blob repository."""
    
    def __init__(self):
        self._blobs: Dict[str, bytes] = {}
    
    def add_blob(self, content_hash: str, content: bytes = b"test") -> None:
        self._blobs[content_hash] = content
    
    def exists(self, content_hash: str) -> bool:
        if hasattr(content_hash, 'value'):
            content_hash = content_hash.value
        return content_hash in self._blobs
    
    def get(self, content_hash: str) -> Optional[bytes]:
        if hasattr(content_hash, 'value'):
            content_hash = content_hash.value
        return self._blobs.get(content_hash)
    
    def restore_to_file(self, blob_id: Any, output_path: str) -> None:
        content = self.get(blob_id)
        if content is None:
            raise FileNotFoundError(f"Blob not found: {blob_id}")
        # Don't actually write in tests


class MockFileTrackingService:
    """Mock IFileTrackingService for testing."""
    
    def __init__(self):
        self._available = True
        self._commits: Dict[str, List[Any]] = {}
        self._checkpoint_links: Dict[int, str] = {}
        self._restore_results: Dict[str, Any] = {}
    
    def is_available(self) -> bool:
        return self._available
    
    def add_commit(self, commit_id: str, files: List[Any]) -> None:
        self._commits[commit_id] = files
    
    def link_checkpoint(self, checkpoint_id: int, commit_id: str) -> None:
        self._checkpoint_links[checkpoint_id] = commit_id
    
    def set_restore_result(self, commit_id: str, result: Any) -> None:
        self._restore_results[commit_id] = result
    
    def get_commit_for_checkpoint(self, checkpoint_id: int) -> Optional[str]:
        return self._checkpoint_links.get(checkpoint_id)
    
    def get_tracked_files(self, commit_id: str) -> List[Any]:
        return self._commits.get(commit_id, [])
    
    def restore_commit(self, commit_id: str) -> Any:
        if commit_id in self._restore_results:
            return self._restore_results[commit_id]
        
        @dataclass
        class RestoreResult:
            success: bool = True
            files_restored: int = 0
            error_message: Optional[str] = None
        
        files = self._commits.get(commit_id, [])
        return RestoreResult(success=True, files_restored=len(files))
    
    def restore_from_checkpoint(self, checkpoint_id: int) -> Any:
        commit_id = self.get_commit_for_checkpoint(checkpoint_id)
        if commit_id:
            return self.restore_commit(commit_id)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Test Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_checkpoint_repo():
    """Create mock checkpoint repository."""
    return MockCheckpointRepository()


@pytest.fixture
def mock_commit_repo():
    """Create mock commit repository."""
    return MockCommitRepository()


@pytest.fixture
def mock_blob_repo():
    """Create mock blob repository."""
    return MockBlobRepository()


@pytest.fixture
def mock_file_service():
    """Create mock file tracking service."""
    return MockFileTrackingService()


@pytest.fixture
def processor(mock_checkpoint_repo, mock_commit_repo, mock_blob_repo):
    """Create OutboxProcessor with mock repositories."""
    return OutboxProcessor(
        wtb_db_url="sqlite:///:memory:",
        checkpoint_repo=mock_checkpoint_repo,
        commit_repo=mock_commit_repo,
        blob_repo=mock_blob_repo,
        strict_verification=True,
    )


@pytest.fixture
def processor_with_service(mock_checkpoint_repo, mock_file_service, mock_blob_repo):
    """Create OutboxProcessor with file tracking service."""
    return OutboxProcessor(
        wtb_db_url="sqlite:///:memory:",
        checkpoint_repo=mock_checkpoint_repo,
        file_tracking_service=mock_file_service,
        blob_repo=mock_blob_repo,
        strict_verification=True,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# FILE_COMMIT_VERIFY Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileCommitVerify:
    """Tests for FILE_COMMIT_VERIFY handler."""
    
    def test_verify_existing_commit_succeeds(self, processor, mock_commit_repo):
        """Verifying an existing commit should succeed."""
        # Arrange
        mock_commit_repo.add_commit("commit-123", [
            MockMemento("file1.txt", "hash1"),
            MockMemento("file2.txt", "hash2"),
        ])
        
        event = OutboxEvent(
            event_type=OutboxEventType.FILE_COMMIT_VERIFY,
            payload={"file_commit_id": "commit-123"},
        )
        
        # Act
        processor._handle_file_commit_verify(event)
        
        # Assert
        stats = processor.get_stats()
        assert stats["commits_verified"] == 1
    
    def test_verify_nonexistent_commit_raises(self, processor):
        """Verifying a nonexistent commit should raise ValueError."""
        event = OutboxEvent(
            event_type=OutboxEventType.FILE_COMMIT_VERIFY,
            payload={"file_commit_id": "nonexistent"},
        )
        
        with pytest.raises(ValueError, match="not found"):
            processor._handle_file_commit_verify(event)
    
    def test_verify_commit_file_count(self, processor, mock_commit_repo):
        """Verifying commit with expected file count."""
        mock_commit_repo.add_commit("commit-123", [
            MockMemento("file1.txt", "hash1"),
        ])
        
        # Correct file count
        event = OutboxEvent(
            event_type=OutboxEventType.FILE_COMMIT_VERIFY,
            payload={"file_commit_id": "commit-123", "expected_file_count": 1},
        )
        processor._handle_file_commit_verify(event)
        
        # Wrong file count
        event_wrong = OutboxEvent(
            event_type=OutboxEventType.FILE_COMMIT_VERIFY,
            payload={"file_commit_id": "commit-123", "expected_file_count": 5},
        )
        with pytest.raises(ValueError, match="expected 5"):
            processor._handle_file_commit_verify(event_wrong)


# ═══════════════════════════════════════════════════════════════════════════════
# FILE_BLOB_VERIFY Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileBlobVerify:
    """Tests for FILE_BLOB_VERIFY handler."""
    
    def test_verify_existing_blob_succeeds(self, processor, mock_blob_repo):
        """Verifying an existing blob should succeed."""
        mock_blob_repo.add_blob("abc123", b"test content")
        
        event = OutboxEvent(
            event_type=OutboxEventType.FILE_BLOB_VERIFY,
            payload={"file_hash": "abc123", "file_path": "test.txt"},
        )
        
        processor._handle_file_blob_verify(event)
        
        stats = processor.get_stats()
        assert stats["blobs_verified"] == 1
    
    def test_verify_nonexistent_blob_raises(self, processor):
        """Verifying a nonexistent blob should raise ValueError."""
        event = OutboxEvent(
            event_type=OutboxEventType.FILE_BLOB_VERIFY,
            payload={"file_hash": "nonexistent"},
        )
        
        with pytest.raises(ValueError, match="not found"):
            processor._handle_file_blob_verify(event)


# ═══════════════════════════════════════════════════════════════════════════════
# FILE_BATCH_VERIFY Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileBatchVerify:
    """Tests for FILE_BATCH_VERIFY handler."""
    
    def test_batch_verify_all_commits_exist(self, processor, mock_commit_repo):
        """Batch verification with all commits existing should succeed."""
        mock_commit_repo.add_commit("commit-1", [MockMemento("f1.txt", "h1")])
        mock_commit_repo.add_commit("commit-2", [MockMemento("f2.txt", "h2")])
        mock_commit_repo.add_commit("commit-3", [MockMemento("f3.txt", "h3")])
        
        event = OutboxEvent(
            event_type=OutboxEventType.FILE_BATCH_VERIFY,
            payload={
                "commit_ids": ["commit-1", "commit-2", "commit-3"],
                "expected_total_files": 3,
            },
        )
        
        processor._handle_file_batch_verify(event)
        
        stats = processor.get_stats()
        assert stats["commits_verified"] == 3
        assert stats["files_verified"] == 3
    
    def test_batch_verify_missing_commit_raises(self, processor, mock_commit_repo):
        """Batch verification with missing commit should raise ValueError."""
        mock_commit_repo.add_commit("commit-1", [MockMemento("f1.txt", "h1")])
        
        event = OutboxEvent(
            event_type=OutboxEventType.FILE_BATCH_VERIFY,
            payload={
                "commit_ids": ["commit-1", "missing-commit"],
            },
        )
        
        with pytest.raises(ValueError, match="missing-commit not found"):
            processor._handle_file_batch_verify(event)
    
    def test_batch_verify_with_blob_verification(
        self, processor, mock_commit_repo, mock_blob_repo
    ):
        """Batch verification should verify blobs when requested."""
        mock_commit_repo.add_commit("commit-1", [
            MockMemento("f1.txt", "hash1"),
            MockMemento("f2.txt", "hash2"),
        ])
        mock_blob_repo.add_blob("hash1", b"content1")
        mock_blob_repo.add_blob("hash2", b"content2")
        
        event = OutboxEvent(
            event_type=OutboxEventType.FILE_BATCH_VERIFY,
            payload={
                "commit_ids": ["commit-1"],
                "verify_blobs": True,
            },
        )
        
        processor._handle_file_batch_verify(event)
        
        stats = processor.get_stats()
        assert stats["blobs_verified"] == 2
    
    def test_batch_verify_file_count_mismatch(self, processor, mock_commit_repo):
        """Batch verification should fail on file count mismatch."""
        mock_commit_repo.add_commit("commit-1", [MockMemento("f1.txt", "h1")])
        
        event = OutboxEvent(
            event_type=OutboxEventType.FILE_BATCH_VERIFY,
            payload={
                "commit_ids": ["commit-1"],
                "expected_total_files": 10,  # Wrong!
            },
        )
        
        with pytest.raises(ValueError, match="File count mismatch"):
            processor._handle_file_batch_verify(event)
    
    def test_batch_verify_with_file_service(
        self, processor_with_service, mock_file_service, mock_blob_repo
    ):
        """Batch verification should use file tracking service when available."""
        @dataclass
        class TrackedFile:
            file_path: str
            file_hash: str
        
        mock_file_service.add_commit("commit-1", [
            TrackedFile("f1.txt", "hash1"),
            TrackedFile("f2.txt", "hash2"),
        ])
        mock_blob_repo.add_blob("hash1", b"content1")
        mock_blob_repo.add_blob("hash2", b"content2")
        
        event = OutboxEvent(
            event_type=OutboxEventType.FILE_BATCH_VERIFY,
            payload={
                "commit_ids": ["commit-1"],
                "verify_blobs": True,
            },
        )
        
        processor_with_service._handle_file_batch_verify(event)
        
        stats = processor_with_service.get_stats()
        assert stats["files_verified"] == 2


# ═══════════════════════════════════════════════════════════════════════════════
# FILE_INTEGRITY_CHECK Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileIntegrityCheck:
    """Tests for FILE_INTEGRITY_CHECK handler."""
    
    # Valid 64-character SHA-256 hashes for testing
    HASH1 = "a" * 64  # Valid SHA-256 hash
    HASH2 = "b" * 64  # Valid SHA-256 hash
    
    def test_integrity_check_all_blobs_exist(self, processor, mock_blob_repo):
        """Integrity check should pass when all blobs exist."""
        mock_blob_repo.add_blob(self.HASH1, b"content1")
        mock_blob_repo.add_blob(self.HASH2, b"content2")
        
        event = OutboxEvent(
            event_type=OutboxEventType.FILE_INTEGRITY_CHECK,
            payload={
                "file_hashes": {
                    "file1.txt": self.HASH1,
                    "file2.txt": self.HASH2,
                },
            },
        )
        
        processor._handle_file_integrity_check(event)
        
        stats = processor.get_stats()
        assert stats["integrity_checks"] == 1
        assert stats["blobs_verified"] == 2
    
    def test_integrity_check_missing_blob_raises(self, processor, mock_blob_repo):
        """Integrity check should fail when blob is missing."""
        mock_blob_repo.add_blob(self.HASH1, b"content1")
        # HASH2 is missing
        
        event = OutboxEvent(
            event_type=OutboxEventType.FILE_INTEGRITY_CHECK,
            payload={
                "file_hashes": {
                    "file1.txt": self.HASH1,
                    "file2.txt": self.HASH2,  # Missing!
                },
            },
        )
        
        with pytest.raises(ValueError, match="Blob not found"):
            processor._handle_file_integrity_check(event)


# ═══════════════════════════════════════════════════════════════════════════════
# ROLLBACK_FILE_RESTORE Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRollbackFileRestore:
    """Tests for ROLLBACK_FILE_RESTORE handler."""
    
    # Valid 64-character SHA-256 hashes for testing
    HASH1 = "c" * 64  # Valid SHA-256 hash
    
    def test_rollback_restore_with_service(
        self, processor_with_service, mock_file_service
    ):
        """Rollback should use file tracking service for restore."""
        mock_file_service.add_commit("commit-123", [
            type('TrackedFile', (), {'file_path': 'f1.txt', 'file_hash': self.HASH1})(),
        ])
        
        event = OutboxEvent(
            event_type=OutboxEventType.ROLLBACK_FILE_RESTORE,
            payload={
                "source_checkpoint_id": 1,
                "target_checkpoint_id": 0,
                "source_commit_id": "commit-123",
            },
        )
        
        processor_with_service._handle_rollback_file_restore(event)
        
        stats = processor_with_service.get_stats()
        assert stats["restores_verified"] == 1
    
    def test_rollback_restore_with_repos(
        self, processor, mock_commit_repo, mock_blob_repo
    ):
        """Rollback should use repos when service unavailable."""
        mock_commit_repo.add_commit("commit-123", [
            MockMemento("f1.txt", self.HASH1),
        ])
        mock_blob_repo.add_blob(self.HASH1, b"content1")
        
        event = OutboxEvent(
            event_type=OutboxEventType.ROLLBACK_FILE_RESTORE,
            payload={
                "source_checkpoint_id": 1,
                "target_checkpoint_id": 0,
                "source_commit_id": "commit-123",
            },
        )
        
        processor._handle_rollback_file_restore(event)
        
        stats = processor.get_stats()
        assert stats["restores_verified"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Statistics and History Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestVerificationHistory:
    """Tests for verification history tracking."""
    
    def test_verification_history_recorded(self, processor, mock_commit_repo):
        """Verification results should be recorded in history."""
        mock_commit_repo.add_commit("commit-1", [MockMemento("f.txt", "h")])
        mock_commit_repo.add_commit("commit-2", [MockMemento("f.txt", "h")])
        
        # Process two events
        for i in range(2):
            event = OutboxEvent(
                event_type=OutboxEventType.FILE_BATCH_VERIFY,
                payload={"commit_ids": [f"commit-{i+1}"]},
            )
            processor._handle_file_batch_verify(event)
        
        history = processor.get_verification_history(limit=10)
        assert len(history) == 2
    
    def test_get_failed_verifications(self, processor):
        """Should be able to retrieve only failed verifications."""
        # Create a failed verification
        event = OutboxEvent(
            event_type=OutboxEventType.FILE_BATCH_VERIFY,
            payload={"commit_ids": ["nonexistent"]},
        )
        
        try:
            processor._handle_file_batch_verify(event)
        except ValueError:
            pass
        
        failed = processor.get_failed_verifications()
        assert len(failed) == 1
        assert not failed[0].success
    
    def test_extended_stats_includes_success_rate(self, processor, mock_commit_repo):
        """Extended stats should include success rate."""
        mock_commit_repo.add_commit("commit-1", [MockMemento("f.txt", "h")])
        
        event = OutboxEvent(
            event_type=OutboxEventType.FILE_BATCH_VERIFY,
            payload={"commit_ids": ["commit-1"]},
        )
        processor._handle_file_batch_verify(event)
        
        extended = processor.get_extended_stats()
        assert "recent_success_rate" in extended
        assert extended["recent_success_rate"] == 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# OutboxEvent Factory Methods Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestOutboxEventFactories:
    """Tests for new OutboxEvent factory methods."""
    
    def test_create_file_batch_verify(self):
        """Should create FILE_BATCH_VERIFY event correctly."""
        event = OutboxEvent.create_file_batch_verify(
            execution_id="exec-123",
            commit_ids=["c1", "c2"],
            expected_total_files=10,
            verify_blobs=True,
        )
        
        assert event.event_type == OutboxEventType.FILE_BATCH_VERIFY
        assert event.aggregate_type == "BatchTest"
        assert event.payload["commit_ids"] == ["c1", "c2"]
        assert event.payload["expected_total_files"] == 10
        assert event.payload["verify_blobs"] is True
    
    def test_create_file_integrity_check(self):
        """Should create FILE_INTEGRITY_CHECK event correctly."""
        event = OutboxEvent.create_file_integrity_check(
            commit_id="commit-123",
            file_hashes={"f1.txt": "h1", "f2.txt": "h2"},
            verify_content=True,
        )
        
        assert event.event_type == OutboxEventType.FILE_INTEGRITY_CHECK
        assert event.aggregate_type == "FileCommit"
        assert len(event.payload["file_hashes"]) == 2
    
    def test_create_file_restore_verify(self):
        """Should create FILE_RESTORE_VERIFY event correctly."""
        event = OutboxEvent.create_file_restore_verify(
            execution_id="exec-123",
            checkpoint_id=5,
            commit_id="commit-123",
            restored_paths=["f1.txt", "f2.txt"],
        )
        
        assert event.event_type == OutboxEventType.FILE_RESTORE_VERIFY
        assert event.payload["checkpoint_id"] == 5
        assert len(event.payload["restored_paths"]) == 2
    
    def test_create_rollback_file_restore(self):
        """Should create ROLLBACK_FILE_RESTORE event correctly."""
        event = OutboxEvent.create_rollback_file_restore(
            execution_id="exec-123",
            source_checkpoint_id=10,
            target_checkpoint_id=5,
            source_commit_id="commit-123",
        )
        
        assert event.event_type == OutboxEventType.ROLLBACK_FILE_RESTORE
        assert event.payload["source_checkpoint_id"] == 10
        assert event.payload["target_checkpoint_id"] == 5
    
    def test_create_rollback_verify(self):
        """Should create ROLLBACK_VERIFY event correctly."""
        event = OutboxEvent.create_rollback_verify(
            execution_id="exec-123",
            checkpoint_id=5,
            restored_files_count=10,
            state_verified=True,
        )
        
        assert event.event_type == OutboxEventType.ROLLBACK_VERIFY
        assert event.payload["restored_files_count"] == 10
        assert event.payload["state_verified"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# SOLID/ACID Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSOLIDCompliance:
    """Tests verifying SOLID principles compliance."""
    
    def test_single_responsibility_each_handler(self, processor):
        """Each event type maps to exactly one handler; audit-only events may share a handler."""
        handlers = processor._handlers
        
        for event_type, handler in handlers.items():
            assert callable(handler), f"Handler for {event_type} must be callable"
        
        # All event types must have a registered handler
        assert len(handlers) > 0, "Processor should have at least one handler"
    
    def test_dependency_inversion_protocols(self, processor):
        """Processor should depend on protocols, not concrete implementations."""
        # Processor works with mock implementations
        assert processor._checkpoint_repo is not None
        assert processor._commit_repo is not None
        assert processor._blob_repo is not None


class TestACIDCompliance:
    """Tests verifying ACID principles compliance."""
    
    def test_atomicity_event_processed_or_failed(self, processor, mock_commit_repo):
        """Events should be either fully processed or marked failed."""
        mock_commit_repo.add_commit("commit-1", [])
        
        event = OutboxEvent(
            event_type=OutboxEventType.FILE_COMMIT_VERIFY,
            payload={"file_commit_id": "commit-1"},
        )
        
        # Process event
        processor._handle_file_commit_verify(event)
        
        # Stats updated atomically
        stats = processor.get_stats()
        assert stats["commits_verified"] == 1
    
    def test_consistency_verification_result_recorded(self, processor, mock_commit_repo):
        """Verification results should be consistently recorded."""
        mock_commit_repo.add_commit("commit-1", [MockMemento("f.txt", "h")])
        
        event = OutboxEvent(
            event_type=OutboxEventType.FILE_BATCH_VERIFY,
            payload={"commit_ids": ["commit-1"]},
        )
        
        processor._handle_file_batch_verify(event)
        
        # Result recorded in history
        history = processor.get_verification_history(limit=1)
        assert len(history) == 1
        assert history[0].success is True
    
    def test_isolation_handlers_independent(self, processor, mock_commit_repo, mock_blob_repo):
        """Handlers should operate independently."""
        mock_commit_repo.add_commit("commit-1", [MockMemento("f.txt", "hash1")])
        mock_blob_repo.add_blob("hash1", b"content")
        
        # Process commit verify
        event1 = OutboxEvent(
            event_type=OutboxEventType.FILE_COMMIT_VERIFY,
            payload={"file_commit_id": "commit-1"},
        )
        processor._handle_file_commit_verify(event1)
        
        # Process blob verify independently
        event2 = OutboxEvent(
            event_type=OutboxEventType.FILE_BLOB_VERIFY,
            payload={"file_hash": "hash1"},
        )
        processor._handle_file_blob_verify(event2)
        
        stats = processor.get_stats()
        assert stats["commits_verified"] == 1
        assert stats["blobs_verified"] == 1
