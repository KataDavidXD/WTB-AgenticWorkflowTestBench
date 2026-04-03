"""
Unit Tests for FileTrackingLink Interface Value Object.

Tests the interface-layer FileTrackingLink (renamed from CheckpointFileLink)
and its distinction from the domain model CheckpointFileLink.

Design Decision (2026-01-16):
- FileTrackingLink: Interface VO with primitive types (str commit_id)
- CheckpointFileLink: Domain model with value objects (CommitId)

Test Categories:
1. FileTrackingLink Creation and Properties
2. FileTrackingLink vs CheckpointFileLink Distinction
3. MockFileTrackingService Operations
4. Interface Contract Compliance
"""

import pytest
from datetime import datetime
from typing import List

from wtb.domain.interfaces.file_tracking import (
    IFileTrackingService,
    FileTrackingLink,
    TrackedFile,
    FileTrackingResult,
    FileRestoreResult,
    CommitNotFoundError,
    CheckpointLinkError,
)
from wtb.domain.models.file_processing import (
    CheckpointFileLink,
    FileCommit,
    FileMemento,
    CommitId,
    BlobId,
)
from wtb.infrastructure.file_tracking import MockFileTrackingService


# ═══════════════════════════════════════════════════════════════════════════════
# Test: FileTrackingLink Interface VO
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileTrackingLinkVO:
    """Tests for FileTrackingLink interface value object."""
    
    def test_create_file_tracking_link(self):
        """Test creating FileTrackingLink with primitive types."""
        link = FileTrackingLink(
            checkpoint_id=42,
            commit_id="abc123-uuid-string",  # Primitive str, not CommitId
            linked_at=datetime.now(),
            file_count=3,
            total_size_bytes=1024,
        )
        
        assert link.checkpoint_id == 42
        assert link.commit_id == "abc123-uuid-string"
        assert isinstance(link.commit_id, str)  # Primitive, not CommitId
        assert link.file_count == 3
        assert link.total_size_bytes == 1024
    
    def test_file_tracking_link_is_frozen(self):
        """Test that FileTrackingLink is immutable."""
        link = FileTrackingLink(
            checkpoint_id=1,
            commit_id="test-commit",
            linked_at=datetime.now(),
            file_count=1,
            total_size_bytes=100,
        )
        
        with pytest.raises(Exception):  # FrozenInstanceError
            link.checkpoint_id = 2
    
    def test_file_tracking_link_to_dict(self):
        """Test FileTrackingLink serialization."""
        now = datetime.now()
        link = FileTrackingLink(
            checkpoint_id=99,
            commit_id="serialize-test",
            linked_at=now,
            file_count=5,
            total_size_bytes=2048,
        )
        
        data = link.to_dict()
        
        assert data["checkpoint_id"] == 99
        assert data["commit_id"] == "serialize-test"
        assert data["linked_at"] == now.isoformat()
        assert data["file_count"] == 5
        assert data["total_size_bytes"] == 2048
    
    def test_file_tracking_link_equality(self):
        """Test FileTrackingLink equality based on values."""
        now = datetime.now()
        link1 = FileTrackingLink(
            checkpoint_id=1,
            commit_id="same",
            linked_at=now,
            file_count=1,
            total_size_bytes=100,
        )
        link2 = FileTrackingLink(
            checkpoint_id=1,
            commit_id="same",
            linked_at=now,
            file_count=1,
            total_size_bytes=100,
        )
        
        assert link1 == link2


# ═══════════════════════════════════════════════════════════════════════════════
# Test: FileTrackingLink vs CheckpointFileLink Distinction
# ═══════════════════════════════════════════════════════════════════════════════


class TestLinkTypeDistinction:
    """Tests verifying the distinction between interface and domain link types."""
    
    def test_interface_link_uses_primitive_commit_id(self):
        """Test that FileTrackingLink uses str for commit_id."""
        link = FileTrackingLink(
            checkpoint_id=1,
            commit_id="primitive-string-id",
            linked_at=datetime.now(),
            file_count=1,
            total_size_bytes=100,
        )
        
        assert isinstance(link.commit_id, str)
        assert not hasattr(link.commit_id, 'value')  # Not a CommitId
    
    def test_domain_link_uses_value_object_commit_id(self, sample_files):
        """Test that CheckpointFileLink uses CommitId value object."""
        commit = FileCommit.create(message="Domain test")
        memento, _ = FileMemento.from_file(str(sample_files["csv"]))
        commit.add_memento(memento)
        commit.finalize()
        
        link = CheckpointFileLink.create(checkpoint_id=1, commit=commit)
        
        assert isinstance(link.commit_id, CommitId)
        assert hasattr(link.commit_id, 'value')
    
    def test_can_convert_domain_to_interface_link(self, sample_files):
        """Test converting domain CheckpointFileLink to interface FileTrackingLink."""
        # Create domain link
        commit = FileCommit.create(message="Convert test")
        memento, _ = FileMemento.from_file(str(sample_files["csv"]))
        commit.add_memento(memento)
        commit.finalize()
        
        domain_link = CheckpointFileLink.create(checkpoint_id=42, commit=commit)
        
        # Convert to interface link (ACL boundary conversion)
        interface_link = FileTrackingLink(
            checkpoint_id=domain_link.checkpoint_id,
            commit_id=domain_link.commit_id.value,  # Extract primitive from VO
            linked_at=domain_link.linked_at,
            file_count=domain_link.file_count,
            total_size_bytes=domain_link.total_size_bytes,
        )
        
        assert interface_link.checkpoint_id == domain_link.checkpoint_id
        assert interface_link.commit_id == domain_link.commit_id.value
        assert isinstance(interface_link.commit_id, str)
    
    def test_can_convert_interface_to_domain_link(self):
        """Test converting interface FileTrackingLink to domain CheckpointFileLink."""
        # Create interface link (from external system)
        interface_link = FileTrackingLink(
            checkpoint_id=99,
            commit_id="550e8400-e29b-41d4-a716-446655440000",  # Valid UUID
            linked_at=datetime.now(),
            file_count=2,
            total_size_bytes=500,
        )
        
        # Convert to domain link (entering domain boundary)
        domain_link = CheckpointFileLink(
            checkpoint_id=interface_link.checkpoint_id,
            commit_id=CommitId(interface_link.commit_id),  # Wrap in VO
            linked_at=interface_link.linked_at,
            file_count=interface_link.file_count,
            total_size_bytes=interface_link.total_size_bytes,
        )
        
        assert domain_link.checkpoint_id == interface_link.checkpoint_id
        assert domain_link.commit_id.value == interface_link.commit_id
        assert isinstance(domain_link.commit_id, CommitId)


# ═══════════════════════════════════════════════════════════════════════════════
# Test: MockFileTrackingService
# ═══════════════════════════════════════════════════════════════════════════════


class TestMockFileTrackingService:
    """Tests for MockFileTrackingService implementation."""
    
    @pytest.fixture
    def mock_service(self):
        """Create fresh mock service for each test."""
        return MockFileTrackingService()
    
    def test_implements_interface(self, mock_service):
        """Test that MockFileTrackingService implements IFileTrackingService."""
        assert isinstance(mock_service, IFileTrackingService)
    
    def test_track_files(self, mock_service):
        """Test tracking files creates commit."""
        result = mock_service.track_files(
            file_paths=["data/output.csv", "data/model.pkl"],
            message="Test tracking",
        )
        
        assert isinstance(result, FileTrackingResult)
        assert result.files_tracked == 2
        assert result.commit_id != ""
        assert "data/output.csv" in result.file_hashes
    
    def test_track_and_link(self, mock_service):
        """Test tracking files and linking to checkpoint."""
        result = mock_service.track_and_link(
            checkpoint_id=42,
            file_paths=["output.csv"],
            message="Checkpoint link",
        )
        
        assert result.files_tracked == 1
        
        # Verify link was created
        commit_id = mock_service.get_commit_for_checkpoint(42)
        assert commit_id == result.commit_id
    
    def test_link_to_checkpoint_returns_file_tracking_link(self, mock_service):
        """Test that link_to_checkpoint returns FileTrackingLink (not CheckpointFileLink)."""
        # First create a commit
        result = mock_service.track_files(["test.csv"])
        
        # Then link it
        link = mock_service.link_to_checkpoint(
            checkpoint_id=99,
            commit_id=result.commit_id,
        )
        
        # Should return interface type
        assert isinstance(link, FileTrackingLink)
        assert isinstance(link.commit_id, str)  # Primitive, not CommitId
        assert link.checkpoint_id == 99
        assert link.commit_id == result.commit_id
    
    def test_link_to_nonexistent_commit_raises(self, mock_service):
        """Test linking to nonexistent commit raises error."""
        with pytest.raises(CommitNotFoundError):
            mock_service.link_to_checkpoint(
                checkpoint_id=1,
                commit_id="nonexistent-commit-id",
            )
    
    def test_restore_from_checkpoint(self, mock_service):
        """Test restoring files from checkpoint."""
        # Track and link
        mock_service.track_and_link(
            checkpoint_id=10,
            file_paths=["a.csv", "b.csv"],
        )
        
        # Restore
        result = mock_service.restore_from_checkpoint(10)
        
        assert isinstance(result, FileRestoreResult)
        assert result.success is True
        assert result.files_restored == 2
        assert "a.csv" in result.restored_paths
    
    def test_restore_from_unlinked_checkpoint_raises(self, mock_service):
        """Test restoring from checkpoint with no link raises error."""
        with pytest.raises(CheckpointLinkError):
            mock_service.restore_from_checkpoint(999)
    
    def test_get_tracked_files(self, mock_service):
        """Test getting tracked files for a commit."""
        result = mock_service.track_files(["x.csv", "y.json"])
        
        files = mock_service.get_tracked_files(result.commit_id)
        
        assert len(files) == 2
        assert all(isinstance(f, TrackedFile) for f in files)
        paths = [f.file_path for f in files]
        assert "x.csv" in paths
        assert "y.json" in paths
    
    def test_is_available(self, mock_service):
        """Test availability check."""
        assert mock_service.is_available() is True
    
    def test_operation_log(self, mock_service):
        """Test operation logging for test verification."""
        mock_service.track_files(["a.csv"])
        mock_service.track_and_link(1, ["b.csv"])
        
        log = mock_service.get_operation_log()
        
        assert len(log) >= 2
        operations = [entry["operation"] for entry in log]
        assert "track_files" in operations
        assert "track_and_link" in operations
    
    def test_clear_state(self, mock_service):
        """Test clearing all state."""
        mock_service.track_and_link(1, ["test.csv"])
        
        assert mock_service.get_commit_count() > 0
        
        mock_service.clear()
        
        assert mock_service.get_commit_count() == 0
        assert mock_service.get_link_count() == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Interface Contract Compliance
# ═══════════════════════════════════════════════════════════════════════════════


class TestInterfaceContractCompliance:
    """Tests verifying IFileTrackingService contract compliance."""
    
    @pytest.fixture
    def service(self):
        """Create service for contract tests."""
        return MockFileTrackingService()
    
    def test_track_files_returns_result(self, service):
        """Test track_files returns FileTrackingResult."""
        result = service.track_files(["test.csv"])
        
        assert isinstance(result, FileTrackingResult)
        assert hasattr(result, 'commit_id')
        assert hasattr(result, 'files_tracked')
        assert hasattr(result, 'total_size_bytes')
        assert hasattr(result, 'file_hashes')
    
    def test_track_and_link_returns_result(self, service):
        """Test track_and_link returns FileTrackingResult."""
        result = service.track_and_link(1, ["test.csv"])
        
        assert isinstance(result, FileTrackingResult)
    
    def test_link_to_checkpoint_returns_link(self, service):
        """Test link_to_checkpoint returns FileTrackingLink."""
        result = service.track_files(["test.csv"])
        link = service.link_to_checkpoint(1, result.commit_id)
        
        assert isinstance(link, FileTrackingLink)
    
    def test_restore_from_checkpoint_returns_result(self, service):
        """Test restore_from_checkpoint returns FileRestoreResult."""
        service.track_and_link(1, ["test.csv"])
        result = service.restore_from_checkpoint(1)
        
        assert isinstance(result, FileRestoreResult)
        assert hasattr(result, 'commit_id')
        assert hasattr(result, 'files_restored')
        assert hasattr(result, 'success')
    
    def test_restore_commit_returns_result(self, service):
        """Test restore_commit returns FileRestoreResult."""
        track_result = service.track_files(["test.csv"])
        result = service.restore_commit(track_result.commit_id)
        
        assert isinstance(result, FileRestoreResult)
    
    def test_get_commit_for_checkpoint_returns_optional_str(self, service):
        """Test get_commit_for_checkpoint returns Optional[str]."""
        # No link
        result = service.get_commit_for_checkpoint(999)
        assert result is None
        
        # With link
        track_result = service.track_and_link(1, ["test.csv"])
        result = service.get_commit_for_checkpoint(1)
        assert isinstance(result, str)
        assert result == track_result.commit_id
    
    def test_get_tracked_files_returns_list(self, service):
        """Test get_tracked_files returns List[TrackedFile]."""
        result = service.track_files(["test.csv"])
        files = service.get_tracked_files(result.commit_id)
        
        assert isinstance(files, list)
        assert all(isinstance(f, TrackedFile) for f in files)
    
    def test_is_available_returns_bool(self, service):
        """Test is_available returns bool."""
        result = service.is_available()
        assert isinstance(result, bool)
