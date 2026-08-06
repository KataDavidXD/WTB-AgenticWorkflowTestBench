
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Generic, Optional, TypeVar

# Forward references for type checking
from wtb.domain.models.file_processing import (
    BlobId,
    CheckpointFileLink,
    CommitId,
    FileCommit,
)
from wtb.domain.models.outbox import OutboxEvent

T = TypeVar('T')


class IAsyncReadRepository(ABC, Generic[T]):
    """Async read-only repository interface."""
  
    @abstractmethod
    async def aget(self, id: str) -> T | None:
        """Get entity by ID asynchronously."""
        pass
  
    @abstractmethod
    async def alist(self, limit: int = 100, offset: int = 0) -> list[T]:
        """List entities with pagination asynchronously."""
        pass
  
    @abstractmethod
    async def aexists(self, id: str) -> bool:
        """Check if entity exists asynchronously."""
        pass


class IAsyncWriteRepository(ABC, Generic[T]):
    """Async write repository interface."""
  
    @abstractmethod
    async def aadd(self, entity: T) -> T:
        """Add entity asynchronously."""
        pass
  
    @abstractmethod
    async def aupdate(self, entity: T) -> T:
        """Update entity asynchronously."""
        pass
  
    @abstractmethod
    async def adelete(self, id: str) -> bool:
        """Delete entity asynchronously."""
        pass


class IAsyncRepository(IAsyncReadRepository[T], IAsyncWriteRepository[T]):
    """Combined async read/write repository."""
    pass


class IAsyncOutboxRepository(ABC):
    """
    Async outbox repository with FIFO ordering guarantee.
  
    IMPORTANT: Events MUST be processed in creation order (FIFO)
    to maintain causal consistency.
    """
  
    @abstractmethod
    async def aadd(self, event: "OutboxEvent") -> "OutboxEvent":
        """Add outbox event."""
        pass
  
    @abstractmethod
    async def aget_pending(
        self,
        limit: int = 100,
        order_by: str = "created_at",  # FIFO ordering - DO NOT CHANGE
    ) -> list["OutboxEvent"]:
        """
        Get pending events in FIFO order.
      
        Args:
            limit: Max events to retrieve
            order_by: Column to order by (default: created_at for FIFO)
      
        Returns:
            Events ordered by creation time (oldest first)
        """
        pass
  
    @abstractmethod
    async def aupdate(self, event: "OutboxEvent") -> "OutboxEvent":
        """Update outbox event status."""
        pass
  
    @abstractmethod
    async def adelete_processed(self, older_than_hours: int = 24) -> int:
        """Delete processed events older than threshold."""
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# Async File Processing Repositories
# (Aligned with FILE_TRACKING_ARCHITECTURE_DECISION.md)
# ═══════════════════════════════════════════════════════════════════════════════


class IAsyncBlobRepository(ABC):
    """
    Async blob storage interface.
    
    Content-addressable storage using SHA-256 hashes.
    Aligned with sync IBlobRepository from file_processing_repository.py.
    
    Two-Phase Write Pattern:
    1. Filesystem write (async via aiofiles)
    2. DB record in session
    3. Commit → both or neither persisted
    
    Note: If commit fails, orphaned blobs may remain on filesystem.
    Use AsyncBlobOrphanCleaner for periodic cleanup.
    """
    
    @abstractmethod
    async def asave(self, content: bytes) -> "BlobId":
        """
        Save content to blob storage asynchronously.
        
        Content-addressable: Returns same BlobId for same content.
        Idempotent: Calling with same content multiple times is safe.
        
        Args:
            content: Binary content to store
            
        Returns:
            BlobId (SHA-256 hash of content)
        """
        pass
    
    @abstractmethod
    async def aget(self, blob_id: "BlobId") -> bytes | None:
        """
        Retrieve content by blob ID asynchronously.
        
        Args:
            blob_id: Content-addressable identifier
            
        Returns:
            Binary content if found, None otherwise
        """
        pass
    
    @abstractmethod
    async def aexists(self, blob_id: "BlobId") -> bool:
        """Check if blob exists asynchronously."""
        pass
    
    @abstractmethod
    async def adelete(self, blob_id: "BlobId") -> bool:
        """Delete blob asynchronously. Returns True if deleted."""
        pass
    
    @abstractmethod
    async def arestore_to_file(self, blob_id: "BlobId", output_path: Path) -> None:
        """
        Restore blob content to file system asynchronously.
        
        Creates parent directories if needed using aiofiles.os.makedirs().
        
        Args:
            blob_id: Blob to restore
            output_path: Destination file path
            
        Raises:
            FileNotFoundError: If blob not found
        """
        pass
    
    @abstractmethod
    async def alist_all_hashes(self) -> list["BlobId"]:
        """
        List all blob hashes in storage.
        
        Used by AsyncBlobOrphanCleaner to detect orphans.
        """
        pass


class IAsyncFileCommitRepository(ABC):
    """
    Async repository for FileCommit aggregate root.
    
    Aligned with sync IFileCommitRepository from file_processing_repository.py.
    
    Aggregate Pattern:
    - FileCommit is the aggregate root
    - FileMemento entities are owned by FileCommit
    - Save/delete operations cascade to mementos
    """
    
    @abstractmethod
    async def asave(self, commit: "FileCommit") -> None:
        """
        Save commit with all mementos atomically.
        
        Updates existing commit if ID exists.
        """
        pass
    
    @abstractmethod
    async def aget_by_id(self, commit_id: "CommitId") -> Optional["FileCommit"]:
        """Get commit by ID with mementos loaded."""
        pass
    
    @abstractmethod
    async def aget_by_id_without_mementos(self, commit_id: "CommitId") -> Optional["FileCommit"]:
        """Get commit by ID WITHOUT mementos (performance optimization)."""
        pass
    
    @abstractmethod
    async def aget_by_checkpoint_id(self, checkpoint_id: str) -> Optional["FileCommit"]:
        """Get commit linked to checkpoint."""
        pass
    
    @abstractmethod
    async def aget_by_execution_id(self, execution_id: str) -> list["FileCommit"]:
        """Get all commits for an execution."""
        pass
    
    @abstractmethod
    async def adelete(self, commit_id: "CommitId") -> bool:
        """Delete commit and cascade to mementos."""
        pass
    
    @abstractmethod
    async def acount(self) -> int:
        """Get total number of commits."""
        pass


class IAsyncCheckpointFileLinkRepository(ABC):
    """
    Async repository for checkpoint-file links.
    
    Manages associations between WTB checkpoints and file commits.
    Used for coordinated rollback across state and file systems.
    """
    
    @abstractmethod
    async def aadd(self, link: "CheckpointFileLink") -> None:
        """Add a checkpoint-file link (upsert behavior)."""
        pass
    
    @abstractmethod
    async def aget_by_checkpoint(self, checkpoint_id: str) -> Optional["CheckpointFileLink"]:
        """Get link by checkpoint ID."""
        pass
    
    @abstractmethod
    async def aget_by_commit(self, commit_id: "CommitId") -> list["CheckpointFileLink"]:
        """Get all links for a commit."""
        pass
    
    @abstractmethod
    async def adelete_by_checkpoint(self, checkpoint_id: str) -> bool:
        """Delete link by checkpoint ID."""
        pass
    
    @abstractmethod
    async def adelete_by_commit(self, commit_id: "CommitId") -> int:
        """Delete all links for a commit. Returns count deleted."""
        pass
