"""
File Tracking Service Interface.

Defines the contract for file tracking integration with Ray batch test execution.
Follows DIP - high-level components depend on this abstraction, not concrete FileTracker.

Design Principles:
- SOLID: Single responsibility (file operations only), Interface Segregation
- DDD: Domain interface with value objects for results
- ACID: Operations designed for transactional consistency

Usage:
    from wtb.domain.interfaces.file_tracking import IFileTrackingService, TrackedFile
    
    class MyService:
        def __init__(self, file_tracking: IFileTrackingService):
            self._file_tracking = file_tracking
        
        def process_with_tracking(self, checkpoint_id: int, files: List[str]):
            result = self._file_tracking.track_and_link(checkpoint_id, files)
            return result.commit_id
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from enum import Enum


class FileTrackingError(Exception):
    """Base exception for file tracking errors."""
    pass


class FileNotFoundError(FileTrackingError):
    """File not found during tracking."""
    pass


class CommitNotFoundError(FileTrackingError):
    """Commit not found during restore."""
    pass


class CheckpointLinkError(FileTrackingError):
    """Error linking checkpoint to commit."""
    pass


# ═══════════════════════════════════════════════════════════════════════════════
# Value Objects
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class TrackedFile:
    """
    Information about a tracked file.
    
    Value object representing a file's tracking state.
    Immutable for thread-safety.
    """
    file_path: str
    file_hash: str
    size_bytes: int
    tracked_at: datetime
    
    def to_dict(self) -> Dict:
        """Serialize to dictionary."""
        return {
            "file_path": self.file_path,
            "file_hash": self.file_hash,
            "size_bytes": self.size_bytes,
            "tracked_at": self.tracked_at.isoformat(),
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> "TrackedFile":
        """Deserialize from dictionary."""
        return cls(
            file_path=data["file_path"],
            file_hash=data["file_hash"],
            size_bytes=data["size_bytes"],
            tracked_at=datetime.fromisoformat(data["tracked_at"]),
        )


@dataclass(frozen=True)
class FileTrackingResult:
    """
    Result of a file tracking operation.
    
    Value object containing commit information and tracked files.
    Immutable for thread-safety.
    """
    commit_id: str
    files_tracked: int
    total_size_bytes: int
    file_hashes: Dict[str, str]  # path -> hash
    message: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    
    @property
    def is_empty(self) -> bool:
        """Check if no files were tracked."""
        return self.files_tracked == 0
    
    def to_dict(self) -> Dict:
        """Serialize to dictionary."""
        return {
            "commit_id": self.commit_id,
            "files_tracked": self.files_tracked,
            "total_size_bytes": self.total_size_bytes,
            "file_hashes": self.file_hashes,
            "message": self.message,
            "created_at": self.created_at.isoformat(),
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> "FileTrackingResult":
        """Deserialize from dictionary."""
        return cls(
            commit_id=data["commit_id"],
            files_tracked=data["files_tracked"],
            total_size_bytes=data["total_size_bytes"],
            file_hashes=data["file_hashes"],
            message=data.get("message"),
            created_at=datetime.fromisoformat(data["created_at"]),
        )


@dataclass(frozen=True)
class FileRestoreResult:
    """
    Result of a file restore operation.
    
    Value object containing information about restored files.
    """
    commit_id: str
    files_restored: int
    total_size_bytes: int
    restored_paths: List[str]
    success: bool
    error_message: Optional[str] = None
    
    def to_dict(self) -> Dict:
        """Serialize to dictionary."""
        return {
            "commit_id": self.commit_id,
            "files_restored": self.files_restored,
            "total_size_bytes": self.total_size_bytes,
            "restored_paths": self.restored_paths,
            "success": self.success,
            "error_message": self.error_message,
        }


@dataclass(frozen=True)
class FileTrackingLink:
    """
    Link between a WTB checkpoint and a FileTracker commit.
    
    Interface Value Object for ACL boundary - uses primitive types
    for serialization and external system integration.
    
    Note: For domain-internal use, see domain/models/file_processing.CheckpointFileLink
    which uses rich value objects like CommitId.
    
    Design Decision (2026-01-16):
    - Renamed from CheckpointFileLink to FileTrackingLink to avoid confusion
    - with domain model CheckpointFileLink in file_processing.py
    - Interface VOs use primitives; domain models use value objects
    """
    checkpoint_id: int
    commit_id: str  # Primitive str for ACL boundary (not CommitId value object)
    linked_at: datetime
    file_count: int
    total_size_bytes: int
    
    def to_dict(self) -> Dict:
        """Serialize to dictionary."""
        return {
            "checkpoint_id": self.checkpoint_id,
            "commit_id": self.commit_id,
            "linked_at": self.linked_at.isoformat(),
            "file_count": self.file_count,
            "total_size_bytes": self.total_size_bytes,
        }


@dataclass(frozen=True)
class FileCleanupResult:
    """
    Result of orphaned file cleanup operation (v1.9).
    
    Value object representing the outcome of cleaning up files created after
    a checkpoint during rollback. Immutable for thread-safety.
    
    Design Decision (2026-02-13):
    - Separate from FileRestoreResult to follow SRP
    - Uses tuples (not lists) for frozen=True compatibility
    - success property derived from errors for consistency
    """
    checkpoint_id: int
    execution_id: str
    files_deleted: int = 0
    files_backed_up: int = 0
    files_skipped: int = 0
    deleted_paths: tuple = field(default_factory=tuple)  # Immutable for frozen=True
    backed_up_paths: tuple = field(default_factory=tuple)
    skipped_paths: tuple = field(default_factory=tuple)
    errors: tuple = field(default_factory=tuple)
    dry_run: bool = False
    
    @property
    def success(self) -> bool:
        """Check if cleanup completed without errors."""
        return len(self.errors) == 0
    
    @property
    def total_processed(self) -> int:
        """Total files processed (deleted + backed up + skipped)."""
        return self.files_deleted + self.files_backed_up + self.files_skipped
    
    def to_dict(self) -> Dict:
        """Serialize to dictionary."""
        return {
            "checkpoint_id": self.checkpoint_id,
            "execution_id": self.execution_id,
            "files_deleted": self.files_deleted,
            "files_backed_up": self.files_backed_up,
            "files_skipped": self.files_skipped,
            "deleted_paths": list(self.deleted_paths),
            "backed_up_paths": list(self.backed_up_paths),
            "skipped_paths": list(self.skipped_paths),
            "errors": list(self.errors),
            "dry_run": self.dry_run,
            "success": self.success,
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> "FileCleanupResult":
        """Deserialize from dictionary."""
        return cls(
            checkpoint_id=data["checkpoint_id"],
            execution_id=data["execution_id"],
            files_deleted=data.get("files_deleted", 0),
            files_backed_up=data.get("files_backed_up", 0),
            files_skipped=data.get("files_skipped", 0),
            deleted_paths=tuple(data.get("deleted_paths", [])),
            backed_up_paths=tuple(data.get("backed_up_paths", [])),
            skipped_paths=tuple(data.get("skipped_paths", [])),
            errors=tuple(data.get("errors", [])),
            dry_run=data.get("dry_run", False),
        )
    
    @classmethod
    def empty(cls, checkpoint_id: int, execution_id: str, dry_run: bool = False) -> "FileCleanupResult":
        """Create empty result (no files to cleanup)."""
        return cls(
            checkpoint_id=checkpoint_id,
            execution_id=execution_id,
            dry_run=dry_run,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Service Interface
# ═══════════════════════════════════════════════════════════════════════════════


class IFileTrackingService(ABC):
    """
    Interface for file tracking integration.
    
    Follows DIP - high-level components depend on this abstraction,
    not on concrete FileTracker implementation.
    
    Design Decisions:
    - Single Responsibility: Only handles file operations
    - Interface Segregation: Focused methods for specific operations
    - Dependency Inversion: Domain depends on abstraction
    
    Implementations:
    - MockFileTrackingService: For testing (in-memory)
    - FileTrackerService: Real FileTracker integration
    - RayFileTrackerService: Ray-compatible wrapper (serializable config)
    
    Transaction Strategy:
    - track_and_link(): Atomic operation combining tracking and linking
    - restore_from_checkpoint(): Looks up link then restores files
    """
    
    @abstractmethod
    def track_files(
        self,
        file_paths: List[str],
        message: Optional[str] = None,
    ) -> FileTrackingResult:
        """
        Track specified files and create a commit.
        
        Creates a FileTracker commit containing snapshots of all specified files.
        Each file is hashed and stored in content-addressed storage.
        
        Args:
            file_paths: List of file paths to track (absolute or relative)
            message: Optional commit message for audit trail
            
        Returns:
            FileTrackingResult with commit info and file hashes
            
        Raises:
            FileNotFoundError: If any file does not exist
            FileTrackingError: If tracking fails
        """
        pass
    
    @abstractmethod
    def track_and_link(
        self,
        checkpoint_id: int,
        file_paths: List[str],
        message: Optional[str] = None,
    ) -> FileTrackingResult:
        """
        Track files AND link to checkpoint in single operation.
        
        Atomic operation that:
        1. Creates FileTracker commit with file snapshots
        2. Links the commit to the specified WTB checkpoint
        
        This is the primary method for checkpoint-file coordination.
        
        Args:
            checkpoint_id: WTB checkpoint ID to link to
            file_paths: Files to track
            message: Optional commit message
            
        Returns:
            FileTrackingResult with commit info
            
        Raises:
            FileNotFoundError: If any file does not exist
            CheckpointLinkError: If linking fails
        """
        pass
    
    @abstractmethod
    def link_to_checkpoint(
        self,
        checkpoint_id: int,
        commit_id: str,
    ) -> FileTrackingLink:
        """
        Link existing commit to checkpoint.
        
        Creates a checkpoint_files record linking a WTB checkpoint
        to an existing FileTracker commit.
        
        Args:
            checkpoint_id: WTB checkpoint ID
            commit_id: FileTracker commit ID
            
        Returns:
            FileTrackingLink with link details
            
        Raises:
            CommitNotFoundError: If commit does not exist
            CheckpointLinkError: If linking fails
        """
        pass
    
    @abstractmethod
    def restore_from_checkpoint(
        self,
        checkpoint_id: int,
    ) -> FileRestoreResult:
        """
        Restore files from checkpoint's linked commit.
        
        Looks up the commit linked to the checkpoint, then restores
        all files from that commit to their original paths.
        
        Args:
            checkpoint_id: Checkpoint to restore from
            
        Returns:
            FileRestoreResult with restore details
            
        Raises:
            CheckpointLinkError: If checkpoint has no linked commit
            CommitNotFoundError: If linked commit not found
        """
        pass
    
    @abstractmethod
    def restore_commit(
        self,
        commit_id: str,
    ) -> FileRestoreResult:
        """
        Restore files from a specific commit.
        
        Restores all files from the commit to their original paths.
        
        Args:
            commit_id: FileTracker commit ID
            
        Returns:
            FileRestoreResult with restore details
            
        Raises:
            CommitNotFoundError: If commit not found
        """
        pass
    
    @abstractmethod
    def get_commit_for_checkpoint(
        self,
        checkpoint_id: int,
    ) -> Optional[str]:
        """
        Get the file commit ID linked to a checkpoint.
        
        Args:
            checkpoint_id: Checkpoint to query
            
        Returns:
            Commit ID if linked, None otherwise
        """
        pass
    
    @abstractmethod
    def get_tracked_files(
        self,
        commit_id: str,
    ) -> List[TrackedFile]:
        """
        Get list of tracked files for a commit.
        
        Args:
            commit_id: FileTracker commit ID
            
        Returns:
            List of TrackedFile objects
            
        Raises:
            CommitNotFoundError: If commit not found
        """
        pass
    
    @abstractmethod
    def is_available(self) -> bool:
        """
        Check if file tracking service is available and configured.
        
        Returns:
            True if service can track files, False otherwise
        """
        pass
    
    @abstractmethod
    def get_files_at_checkpoint(
        self,
        checkpoint_id: int,
    ) -> List[str]:
        """
        Get file paths that existed at a specific checkpoint.
        
        Uses existing checkpoint_links -> commit_id -> mementos relationship.
        NO new table required - leverages existing schema.
        
        Args:
            checkpoint_id: Checkpoint to query
            
        Returns:
            List of file paths that existed at the checkpoint
            
        Design Decision (2026-02-13):
        - Added for v1.9 rollback cleanup feature
        - Uses existing tables: checkpoint_links + mementos
        - Avoids data duplication
        """
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# File Cleanup Service Interface (v1.9 - ISP Compliant)
# ═══════════════════════════════════════════════════════════════════════════════


class IFileCleanupService(ABC):
    """
    Interface for post-rollback file cleanup operations (v1.9).
    
    Follows Interface Segregation Principle - cleanup is a separate concern
    from file tracking. This interface handles identifying and removing
    orphaned files (files created after a checkpoint during rollback).
    
    Design Principles:
    - ISP: Cleanup operations separated from IFileTrackingService
    - SRP: Single responsibility - orphaned file management
    - OCP: Open for extension (strategies), closed for modification
    
    Safety Features:
    - max_files limit prevents runaway deletion
    - dry_run mode for testing cleanup logic
    - backup option preserves files before deletion
    
    Usage:
        cleanup_service = FileCleanupService()
        orphaned = cleanup_service.identify_orphaned_files(
            target_checkpoint_id=42,
            current_workspace_path=Path("/project"),
            track_patterns=["*.py", "data/*.json"],
            exclude_patterns=["*.pyc", "__pycache__/*"],
            file_tracking_service=file_tracker,
        )
        result = cleanup_service.cleanup_orphaned_files(
            orphaned_paths=orphaned,
            backup_dir=Path(".rollback_backup"),
            dry_run=False,
            max_files=100,
        )
    """
    
    @abstractmethod
    def identify_orphaned_files(
        self,
        target_checkpoint_id: int,
        execution_id: str,
        current_workspace_path: Path,
        track_patterns: List[str],
        exclude_patterns: List[str],
        file_tracking_service: IFileTrackingService,
    ) -> List[str]:
        """
        Identify files created after target checkpoint.
        
        Compares current workspace files (matching patterns) against
        files tracked at the checkpoint. Returns paths that exist now
        but weren't tracked at the checkpoint.
        
        Args:
            target_checkpoint_id: Checkpoint being rolled back to
            execution_id: Execution ID for logging/tracking
            current_workspace_path: Root path to scan for current files
            track_patterns: Glob patterns for files to consider (e.g., ["*.py"])
            exclude_patterns: Glob patterns to exclude (e.g., ["*.pyc"])
            file_tracking_service: Service to query checkpoint file state
            
        Returns:
            List of file paths that are orphaned (created after checkpoint)
        """
        pass
    
    @abstractmethod
    def cleanup_orphaned_files(
        self,
        checkpoint_id: int,
        execution_id: str,
        orphaned_paths: List[str],
        backup_dir: Optional[Path] = None,
        dry_run: bool = False,
        max_files: int = 100,
    ) -> FileCleanupResult:
        """
        Delete orphaned files with safety checks.
        
        Removes files that were created after a checkpoint, with optional
        backup and safety limits.
        
        Args:
            checkpoint_id: Source checkpoint (for result tracking)
            execution_id: Execution being rolled back
            orphaned_paths: Files to delete (from identify_orphaned_files)
            backup_dir: If provided, backup files before deletion
            dry_run: If True, log actions but don't delete
            max_files: Safety limit - refuse to delete more than this
            
        Returns:
            FileCleanupResult with detailed operation outcome
            
        Raises:
            ValueError: If orphaned_paths exceeds max_files and not dry_run
        """
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# Factory Interface
# ═══════════════════════════════════════════════════════════════════════════════


class IFileTrackingServiceFactory(ABC):
    """
    Factory for creating IFileTrackingService instances.
    
    Supports different creation strategies for testing vs production.
    """
    
    @abstractmethod
    def create(self) -> IFileTrackingService:
        """
        Create a new file tracking service instance.
        
        Returns:
            Configured IFileTrackingService
        """
        pass
    
    @abstractmethod
    def create_for_testing(self) -> IFileTrackingService:
        """
        Create a mock service for testing.
        
        Returns:
            Mock IFileTrackingService
        """
        pass
