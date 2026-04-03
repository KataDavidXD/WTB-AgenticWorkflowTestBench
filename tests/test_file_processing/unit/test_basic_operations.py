"""
Unit Tests for Basic File Operations with LangGraph.

Tests basic file tracking operations in the context of LangGraph workflow execution.

Test Categories:
1. File Memento Creation
2. File Commit Lifecycle
3. Blob Storage Operations
4. Checkpoint File Links
5. LangGraph State Integration

Design Principles:
- SOLID: Single responsibility per test
- ACID: Verify transaction properties
- Isolation: No external dependencies
"""

import pytest
from pathlib import Path
from datetime import datetime
from typing import TypedDict, Annotated
import operator
import uuid

from wtb.domain.models.file_processing import (
    BlobId,
    CommitId,
    FileMemento,
    FileCommit,
    CheckpointFileLink,
    CommitStatus,
    DuplicateFileError,
    CommitAlreadyFinalized,
    InvalidBlobIdError,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Test: File Memento Creation
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileMementoCreation:
    """Tests for FileMemento creation and properties."""
    
    def test_create_memento_from_existing_file(self, sample_files):
        """Test creating memento from real file."""
        csv_path = sample_files["csv"]
        
        memento, content = FileMemento.from_file(str(csv_path))
        
        assert memento.file_path == str(csv_path)
        assert memento.file_size == len(content)
        assert memento.file_hash is not None
        assert len(memento.file_hash.value) == 64  # SHA-256
    
    def test_memento_immutability(self, sample_files):
        """Test that memento is immutable (frozen dataclass)."""
        memento, _ = FileMemento.from_file(str(sample_files["csv"]))
        
        with pytest.raises(Exception):  # FrozenInstanceError
            memento.file_path = "new_path.csv"
    
    def test_memento_from_path_and_hash(self):
        """Test creating memento with pre-computed values."""
        blob_id = BlobId.from_content(b"test content")
        
        memento = FileMemento.from_path_and_hash(
            file_path="/data/test.csv",
            file_hash=blob_id,
            file_size=12,
        )
        
        assert memento.file_path == "/data/test.csv"
        assert memento.file_hash == blob_id
        assert memento.file_size == 12
    
    def test_memento_file_not_found(self):
        """Test error when file doesn't exist."""
        with pytest.raises(FileNotFoundError):
            FileMemento.from_file("/nonexistent/file.csv")
    
    def test_memento_properties(self, sample_files):
        """Test memento derived properties."""
        memento, _ = FileMemento.from_file(str(sample_files["csv"]))
        
        assert memento.filename == "data.csv"
        assert memento.extension == ".csv"
        assert memento.size_kb == memento.file_size / 1024
        assert memento.size_mb == memento.file_size / (1024 * 1024)
    
    def test_memento_serialization(self, sample_files):
        """Test memento to_dict and from_dict."""
        original, _ = FileMemento.from_file(str(sample_files["json"]))
        
        data = original.to_dict()
        restored = FileMemento.from_dict(data)
        
        assert restored.file_path == original.file_path
        assert restored.file_hash == original.file_hash
        assert restored.file_size == original.file_size


# ═══════════════════════════════════════════════════════════════════════════════
# Test: File Commit Lifecycle
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileCommitLifecycle:
    """Tests for FileCommit aggregate root lifecycle."""
    
    def test_create_commit(self):
        """Test creating a new commit."""
        commit = FileCommit.create(message="Initial commit")
        
        assert commit.commit_id is not None
        assert commit.message == "Initial commit"
        assert commit.status == CommitStatus.PENDING
        assert commit.file_count == 0
    
    def test_create_commit_with_execution_id(self):
        """Test commit creation with WTB execution context."""
        commit = FileCommit.create(
            message="Execution snapshot",
            execution_id="exec-123",
            created_by="test_user",
        )
        
        assert commit.execution_id == "exec-123"
        assert commit.created_by == "test_user"
    
    def test_add_memento_to_commit(self, sample_files):
        """Test adding mementos to commit."""
        commit = FileCommit.create(message="Test")
        memento, _ = FileMemento.from_file(str(sample_files["csv"]))
        
        commit.add_memento(memento)
        
        assert commit.file_count == 1
        assert memento in commit.mementos
    
    def test_add_multiple_mementos(self, sample_files):
        """Test adding multiple files to single commit."""
        commit = FileCommit.create(message="Multi-file")
        
        for key in ["csv", "json", "txt"]:
            memento, _ = FileMemento.from_file(str(sample_files[key]))
            commit.add_memento(memento)
        
        assert commit.file_count == 3
        assert len(commit.file_paths) == 3
    
    def test_duplicate_file_prevented(self, sample_files):
        """Test that duplicate files are rejected."""
        commit = FileCommit.create(message="Test")
        memento, _ = FileMemento.from_file(str(sample_files["csv"]))
        
        commit.add_memento(memento)
        
        with pytest.raises(DuplicateFileError):
            commit.add_memento(memento)  # Same file path
    
    def test_finalize_commit(self, sample_files):
        """Test finalizing a commit."""
        commit = FileCommit.create(message="Final")
        memento, _ = FileMemento.from_file(str(sample_files["csv"]))
        commit.add_memento(memento)
        
        commit.finalize()
        
        assert commit.status == CommitStatus.FINALIZED
        assert commit.is_finalized is True
        assert commit.is_pending is False
    
    def test_cannot_finalize_empty_commit(self):
        """Test that empty commits cannot be finalized."""
        commit = FileCommit.create(message="Empty")
        
        with pytest.raises(ValueError, match="no mementos"):
            commit.finalize()
    
    def test_cannot_add_to_finalized_commit(self, sample_files):
        """Test that finalized commits are immutable."""
        commit = FileCommit.create(message="Locked")
        memento1, _ = FileMemento.from_file(str(sample_files["csv"]))
        commit.add_memento(memento1)
        commit.finalize()
        
        memento2, _ = FileMemento.from_file(str(sample_files["json"]))
        
        with pytest.raises(CommitAlreadyFinalized):
            commit.add_memento(memento2)
    
    def test_commit_verify(self, sample_files):
        """Test marking commit as verified."""
        commit = FileCommit.create(message="Verify")
        memento, _ = FileMemento.from_file(str(sample_files["csv"]))
        commit.add_memento(memento)
        commit.finalize()
        
        commit.mark_verified()
        
        assert commit.status == CommitStatus.VERIFIED
    
    def test_commit_total_size(self, sample_files):
        """Test commit total size calculation."""
        commit = FileCommit.create(message="Sizes")
        
        total = 0
        for key in ["csv", "json"]:
            memento, _ = FileMemento.from_file(str(sample_files[key]))
            commit.add_memento(memento)
            total += memento.file_size
        
        assert commit.total_size == total
        assert commit.total_size_mb == total / (1024 * 1024)
    
    def test_commit_file_hashes(self, sample_files):
        """Test commit file hashes dictionary."""
        commit = FileCommit.create(message="Hashes")
        memento, _ = FileMemento.from_file(str(sample_files["csv"]))
        commit.add_memento(memento)
        
        hashes = commit.file_hashes
        
        assert str(sample_files["csv"]) in hashes
        assert hashes[str(sample_files["csv"])] == memento.file_hash.value
    
    def test_commit_serialization(self, sample_files):
        """Test commit serialization round-trip."""
        original = FileCommit.create(
            message="Serialize",
            execution_id="exec-456",
        )
        memento, _ = FileMemento.from_file(str(sample_files["csv"]))
        original.add_memento(memento)
        original.finalize()
        
        data = original.to_dict()
        restored = FileCommit.from_dict(data)
        
        assert restored.commit_id.value == original.commit_id.value
        assert restored.message == original.message
        assert restored.status == original.status
        assert restored.file_count == original.file_count
        assert restored.execution_id == original.execution_id
    
    def test_link_commit_to_checkpoint(self, sample_files):
        """Test linking commit to WTB checkpoint."""
        commit = FileCommit.create(message="Checkpoint")
        memento, _ = FileMemento.from_file(str(sample_files["csv"]))
        commit.add_memento(memento)
        
        commit.link_to_checkpoint(checkpoint_id=42)
        
        assert commit.checkpoint_id == 42


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Blob Storage Operations
# ═══════════════════════════════════════════════════════════════════════════════


class TestBlobStorageOperations:
    """Tests for blob storage operations."""
    
    def test_save_and_retrieve_blob(self, blob_repository, sample_files):
        """Test saving and retrieving blob content."""
        memento, content = FileMemento.from_file(str(sample_files["csv"]))
        
        saved_id = blob_repository.save(content)
        retrieved = blob_repository.get(saved_id)
        
        assert retrieved == content
        assert saved_id == memento.file_hash  # Content-addressable
    
    def test_blob_exists_check(self, blob_repository, sample_files):
        """Test checking blob existence."""
        memento, content = FileMemento.from_file(str(sample_files["csv"]))
        
        assert blob_repository.exists(memento.file_hash) is False
        
        blob_repository.save(content)
        
        assert blob_repository.exists(memento.file_hash) is True
    
    def test_blob_content_addressable(self, blob_repository):
        """Test that same content gets same blob ID."""
        content = b"Same content for both"
        blob_id1 = BlobId.from_content(content)
        blob_id2 = BlobId.from_content(content)
        
        assert blob_id1 == blob_id2
        
        # Save once - returns content-addressed BlobId
        saved_id = blob_repository.save(content)
        assert saved_id == blob_id1
        
        # Retrieve with second ID (same hash)
        retrieved = blob_repository.get(blob_id2)
        
        assert retrieved == content
    
    def test_blob_delete(self, blob_repository, sample_files):
        """Test deleting blob."""
        memento, content = FileMemento.from_file(str(sample_files["csv"]))
        blob_repository.save(content)
        
        success = blob_repository.delete(memento.file_hash)
        
        assert success is True
        assert blob_repository.exists(memento.file_hash) is False
    
    def test_blob_total_size(self, blob_repository, sample_files):
        """Test total blob storage size."""
        total = 0
        for key in ["csv", "json"]:
            memento, content = FileMemento.from_file(str(sample_files[key]))
            blob_repository.save(content)
            total += len(content)
        
        stats = blob_repository.get_stats()
        assert stats["total_size_bytes"] == total
    
    def test_large_blob(self, blob_repository, large_file):
        """Test handling large blobs (1MB)."""
        memento, content = FileMemento.from_file(str(large_file))
        
        blob_repository.save(content)
        retrieved = blob_repository.get(memento.file_hash)
        
        assert len(retrieved) == len(content)
        assert retrieved == content


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Checkpoint File Links
# ═══════════════════════════════════════════════════════════════════════════════


class TestCheckpointFileLinks:
    """Tests for checkpoint-commit linking."""
    
    def test_create_checkpoint_link(self, sample_files, commit_repository, checkpoint_link_repository):
        """Test creating checkpoint file link."""
        commit = FileCommit.create(message="Linked")
        memento, _ = FileMemento.from_file(str(sample_files["csv"]))
        commit.add_memento(memento)
        commit.finalize()
        
        link = CheckpointFileLink.create(checkpoint_id=1, commit=commit)
        
        assert link.checkpoint_id == 1
        assert link.commit_id == commit.commit_id
        assert link.file_count == 1
        assert link.total_size_bytes == commit.total_size
    
    def test_save_and_retrieve_link(self, sample_files, checkpoint_link_repository):
        """Test saving and retrieving checkpoint link."""
        commit = FileCommit.create(message="Link")
        memento, _ = FileMemento.from_file(str(sample_files["csv"]))
        commit.add_memento(memento)
        commit.finalize()
        
        link = CheckpointFileLink.create(checkpoint_id=42, commit=commit)
        checkpoint_link_repository.add(link)
        
        retrieved = checkpoint_link_repository.get_by_checkpoint(42)
        
        assert retrieved is not None
        assert retrieved.commit_id == commit.commit_id
    
    def test_find_links_by_commit(self, sample_files, checkpoint_link_repository):
        """Test finding all links for a commit."""
        commit = FileCommit.create(message="Multi-link")
        memento, _ = FileMemento.from_file(str(sample_files["csv"]))
        commit.add_memento(memento)
        commit.finalize()
        
        # Create multiple links to same commit
        for cp_id in [1, 2, 3]:
            link = CheckpointFileLink.create(checkpoint_id=cp_id, commit=commit)
            checkpoint_link_repository.add(link)
        
        links = checkpoint_link_repository.get_by_commit(commit.commit_id)
        
        assert len(links) == 3
    
    def test_link_serialization(self, sample_files):
        """Test link serialization round-trip."""
        commit = FileCommit.create(message="Serialize")
        memento, _ = FileMemento.from_file(str(sample_files["csv"]))
        commit.add_memento(memento)
        commit.finalize()
        
        original = CheckpointFileLink.create(checkpoint_id=99, commit=commit)
        
        data = original.to_dict()
        restored = CheckpointFileLink.from_dict(data)
        
        assert restored.checkpoint_id == original.checkpoint_id
        assert restored.commit_id.value == original.commit_id.value
        assert restored.file_count == original.file_count


# ═══════════════════════════════════════════════════════════════════════════════
# Test: BlobId Value Object
# ═══════════════════════════════════════════════════════════════════════════════


class TestBlobIdValueObject:
    """Tests for BlobId value object."""
    
    def test_blob_id_from_content(self):
        """Test creating blob ID from content."""
        content = b"Hello, World!"
        blob_id = BlobId.from_content(content)
        
        assert len(blob_id.value) == 64
        assert all(c in '0123456789abcdef' for c in blob_id.value)
    
    def test_blob_id_from_file(self, sample_files):
        """Test creating blob ID from file."""
        blob_id = BlobId.from_file(str(sample_files["csv"]))
        
        assert len(blob_id.value) == 64
    
    def test_blob_id_invalid_length(self):
        """Test that invalid length raises error."""
        with pytest.raises(InvalidBlobIdError, match="64 hex"):
            BlobId("abc123")  # Too short
    
    def test_blob_id_invalid_characters(self):
        """Test that non-hex characters raise error."""
        with pytest.raises(InvalidBlobIdError, match="hexadecimal"):
            BlobId("z" * 64)  # Invalid hex
    
    def test_blob_id_storage_path(self):
        """Test blob storage path generation."""
        blob_id = BlobId.from_content(b"test")
        
        path = blob_id.storage_path
        
        assert path.startswith("objects/")
        assert path == f"objects/{blob_id.value[:2]}/{blob_id.value[2:]}"
    
    def test_blob_id_equality(self):
        """Test blob ID equality for same content."""
        content = b"Same content"
        id1 = BlobId.from_content(content)
        id2 = BlobId.from_content(content)
        
        assert id1 == id2
        assert hash(id1) == hash(id2)
    
    def test_blob_id_short(self):
        """Test short representation."""
        blob_id = BlobId.from_content(b"test")
        
        assert len(blob_id.short) == 8
        assert blob_id.short == blob_id.value[:8]


# ═══════════════════════════════════════════════════════════════════════════════
# Test: CommitId Value Object
# ═══════════════════════════════════════════════════════════════════════════════


class TestCommitIdValueObject:
    """Tests for CommitId value object."""
    
    def test_commit_id_generate(self):
        """Test generating new commit ID."""
        commit_id = CommitId.generate()
        
        assert commit_id.value is not None
        assert len(commit_id.value) == 36  # UUID with hyphens
    
    def test_commit_id_uniqueness(self):
        """Test that generated IDs are unique."""
        ids = [CommitId.generate() for _ in range(100)]
        unique_ids = set(c.value for c in ids)
        
        assert len(unique_ids) == 100
    
    def test_commit_id_from_string(self):
        """Test creating commit ID from existing UUID string."""
        uuid_str = str(uuid.uuid4())
        commit_id = CommitId(uuid_str)
        
        assert commit_id.value == uuid_str
    
    def test_commit_id_invalid_format(self):
        """Test that invalid UUID format raises error."""
        from wtb.domain.models.file_processing import InvalidCommitIdError
        
        with pytest.raises(InvalidCommitIdError, match="valid UUID"):
            CommitId("not-a-uuid")
    
    def test_commit_id_serialization(self):
        """Test commit ID serialization."""
        original = CommitId.generate()
        
        data = original.to_dict()
        restored = CommitId.from_dict(data)
        
        assert restored == original


# ═══════════════════════════════════════════════════════════════════════════════
# Test: LangGraph State Integration
# ═══════════════════════════════════════════════════════════════════════════════


class TestLangGraphStateIntegration:
    """Tests for LangGraph state integration patterns."""
    
    def test_commit_in_workflow_state(self, sample_files):
        """Test storing commit info in workflow state."""
        # Simulate workflow state
        state = {
            "messages": [],
            "file_commits": {},
        }
        
        # Create commit
        commit = FileCommit.create(message="Workflow state commit")
        memento, _ = FileMemento.from_file(str(sample_files["csv"]))
        commit.add_memento(memento)
        commit.finalize()
        
        # Store in state
        state["file_commits"]["step_1"] = commit.commit_id.value
        
        assert "step_1" in state["file_commits"]
        assert state["file_commits"]["step_1"] == commit.commit_id.value
    
    def test_track_files_per_node(self, sample_files):
        """Test tracking files for each node execution."""
        node_commits = {}
        
        # Simulate multiple nodes tracking files
        for node_name in ["preprocess", "transform", "postprocess"]:
            commit = FileCommit.create(
                message=f"{node_name} output",
                execution_id="exec-123",
            )
            memento, _ = FileMemento.from_file(str(sample_files["csv"]))
            # Use different path for each node
            mock_memento = FileMemento.from_path_and_hash(
                file_path=f"/output/{node_name}/data.csv",
                file_hash=memento.file_hash,
                file_size=memento.file_size,
            )
            commit.add_memento(mock_memento)
            commit.finalize()
            node_commits[node_name] = commit.commit_id
        
        assert len(node_commits) == 3
        assert all(isinstance(cid, CommitId) for cid in node_commits.values())
    
    def test_checkpoint_correlation(self, sample_files, checkpoint_link_repository):
        """Test correlating file commits with LangGraph checkpoints."""
        # Simulate LangGraph checkpoint IDs (integers in MemorySaver)
        checkpoint_ids = [1, 2, 3, 4, 5]
        
        for cp_id in checkpoint_ids:
            commit = FileCommit.create(
                message=f"Checkpoint {cp_id}",
                execution_id="exec-test",
            )
            memento, _ = FileMemento.from_file(str(sample_files["csv"]))
            commit.add_memento(
                FileMemento.from_path_and_hash(
                    file_path=f"/step_{cp_id}/output.csv",
                    file_hash=memento.file_hash,
                    file_size=memento.file_size,
                )
            )
            commit.finalize()
            commit.link_to_checkpoint(cp_id)
            
            link = CheckpointFileLink.create(checkpoint_id=cp_id, commit=commit)
            checkpoint_link_repository.add(link)
        
        # Verify all links
        all_links = checkpoint_link_repository.list_all()
        assert len(all_links) == 5
        
        # Can retrieve specific checkpoint's files
        link = checkpoint_link_repository.get_by_checkpoint(3)
        assert link is not None
        assert link.checkpoint_id == 3


# ═══════════════════════════════════════════════════════════════════════════════
# Test: ACID Properties
# ═══════════════════════════════════════════════════════════════════════════════


class TestACIDProperties:
    """Tests verifying ACID property compliance."""
    
    def test_atomicity_commit_creation(self, sample_files, blob_repository, commit_repository):
        """Test atomicity: commit and blobs saved together or not at all."""
        commit = FileCommit.create(message="Atomic")
        
        # Add mementos and save blobs
        for key in ["csv", "json"]:
            memento, content = FileMemento.from_file(str(sample_files[key]))
            blob_repository.save(content)
            commit.add_memento(memento)
        
        commit.finalize()
        commit_repository.save(commit)
        
        # All or nothing - both commit and blobs exist
        assert commit_repository.exists(commit.commit_id)
        for memento in commit.mementos:
            assert blob_repository.exists(memento.file_hash)
    
    def test_consistency_commit_invariants(self, sample_files):
        """Test consistency: commit invariants always maintained."""
        commit = FileCommit.create(message="Consistent")
        
        # Cannot finalize empty commit
        with pytest.raises(ValueError):
            commit.finalize()
        
        # Add memento
        memento, _ = FileMemento.from_file(str(sample_files["csv"]))
        commit.add_memento(memento)
        commit.finalize()
        
        # Cannot add to finalized commit
        memento2, _ = FileMemento.from_file(str(sample_files["json"]))
        with pytest.raises(CommitAlreadyFinalized):
            commit.add_memento(memento2)
    
    def test_isolation_concurrent_commits(self, sample_files):
        """Test isolation: commits are independent."""
        commit1 = FileCommit.create(message="Commit 1", execution_id="exec-1")
        commit2 = FileCommit.create(message="Commit 2", execution_id="exec-2")
        
        memento, _ = FileMemento.from_file(str(sample_files["csv"]))
        
        # Same file tracked in different commits
        commit1.add_memento(memento)
        
        # Need different path for commit2
        memento2 = FileMemento.from_path_and_hash(
            file_path="/other/data.csv",
            file_hash=memento.file_hash,
            file_size=memento.file_size,
        )
        commit2.add_memento(memento2)
        
        # Commits are independent
        assert commit1.file_count == 1
        assert commit2.file_count == 1
        assert commit1.commit_id != commit2.commit_id
    
    def test_durability_serialization(self, sample_files, blob_repository, commit_repository):
        """Test durability: data survives serialization round-trip."""
        # Create and save
        commit = FileCommit.create(message="Durable")
        memento, content = FileMemento.from_file(str(sample_files["csv"]))
        blob_repository.save(content)
        commit.add_memento(memento)
        commit.finalize()
        commit_repository.save(commit)
        
        # Simulate persistence via serialization
        data = commit.to_dict()
        
        # Clear repositories
        commit_repository.clear()
        
        # Restore from serialized data
        restored = FileCommit.from_dict(data)
        
        # Data intact
        assert restored.commit_id.value == commit.commit_id.value
        assert restored.message == commit.message
        assert restored.file_count == commit.file_count
        assert restored.status == commit.status
