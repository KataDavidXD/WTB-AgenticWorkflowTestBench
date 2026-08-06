"""
File Processing Repository Interfaces.

Defines abstract interfaces for file processing persistence following DIP.
High-level domain code depends on these abstractions, not concrete implementations.

Design Principles:
- Interface Segregation: Separate interfaces for commits, blobs, mementos
- Dependency Inversion: Domain depends on abstractions
- Repository Pattern: Encapsulates data access logic

Implementations:
- SQLAlchemy: wtb/infrastructure/database/repositories/file_processing_repository.py
- InMemory: wtb/infrastructure/database/repositories/file_processing_inmemory.py
"""

from abc import ABC, abstractmethod
from typing import Any

from wtb.domain.models.file_processing import (
    BlobId,
    CheckpointFileLink,
    CommitId,
    FileCommit,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Blob Repository Interface
# ═══════════════════════════════════════════════════════════════════════════════


class IBlobRepository(ABC):
    """
    Repository interface for blob content storage.
    
    Handles content-addressable storage of file content.
    Blobs are identified by their SHA-256 hash (BlobId).
    
    Thread Safety:
    - Implementations should be thread-safe for concurrent access
    - Blob storage is inherently safe (same content = same ID)
    
    ACID Compliance:
    - save(): Should be idempotent (same content = same result)
    - delete(): Should handle missing blobs gracefully
    """
    
    @abstractmethod
    def save(self, content: bytes) -> BlobId:
        """
        Save content to blob storage.
        
        Content-addressable: Returns same BlobId for same content.
        Idempotent: Calling with same content multiple times is safe.
        
        Args:
            content: Binary content to store
            
        Returns:
            BlobId (SHA-256 hash of content)
        """
        pass
    
    @abstractmethod
    def get(self, blob_id: BlobId) -> bytes | None:
        """
        Retrieve content by blob ID.
        
        Args:
            blob_id: Content-addressable identifier
            
        Returns:
            Binary content if found, None otherwise
        """
        pass
    
    @abstractmethod
    def exists(self, blob_id: BlobId) -> bool:
        """
        Check if blob exists in storage.
        
        Args:
            blob_id: Blob identifier to check
            
        Returns:
            True if blob exists, False otherwise
        """
        pass
    
    @abstractmethod
    def delete(self, blob_id: BlobId) -> bool:
        """
        Delete blob from storage.
        
        Args:
            blob_id: Blob to delete
            
        Returns:
            True if blob was deleted, False if not found
        """
        pass
    
    @abstractmethod
    def restore_to_file(self, blob_id: BlobId, output_path: str) -> None:
        """
        Restore blob content to file system.
        
        Creates parent directories if needed.
        
        Args:
            blob_id: Blob to restore
            output_path: Destination file path
            
        Raises:
            FileNotFoundError: If blob not found
        """
        pass
    
    @abstractmethod
    def get_stats(self) -> dict[str, Any]:
        """
        Get storage statistics.
        
        Returns:
            Dictionary with:
            - blob_count: Number of blobs
            - total_size_bytes: Total storage size
            - total_size_mb: Size in megabytes
        """
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# File Commit Repository Interface
# ═══════════════════════════════════════════════════════════════════════════════


class IFileCommitRepository(ABC):
    """
    Repository interface for FileCommit aggregate root.
    
    Manages persistence of file commits and their mementos.
    Commits are the aggregate root - mementos are saved/loaded together.
    
    Transaction Boundary:
    - save(): Saves commit + all mementos atomically
    - delete(): Deletes commit + cascades to mementos
    
    Design:
    - Aggregate root repository (owns memento lifecycle)
    - Load patterns: with/without mementos for performance
    """
    
    @abstractmethod
    def save(self, commit: FileCommit) -> None:
        """
        Save commit with all mementos.
        
        Atomic operation: Either all saved or none.
        Updates existing commit if ID exists.
        
        Args:
            commit: FileCommit to persist
        """
        pass
    
    @abstractmethod
    def get_by_id(self, commit_id: CommitId) -> FileCommit | None:
        """
        Get commit by ID with mementos loaded.
        
        Args:
            commit_id: Commit identifier
            
        Returns:
            FileCommit with mementos if found, None otherwise
        """
        pass
    
    @abstractmethod
    def get_by_id_without_mementos(self, commit_id: CommitId) -> FileCommit | None:
        """
        Get commit by ID WITHOUT loading mementos.
        
        Performance optimization for listings.
        
        Args:
            commit_id: Commit identifier
            
        Returns:
            FileCommit with empty mementos list if found, None otherwise
        """
        pass
    
    @abstractmethod
    def get_all(self, limit: int = 100, offset: int = 0) -> list[FileCommit]:
        """
        Get all commits (without mementos).
        
        For listing/pagination. Mementos not loaded for performance.
        Ordered by timestamp descending (newest first).
        
        Args:
            limit: Maximum commits to return
            offset: Number of commits to skip
            
        Returns:
            List of FileCommit instances (mementos not loaded)
        """
        pass
    
    @abstractmethod
    def get_by_execution_id(self, execution_id: str) -> list[FileCommit]:
        """
        Get commits for a WTB execution.
        
        Args:
            execution_id: WTB execution identifier
            
        Returns:
            List of commits for that execution
        """
        pass
    
    @abstractmethod
    def get_by_checkpoint_id(self, checkpoint_id: str) -> FileCommit | None:
        """
        Get commit linked to checkpoint.
        
        Args:
            checkpoint_id: WTB checkpoint ID
            
        Returns:
            Linked FileCommit if found, None otherwise
        """
        pass
    
    @abstractmethod
    def delete(self, commit_id: CommitId) -> bool:
        """
        Delete commit and cascade to mementos.
        
        Does NOT delete blob content (may be shared).
        
        Args:
            commit_id: Commit to delete
            
        Returns:
            True if deleted, False if not found
        """
        pass
    
    @abstractmethod
    def count(self) -> int:
        """
        Get total number of commits.
        
        Returns:
            Total commit count
        """
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# Checkpoint File Link Repository Interface
# ═══════════════════════════════════════════════════════════════════════════════


class ICheckpointFileLinkRepository(ABC):
    """
    Repository interface for checkpoint-file links.
    
    Manages the association between WTB checkpoints and file commits.
    Used for coordinated rollback across state and file systems.
    """
    
    @abstractmethod
    def add(self, link: CheckpointFileLink) -> None:
        """
        Add a checkpoint-file link.
        
        Upsert behavior: Updates if checkpoint_id exists.
        
        Args:
            link: Link to add
        """
        pass
    
    @abstractmethod
    def get_by_checkpoint(self, checkpoint_id: str) -> CheckpointFileLink | None:
        """
        Get link by checkpoint ID.
        
        Args:
            checkpoint_id: Checkpoint to look up
            
        Returns:
            CheckpointFileLink if found, None otherwise
        """
        pass
    
    @abstractmethod
    def get_by_commit(self, commit_id: CommitId) -> list[CheckpointFileLink]:
        """
        Get all links for a commit.
        
        A commit may be linked to multiple checkpoints.
        
        Args:
            commit_id: Commit to look up
            
        Returns:
            List of links for that commit
        """
        pass
    
    @abstractmethod
    def delete_by_checkpoint(self, checkpoint_id: str) -> bool:
        """
        Delete link by checkpoint ID.
        
        Args:
            checkpoint_id: Checkpoint whose link to delete
            
        Returns:
            True if deleted, False if not found
        """
        pass
    
    @abstractmethod
    def delete_by_commit(self, commit_id: CommitId) -> int:
        """
        Delete all links for a commit.
        
        Args:
            commit_id: Commit whose links to delete
            
        Returns:
            Number of links deleted
        """
        pass
    
    @abstractmethod
    def list_all(self, limit: int = 10000) -> list[CheckpointFileLink]:
        """
        List all checkpoint file links.
        
        Used by integrity checkers to validate cross-system references.
        
        Args:
            limit: Maximum number of links to return
            
        Returns:
            List of checkpoint file links
        """
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# Factory Interface
# ═══════════════════════════════════════════════════════════════════════════════


class IFileProcessingUnitOfWork(ABC):
    """
    Unit of Work interface for file processing operations.
    
    Provides transactional access to all file processing repositories.
    Ensures ACID compliance across commit + memento + link operations.
    
    Usage:
        with uow:
            commit = FileCommit.create(message="Snapshot")
            # Add mementos, save blobs
            uow.file_commits.save(commit)
            uow.checkpoint_links.add(link)
            uow.commit()  # Atomic
    """
    
    @property
    @abstractmethod
    def file_commits(self) -> IFileCommitRepository:
        """Access to file commit repository."""
        pass
    
    @property
    @abstractmethod
    def blobs(self) -> IBlobRepository:
        """Access to blob repository."""
        pass
    
    @property
    @abstractmethod
    def checkpoint_links(self) -> ICheckpointFileLinkRepository:
        """Access to checkpoint-file link repository."""
        pass
    
    @abstractmethod
    def __enter__(self) -> "IFileProcessingUnitOfWork":
        """Begin transaction."""
        pass
    
    @abstractmethod
    def __exit__(self, exc_type, exc_val, exc_tb):
        """End transaction (rollback on exception)."""
        pass
    
    @abstractmethod
    def commit(self) -> None:
        """Commit transaction."""
        pass
    
    @abstractmethod
    def rollback(self) -> None:
        """Rollback transaction."""
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# Export
# ═══════════════════════════════════════════════════════════════════════════════

__all__ = [
    "IBlobRepository",
    "IFileCommitRepository",
    "ICheckpointFileLinkRepository",
    "IFileProcessingUnitOfWork",
]
