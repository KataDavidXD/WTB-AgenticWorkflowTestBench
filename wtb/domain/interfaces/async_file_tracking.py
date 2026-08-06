
from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FileTrackingResult:
    commit_id: str
    files_tracked: int
    total_size_bytes: int


class IAsyncFileTrackingService(ABC):
    """
    Async File Tracking Service Interface.
    
    Provides high-level file tracking operations using repository pattern.
    Aligned with ASYNC_ARCHITECTURE_PLAN.md.
    """

    @abstractmethod
    async def atrack_files(
        self,
        file_paths: List[str],
        message: str,
        checkpoint_id: Optional[str] = None,
    ) -> FileTrackingResult:
        """
        Track files asynchronously with content-addressable storage.
        
        ACID Compliance:
        - All operations within single UoW transaction
        - Blob storage + commit + link atomically committed
        - Rollback on any failure
        """
        pass

    @abstractmethod
    async def arestore_files(
        self,
        checkpoint_id: str,
        output_dir: Path,
    ) -> int:
        """
        Restore files from checkpoint asynchronously.
        
        Lookup flow:
        1. checkpoint_id → CheckpointFileLink → commit_id
        2. commit_id → FileCommit → mementos
        3. memento.blob_hash → BlobRepository → content
        4. Write content to output_dir
        
        Returns:
            Number of files restored
        """
        pass

    @abstractmethod
    async def atrack_and_link(
        self,
        checkpoint_id: str,
        file_paths: List[str],
        message: str,
    ) -> FileTrackingResult:
        """
        Convenience method: Track files and link to checkpoint in one call.
        """
        pass

    async def atrack_and_link_in_uow(
        self,
        uow: Any,
        checkpoint_id: str,
        file_paths: List[str],
        message: str,
    ) -> FileTrackingResult:
        """Track and link files inside a caller-owned unit of work.

        The default keeps legacy implementations instantiable, while callers
        that require atomic execution and file metadata must fail closed unless
        the service explicitly implements this capability.
        """
        raise NotImplementedError(
            "File tracking service does not support a shared unit of work"
        )
