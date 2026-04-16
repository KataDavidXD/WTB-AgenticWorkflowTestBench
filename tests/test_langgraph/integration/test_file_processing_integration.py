"""
Integration tests for LangGraph + Event + Audit + File Processing.

Tests complete integration including:
- File tracking during execution
- Checkpoint-file links
- Transaction consistency
- Rollback with file state

ACID Compliance:
- Atomicity: File commits are atomic
- Consistency: Checkpoint-file links maintain referential integrity
- Isolation: File operations are isolated per execution
- Durability: File commits persist with checkpoints

Run with: pytest tests/test_langgraph/integration/test_file_processing_integration.py -v
"""

import pytest
from typing import Dict, Any, List
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch
import tempfile

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

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
)
from wtb.infrastructure.events import WTBEventBus

from tests.test_langgraph.helpers import (
    create_initial_file_tracking_state,
    FileTrackingState,
)


# ═══════════════════════════════════════════════════════════════════════════════
# File Processing Domain Model Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileProcessingModels:
    """Tests for file processing domain models."""
    
    def test_create_blob_id(self):
        """Test creating BlobId."""
        blob_id = BlobId(value="a" * 64)
        
        assert blob_id.value == "a" * 64
    
    def test_create_commit_id(self):
        """Test creating CommitId."""
        commit_id = CommitId.generate()
        
        assert commit_id is not None
        assert len(commit_id.value) > 0
    
    def test_create_file_memento(self, temp_files):
        """Test creating FileMemento."""
        file_data = list(temp_files.values())[0]
        blob_id = BlobId(value="a" * 64)
        
        memento = FileMemento.from_path_and_hash(
            file_path=file_data["path"],
            file_hash=blob_id,
            file_size=file_data["size"],
        )
        
        assert memento.file_path == file_data["path"]
        assert memento.file_hash == blob_id
    
    def test_create_file_commit(self):
        """Test creating FileCommit."""
        commit = FileCommit.create(message="Test commit")
        
        assert commit.status == CommitStatus.PENDING
        assert commit.message == "Test commit"
    
    def test_add_memento_to_commit(self, temp_files):
        """Test adding memento to commit."""
        commit = FileCommit.create()
        blob_id = BlobId(value="a" * 64)
        
        for name, data in temp_files.items():
            memento = FileMemento.from_path_and_hash(
                data["path"], blob_id, data["size"]
            )
            commit.add_memento(memento)
        
        assert commit.file_count == len(temp_files)
    
    def test_finalize_commit(self, temp_files):
        """Test finalizing commit."""
        commit = FileCommit.create()
        blob_id = BlobId(value="a" * 64)
        
        memento = FileMemento.from_path_and_hash(
            list(temp_files.values())[0]["path"],
            blob_id,
            100,
        )
        commit.add_memento(memento)
        commit.finalize()
        
        assert commit.status == CommitStatus.FINALIZED
    
    def test_create_checkpoint_file_link(self, temp_files):
        """Test creating checkpoint-file link."""
        commit = FileCommit.create()
        blob_id = BlobId(value="a" * 64)
        
        memento = FileMemento.from_path_and_hash(
            list(temp_files.values())[0]["path"],
            blob_id,
            100,
        )
        commit.add_memento(memento)
        commit.finalize()
        
        link = CheckpointFileLink.create(checkpoint_id=42, commit=commit)
        
        assert link.checkpoint_id == 42
        assert link.commit_id == commit.commit_id


# ═══════════════════════════════════════════════════════════════════════════════
# Blob Repository Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBlobRepository:
    """Tests for blob repository operations."""
    
    def test_save_blob(self, blob_repository, temp_files):
        """Test saving blob content."""
        content = list(temp_files.values())[0]["content"]
        
        blob_id = blob_repository.save(content)
        
        assert blob_id is not None
        assert blob_repository.exists(blob_id)
    
    def test_blob_deduplication(self, blob_repository):
        """Test identical content produces same blob ID."""
        content = b"Same content for deduplication test"
        
        blob_id1 = blob_repository.save(content)
        blob_id2 = blob_repository.save(content)
        
        assert blob_id1 == blob_id2
    
    def test_restore_blob_to_file(self, blob_repository, tmp_path):
        """Test restoring blob to file."""
        content = b"Content to restore"
        blob_id = blob_repository.save(content)
        
        output_path = str(tmp_path / "restored.txt")
        blob_repository.restore_to_file(blob_id, output_path)
        
        assert Path(output_path).exists()
        assert Path(output_path).read_bytes() == content
    
    def test_get_blob_stats(self, blob_repository, temp_files):
        """Test getting blob repository stats."""
        for data in temp_files.values():
            blob_repository.save(data["content"])
        
        stats = blob_repository.get_stats()
        
        assert "blob_count" in stats
        assert stats["blob_count"] >= 0


# ═══════════════════════════════════════════════════════════════════════════════
# Commit Repository Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCommitRepository:
    """Tests for commit repository operations."""
    
    def test_save_commit(self, commit_repository, blob_repository, temp_files):
        """Test saving commit."""
        commit = FileCommit.create(message="Test save")
        
        for data in temp_files.values():
            blob_id = blob_repository.save(data["content"])
            commit.add_memento(FileMemento.from_path_and_hash(
                data["path"], blob_id, data["size"]
            ))
        
        commit.finalize()
        commit_repository.save(commit)
        
        retrieved = commit_repository.get_by_id(commit.commit_id)
        
        assert retrieved is not None
        assert retrieved.commit_id == commit.commit_id
    
    def test_get_commits_by_execution(self, commit_repository, blob_repository):
        """Test getting commits by execution ID."""
        blob_id = BlobId(value="b" * 64)
        
        for i in range(3):
            commit = FileCommit.create(execution_id="exec-1")
            commit.add_memento(FileMemento.from_path_and_hash(
                f"file{i}.txt", blob_id, 100
            ))
            commit.finalize()
            commit_repository.save(commit)
        
        commits = commit_repository.get_by_execution_id("exec-1")
        
        assert len(commits) == 3


# ═══════════════════════════════════════════════════════════════════════════════
# Checkpoint-File Link Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCheckpointFileLink:
    """Tests for checkpoint-file link operations."""
    
    def test_create_link(self, link_repository, commit_repository, blob_repository):
        """Test creating checkpoint-file link."""
        # Create commit
        commit = FileCommit.create()
        blob_id = BlobId(value="c" * 64)
        commit.add_memento(FileMemento.from_path_and_hash(
            "file.txt", blob_id, 100
        ))
        commit.finalize()
        commit_repository.save(commit)
        
        # Create link
        link = CheckpointFileLink.create(checkpoint_id=1, commit=commit)
        link_repository.add(link)
        
        # Retrieve
        retrieved = link_repository.get_by_checkpoint(1)
        
        assert retrieved is not None
        assert retrieved.commit_id == commit.commit_id
    
    def test_link_contains_file_metadata(self, link_repository, commit_repository, blob_repository, temp_files):
        """Test link contains file metadata."""
        commit = FileCommit.create()
        total_size = 0
        
        for data in temp_files.values():
            blob_id = blob_repository.save(data["content"])
            commit.add_memento(FileMemento.from_path_and_hash(
                data["path"], blob_id, data["size"]
            ))
            total_size += data["size"]
        
        commit.finalize()
        commit_repository.save(commit)
        
        link = CheckpointFileLink.create(checkpoint_id=5, commit=commit)
        link_repository.add(link)
        
        retrieved = link_repository.get_by_checkpoint(5)
        
        assert retrieved.file_count == len(temp_files)
        assert retrieved.total_size_bytes == total_size


# ═══════════════════════════════════════════════════════════════════════════════
# LangGraph + File Processing Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestLangGraphFileProcessing:
    """Tests for LangGraph + File Processing integration."""
    
    def test_file_tracking_during_execution(
        self, 
        file_tracking_compiled_graph,
        blob_repository,
        commit_repository,
    ):
        """Test file tracking during graph execution."""
        initial_state = create_initial_file_tracking_state()
        config = {"configurable": {"thread_id": "file-track-1"}}
        
        result = file_tracking_compiled_graph.invoke(initial_state, config)
        
        # Verify execution completed
        assert len(result["files_processed"]) > 0
        assert len(result["commit_ids"]) > 0
    
    def test_checkpoint_with_file_state(
        self,
        file_tracking_compiled_graph,
        blob_repository,
        commit_repository,
        link_repository,
    ):
        """Test checkpoints capture file state."""
        initial_state = create_initial_file_tracking_state()
        config = {"configurable": {"thread_id": "file-checkpoint-1"}}
        
        # Execute
        result = file_tracking_compiled_graph.invoke(initial_state, config)
        
        # Get checkpoint history
        history = list(file_tracking_compiled_graph.get_state_history(config))
        
        # Each checkpoint should be linkable to file state
        assert len(history) > 0
        
        # Verify state contains file tracking info
        for snapshot in history:
            if "files_processed" in snapshot.values:
                # File state is captured
                assert isinstance(snapshot.values["files_processed"], list)


# ═══════════════════════════════════════════════════════════════════════════════
# Transaction Consistency Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestTransactionConsistency:
    """Tests for transaction consistency across systems."""
    
    def test_atomic_commit_with_mementos(
        self, 
        blob_repository, 
        commit_repository, 
        temp_files
    ):
        """Test commit and mementos are saved atomically."""
        commit = FileCommit.create(message="Atomic test")
        
        for data in temp_files.values():
            blob_id = blob_repository.save(data["content"])
            commit.add_memento(FileMemento.from_path_and_hash(
                data["path"], blob_id, data["size"]
            ))
        
        commit.finalize()
        commit_repository.save(commit)
        
        # Retrieve should have all mementos
        retrieved = commit_repository.get_by_id(commit.commit_id)
        
        assert retrieved.file_count == len(temp_files)
    
    def test_checkpoint_file_link_integrity(
        self,
        blob_repository,
        commit_repository,
        link_repository,
        temp_files,
    ):
        """Test checkpoint-file link maintains integrity."""
        # Create commit
        commit = FileCommit.create()
        for data in temp_files.values():
            blob_id = blob_repository.save(data["content"])
            commit.add_memento(FileMemento.from_path_and_hash(
                data["path"], blob_id, data["size"]
            ))
        commit.finalize()
        commit_repository.save(commit)
        
        # Create link
        link = CheckpointFileLink.create(checkpoint_id=100, commit=commit)
        link_repository.add(link)
        
        # Verify integrity
        retrieved_link = link_repository.get_by_checkpoint(100)
        retrieved_commit = commit_repository.get_by_id(retrieved_link.commit_id)
        
        assert retrieved_commit is not None
        assert retrieved_link.file_count == retrieved_commit.file_count
    
    def test_cascading_restore_from_checkpoint(
        self,
        blob_repository,
        commit_repository,
        link_repository,
        temp_files,
        tmp_path,
    ):
        """Test complete restore from checkpoint."""
        # Setup: Create commit with files
        commit = FileCommit.create()
        restore_dir = tmp_path / "restore"
        restore_dir.mkdir()
        
        for name, data in temp_files.items():
            blob_id = blob_repository.save(data["content"])
            commit.add_memento(FileMemento.from_path_and_hash(
                str(restore_dir / name),
                blob_id,
                data["size"],
            ))
        commit.finalize()
        commit_repository.save(commit)
        
        # Create checkpoint link
        link = CheckpointFileLink.create(checkpoint_id=200, commit=commit)
        link_repository.add(link)
        
        # Restore from checkpoint
        retrieved_link = link_repository.get_by_checkpoint(200)
        retrieved_commit = commit_repository.get_by_id(retrieved_link.commit_id)
        
        for memento in retrieved_commit.mementos:
            blob_repository.restore_to_file(memento.file_hash, memento.file_path)
        
        # Verify all files restored
        for name in temp_files.keys():
            assert (restore_dir / name).exists()


# ═══════════════════════════════════════════════════════════════════════════════
# Rollback with File State Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRollbackWithFileState:
    """Tests for rollback operations with file state."""
    
    def test_rollback_restores_file_links(
        self,
        blob_repository,
        commit_repository,
        link_repository,
    ):
        """Test rollback can restore file state via links."""
        # Create commits at different checkpoints
        for checkpoint_id in [1, 2, 3]:
            commit = FileCommit.create(message=f"Checkpoint {checkpoint_id}")
            blob_id = BlobId(value=f"{checkpoint_id:064d}")
            commit.add_memento(FileMemento.from_path_and_hash(
                f"state_cp{checkpoint_id}.txt",
                blob_id,
                100 * checkpoint_id,
            ))
            commit.finalize()
            commit_repository.save(commit)
            
            link = CheckpointFileLink.create(checkpoint_id=checkpoint_id, commit=commit)
            link_repository.add(link)
        
        # "Rollback" to checkpoint 2
        link_at_2 = link_repository.get_by_checkpoint(2)
        commit_at_2 = commit_repository.get_by_id(link_at_2.commit_id)
        
        # Verify correct state
        assert commit_at_2.file_count == 1
        assert link_at_2.total_size_bytes == 200  # 100 * 2


# ═══════════════════════════════════════════════════════════════════════════════
# Event Publishing Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileEventPublishing:
    """Tests for file-related event publishing."""
    
    def test_commit_created_event(self, event_bus, blob_repository, commit_repository, temp_files):
        """Test FileCommitCreatedEvent is published."""
        received = []
        event_bus.subscribe(FileCommitCreatedEvent, received.append)
        
        commit = FileCommit.create(message="Event test")
        for data in temp_files.values():
            blob_id = blob_repository.save(data["content"])
            commit.add_memento(FileMemento.from_path_and_hash(
                data["path"], blob_id, data["size"]
            ))
        commit.finalize()
        commit_repository.save(commit)
        
        # Publish event
        event = FileCommitCreatedEvent(
            commit_id=commit.commit_id.value,
            file_count=commit.file_count,
            total_size_bytes=commit.total_size,
            message=commit.message,
            file_paths=tuple(commit.file_paths),
        )
        event_bus.publish(event)
        
        assert len(received) == 1
        assert received[0].file_count == len(temp_files)
    
    def test_checkpoint_link_created_event(self, event_bus, link_repository, commit_repository):
        """Test CheckpointFileLinkCreatedEvent is published."""
        received = []
        event_bus.subscribe(CheckpointFileLinkCreatedEvent, received.append)
        
        commit = FileCommit.create()
        blob_id = BlobId(value="d" * 64)
        commit.add_memento(FileMemento.from_path_and_hash("f.txt", blob_id, 100))
        commit.finalize()
        commit_repository.save(commit)
        
        link = CheckpointFileLink.create(checkpoint_id=50, commit=commit)
        link_repository.add(link)
        
        # Publish event
        event = CheckpointFileLinkCreatedEvent(
            checkpoint_id=link.checkpoint_id,
            commit_id=link.commit_id.value,
            file_count=link.file_count,
            total_size_bytes=link.total_size_bytes,
        )
        event_bus.publish(event)
        
        assert len(received) == 1
        assert received[0].checkpoint_id == 50


# ═══════════════════════════════════════════════════════════════════════════════
# ACID Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileProcessingACID:
    """Tests for ACID compliance in file processing."""
    
    def test_atomicity_commit_creation(self, blob_repository, commit_repository):
        """Test commit creation is atomic."""
        commit = FileCommit.create()
        blob_id = BlobId(value="e" * 64)
        
        commit.add_memento(FileMemento.from_path_and_hash("f1.txt", blob_id, 100))
        commit.add_memento(FileMemento.from_path_and_hash("f2.txt", blob_id, 200))
        commit.finalize()
        commit_repository.save(commit)
        
        # Should be all or nothing
        retrieved = commit_repository.get_by_id(commit.commit_id)
        assert retrieved.file_count == 2
    
    def test_consistency_blob_deduplication(self, blob_repository):
        """Test blob storage maintains consistency via deduplication."""
        content = b"Consistent content"
        
        # Save same content multiple times
        blob_ids = [blob_repository.save(content) for _ in range(10)]
        
        # All should reference same blob
        assert all(bid == blob_ids[0] for bid in blob_ids)
        
        # Only one blob stored
        stats = blob_repository.get_stats()
        assert stats["blob_count"] == 1
    
    def test_isolation_execution_commits(self, blob_repository, commit_repository):
        """Test commits from different executions are isolated."""
        blob_id = BlobId(value="f" * 64)
        
        # Commit for execution 1
        commit1 = FileCommit.create(execution_id="iso-exec-1")
        commit1.add_memento(FileMemento.from_path_and_hash("f1.txt", blob_id, 100))
        commit1.finalize()
        commit_repository.save(commit1)
        
        # Commit for execution 2
        commit2 = FileCommit.create(execution_id="iso-exec-2")
        commit2.add_memento(FileMemento.from_path_and_hash("f2.txt", blob_id, 200))
        commit2.finalize()
        commit_repository.save(commit2)
        
        # Each execution has isolated commits
        exec1_commits = commit_repository.get_by_execution_id("iso-exec-1")
        exec2_commits = commit_repository.get_by_execution_id("iso-exec-2")
        
        assert len(exec1_commits) == 1
        assert len(exec2_commits) == 1
        assert exec1_commits[0].commit_id != exec2_commits[0].commit_id
    
    def test_durability_blob_persistence(self, blob_repository, tmp_path):
        """Test blobs persist and can be restored."""
        content = b"Durable content for persistence test"
        blob_id = blob_repository.save(content)
        
        # Restore to file
        output_path = str(tmp_path / "durable_output.txt")
        blob_repository.restore_to_file(blob_id, output_path)
        
        # Verify content matches
        restored_content = Path(output_path).read_bytes()
        assert restored_content == content
