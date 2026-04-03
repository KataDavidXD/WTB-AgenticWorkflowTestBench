"""
Pytest fixtures for File Processing tests.

Shared fixtures for unit and integration tests.
"""

import pytest
import tempfile
import os
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime
from unittest.mock import MagicMock, Mock
import uuid

from wtb.domain.models.file_processing import (
    BlobId,
    CommitId,
    FileMemento,
    FileCommit,
    CheckpointFileLink,
    CommitStatus,
)
from wtb.domain.interfaces.file_processing_repository import (
    IBlobRepository,
    IFileCommitRepository,
    ICheckpointFileLinkRepository,
)
from wtb.domain.events.file_processing_events import (
    FileCommitCreatedEvent,
    FileRestoredEvent,
    BlobCreatedEvent,
)
from wtb.infrastructure.events import WTBEventBus


# ═══════════════════════════════════════════════════════════════════════════════
# Temporary File Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files."""
    temp_path = tempfile.mkdtemp(prefix="wtb_file_test_")
    yield temp_path
    # Cleanup
    shutil.rmtree(temp_path, ignore_errors=True)


@pytest.fixture
def sample_files(temp_dir) -> Dict[str, Path]:
    """Create sample test files with known content."""
    files = {}
    
    # CSV file
    csv_path = Path(temp_dir) / "data.csv"
    csv_content = "id,name,value\n1,test,100\n2,sample,200\n"
    csv_path.write_text(csv_content, encoding="utf-8")
    files["csv"] = csv_path
    
    # JSON file
    json_path = Path(temp_dir) / "config.json"
    json_content = '{"key": "value", "number": 42}'
    json_path.write_text(json_content, encoding="utf-8")
    files["json"] = json_path
    
    # Binary file (pickle simulation)
    pkl_path = Path(temp_dir) / "model.pkl"
    pkl_content = b"\x80\x04\x95\x10\x00\x00\x00\x00\x00\x00\x00}\x94\x8c\x05model\x94K\x01s."
    pkl_path.write_bytes(pkl_content)
    files["pkl"] = pkl_path
    
    # Text file
    txt_path = Path(temp_dir) / "notes.txt"
    txt_content = "This is a test file for WTB file processing tests."
    txt_path.write_text(txt_content, encoding="utf-8")
    files["txt"] = txt_path
    
    return files


@pytest.fixture
def large_file(temp_dir) -> Path:
    """Create a large test file (1MB)."""
    large_path = Path(temp_dir) / "large_data.bin"
    # Create 1MB of random-ish data
    content = b"x" * (1024 * 1024)
    large_path.write_bytes(content)
    return large_path


# ═══════════════════════════════════════════════════════════════════════════════
# Mock Repository Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


class InMemoryBlobRepository(IBlobRepository):
    """In-memory blob repository for testing."""
    
    def __init__(self):
        self._storage: Dict[str, bytes] = {}
    
    def save(self, content: bytes) -> BlobId:
        """Save content and return content-addressed BlobId."""
        blob_id = BlobId.from_content(content)
        self._storage[blob_id.value] = content
        return blob_id
    
    def get(self, blob_id: BlobId) -> Optional[bytes]:
        return self._storage.get(blob_id.value)
    
    def exists(self, blob_id: BlobId) -> bool:
        return blob_id.value in self._storage
    
    def delete(self, blob_id: BlobId) -> bool:
        if blob_id.value in self._storage:
            del self._storage[blob_id.value]
            return True
        return False
    
    def restore_to_file(self, blob_id: BlobId, output_path: str) -> None:
        """Restore blob content to file system."""
        content = self.get(blob_id)
        if content is None:
            raise FileNotFoundError(f"Blob not found: {blob_id.value}")
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get storage statistics."""
        total_size = sum(len(content) for content in self._storage.values())
        return {
            "blob_count": len(self._storage),
            "total_size_bytes": total_size,
            "total_size_mb": total_size / (1024 * 1024),
        }
    
    def get_all_ids(self) -> List[BlobId]:
        """Helper method for testing - not in interface."""
        return [BlobId(hash_val) for hash_val in self._storage.keys()]
    
    def clear(self):
        """Helper method for testing - not in interface."""
        self._storage.clear()


class InMemoryFileCommitRepository(IFileCommitRepository):
    """In-memory file commit repository for testing."""
    
    def __init__(self):
        self._storage: Dict[str, FileCommit] = {}
        self._checkpoint_links: Dict[int, str] = {}  # checkpoint_id -> commit_id
    
    def save(self, commit: FileCommit) -> None:
        self._storage[commit.commit_id.value] = commit
    
    def get_by_id(self, commit_id: CommitId) -> Optional[FileCommit]:
        return self._storage.get(commit_id.value)
    
    def get_by_id_without_mementos(self, commit_id: CommitId) -> Optional[FileCommit]:
        """Get commit without loading mementos (optimization for listings)."""
        commit = self._storage.get(commit_id.value)
        if commit:
            # For in-memory testing, return shallow copy without mementos
            return FileCommit(
                commit_id=commit.commit_id,
                message=commit.message,
                status=commit.status,
                execution_id=commit.execution_id,
                timestamp=commit.timestamp,
                mementos=[],  # Empty mementos
            )
        return None
    
    def get_all(self, limit: int = 100, offset: int = 0) -> List[FileCommit]:
        """Get all commits without mementos, ordered newest first."""
        commits = sorted(
            self._storage.values(),
            key=lambda c: c.timestamp,
            reverse=True,
        )
        return commits[offset:offset + limit]
    
    def get_by_execution_id(self, execution_id: str) -> List[FileCommit]:
        return [
            commit for commit in self._storage.values()
            if commit.execution_id == execution_id
        ]
    
    def get_by_checkpoint_id(self, checkpoint_id: int) -> Optional[FileCommit]:
        """Get commit linked to a checkpoint."""
        commit_id = self._checkpoint_links.get(checkpoint_id)
        if commit_id:
            return self._storage.get(commit_id)
        return None
    
    def delete(self, commit_id: CommitId) -> bool:
        if commit_id.value in self._storage:
            del self._storage[commit_id.value]
            # Clean up any checkpoint links pointing to this commit
            links_to_remove = [
                cp_id for cp_id, c_id in self._checkpoint_links.items()
                if c_id == commit_id.value
            ]
            for cp_id in links_to_remove:
                del self._checkpoint_links[cp_id]
            return True
        return False
    
    def count(self) -> int:
        return len(self._storage)
    
    # Helper methods for testing (not in interface)
    def link_checkpoint(self, checkpoint_id: int, commit_id: str) -> None:
        """Helper: link a checkpoint to commit (used by tests)."""
        self._checkpoint_links[checkpoint_id] = commit_id
    
    def exists(self, commit_id: CommitId) -> bool:
        """Helper: check if commit exists (used by tests)."""
        return commit_id.value in self._storage
    
    def clear(self):
        """Helper: clear all storage (used by tests)."""
        self._storage.clear()
        self._checkpoint_links.clear()


class InMemoryCheckpointFileLinkRepository(ICheckpointFileLinkRepository):
    """In-memory checkpoint file link repository for testing."""
    
    def __init__(self):
        self._storage: Dict[int, CheckpointFileLink] = {}
    
    def add(self, link: CheckpointFileLink) -> None:
        """Add or update a checkpoint-file link (upsert)."""
        self._storage[link.checkpoint_id] = link
    
    def get_by_checkpoint(self, checkpoint_id: int) -> Optional[CheckpointFileLink]:
        return self._storage.get(checkpoint_id)
    
    def get_by_commit(self, commit_id: CommitId) -> List[CheckpointFileLink]:
        return [
            link for link in self._storage.values()
            if link.commit_id.value == commit_id.value
        ]
    
    def delete_by_checkpoint(self, checkpoint_id: int) -> bool:
        if checkpoint_id in self._storage:
            del self._storage[checkpoint_id]
            return True
        return False
    
    def delete_by_commit(self, commit_id: CommitId) -> int:
        """Delete all links for a commit."""
        links_to_delete = [
            cp_id for cp_id, link in self._storage.items()
            if link.commit_id.value == commit_id.value
        ]
        for cp_id in links_to_delete:
            del self._storage[cp_id]
        return len(links_to_delete)
    
    # Helper methods for testing (not in interface)
    def exists(self, checkpoint_id: int) -> bool:
        """Helper: check if link exists (used by tests)."""
        return checkpoint_id in self._storage
    
    def list_all(self, limit: int = 100, offset: int = 0) -> List[CheckpointFileLink]:
        """Helper: list all links (used by tests)."""
        links = list(self._storage.values())
        return links[offset:offset + limit]
    
    def clear(self):
        """Helper: clear all storage (used by tests)."""
        self._storage.clear()


@pytest.fixture
def blob_repository():
    """Provide in-memory blob repository."""
    return InMemoryBlobRepository()


@pytest.fixture
def commit_repository():
    """Provide in-memory commit repository."""
    return InMemoryFileCommitRepository()


@pytest.fixture
def checkpoint_link_repository():
    """Provide in-memory checkpoint link repository."""
    return InMemoryCheckpointFileLinkRepository()


# ═══════════════════════════════════════════════════════════════════════════════
# Event Bus Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def event_bus():
    """Provide fresh event bus."""
    return WTBEventBus(max_history=1000)


@pytest.fixture
def event_collector(event_bus):
    """Create event collector that captures all file events."""
    collector = {
        "commit_created": [],
        "file_restored": [],
        "blob_created": [],
        "all": [],
    }
    
    def on_commit_created(event):
        collector["commit_created"].append(event)
        collector["all"].append(event)
    
    def on_file_restored(event):
        collector["file_restored"].append(event)
        collector["all"].append(event)
    
    def on_blob_created(event):
        collector["blob_created"].append(event)
        collector["all"].append(event)
    
    event_bus.subscribe(FileCommitCreatedEvent, on_commit_created)
    event_bus.subscribe(FileRestoredEvent, on_file_restored)
    event_bus.subscribe(BlobCreatedEvent, on_blob_created)
    
    return collector


# ═══════════════════════════════════════════════════════════════════════════════
# LangGraph Simulation Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def langgraph_checkpointer():
    """Create mock LangGraph MemorySaver checkpointer."""
    try:
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()
    except ImportError:
        # Return mock if LangGraph not installed
        mock_checkpointer = MagicMock()
        mock_checkpointer.get = MagicMock(return_value=None)
        mock_checkpointer.put = MagicMock()
        return mock_checkpointer


@pytest.fixture
def execution_context():
    """Create standard execution context for tests."""
    return {
        "execution_id": f"exec-{uuid.uuid4().hex[:8]}",
        "workflow_id": f"wf-{uuid.uuid4().hex[:8]}",
        "thread_id": f"thread-{uuid.uuid4().hex[:8]}",
        "checkpoint_step": 0,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════════════════════


def create_test_commit(
    file_paths: List[str],
    message: str = "Test commit",
    execution_id: Optional[str] = None,
) -> FileCommit:
    """
    Create a test commit with mementos from file paths.
    
    Note: Does NOT save blobs, only creates domain objects.
    """
    commit = FileCommit.create(message=message, execution_id=execution_id)
    
    for file_path in file_paths:
        if os.path.exists(file_path):
            memento, _ = FileMemento.from_file(file_path)
            commit.add_memento(memento)
        else:
            # Create mock memento for non-existent files
            mock_hash = BlobId.from_content(file_path.encode())
            memento = FileMemento.from_path_and_hash(
                file_path=file_path,
                file_hash=mock_hash,
                file_size=len(file_path),
            )
            commit.add_memento(memento)
    
    return commit


def create_and_save_commit(
    blob_repo: IBlobRepository,
    commit_repo: IFileCommitRepository,
    file_paths: List[str],
    message: str = "Test commit",
    execution_id: Optional[str] = None,
) -> FileCommit:
    """
    Create commit, save blobs, and save commit to repositories.
    
    Full workflow for creating a complete commit.
    """
    commit = FileCommit.create(message=message, execution_id=execution_id)
    
    for file_path in file_paths:
        memento, content = FileMemento.from_file(file_path)
        # Save blob (content-addressed, returns BlobId)
        blob_repo.save(content)
        commit.add_memento(memento)
    
    commit.finalize()
    commit_repo.save(commit)
    
    return commit


@pytest.fixture
def create_commit_helper(blob_repository, commit_repository):
    """Fixture providing commit creation helper function."""
    def _create(file_paths: List[str], message: str = "Test", execution_id: str = None):
        return create_and_save_commit(
            blob_repository,
            commit_repository,
            file_paths,
            message,
            execution_id,
        )
    return _create
