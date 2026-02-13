"""
File Processing Domain Events.

Events emitted during file tracking operations for audit trail and
cross-system consistency notifications.

Design Principles:
- Event Sourcing ready: Events capture complete state change
- Immutable: Frozen dataclasses for thread-safety
- Serializable: to_dict/from_dict for persistence and messaging

Integration:
- Published via WTBEventBus after transaction commits
- Consumed by WTBAuditTrail for audit logging
- Can trigger OutboxProcessor verification

Usage:
    from wtb.domain.events.file_processing_events import (
        FileCommitCreatedEvent,
        FileRestoredEvent,
    )
    from wtb.infrastructure.events import get_wtb_event_bus
    
    # After transaction commits
    event_bus = get_wtb_event_bus()
    event_bus.publish(FileCommitCreatedEvent(
        commit_id="...",
        file_count=3,
        total_size_bytes=1024000,
    ))
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any

from wtb.domain.events.execution_events import WTBEvent


# ═══════════════════════════════════════════════════════════════════════════════
# File Commit Events
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class FileCommitCreatedEvent(WTBEvent):
    """
    Emitted when a file commit is created and persisted.
    
    This event is published AFTER the transaction commits to ensure
    the commit is actually persisted before notifications go out.
    
    Attributes:
        commit_id: Unique commit identifier (UUID)
        file_count: Number of files in the commit
        total_size_bytes: Total size of all files
        message: Optional commit message
        file_paths: List of tracked file paths
        execution_id: Optional WTB execution correlation ID
    """
    commit_id: str = ""
    file_count: int = 0
    total_size_bytes: int = 0
    message: Optional[str] = None
    file_paths: tuple = field(default_factory=tuple)
    execution_id: Optional[str] = None
    
    @property
    def event_type(self) -> str:
        return "file_commit.created"
    
    @property
    def total_size_mb(self) -> float:
        return self.total_size_bytes / (1024 * 1024)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "commit_id": self.commit_id,
            "file_count": self.file_count,
            "total_size_bytes": self.total_size_bytes,
            "message": self.message,
            "file_paths": list(self.file_paths),
            "execution_id": self.execution_id,
        }


@dataclass
class FileCommitDeletedEvent(WTBEvent):
    """
    Emitted when a file commit is deleted.
    
    Note: Blobs are NOT deleted with commits (may be shared).
    
    Attributes:
        commit_id: Deleted commit identifier
        file_count: Number of mementos that were deleted
    """
    commit_id: str = ""
    file_count: int = 0
    
    @property
    def event_type(self) -> str:
        return "file_commit.deleted"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "commit_id": self.commit_id,
            "file_count": self.file_count,
        }


@dataclass
class FileCommitVerifiedEvent(WTBEvent):
    """
    Emitted when a file commit is verified by outbox processor.
    
    Indicates cross-database consistency has been confirmed.
    
    Attributes:
        commit_id: Verified commit identifier
        blobs_verified: Number of blobs verified
    """
    commit_id: str = ""
    blobs_verified: int = 0
    
    @property
    def event_type(self) -> str:
        return "file_commit.verified"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "commit_id": self.commit_id,
            "blobs_verified": self.blobs_verified,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# File Restore Events
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class FileRestoredEvent(WTBEvent):
    """
    Emitted when files are restored from a commit.
    
    Indicates file system has been reverted to a previous state.
    
    Attributes:
        commit_id: Source commit identifier
        files_restored: Number of files restored
        total_size_bytes: Total size of restored files
        restored_paths: Paths of restored files
        checkpoint_id: WTB checkpoint if restored via checkpoint
    """
    commit_id: str = ""
    files_restored: int = 0
    total_size_bytes: int = 0
    restored_paths: tuple = field(default_factory=tuple)
    checkpoint_id: Optional[int] = None
    
    @property
    def event_type(self) -> str:
        return "file.restored"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "commit_id": self.commit_id,
            "files_restored": self.files_restored,
            "total_size_bytes": self.total_size_bytes,
            "restored_paths": list(self.restored_paths),
            "checkpoint_id": self.checkpoint_id,
        }


@dataclass
class FileRestoreFailedEvent(WTBEvent):
    """
    Emitted when file restoration fails.
    
    Attributes:
        commit_id: Attempted source commit
        error_message: Reason for failure
        partial_restored: Files that were restored before failure
    """
    commit_id: str = ""
    error_message: str = ""
    partial_restored: tuple = field(default_factory=tuple)
    
    @property
    def event_type(self) -> str:
        return "file.restore_failed"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "commit_id": self.commit_id,
            "error_message": self.error_message,
            "partial_restored": list(self.partial_restored),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# File Cleanup Events (v1.9)
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class FileCleanupCompletedEvent(WTBEvent):
    """
    Emitted when orphaned files are cleaned up after rollback (v1.9).
    
    This event is published after OutboxProcessor identifies and removes
    files that were created after the target checkpoint during rollback.
    
    Attributes:
        execution_id: Execution that was rolled back
        checkpoint_id: Target checkpoint rolled back to
        files_deleted: Number of files deleted
        files_backed_up: Number of files backed up before deletion
        files_skipped: Number of files skipped (errors or limits)
        deleted_paths: Paths of deleted files
        backed_up_paths: Paths of backed up files
        backup_dir: Directory where backups were stored
        dry_run: Whether this was a dry run (no actual deletion)
        errors: List of error messages (if any)
    """
    execution_id: str = ""
    checkpoint_id: int = 0
    files_deleted: int = 0
    files_backed_up: int = 0
    files_skipped: int = 0
    deleted_paths: tuple = field(default_factory=tuple)
    backed_up_paths: tuple = field(default_factory=tuple)
    backup_dir: Optional[str] = None
    dry_run: bool = False
    errors: tuple = field(default_factory=tuple)
    
    @property
    def event_type(self) -> str:
        return "file_cleanup.completed"
    
    @property
    def total_processed(self) -> int:
        return self.files_deleted + self.files_backed_up + self.files_skipped
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "execution_id": self.execution_id,
            "checkpoint_id": self.checkpoint_id,
            "files_deleted": self.files_deleted,
            "files_backed_up": self.files_backed_up,
            "files_skipped": self.files_skipped,
            "deleted_paths": list(self.deleted_paths),
            "backed_up_paths": list(self.backed_up_paths),
            "backup_dir": self.backup_dir,
            "dry_run": self.dry_run,
            "errors": list(self.errors),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Checkpoint-File Link Events
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class CheckpointFileLinkCreatedEvent(WTBEvent):
    """
    Emitted when checkpoint-file link is created.
    
    Links WTB checkpoint to file commit for coordinated rollback.
    
    Attributes:
        checkpoint_id: WTB checkpoint ID
        commit_id: FileTracker commit ID
        file_count: Number of files in linked commit
        total_size_bytes: Total size of files
    """
    checkpoint_id: int = 0
    commit_id: str = ""
    file_count: int = 0
    total_size_bytes: int = 0
    
    @property
    def event_type(self) -> str:
        return "checkpoint_file_link.created"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "checkpoint_id": self.checkpoint_id,
            "commit_id": self.commit_id,
            "file_count": self.file_count,
            "total_size_bytes": self.total_size_bytes,
        }


@dataclass
class CheckpointFileLinkVerifiedEvent(WTBEvent):
    """
    Emitted when checkpoint-file link is verified.
    
    Confirms cross-database consistency:
    - WTB checkpoint exists
    - FileTracker commit exists
    - All blobs exist
    
    Attributes:
        checkpoint_id: Verified checkpoint ID
        commit_id: Verified commit ID
        blobs_verified: Number of blobs verified
    """
    checkpoint_id: int = 0
    commit_id: str = ""
    blobs_verified: int = 0
    
    @property
    def event_type(self) -> str:
        return "checkpoint_file_link.verified"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "checkpoint_id": self.checkpoint_id,
            "commit_id": self.commit_id,
            "blobs_verified": self.blobs_verified,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Blob Events
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class BlobCreatedEvent(WTBEvent):
    """
    Emitted when a new blob is stored.
    
    Note: Not emitted for duplicate content (same hash = no new blob).
    
    Attributes:
        blob_id: Content hash (SHA-256)
        size_bytes: Blob size
        is_duplicate: False for new blobs
    """
    blob_id: str = ""
    size_bytes: int = 0
    is_duplicate: bool = False
    
    @property
    def event_type(self) -> str:
        return "blob.created"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "blob_id": self.blob_id,
            "size_bytes": self.size_bytes,
            "is_duplicate": self.is_duplicate,
        }


@dataclass
class BlobDeletedEvent(WTBEvent):
    """
    Emitted when a blob is deleted.
    
    Attributes:
        blob_id: Deleted blob hash
        size_bytes: Freed storage size
    """
    blob_id: str = ""
    size_bytes: int = 0
    
    @property
    def event_type(self) -> str:
        return "blob.deleted"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "blob_id": self.blob_id,
            "size_bytes": self.size_bytes,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# File Tracking Service Events
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class FileTrackingStartedEvent(WTBEvent):
    """
    Emitted when file tracking operation starts.
    
    Useful for progress tracking and timeout monitoring.
    
    Attributes:
        operation_id: Unique operation identifier
        file_paths: Files to be tracked
        checkpoint_id: Target checkpoint (if track_and_link)
    """
    operation_id: str = ""
    file_paths: tuple = field(default_factory=tuple)
    checkpoint_id: Optional[int] = None
    
    @property
    def event_type(self) -> str:
        return "file_tracking.started"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "operation_id": self.operation_id,
            "file_paths": list(self.file_paths),
            "checkpoint_id": self.checkpoint_id,
        }


@dataclass
class FileTrackingCompletedEvent(WTBEvent):
    """
    Emitted when file tracking operation completes.
    
    Attributes:
        operation_id: Operation identifier
        commit_id: Created commit ID
        duration_ms: Operation duration in milliseconds
    """
    operation_id: str = ""
    commit_id: str = ""
    duration_ms: int = 0
    
    @property
    def event_type(self) -> str:
        return "file_tracking.completed"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "operation_id": self.operation_id,
            "commit_id": self.commit_id,
            "duration_ms": self.duration_ms,
        }


@dataclass
class FileTrackingFailedEvent(WTBEvent):
    """
    Emitted when file tracking operation fails.
    
    Attributes:
        operation_id: Operation identifier
        error_message: Failure reason
        file_path: File that caused failure (if applicable)
    """
    operation_id: str = ""
    error_message: str = ""
    file_path: Optional[str] = None
    
    @property
    def event_type(self) -> str:
        return "file_tracking.failed"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "operation_id": self.operation_id,
            "error_message": self.error_message,
            "file_path": self.file_path,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Export
# ═══════════════════════════════════════════════════════════════════════════════

__all__ = [
    # Commit events
    "FileCommitCreatedEvent",
    "FileCommitDeletedEvent",
    "FileCommitVerifiedEvent",
    # Restore events
    "FileRestoredEvent",
    "FileRestoreFailedEvent",
    # Cleanup events (v1.9)
    "FileCleanupCompletedEvent",
    # Link events
    "CheckpointFileLinkCreatedEvent",
    "CheckpointFileLinkVerifiedEvent",
    # Blob events
    "BlobCreatedEvent",
    "BlobDeletedEvent",
    # Tracking operation events
    "FileTrackingStartedEvent",
    "FileTrackingCompletedEvent",
    "FileTrackingFailedEvent",
]
