"""
Unit tests for File Processing Domain Models.

Tests domain models, value objects, and business logic.

Test Categories:
1. BlobId value object
2. CommitId value object
3. FileMemento value object
4. FileCommit entity
5. CheckpointFileLink association
6. Domain invariants and validation
"""

import pytest
import tempfile
from datetime import datetime
from pathlib import Path

from wtb.domain.models.file_processing import (
    BlobId,
    CommitId,
    FileMemento,
    FileCommit,
    CheckpointFileLink,
    CommitStatus,
    InvalidBlobIdError,
    InvalidCommitIdError,
    DuplicateFileError,
    CommitAlreadyFinalized,
)


# ═══════════════════════════════════════════════════════════════════════════════
# BlobId Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBlobId:
    """Tests for BlobId value object."""
    
    def test_create_valid_blob_id(self):
        """Test creating a valid BlobId."""
        # SHA-256 produces 64 hex characters
        hash_value = "a" * 64
        blob_id = BlobId(value=hash_value)
        assert blob_id.value == hash_value
    
    def test_blob_id_invalid_length(self):
        """Test that invalid length raises error."""
        with pytest.raises(InvalidBlobIdError):
            BlobId(value="abc")  # Too short
        
        with pytest.raises(InvalidBlobIdError):
            BlobId(value="a" * 65)  # Too long
    
    def test_blob_id_invalid_characters(self):
        """Test that non-hex characters raise error."""
        with pytest.raises(InvalidBlobIdError):
            BlobId(value="g" * 64)  # 'g' is not hex
    
    def test_blob_id_from_content(self):
        """Test creating BlobId from content bytes."""
        content = b"Hello, World!"
        blob_id = BlobId.from_content(content)
        
        # SHA-256 of "Hello, World!" is known
        assert len(blob_id.value) == 64
        assert blob_id.value == "dffd6021bb2bd5b0af676290809ec3a53191dd81c7f70a4b28688a362182986f"
    
    def test_blob_id_from_file(self, tmp_path):
        """Test creating BlobId from file."""
        file_path = tmp_path / "test.txt"
        file_path.write_bytes(b"Test content")
        
        blob_id = BlobId.from_file(str(file_path))
        assert len(blob_id.value) == 64
    
    def test_blob_id_storage_path(self):
        """Test storage path generation."""
        blob_id = BlobId(value="ab" + "c" * 62)
        assert blob_id.storage_path == "objects/ab/" + "c" * 62
    
    def test_blob_id_short(self):
        """Test short representation."""
        blob_id = BlobId(value="abcdef12" + "0" * 56)
        assert blob_id.short == "abcdef12"
    
    def test_blob_id_immutable(self):
        """Test that BlobId is immutable."""
        blob_id = BlobId(value="a" * 64)
        with pytest.raises(AttributeError):
            blob_id.value = "b" * 64
    
    def test_blob_id_serialization(self):
        """Test to_dict and from_dict."""
        blob_id = BlobId(value="a" * 64)
        data = blob_id.to_dict()
        restored = BlobId.from_dict(data)
        assert restored.value == blob_id.value
    
    def test_blob_id_equality(self):
        """Test value equality."""
        id1 = BlobId(value="a" * 64)
        id2 = BlobId(value="a" * 64)
        id3 = BlobId(value="b" * 64)
        
        assert id1 == id2
        assert id1 != id3


# ═══════════════════════════════════════════════════════════════════════════════
# CommitId Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCommitId:
    """Tests for CommitId value object."""
    
    def test_create_valid_commit_id(self):
        """Test creating a valid CommitId with UUID."""
        uuid_str = "12345678-1234-1234-1234-123456789012"
        commit_id = CommitId(value=uuid_str)
        assert commit_id.value == uuid_str
    
    def test_commit_id_invalid_uuid(self):
        """Test that invalid UUID raises error."""
        with pytest.raises(InvalidCommitIdError):
            CommitId(value="not-a-uuid")
    
    def test_commit_id_generate(self):
        """Test generating a new unique CommitId."""
        commit_id = CommitId.generate()
        assert len(commit_id.value) == 36  # Standard UUID format
        
        # Should be unique
        commit_id2 = CommitId.generate()
        assert commit_id.value != commit_id2.value
    
    def test_commit_id_short(self):
        """Test short representation."""
        commit_id = CommitId(value="12345678-1234-1234-1234-123456789012")
        assert commit_id.short == "12345678"
    
    def test_commit_id_serialization(self):
        """Test to_dict and from_dict."""
        commit_id = CommitId.generate()
        data = commit_id.to_dict()
        restored = CommitId.from_dict(data)
        assert restored.value == commit_id.value


# ═══════════════════════════════════════════════════════════════════════════════
# FileMemento Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileMemento:
    """Tests for FileMemento value object."""
    
    def test_create_memento(self):
        """Test creating a FileMemento."""
        blob_id = BlobId(value="a" * 64)
        memento = FileMemento(
            file_path="/data/model.pkl",
            file_hash=blob_id,
            file_size=1024,
        )
        
        assert memento.file_path == "/data/model.pkl"
        assert memento.file_hash == blob_id
        assert memento.file_size == 1024
    
    def test_memento_immutable(self):
        """Test that FileMemento is immutable."""
        blob_id = BlobId(value="a" * 64)
        memento = FileMemento(
            file_path="/data/model.pkl",
            file_hash=blob_id,
            file_size=1024,
        )
        
        with pytest.raises(AttributeError):
            memento.file_path = "/other/path"
    
    def test_memento_from_path_and_hash(self):
        """Test factory method."""
        blob_id = BlobId(value="a" * 64)
        memento = FileMemento.from_path_and_hash(
            file_path="data.csv",
            file_hash=blob_id,
            file_size=2048,
        )
        
        assert memento.file_path == "data.csv"
        assert memento.file_size == 2048
    
    def test_memento_from_file(self, tmp_path):
        """Test creating memento from actual file."""
        file_path = tmp_path / "test.txt"
        content = b"Test file content for memento"
        file_path.write_bytes(content)
        
        memento, read_content = FileMemento.from_file(str(file_path))
        
        assert memento.file_path == str(file_path)
        assert memento.file_size == len(content)
        assert read_content == content
        assert memento.file_hash == BlobId.from_content(content)
    
    def test_memento_from_file_not_found(self):
        """Test error when file not found."""
        with pytest.raises(FileNotFoundError):
            FileMemento.from_file("/nonexistent/file.txt")
    
    def test_memento_filename(self):
        """Test filename extraction."""
        blob_id = BlobId(value="a" * 64)
        memento = FileMemento(
            file_path="/data/models/classifier.pkl",
            file_hash=blob_id,
            file_size=1024,
        )
        assert memento.filename == "classifier.pkl"
    
    def test_memento_extension(self):
        """Test extension extraction."""
        blob_id = BlobId(value="a" * 64)
        memento = FileMemento(
            file_path="/data/config.json",
            file_hash=blob_id,
            file_size=512,
        )
        assert memento.extension == ".json"
    
    def test_memento_size_conversions(self):
        """Test size unit conversions."""
        blob_id = BlobId(value="a" * 64)
        memento = FileMemento(
            file_path="large_file.bin",
            file_hash=blob_id,
            file_size=1024 * 1024,  # 1 MB
        )
        
        assert memento.size_kb == 1024.0
        assert memento.size_mb == 1.0
    
    def test_memento_validation_empty_path(self):
        """Test validation rejects empty path."""
        blob_id = BlobId(value="a" * 64)
        with pytest.raises(ValueError):
            FileMemento(file_path="", file_hash=blob_id, file_size=100)
    
    def test_memento_validation_negative_size(self):
        """Test validation rejects negative size."""
        blob_id = BlobId(value="a" * 64)
        with pytest.raises(ValueError):
            FileMemento(file_path="file.txt", file_hash=blob_id, file_size=-1)
    
    def test_memento_serialization(self):
        """Test to_dict and from_dict."""
        blob_id = BlobId(value="a" * 64)
        memento = FileMemento(
            file_path="/data/file.txt",
            file_hash=blob_id,
            file_size=512,
        )
        
        data = memento.to_dict()
        restored = FileMemento.from_dict(data)
        
        assert restored.file_path == memento.file_path
        assert restored.file_hash == memento.file_hash
        assert restored.file_size == memento.file_size


# ═══════════════════════════════════════════════════════════════════════════════
# FileCommit Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileCommit:
    """Tests for FileCommit entity."""
    
    def test_create_commit(self):
        """Test creating a FileCommit."""
        commit = FileCommit.create(message="Initial snapshot")
        
        assert commit.message == "Initial snapshot"
        assert commit.status == CommitStatus.PENDING
        assert commit.file_count == 0
        assert isinstance(commit.commit_id, CommitId)
    
    def test_create_commit_with_id(self):
        """Test creating commit with specific ID."""
        commit_id = CommitId.generate()
        commit = FileCommit.create(message="Test", commit_id=commit_id)
        
        assert commit.commit_id == commit_id
    
    def test_add_memento(self):
        """Test adding mementos to commit."""
        commit = FileCommit.create(message="Snapshot")
        blob_id = BlobId(value="a" * 64)
        memento = FileMemento(
            file_path="data.csv",
            file_hash=blob_id,
            file_size=1024,
        )
        
        commit.add_memento(memento)
        
        assert commit.file_count == 1
        assert commit.total_size == 1024
        assert commit.file_paths == ["data.csv"]
    
    def test_add_multiple_mementos(self):
        """Test adding multiple mementos."""
        commit = FileCommit.create()
        
        for i in range(3):
            blob_id = BlobId(value=f"{i:064d}")
            memento = FileMemento(
                file_path=f"file{i}.txt",
                file_hash=blob_id,
                file_size=100 * (i + 1),
            )
            commit.add_memento(memento)
        
        assert commit.file_count == 3
        assert commit.total_size == 100 + 200 + 300
    
    def test_add_duplicate_file_path_rejected(self):
        """Test that duplicate file paths are rejected."""
        commit = FileCommit.create()
        blob_id1 = BlobId(value="a" * 64)
        blob_id2 = BlobId(value="b" * 64)
        
        memento1 = FileMemento(file_path="data.csv", file_hash=blob_id1, file_size=100)
        memento2 = FileMemento(file_path="data.csv", file_hash=blob_id2, file_size=200)
        
        commit.add_memento(memento1)
        
        with pytest.raises(DuplicateFileError):
            commit.add_memento(memento2)
    
    def test_finalize_commit(self):
        """Test finalizing a commit."""
        commit = FileCommit.create()
        blob_id = BlobId(value="a" * 64)
        memento = FileMemento(file_path="file.txt", file_hash=blob_id, file_size=100)
        commit.add_memento(memento)
        
        commit.finalize()
        
        assert commit.status == CommitStatus.FINALIZED
        assert commit.is_finalized
    
    def test_finalize_empty_commit_rejected(self):
        """Test that empty commit cannot be finalized."""
        commit = FileCommit.create()
        
        with pytest.raises(ValueError):
            commit.finalize()
    
    def test_add_to_finalized_commit_rejected(self):
        """Test that adding to finalized commit is rejected."""
        commit = FileCommit.create()
        blob_id = BlobId(value="a" * 64)
        memento1 = FileMemento(file_path="file1.txt", file_hash=blob_id, file_size=100)
        memento2 = FileMemento(file_path="file2.txt", file_hash=blob_id, file_size=100)
        
        commit.add_memento(memento1)
        commit.finalize()
        
        with pytest.raises(CommitAlreadyFinalized):
            commit.add_memento(memento2)
    
    def test_double_finalize_rejected(self):
        """Test that double finalization is rejected."""
        commit = FileCommit.create()
        blob_id = BlobId(value="a" * 64)
        memento = FileMemento(file_path="file.txt", file_hash=blob_id, file_size=100)
        commit.add_memento(memento)
        commit.finalize()
        
        with pytest.raises(CommitAlreadyFinalized):
            commit.finalize()
    
    def test_mark_verified(self):
        """Test marking commit as verified."""
        commit = FileCommit.create()
        blob_id = BlobId(value="a" * 64)
        memento = FileMemento(file_path="file.txt", file_hash=blob_id, file_size=100)
        commit.add_memento(memento)
        commit.finalize()
        
        commit.mark_verified()
        
        assert commit.status == CommitStatus.VERIFIED
    
    def test_get_memento(self):
        """Test retrieving memento by path."""
        commit = FileCommit.create()
        blob_id = BlobId(value="a" * 64)
        memento = FileMemento(file_path="data.csv", file_hash=blob_id, file_size=100)
        commit.add_memento(memento)
        
        found = commit.get_memento("data.csv")
        assert found == memento
        
        not_found = commit.get_memento("other.txt")
        assert not_found is None
    
    def test_mementos_property_returns_copy(self):
        """Test that mementos property returns a copy."""
        commit = FileCommit.create()
        blob_id = BlobId(value="a" * 64)
        memento = FileMemento(file_path="file.txt", file_hash=blob_id, file_size=100)
        commit.add_memento(memento)
        
        mementos = commit.mementos
        mementos.clear()  # Modify the returned list
        
        assert commit.file_count == 1  # Original unchanged
    
    def test_file_hashes_property(self):
        """Test file_hashes property."""
        commit = FileCommit.create()
        blob_id1 = BlobId(value="a" * 64)
        blob_id2 = BlobId(value="b" * 64)
        
        commit.add_memento(FileMemento(file_path="file1.txt", file_hash=blob_id1, file_size=100))
        commit.add_memento(FileMemento(file_path="file2.txt", file_hash=blob_id2, file_size=200))
        
        hashes = commit.file_hashes
        assert hashes == {
            "file1.txt": "a" * 64,
            "file2.txt": "b" * 64,
        }
    
    def test_link_to_checkpoint(self):
        """Test linking commit to checkpoint."""
        commit = FileCommit.create()
        commit.link_to_checkpoint(42)
        
        assert commit.checkpoint_id == 42
    
    def test_commit_serialization(self):
        """Test to_dict and from_dict."""
        commit = FileCommit.create(
            message="Test commit",
            created_by="test_user",
            execution_id="exec-123",
        )
        blob_id = BlobId(value="a" * 64)
        commit.add_memento(FileMemento(file_path="file.txt", file_hash=blob_id, file_size=100))
        
        data = commit.to_dict()
        restored = FileCommit.from_dict(data)
        
        assert restored.message == commit.message
        assert restored.created_by == commit.created_by
        assert restored.execution_id == commit.execution_id
        assert restored.file_count == commit.file_count
    
    def test_reconstitute_commit(self):
        """Test reconstituting commit from persistence data."""
        blob_id = BlobId(value="a" * 64)
        mementos = [
            FileMemento(file_path="file1.txt", file_hash=blob_id, file_size=100),
            FileMemento(file_path="file2.txt", file_hash=blob_id, file_size=200),
        ]
        
        commit = FileCommit.reconstitute(
            commit_id="12345678-1234-1234-1234-123456789012",
            message="Restored commit",
            timestamp=datetime(2026, 1, 15, 12, 0, 0),
            status="finalized",
            mementos=mementos,
            created_by="system",
            execution_id="exec-456",
            checkpoint_id=99,
        )
        
        assert commit.commit_id.value == "12345678-1234-1234-1234-123456789012"
        assert commit.status == CommitStatus.FINALIZED
        assert commit.file_count == 2
        assert commit.checkpoint_id == 99


# ═══════════════════════════════════════════════════════════════════════════════
# CheckpointFileLink Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCheckpointFileLink:
    """Tests for CheckpointFileLink association."""
    
    def test_create_link(self):
        """Test creating a checkpoint-file link."""
        commit_id = CommitId.generate()
        link = CheckpointFileLink(
            checkpoint_id=42,
            commit_id=commit_id,
            linked_at=datetime.now(),
            file_count=3,
            total_size_bytes=1024,
        )
        
        assert link.checkpoint_id == 42
        assert link.commit_id == commit_id
        assert link.file_count == 3
    
    def test_create_link_from_commit(self):
        """Test creating link from commit."""
        commit = FileCommit.create()
        blob_id = BlobId(value="a" * 64)
        commit.add_memento(FileMemento(file_path="file1.txt", file_hash=blob_id, file_size=500))
        commit.add_memento(FileMemento(file_path="file2.txt", file_hash=blob_id, file_size=500))
        
        link = CheckpointFileLink.create(checkpoint_id=42, commit=commit)
        
        assert link.checkpoint_id == 42
        assert link.commit_id == commit.commit_id
        assert link.file_count == 2
        assert link.total_size_bytes == 1000
    
    def test_link_serialization(self):
        """Test to_dict and from_dict."""
        commit_id = CommitId.generate()
        link = CheckpointFileLink(
            checkpoint_id=42,
            commit_id=commit_id,
            linked_at=datetime.now(),
            file_count=3,
            total_size_bytes=1024,
        )
        
        data = link.to_dict()
        restored = CheckpointFileLink.from_dict(data)
        
        assert restored.checkpoint_id == link.checkpoint_id
        assert restored.commit_id.value == link.commit_id.value
        assert restored.file_count == link.file_count


# ═══════════════════════════════════════════════════════════════════════════════
# CommitStatus Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCommitStatus:
    """Tests for CommitStatus enum."""
    
    def test_status_values(self):
        """Test status enum values."""
        assert CommitStatus.PENDING.value == "pending"
        assert CommitStatus.FINALIZED.value == "finalized"
        assert CommitStatus.VERIFIED.value == "verified"
    
    def test_status_from_string(self):
        """Test creating status from string."""
        assert CommitStatus("pending") == CommitStatus.PENDING
        assert CommitStatus("finalized") == CommitStatus.FINALIZED
        assert CommitStatus("verified") == CommitStatus.VERIFIED
