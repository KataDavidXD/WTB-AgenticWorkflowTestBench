"""
Unit tests for File Tracking Interface and Mock Service.

Tests IFileTrackingService interface compliance and MockFileTrackingService behavior.

Test Categories:
1. Interface Value Objects (TrackedFile, FileTrackingResult, etc.)
2. MockFileTrackingService basic operations
3. Checkpoint-commit linking
4. Restore operations
5. Error handling
6. Thread safety
"""

import pytest
import os
import tempfile
import threading
import time
from datetime import datetime
from typing import List

from wtb.domain.interfaces.file_tracking import (
    IFileTrackingService,
    TrackedFile,
    FileTrackingResult,
    FileRestoreResult,
    FileTrackingLink,  # Renamed from CheckpointFileLink (2026-01-16)
    FileTrackingError,
    CommitNotFoundError,
    CheckpointLinkError,
)
from wtb.infrastructure.file_tracking import MockFileTrackingService
from wtb.config import FileTrackingConfig


# ═══════════════════════════════════════════════════════════════════════════════
# Test Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_service() -> MockFileTrackingService:
    """Create a mock file tracking service."""
    return MockFileTrackingService()


@pytest.fixture
def real_files_service() -> MockFileTrackingService:
    """Create a mock service that requires files to exist."""
    return MockFileTrackingService(simulate_file_existence=True)


@pytest.fixture
def temp_files(tmp_path) -> List[str]:
    """Create temporary test files."""
    files = []
    for name in ["data.csv", "model.pkl", "config.json"]:
        path = tmp_path / name
        path.write_text(f"Content of {name}")
        files.append(str(path))
    return files


# ═══════════════════════════════════════════════════════════════════════════════
# Value Object Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestTrackedFile:
    """Tests for TrackedFile value object."""
    
    def test_create_tracked_file(self):
        """Test creating a TrackedFile."""
        tf = TrackedFile(
            file_path="/data/model.pkl",
            file_hash="abc123",
            size_bytes=1024,
            tracked_at=datetime.now(),
        )
        
        assert tf.file_path == "/data/model.pkl"
        assert tf.file_hash == "abc123"
        assert tf.size_bytes == 1024
    
    def test_tracked_file_immutable(self):
        """Test that TrackedFile is immutable."""
        tf = TrackedFile(
            file_path="/data/model.pkl",
            file_hash="abc123",
            size_bytes=1024,
            tracked_at=datetime.now(),
        )
        
        with pytest.raises(AttributeError):
            tf.file_path = "/other/path"
    
    def test_tracked_file_serialization(self):
        """Test TrackedFile to_dict and from_dict."""
        tf = TrackedFile(
            file_path="/data/model.pkl",
            file_hash="abc123",
            size_bytes=1024,
            tracked_at=datetime(2026, 1, 15, 10, 30, 0),
        )
        
        data = tf.to_dict()
        assert data["file_path"] == "/data/model.pkl"
        assert data["file_hash"] == "abc123"
        
        restored = TrackedFile.from_dict(data)
        assert restored.file_path == tf.file_path
        assert restored.file_hash == tf.file_hash
        assert restored.size_bytes == tf.size_bytes


class TestFileTrackingResult:
    """Tests for FileTrackingResult value object."""
    
    def test_create_tracking_result(self):
        """Test creating a FileTrackingResult."""
        result = FileTrackingResult(
            commit_id="commit-123",
            files_tracked=3,
            total_size_bytes=10240,
            file_hashes={"a.txt": "hash1", "b.txt": "hash2"},
            message="Test commit",
        )
        
        assert result.commit_id == "commit-123"
        assert result.files_tracked == 3
        assert result.total_size_bytes == 10240
        assert len(result.file_hashes) == 2
    
    def test_is_empty_property(self):
        """Test is_empty property."""
        empty = FileTrackingResult(
            commit_id="",
            files_tracked=0,
            total_size_bytes=0,
            file_hashes={},
        )
        assert empty.is_empty is True
        
        non_empty = FileTrackingResult(
            commit_id="commit-123",
            files_tracked=1,
            total_size_bytes=100,
            file_hashes={"a.txt": "hash1"},
        )
        assert non_empty.is_empty is False
    
    def test_tracking_result_serialization(self):
        """Test FileTrackingResult to_dict and from_dict."""
        result = FileTrackingResult(
            commit_id="commit-123",
            files_tracked=2,
            total_size_bytes=500,
            file_hashes={"a.txt": "hash1"},
            message="Test",
        )
        
        data = result.to_dict()
        restored = FileTrackingResult.from_dict(data)
        
        assert restored.commit_id == result.commit_id
        assert restored.files_tracked == result.files_tracked


class TestFileTrackingConfig:
    """Tests for FileTrackingConfig."""
    
    def test_for_testing_config(self):
        """Test creating testing config."""
        config = FileTrackingConfig.for_testing()
        
        assert config.enabled is False
    
    def test_for_development_config(self):
        """Test creating development config."""
        config = FileTrackingConfig.for_development(
            storage_path="./test_storage",
            wtb_db_url="sqlite:///test.db",
        )
        
        assert config.enabled is True
        assert config.storage_path == "./test_storage"
    
    def test_for_production_config(self):
        """Test creating production config."""
        config = FileTrackingConfig.for_production(
            postgres_url="postgresql://localhost/filetracker",
            storage_path="/data/storage",
            wtb_db_url="postgresql://localhost/wtb",
        )
        
        assert config.enabled is True
        assert config.postgres_url == "postgresql://localhost/filetracker"
    
    def test_config_serialization(self):
        """Test config to_dict and from_dict."""
        config = FileTrackingConfig(
            enabled=True,
            postgres_url="postgresql://localhost/test",
            storage_path="/data",
            auto_track_patterns=["*.csv", "*.json"],
        )
        
        data = config.to_dict()
        restored = FileTrackingConfig.from_dict(data)
        
        assert restored.enabled == config.enabled
        assert restored.postgres_url == config.postgres_url
        assert restored.auto_track_patterns == config.auto_track_patterns


# ═══════════════════════════════════════════════════════════════════════════════
# Mock Service Basic Operations
# ═══════════════════════════════════════════════════════════════════════════════


class TestMockServiceBasicOperations:
    """Tests for MockFileTrackingService basic operations."""
    
    def test_track_files_creates_commit(self, mock_service):
        """Test that tracking files creates a commit."""
        result = mock_service.track_files(
            ["data/a.csv", "data/b.csv"],
            message="Test tracking",
        )
        
        assert result.commit_id is not None
        assert result.files_tracked == 2
        assert result.message == "Test tracking"
        assert mock_service.get_commit_count() == 1
    
    def test_track_files_generates_hashes(self, mock_service):
        """Test that tracking generates file hashes."""
        result = mock_service.track_files(["model.pkl"])
        
        assert "model.pkl" in result.file_hashes
        assert len(result.file_hashes["model.pkl"]) == 64  # SHA-256 hex
    
    def test_track_files_deterministic_hashes(self, mock_service):
        """Test that hashes are deterministic for same path."""
        result1 = mock_service.track_files(["same/path.txt"])
        mock_service.clear()
        result2 = mock_service.track_files(["same/path.txt"])
        
        assert result1.file_hashes["same/path.txt"] == result2.file_hashes["same/path.txt"]
    
    def test_track_and_link_creates_link(self, mock_service):
        """Test that track_and_link creates checkpoint-commit link."""
        result = mock_service.track_and_link(
            checkpoint_id=42,
            file_paths=["data.csv"],
            message="Checkpoint 42 files",
        )
        
        assert result.files_tracked == 1
        assert mock_service.get_commit_for_checkpoint(42) == result.commit_id
        assert mock_service.get_link_count() == 1
    
    def test_link_to_existing_checkpoint(self, mock_service):
        """Test linking existing commit to checkpoint."""
        # First create a commit
        result = mock_service.track_files(["data.csv"])
        
        # Then link it
        link = mock_service.link_to_checkpoint(
            checkpoint_id=100,
            commit_id=result.commit_id,
        )
        
        assert link.checkpoint_id == 100
        assert link.commit_id == result.commit_id
        assert mock_service.get_commit_for_checkpoint(100) == result.commit_id
    
    def test_is_available_returns_true(self, mock_service):
        """Test that mock service is always available."""
        assert mock_service.is_available() is True
    
    def test_get_tracked_files(self, mock_service):
        """Test getting tracked files for a commit."""
        result = mock_service.track_files(["a.csv", "b.json"])
        
        tracked = mock_service.get_tracked_files(result.commit_id)
        
        assert len(tracked) == 2
        paths = [t.file_path for t in tracked]
        assert "a.csv" in paths
        assert "b.json" in paths


# ═══════════════════════════════════════════════════════════════════════════════
# Restore Operations
# ═══════════════════════════════════════════════════════════════════════════════


class TestRestoreOperations:
    """Tests for file restore operations."""
    
    def test_restore_from_checkpoint(self, mock_service):
        """Test restoring files from checkpoint."""
        # Track and link
        mock_service.track_and_link(
            checkpoint_id=50,
            file_paths=["model.pkl", "config.json"],
        )
        
        # Restore
        result = mock_service.restore_from_checkpoint(50)
        
        assert result.success is True
        assert result.files_restored == 2
        assert "model.pkl" in result.restored_paths
    
    def test_restore_commit(self, mock_service):
        """Test restoring files from specific commit."""
        track_result = mock_service.track_files(["data.csv"])
        
        restore_result = mock_service.restore_commit(track_result.commit_id)
        
        assert restore_result.success is True
        assert restore_result.commit_id == track_result.commit_id
        assert restore_result.files_restored == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Error Handling
# ═══════════════════════════════════════════════════════════════════════════════


class TestErrorHandling:
    """Tests for error handling."""
    
    def test_link_nonexistent_commit_raises(self, mock_service):
        """Test that linking nonexistent commit raises error."""
        with pytest.raises(CommitNotFoundError):
            mock_service.link_to_checkpoint(
                checkpoint_id=1,
                commit_id="nonexistent-commit-id",
            )
    
    def test_restore_nonexistent_checkpoint_raises(self, mock_service):
        """Test that restoring from unlinked checkpoint raises error."""
        with pytest.raises(CheckpointLinkError):
            mock_service.restore_from_checkpoint(checkpoint_id=999)
    
    def test_restore_nonexistent_commit_raises(self, mock_service):
        """Test that restoring nonexistent commit raises error."""
        with pytest.raises(CommitNotFoundError):
            mock_service.restore_commit("nonexistent-commit")
    
    def test_get_tracked_files_nonexistent_raises(self, mock_service):
        """Test getting tracked files for nonexistent commit raises error."""
        with pytest.raises(CommitNotFoundError):
            mock_service.get_tracked_files("nonexistent-commit")


# ═══════════════════════════════════════════════════════════════════════════════
# Real File Simulation
# ═══════════════════════════════════════════════════════════════════════════════


class TestRealFileSimulation:
    """Tests with simulate_file_existence=True."""
    
    def test_track_existing_files(self, real_files_service, temp_files):
        """Test tracking real existing files."""
        result = real_files_service.track_files(temp_files)
        
        assert result.files_tracked == len(temp_files)
        assert result.total_size_bytes > 0
    
    def test_track_nonexistent_file_raises(self, real_files_service):
        """Test that tracking nonexistent file raises error."""
        with pytest.raises(Exception):  # FileNotFoundError from interface
            real_files_service.track_files(["/nonexistent/file.txt"])


# ═══════════════════════════════════════════════════════════════════════════════
# Thread Safety
# ═══════════════════════════════════════════════════════════════════════════════


class TestThreadSafety:
    """Tests for thread safety of mock service."""
    
    def test_concurrent_tracking(self, mock_service):
        """Test concurrent file tracking."""
        results = []
        errors = []
        
        def track_files(prefix: str):
            try:
                for i in range(10):
                    result = mock_service.track_files([f"{prefix}_file_{i}.txt"])
                    results.append(result.commit_id)
            except Exception as e:
                errors.append(e)
        
        threads = [
            threading.Thread(target=track_files, args=(f"thread_{i}",))
            for i in range(5)
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0
        assert len(results) == 50
        assert len(set(results)) == 50  # All unique commit IDs
    
    def test_concurrent_link_and_restore(self, mock_service):
        """Test concurrent linking and restoring."""
        errors = []
        
        # Pre-create some commits
        commits = []
        for i in range(10):
            result = mock_service.track_files([f"file_{i}.txt"])
            commits.append(result.commit_id)
        
        def link_and_restore(checkpoint_offset: int):
            try:
                for i in range(10):
                    checkpoint_id = checkpoint_offset + i
                    commit_id = commits[i % len(commits)]
                    
                    mock_service.link_to_checkpoint(checkpoint_id, commit_id)
                    mock_service.restore_from_checkpoint(checkpoint_id)
            except Exception as e:
                errors.append(e)
        
        threads = [
            threading.Thread(target=link_and_restore, args=(i * 100,))
            for i in range(3)
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Operation Log Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestOperationLog:
    """Tests for operation logging."""
    
    def test_operations_are_logged(self, mock_service):
        """Test that operations are recorded in log."""
        mock_service.track_files(["a.txt"])
        mock_service.track_and_link(1, ["b.txt"])
        
        log = mock_service.get_operation_log()
        
        assert len(log) == 3  # track_files + track_and_link (which includes track_files)
        assert log[0]["operation"] == "track_files"
    
    def test_clear_resets_state(self, mock_service):
        """Test that clear resets all state."""
        mock_service.track_files(["a.txt"])
        mock_service.track_and_link(1, ["b.txt"])
        
        mock_service.clear()
        
        assert mock_service.get_commit_count() == 0
        assert mock_service.get_link_count() == 0
        assert len(mock_service.get_operation_log()) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Interface Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestInterfaceCompliance:
    """Tests to verify MockFileTrackingService implements IFileTrackingService."""
    
    def test_implements_interface(self, mock_service):
        """Test that mock service implements the interface."""
        assert isinstance(mock_service, IFileTrackingService)
    
    def test_all_interface_methods_exist(self, mock_service):
        """Test that all interface methods are implemented."""
        methods = [
            "track_files",
            "track_and_link",
            "link_to_checkpoint",
            "restore_from_checkpoint",
            "restore_commit",
            "get_commit_for_checkpoint",
            "get_tracked_files",
            "is_available",
        ]
        
        for method in methods:
            assert hasattr(mock_service, method)
            assert callable(getattr(mock_service, method))
