import pytest
import shutil
import tempfile
import os
from pathlib import Path
from wtb.infrastructure.database.unit_of_work import SQLAlchemyUnitOfWork
from wtb.domain.models.file_processing import FileCommit, FileMemento, CheckpointFileLink, BlobId
from wtb.domain.interfaces.unit_of_work import IUnitOfWork

@pytest.fixture
def temp_blob_storage():
    """Create a temporary directory for blob storage."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)

@pytest.fixture
def uow(temp_blob_storage):
    """Create a SQLAlchemyUnitOfWork with in-memory DB and temp blob storage."""
    # Use in-memory SQLite for speed and isolation
    db_url = "sqlite:///:memory:"
    uow = SQLAlchemyUnitOfWork(db_url=db_url, blob_storage_path=temp_blob_storage)
    return uow

def test_uow_interface_compliance(uow):
    """Verify that SQLAlchemyUnitOfWork implements the new IUnitOfWork properties."""
    assert isinstance(uow, IUnitOfWork)
    with uow:
        # Check for existence of new repositories
        assert hasattr(uow, 'blobs')
        assert hasattr(uow, 'file_commits')
        assert hasattr(uow, 'checkpoint_file_links')
        
        # Verify types (optional, but good sanity check)
        from wtb.domain.interfaces.file_processing_repository import (
            IBlobRepository, 
            IFileCommitRepository,
            ICheckpointFileLinkRepository
        )
        assert isinstance(uow.blobs, IBlobRepository)
        assert isinstance(uow.file_commits, IFileCommitRepository)
        assert isinstance(uow.checkpoint_file_links, ICheckpointFileLinkRepository)

def test_file_tracking_workflow_acid(uow):
    """
    Test a complete file tracking workflow within a UoW transaction.
    Verifies ACID compliance: Blob write + DB commit.
    """
    content = b"test content for acid compliance"
    blob_id = BlobId.from_content(content)
    
    # 1. Transaction: Save everything
    with uow:
        # Save Blob
        saved_blob_id = uow.blobs.save(content)
        assert saved_blob_id == blob_id
        
        # Create Commit
        memento = FileMemento.from_path_and_hash("test.txt", blob_id, len(content))
        commit = FileCommit.create(message="Test Commit")
        commit.add_memento(memento)
        commit.finalize()  # Mark as ready for persistence
        uow.file_commits.save(commit)
        
        # Link to Checkpoint
        checkpoint_id = 123
        link = CheckpointFileLink.create(checkpoint_id=checkpoint_id, commit=commit)
        uow.checkpoint_file_links.add(link)
        
        uow.commit()
        
        # Capture IDs for verification
        commit_id = commit.commit_id

    # 2. Verification: Read back in new transaction
    with uow:
        # Verify Blob content
        fetched_content = uow.blobs.get(blob_id)
        assert fetched_content == content
        
        # Verify Commit
        fetched_commit = uow.file_commits.get_by_id(commit_id)
        assert fetched_commit is not None
        assert fetched_commit.message == "Test Commit"
        assert len(fetched_commit.mementos) == 1
        assert fetched_commit.mementos[0].file_hash == blob_id
        
        # Verify Checkpoint Link
        fetched_link = uow.checkpoint_file_links.get_by_checkpoint(123)
        assert fetched_link is not None
        assert fetched_link.commit_id == commit_id

def test_uow_rollback(uow):
    """Verify that database changes are rolled back on error."""
    content = b"content to rollback"
    blob_id = BlobId.from_content(content)
    
    try:
        with uow:
            # Save Blob (File system write happens immediately usually, but DB record is transactional)
            uow.blobs.save(content)
            
            commit = FileCommit.create(message="Rollback Commit")
            # We don't add mementos or finalize here as we just want to test rollback of the commit entity
            # but usually save() might expect a valid commit. 
            # If finalize() is required for save logic (unlikely but possible), we might need it.
            # However, empty commit is valid for PENDING status.
            uow.file_commits.save(commit)
            
            # Raise error to force rollback
            raise ValueError("Force rollback")
    except ValueError:
        pass
        
    # Verify DB records do not exist
    with uow:
        assert not uow.blobs.exists(blob_id)  # DB record should be gone
        # Note: File on disk might still exist (orphaned), which is expected behavior 
        # as described in architecture doc (Orphan Cleanup required)
        
        commits = uow.file_commits.get_all()
        assert len(commits) == 0
