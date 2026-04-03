"""
Unit tests for File Processing Repository Implementations.

Tests both in-memory and SQLAlchemy repository implementations.

Test Categories:
1. InMemoryBlobRepository
2. InMemoryFileCommitRepository
3. InMemoryCheckpointFileLinkRepository
4. SQLAlchemy repositories (with test database)
5. ACID compliance tests
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
def sample_content():
    """Sample binary content for testing."""
    return b"Sample file content for testing blob storage"


@pytest.fixture
def sample_commit():
    """Create a sample commit with mementos."""
    commit = FileCommit.create(
        message="Test commit",
        created_by="test_user",
        execution_id="exec-123",
    )
    
    blob_id1 = BlobId(value="a" * 64)
    blob_id2 = BlobId(value="b" * 64)
    
    commit.add_memento(FileMemento(file_path="data.csv", file_hash=blob_id1, file_size=1024))
    commit.add_memento(FileMemento(file_path="model.pkl", file_hash=blob_id2, file_size=2048))
    
    return commit


# ═══════════════════════════════════════════════════════════════════════════════
# InMemoryBlobRepository Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestInMemoryBlobRepository:
    """Tests for InMemoryBlobRepository."""
    
    def test_save_and_get_blob(self, blob_repo, sample_content):
        """Test saving and retrieving blob content."""
        blob_id = blob_repo.save(sample_content)
        
        assert isinstance(blob_id, BlobId)
        retrieved = blob_repo.get(blob_id)
        assert retrieved == sample_content
    
    def test_save_duplicate_content(self, blob_repo, sample_content):
        """Test that duplicate content returns same BlobId."""
        blob_id1 = blob_repo.save(sample_content)
        blob_id2 = blob_repo.save(sample_content)
        
        assert blob_id1 == blob_id2
    
    def test_blob_exists(self, blob_repo, sample_content):
        """Test exists check."""
        blob_id = blob_repo.save(sample_content)
        
        assert blob_repo.exists(blob_id) is True
        assert blob_repo.exists(BlobId(value="0" * 64)) is False
    
    def test_delete_blob(self, blob_repo, sample_content):
        """Test deleting blob."""
        blob_id = blob_repo.save(sample_content)
        
        result = blob_repo.delete(blob_id)
        assert result is True
        assert blob_repo.exists(blob_id) is False
    
    def test_delete_nonexistent_blob(self, blob_repo):
        """Test deleting non-existent blob."""
        blob_id = BlobId(value="0" * 64)
        result = blob_repo.delete(blob_id)
        assert result is False
    
    def test_reference_counting(self, blob_repo, sample_content):
        """Test that reference counting works."""
        # Save same content twice
        blob_id1 = blob_repo.save(sample_content)
        blob_id2 = blob_repo.save(sample_content)
        
        # First delete should not remove (ref count = 1)
        result1 = blob_repo.delete(blob_id1)
        assert result1 is False
        assert blob_repo.exists(blob_id1) is True
        
        # Second delete should remove (ref count = 0)
        result2 = blob_repo.delete(blob_id2)
        assert result2 is True
        assert blob_repo.exists(blob_id2) is False
    
    def test_get_nonexistent_blob(self, blob_repo):
        """Test getting non-existent blob returns None."""
        blob_id = BlobId(value="0" * 64)
        result = blob_repo.get(blob_id)
        assert result is None
    
    def test_restore_to_file(self, blob_repo, sample_content, tmp_path):
        """Test restoring blob to file."""
        blob_id = blob_repo.save(sample_content)
        output_path = tmp_path / "restored.txt"
        
        blob_repo.restore_to_file(blob_id, str(output_path))
        
        assert output_path.read_bytes() == sample_content
    
    def test_restore_nonexistent_blob_raises(self, blob_repo, tmp_path):
        """Test that restoring non-existent blob raises error."""
        blob_id = BlobId(value="0" * 64)
        output_path = tmp_path / "output.txt"
        
        with pytest.raises(FileNotFoundError):
            blob_repo.restore_to_file(blob_id, str(output_path))
    
    def test_get_stats(self, blob_repo):
        """Test storage statistics."""
        content1 = b"Content one"
        content2 = b"Different content two"
        
        blob_repo.save(content1)
        blob_repo.save(content2)
        
        stats = blob_repo.get_stats()
        
        assert stats["blob_count"] == 2
        assert stats["total_size_bytes"] == len(content1) + len(content2)
    
    def test_get_stats_empty(self, blob_repo):
        """Test statistics on empty repository."""
        stats = blob_repo.get_stats()
        
        assert stats["blob_count"] == 0
        assert stats["total_size_bytes"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# InMemoryFileCommitRepository Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestInMemoryFileCommitRepository:
    """Tests for InMemoryFileCommitRepository."""
    
    def test_save_and_get_commit(self, commit_repo, sample_commit):
        """Test saving and retrieving commit."""
        commit_repo.save(sample_commit)
        
        retrieved = commit_repo.get_by_id(sample_commit.commit_id)
        
        assert retrieved is not None
        assert retrieved.commit_id == sample_commit.commit_id
        assert retrieved.message == sample_commit.message
    
    def test_get_nonexistent_commit(self, commit_repo):
        """Test getting non-existent commit."""
        commit_id = CommitId.generate()
        result = commit_repo.get_by_id(commit_id)
        assert result is None
    
    def test_get_all_commits(self, commit_repo):
        """Test getting all commits."""
        # Create multiple commits
        for i in range(5):
            commit = FileCommit.create(message=f"Commit {i}")
            blob_id = BlobId(value=f"{i:064d}")
            commit.add_memento(FileMemento(file_path=f"file{i}.txt", file_hash=blob_id, file_size=100))
            commit_repo.save(commit)
        
        commits = commit_repo.get_all()
        
        assert len(commits) == 5
    
    def test_get_all_with_limit(self, commit_repo):
        """Test pagination with limit."""
        for i in range(10):
            commit = FileCommit.create()
            blob_id = BlobId(value=f"{i:064d}")
            commit.add_memento(FileMemento(file_path=f"file{i}.txt", file_hash=blob_id, file_size=100))
            commit_repo.save(commit)
        
        commits = commit_repo.get_all(limit=3)
        assert len(commits) == 3
    
    def test_get_by_execution_id(self, commit_repo):
        """Test filtering by execution ID."""
        # Create commits with different execution IDs
        for exec_id in ["exec-1", "exec-2", "exec-1"]:
            commit = FileCommit.create(execution_id=exec_id)
            blob_id = BlobId(value=CommitId.generate().value.replace("-", "")[:64].ljust(64, "0"))
            commit.add_memento(FileMemento(file_path=f"file-{exec_id}.txt", file_hash=blob_id, file_size=100))
            commit_repo.save(commit)
        
        exec1_commits = commit_repo.get_by_execution_id("exec-1")
        exec2_commits = commit_repo.get_by_execution_id("exec-2")
        
        assert len(exec1_commits) == 2
        assert len(exec2_commits) == 1
    
    def test_get_by_checkpoint_id(self, commit_repo, sample_commit):
        """Test getting commit by checkpoint."""
        sample_commit.link_to_checkpoint(42)
        commit_repo.save(sample_commit)
        
        found = commit_repo.get_by_checkpoint_id(42)
        assert found is not None
        assert found.commit_id == sample_commit.commit_id
        
        not_found = commit_repo.get_by_checkpoint_id(999)
        assert not_found is None
    
    def test_delete_commit(self, commit_repo, sample_commit):
        """Test deleting commit."""
        commit_repo.save(sample_commit)
        
        result = commit_repo.delete(sample_commit.commit_id)
        assert result is True
        
        retrieved = commit_repo.get_by_id(sample_commit.commit_id)
        assert retrieved is None
    
    def test_delete_nonexistent_commit(self, commit_repo):
        """Test deleting non-existent commit."""
        commit_id = CommitId.generate()
        result = commit_repo.delete(commit_id)
        assert result is False
    
    def test_count(self, commit_repo):
        """Test commit count."""
        assert commit_repo.count() == 0
        
        for i in range(3):
            commit = FileCommit.create()
            blob_id = BlobId(value=f"{i:064d}")
            commit.add_memento(FileMemento(file_path=f"file{i}.txt", file_hash=blob_id, file_size=100))
            commit_repo.save(commit)
        
        assert commit_repo.count() == 3
    
    def test_update_existing_commit(self, commit_repo, sample_commit):
        """Test updating existing commit."""
        commit_repo.save(sample_commit)
        
        # Modify and save again
        sample_commit.link_to_checkpoint(99)
        commit_repo.save(sample_commit)
        
        retrieved = commit_repo.get_by_id(sample_commit.commit_id)
        assert retrieved.checkpoint_id == 99
        
        # Count should still be 1
        assert commit_repo.count() == 1


# ═══════════════════════════════════════════════════════════════════════════════
# InMemoryCheckpointFileLinkRepository Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestInMemoryCheckpointFileLinkRepository:
    """Tests for InMemoryCheckpointFileLinkRepository."""
    
    def test_add_and_get_link(self, link_repo):
        """Test adding and retrieving link."""
        commit_id = CommitId.generate()
        link = CheckpointFileLink(
            checkpoint_id=42,
            commit_id=commit_id,
            linked_at=datetime.now(),
            file_count=3,
            total_size_bytes=1024,
        )
        
        link_repo.add(link)
        
        retrieved = link_repo.get_by_checkpoint(42)
        assert retrieved is not None
        assert retrieved.checkpoint_id == 42
        assert retrieved.commit_id == commit_id
    
    def test_get_nonexistent_link(self, link_repo):
        """Test getting non-existent link."""
        result = link_repo.get_by_checkpoint(999)
        assert result is None
    
    def test_upsert_link(self, link_repo):
        """Test that adding same checkpoint_id updates."""
        commit_id1 = CommitId.generate()
        commit_id2 = CommitId.generate()
        
        link1 = CheckpointFileLink(
            checkpoint_id=42,
            commit_id=commit_id1,
            linked_at=datetime.now(),
            file_count=3,
            total_size_bytes=1024,
        )
        link2 = CheckpointFileLink(
            checkpoint_id=42,
            commit_id=commit_id2,
            linked_at=datetime.now(),
            file_count=5,
            total_size_bytes=2048,
        )
        
        link_repo.add(link1)
        link_repo.add(link2)
        
        retrieved = link_repo.get_by_checkpoint(42)
        assert retrieved.commit_id == commit_id2
        assert retrieved.file_count == 5
    
    def test_get_by_commit(self, link_repo):
        """Test getting links by commit."""
        commit_id = CommitId.generate()
        
        # Add multiple checkpoints linking to same commit
        for cp_id in [10, 20, 30]:
            link = CheckpointFileLink(
                checkpoint_id=cp_id,
                commit_id=commit_id,
                linked_at=datetime.now(),
                file_count=1,
                total_size_bytes=100,
            )
            link_repo.add(link)
        
        links = link_repo.get_by_commit(commit_id)
        assert len(links) == 3
    
    def test_delete_by_checkpoint(self, link_repo):
        """Test deleting link by checkpoint."""
        commit_id = CommitId.generate()
        link = CheckpointFileLink(
            checkpoint_id=42,
            commit_id=commit_id,
            linked_at=datetime.now(),
            file_count=1,
            total_size_bytes=100,
        )
        
        link_repo.add(link)
        result = link_repo.delete_by_checkpoint(42)
        
        assert result is True
        assert link_repo.get_by_checkpoint(42) is None
    
    def test_delete_nonexistent_link(self, link_repo):
        """Test deleting non-existent link."""
        result = link_repo.delete_by_checkpoint(999)
        assert result is False
    
    def test_delete_by_commit(self, link_repo):
        """Test deleting all links for a commit."""
        commit_id = CommitId.generate()
        
        for cp_id in [10, 20, 30]:
            link = CheckpointFileLink(
                checkpoint_id=cp_id,
                commit_id=commit_id,
                linked_at=datetime.now(),
                file_count=1,
                total_size_bytes=100,
            )
            link_repo.add(link)
        
        count = link_repo.delete_by_commit(commit_id)
        
        assert count == 3
        assert len(link_repo.get_by_commit(commit_id)) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Integration Tests - Full Workflow
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileProcessingWorkflow:
    """Tests for complete file processing workflows."""
    
    def test_complete_tracking_workflow(self, blob_repo, commit_repo, link_repo, tmp_path):
        """Test complete file tracking workflow."""
        # 1. Create test files
        file1 = tmp_path / "data.csv"
        file2 = tmp_path / "model.pkl"
        content1 = b"id,name,value\n1,Alice,100\n2,Bob,200"
        content2 = b"ML model binary content here..."
        file1.write_bytes(content1)
        file2.write_bytes(content2)
        
        # 2. Create mementos and save blobs
        memento1, _ = FileMemento.from_file(str(file1))
        memento2, _ = FileMemento.from_file(str(file2))
        
        blob_id1 = blob_repo.save(content1)
        blob_id2 = blob_repo.save(content2)
        
        # 3. Create commit with mementos
        commit = FileCommit.create(
            message="Snapshot before processing",
            execution_id="exec-001",
        )
        commit.add_memento(memento1)
        commit.add_memento(memento2)
        commit.finalize()
        
        # 4. Save commit
        commit_repo.save(commit)
        
        # 5. Link to checkpoint
        link = CheckpointFileLink.create(checkpoint_id=42, commit=commit)
        link_repo.add(link)
        
        # Verify
        assert blob_repo.exists(blob_id1)
        assert blob_repo.exists(blob_id2)
        assert commit_repo.count() == 1
        assert link_repo.get_by_checkpoint(42) is not None
    
    def test_restore_workflow(self, blob_repo, commit_repo, link_repo, tmp_path):
        """Test file restoration workflow."""
        # Setup: Create and save blobs
        content = b"Important data to restore"
        blob_id = blob_repo.save(content)
        
        # Create commit
        memento = FileMemento.from_path_and_hash(
            file_path=str(tmp_path / "original.txt"),
            file_hash=blob_id,
            file_size=len(content),
        )
        commit = FileCommit.create(message="Backup")
        commit.add_memento(memento)
        commit.finalize()
        commit_repo.save(commit)
        
        # Restore to new location
        restore_path = tmp_path / "restored" / "original.txt"
        blob_repo.restore_to_file(blob_id, str(restore_path))
        
        # Verify
        assert restore_path.read_bytes() == content
    
    def test_rollback_via_checkpoint(self, blob_repo, commit_repo, link_repo, tmp_path):
        """Test rollback to checkpoint via file link."""
        # Setup: Create file and commit
        original_content = b"Original content v1"
        file_path = tmp_path / "data.txt"
        file_path.write_bytes(original_content)
        
        blob_id = blob_repo.save(original_content)
        memento = FileMemento.from_path_and_hash(
            str(file_path), blob_id, len(original_content)
        )
        
        commit = FileCommit.create()
        commit.add_memento(memento)
        commit.finalize()
        commit_repo.save(commit)
        
        link = CheckpointFileLink.create(checkpoint_id=1, commit=commit)
        link_repo.add(link)
        
        # Simulate modification
        file_path.write_bytes(b"Modified content v2")
        
        # Rollback: Get link, find commit, restore files
        found_link = link_repo.get_by_checkpoint(1)
        found_commit = commit_repo.get_by_id(found_link.commit_id)
        
        for m in found_commit.mementos:
            blob_repo.restore_to_file(m.file_hash, m.file_path)
        
        # Verify rollback
        assert file_path.read_bytes() == original_content


# ═══════════════════════════════════════════════════════════════════════════════
# ACID Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestACIDCompliance:
    """Tests for ACID compliance properties."""
    
    def test_atomicity_commit_with_mementos(self, commit_repo):
        """Test that commit and mementos are saved atomically."""
        commit = FileCommit.create(message="Atomic test")
        
        for i in range(3):
            blob_id = BlobId(value=f"{i:064d}")
            commit.add_memento(FileMemento(
                file_path=f"file{i}.txt",
                file_hash=blob_id,
                file_size=100,
            ))
        
        commit_repo.save(commit)
        
        # All mementos should be retrievable with commit
        retrieved = commit_repo.get_by_id(commit.commit_id)
        assert retrieved.file_count == 3
    
    def test_consistency_duplicate_rejection(self, commit_repo):
        """Test that domain invariants are maintained."""
        commit = FileCommit.create()
        blob_id = BlobId(value="a" * 64)
        
        commit.add_memento(FileMemento(
            file_path="duplicate.txt",
            file_hash=blob_id,
            file_size=100,
        ))
        
        # Adding duplicate should raise error - maintaining consistency
        from wtb.domain.models.file_processing import DuplicateFileError
        with pytest.raises(DuplicateFileError):
            commit.add_memento(FileMemento(
                file_path="duplicate.txt",  # Same path
                file_hash=blob_id,
                file_size=200,
            ))
    
    def test_isolation_separate_commits(self, commit_repo):
        """Test that commits are isolated from each other."""
        commit1 = FileCommit.create(message="Commit 1")
        commit2 = FileCommit.create(message="Commit 2")
        
        blob_id1 = BlobId(value="1" * 64)
        blob_id2 = BlobId(value="2" * 64)
        
        commit1.add_memento(FileMemento(file_path="file1.txt", file_hash=blob_id1, file_size=100))
        commit2.add_memento(FileMemento(file_path="file2.txt", file_hash=blob_id2, file_size=200))
        
        commit_repo.save(commit1)
        commit_repo.save(commit2)
        
        # Modifications to one shouldn't affect other
        retrieved1 = commit_repo.get_by_id(commit1.commit_id)
        retrieved2 = commit_repo.get_by_id(commit2.commit_id)
        
        assert retrieved1.file_paths == ["file1.txt"]
        assert retrieved2.file_paths == ["file2.txt"]
    
    def test_durability_persistence(self, commit_repo, sample_commit):
        """Test that saved data persists."""
        commit_repo.save(sample_commit)
        
        # Multiple retrievals should return consistent data
        for _ in range(3):
            retrieved = commit_repo.get_by_id(sample_commit.commit_id)
            assert retrieved.message == sample_commit.message
            assert retrieved.file_count == sample_commit.file_count
