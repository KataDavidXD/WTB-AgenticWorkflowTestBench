"""
Unit Tests for File Processing Package.

Tests the refactored file_processing package modules:
- exceptions.py
- value_objects.py
- entities.py
- checkpoint_link.py

Coverage:
- Value object validation
- Entity invariants
- Factory methods
- Serialization/deserialization
"""

import pytest
from datetime import datetime
from pathlib import Path
import tempfile
import uuid

from wtb.domain.models.file_processing import (
    # Value Objects
    BlobId,
    CommitId,
    # Entities
    FileMemento,
    FileCommit,
    CommitStatus,
    CheckpointFileLink,
    # Exceptions
    FileProcessingError,
    DuplicateFileError,
    InvalidBlobIdError,
    InvalidCommitIdError,
    CommitAlreadyFinalized,
)


# ═══════════════════════════════════════════════════════════════════════════════
# BlobId Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBlobId:
    """Tests for BlobId value object."""
    
    def test_valid_blob_id(self):
        """Test creating BlobId with valid SHA-256 hash."""
        valid_hash = "a" * 64
        blob_id = BlobId(valid_hash)
        assert blob_id.value == valid_hash
    
    def test_blob_id_from_content(self):
        """Test creating BlobId from content bytes."""
        content = b"hello world"
        blob_id = BlobId.from_content(content)
        
        # SHA-256 of "hello world"
        expected = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        assert blob_id.value == expected
    
    def test_blob_id_from_file(self, tmp_path):
        """Test creating BlobId from file."""
        test_file = tmp_path / "test.txt"
        test_file.write_bytes(b"test content")
        
        blob_id = BlobId.from_file(str(test_file))
        assert len(blob_id.value) == 64
    
    def test_invalid_blob_id_length(self):
        """Test BlobId validation - wrong length."""
        with pytest.raises(InvalidBlobIdError, match="64 hex characters"):
            BlobId("tooshort")
    
    def test_invalid_blob_id_not_hex(self):
        """Test BlobId validation - not hexadecimal."""
        with pytest.raises(InvalidBlobIdError, match="hexadecimal"):
            BlobId("z" * 64)
    
    def test_invalid_blob_id_not_string(self):
        """Test BlobId validation - not a string."""
        with pytest.raises(InvalidBlobIdError, match="must be string"):
            BlobId(12345)  # type: ignore
    
    def test_blob_id_storage_path(self):
        """Test Git-like storage path generation."""
        blob_id = BlobId("ab" + "c" * 62)
        assert blob_id.storage_path == "objects/ab/" + "c" * 62
    
    def test_blob_id_short(self):
        """Test short representation."""
        blob_id = BlobId("abcdef01" + "0" * 56)
        assert blob_id.short == "abcdef01"
    
    def test_blob_id_serialization(self):
        """Test to_dict and from_dict."""
        blob_id = BlobId("a" * 64)
        data = blob_id.to_dict()
        restored = BlobId.from_dict(data)
        assert restored == blob_id
    
    def test_blob_id_immutable(self):
        """Test BlobId is immutable."""
        blob_id = BlobId("a" * 64)
        with pytest.raises(AttributeError):
            blob_id.value = "b" * 64  # type: ignore


# ═══════════════════════════════════════════════════════════════════════════════
# CommitId Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCommitId:
    """Tests for CommitId value object."""
    
    def test_valid_commit_id(self):
        """Test creating CommitId with valid UUID."""
        valid_uuid = str(uuid.uuid4())
        commit_id = CommitId(valid_uuid)
        assert commit_id.value == valid_uuid
    
    def test_commit_id_generate(self):
        """Test generating new CommitId."""
        commit_id = CommitId.generate()
        # Should be valid UUID
        uuid.UUID(commit_id.value)
    
    def test_invalid_commit_id_format(self):
        """Test CommitId validation - invalid UUID."""
        with pytest.raises(InvalidCommitIdError, match="valid UUID"):
            CommitId("not-a-uuid")
    
    def test_invalid_commit_id_not_string(self):
        """Test CommitId validation - not a string."""
        with pytest.raises(InvalidCommitIdError, match="must be string"):
            CommitId(12345)  # type: ignore
    
    def test_commit_id_short(self):
        """Test short representation."""
        commit_id = CommitId("12345678-1234-1234-1234-123456789012")
        assert commit_id.short == "12345678"
    
    def test_commit_id_serialization(self):
        """Test to_dict and from_dict."""
        commit_id = CommitId.generate()
        data = commit_id.to_dict()
        restored = CommitId.from_dict(data)
        assert restored == commit_id


# ═══════════════════════════════════════════════════════════════════════════════
# FileMemento Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileMemento:
    """Tests for FileMemento value object."""
    
    @pytest.fixture
    def valid_blob_id(self):
        return BlobId("a" * 64)
    
    def test_create_memento(self, valid_blob_id):
        """Test creating FileMemento."""
        memento = FileMemento(
            file_path="data/test.csv",
            file_hash=valid_blob_id,
            file_size=1024,
        )
        assert memento.file_path == "data/test.csv"
        assert memento.file_hash == valid_blob_id
        assert memento.file_size == 1024
    
    def test_memento_from_path_and_hash(self, valid_blob_id):
        """Test factory method from_path_and_hash."""
        memento = FileMemento.from_path_and_hash(
            "test.txt",
            valid_blob_id,
            512,
        )
        assert memento.file_path == "test.txt"
        assert memento.file_size == 512
    
    def test_memento_from_file(self, tmp_path):
        """Test factory method from_file."""
        test_file = tmp_path / "test.txt"
        test_file.write_bytes(b"test content 12345")
        
        memento, content = FileMemento.from_file(str(test_file))
        
        assert memento.file_path == str(test_file)
        assert memento.file_size == 18
        assert content == b"test content 12345"
    
    def test_memento_from_file_not_found(self):
        """Test from_file with non-existent file."""
        with pytest.raises(FileNotFoundError):
            FileMemento.from_file("/nonexistent/file.txt")
    
    def test_memento_empty_path_validation(self, valid_blob_id):
        """Test validation - empty path."""
        with pytest.raises(ValueError, match="file_path cannot be empty"):
            FileMemento("", valid_blob_id, 100)
    
    def test_memento_negative_size_validation(self, valid_blob_id):
        """Test validation - negative size."""
        with pytest.raises(ValueError, match="cannot be negative"):
            FileMemento("test.txt", valid_blob_id, -100)
    
    def test_memento_properties(self, valid_blob_id):
        """Test memento properties."""
        memento = FileMemento("path/to/file.csv", valid_blob_id, 1024 * 1024)
        
        assert memento.filename == "file.csv"
        assert memento.extension == ".csv"
        assert memento.size_kb == 1024.0
        assert memento.size_mb == 1.0
    
    def test_memento_serialization(self, valid_blob_id):
        """Test to_dict and from_dict."""
        memento = FileMemento("test.txt", valid_blob_id, 256)
        data = memento.to_dict()
        restored = FileMemento.from_dict(data)
        assert restored == memento
    
    def test_memento_immutable(self, valid_blob_id):
        """Test FileMemento is immutable."""
        memento = FileMemento("test.txt", valid_blob_id, 100)
        with pytest.raises(AttributeError):
            memento.file_path = "new.txt"  # type: ignore


# ═══════════════════════════════════════════════════════════════════════════════
# FileCommit Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileCommit:
    """Tests for FileCommit aggregate root."""
    
    @pytest.fixture
    def blob_id(self):
        return BlobId("a" * 64)
    
    @pytest.fixture
    def memento(self, blob_id):
        return FileMemento("test.txt", blob_id, 100)
    
    @pytest.fixture
    def memento2(self, blob_id):
        return FileMemento("test2.txt", blob_id, 200)
    
    def test_create_commit(self):
        """Test creating FileCommit with factory method."""
        commit = FileCommit.create(
            message="Test commit",
            created_by="test_user",
            execution_id="exec-123",
        )
        
        assert commit.message == "Test commit"
        assert commit.created_by == "test_user"
        assert commit.execution_id == "exec-123"
        assert commit.status == CommitStatus.PENDING
        assert commit.file_count == 0
    
    def test_add_memento(self, memento):
        """Test adding memento to commit."""
        commit = FileCommit.create()
        commit.add_memento(memento)
        
        assert commit.file_count == 1
        assert commit.mementos[0] == memento
    
    def test_add_duplicate_memento(self, memento):
        """Test adding duplicate file path - should raise."""
        commit = FileCommit.create()
        commit.add_memento(memento)
        
        duplicate = FileMemento(memento.file_path, memento.file_hash, 50)
        with pytest.raises(DuplicateFileError):
            commit.add_memento(duplicate)
    
    def test_finalize_commit(self, memento):
        """Test finalizing commit."""
        commit = FileCommit.create()
        commit.add_memento(memento)
        commit.finalize()
        
        assert commit.status == CommitStatus.FINALIZED
        assert commit.is_finalized
    
    def test_finalize_empty_commit(self):
        """Test finalizing empty commit - should raise."""
        commit = FileCommit.create()
        with pytest.raises(ValueError, match="no mementos"):
            commit.finalize()
    
    def test_finalize_already_finalized(self, memento):
        """Test finalizing already finalized commit."""
        commit = FileCommit.create()
        commit.add_memento(memento)
        commit.finalize()
        
        with pytest.raises(CommitAlreadyFinalized):
            commit.finalize()
    
    def test_add_to_finalized_commit(self, memento, memento2):
        """Test adding memento to finalized commit."""
        commit = FileCommit.create()
        commit.add_memento(memento)
        commit.finalize()
        
        with pytest.raises(CommitAlreadyFinalized):
            commit.add_memento(memento2)
    
    def test_mark_verified(self, memento):
        """Test marking commit as verified."""
        commit = FileCommit.create()
        commit.add_memento(memento)
        commit.finalize()
        commit.mark_verified()
        
        assert commit.status == CommitStatus.VERIFIED
    
    def test_mark_verified_not_finalized(self, memento):
        """Test marking non-finalized commit as verified."""
        commit = FileCommit.create()
        commit.add_memento(memento)
        
        with pytest.raises(ValueError, match="finalized"):
            commit.mark_verified()
    
    def test_commit_properties(self, memento, memento2):
        """Test commit properties."""
        commit = FileCommit.create()
        commit.add_memento(memento)
        commit.add_memento(memento2)
        
        assert commit.file_count == 2
        assert commit.total_size == 300
        assert commit.file_paths == ["test.txt", "test2.txt"]
        assert commit.is_pending
        assert not commit.is_finalized
    
    def test_get_memento(self, memento, memento2):
        """Test getting memento by path."""
        commit = FileCommit.create()
        commit.add_memento(memento)
        commit.add_memento(memento2)
        
        assert commit.get_memento("test.txt") == memento
        assert commit.get_memento("nonexistent.txt") is None
    
    def test_link_to_checkpoint(self, memento):
        """Test linking commit to checkpoint."""
        commit = FileCommit.create()
        commit.add_memento(memento)
        commit.link_to_checkpoint(42)
        
        assert commit.checkpoint_id == 42
    
    def test_commit_serialization(self, memento):
        """Test to_dict and from_dict."""
        commit = FileCommit.create(message="Test")
        commit.add_memento(memento)
        
        data = commit.to_dict()
        restored = FileCommit.from_dict(data)
        
        assert restored.commit_id == commit.commit_id
        assert restored.message == commit.message
        assert restored.file_count == commit.file_count
    
    def test_mementos_returns_copy(self, memento):
        """Test mementos property returns copy."""
        commit = FileCommit.create()
        commit.add_memento(memento)
        
        mementos = commit.mementos
        mementos.clear()  # Modify returned list
        
        # Original should be unchanged
        assert commit.file_count == 1


# ═══════════════════════════════════════════════════════════════════════════════
# CheckpointFileLink Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCheckpointFileLink:
    """Tests for CheckpointFileLink association entity."""
    
    @pytest.fixture
    def commit(self):
        blob_id = BlobId("a" * 64)
        memento = FileMemento("test.txt", blob_id, 1024)
        commit = FileCommit.create(message="Test")
        commit.add_memento(memento)
        return commit
    
    def test_create_link_from_commit(self, commit):
        """Test creating link from commit."""
        link = CheckpointFileLink.create(
            checkpoint_id=42,
            commit=commit,
        )
        
        assert link.checkpoint_id == 42
        assert link.commit_id == commit.commit_id
        assert link.file_count == 1
        assert link.total_size_bytes == 1024
    
    def test_create_link_from_values(self):
        """Test creating link from values."""
        commit_id = CommitId.generate()
        link = CheckpointFileLink.create_from_values(
            checkpoint_id=99,
            commit_id=commit_id,
            file_count=5,
            total_size_bytes=5000,
        )
        
        assert link.checkpoint_id == 99
        assert link.commit_id == commit_id
        assert link.file_count == 5
    
    def test_link_properties(self, commit):
        """Test link properties."""
        link = CheckpointFileLink.create(42, commit)
        assert link.total_size_mb == 1024 / (1024 * 1024)
    
    def test_link_serialization(self, commit):
        """Test to_dict and from_dict."""
        link = CheckpointFileLink.create(42, commit)
        
        data = link.to_dict()
        restored = CheckpointFileLink.from_dict(data)
        
        assert restored.checkpoint_id == link.checkpoint_id
        assert restored.commit_id == link.commit_id
        assert restored.file_count == link.file_count


# ═══════════════════════════════════════════════════════════════════════════════
# Exception Hierarchy Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestExceptionHierarchy:
    """Tests for exception hierarchy."""
    
    def test_all_exceptions_inherit_from_base(self):
        """Test all exceptions inherit from FileProcessingError."""
        exceptions = [
            DuplicateFileError,
            InvalidBlobIdError,
            InvalidCommitIdError,
            CommitAlreadyFinalized,
        ]
        for exc_class in exceptions:
            assert issubclass(exc_class, FileProcessingError)
    
    def test_can_catch_all_with_base(self):
        """Test catching all exceptions with base class."""
        with pytest.raises(FileProcessingError):
            raise DuplicateFileError("test")
        
        with pytest.raises(FileProcessingError):
            raise InvalidBlobIdError("test")
        
        with pytest.raises(FileProcessingError):
            raise InvalidCommitIdError("test")
        
        with pytest.raises(FileProcessingError):
            raise CommitAlreadyFinalized("test")


# ═══════════════════════════════════════════════════════════════════════════════
# Package Import Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestPackageImports:
    """Tests for backward-compatible imports."""
    
    def test_all_exports_available(self):
        """Test all expected exports are available from package."""
        from wtb.domain.models.file_processing import (
            # Value Objects
            BlobId,
            CommitId,
            FileMemento,
            # Entities
            FileCommit,
            CheckpointFileLink,
            CommitStatus,
            # Errors
            FileProcessingError,
            DuplicateFileError,
            InvalidBlobIdError,
            InvalidCommitIdError,
            CommitAlreadyFinalized,
        )
        
        # All imports succeeded
        assert BlobId is not None
        assert CommitId is not None
        assert FileMemento is not None
        assert FileCommit is not None
        assert CheckpointFileLink is not None
        assert CommitStatus is not None
        assert FileProcessingError is not None
    
    def test_import_from_domain_models(self):
        """Test imports work through domain models __init__."""
        from wtb.domain.models import (
            FileCommit,
            FileMemento,
            BlobId,
            CommitId,
        )
        
        assert FileCommit is not None
        assert BlobId is not None
