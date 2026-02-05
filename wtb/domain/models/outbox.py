"""
Outbox Pattern Domain Model.

Implements the Outbox Pattern for cross-database transaction consistency
between WTB, AgentGit, and FileTracker databases.

Design Philosophy:
- OutboxEvent is written in the same transaction as business data
- Background processor handles cross-database verification
- Guarantees eventual consistency across all three databases
"""

from dataclasses import dataclass, field
from typing import Dict, Any, Optional
from datetime import datetime
from enum import Enum
import uuid


class OutboxEventType(Enum):
    """Outbox event types for cross-database operations."""
    
    # AgentGit related
    CHECKPOINT_CREATE = "checkpoint_create"
    CHECKPOINT_VERIFY = "checkpoint_verify"
    CHECKPOINT_SAVED = "checkpoint_saved"  # v2.0 async
    NODE_BOUNDARY_SYNC = "node_boundary_sync"
    
    # FileTracker related - Basic
    FILE_COMMIT_LINK = "file_commit_link"
    FILE_COMMIT_VERIFY = "file_commit_verify"
    FILE_BLOB_VERIFY = "file_blob_verify"
    FILE_TRACKED = "file_tracked"  # v2.0 async
    
    # FileTracker related - Batch operations (2026-01-15)
    FILE_BATCH_VERIFY = "file_batch_verify"
    FILE_INTEGRITY_CHECK = "file_integrity_check"
    FILE_RESTORE_VERIFY = "file_restore_verify"
    
    # Cross-database joint verification
    CHECKPOINT_FILE_LINK_VERIFY = "checkpoint_file_link_verify"
    
    # Rollback/Recovery operations
    ROLLBACK_FILE_RESTORE = "rollback_file_restore"
    ROLLBACK_VERIFY = "rollback_verify"
    ROLLBACK_PERFORMED = "rollback_performed"  # v2.0 API
    
    # Ray batch test events (2026-01-15)
    RAY_EVENT = "ray_event"
    
    # API/SDK events (2026-01-28)
    EXECUTION_PAUSED = "execution_paused"
    EXECUTION_RESUMED = "execution_resumed"
    EXECUTION_STOPPED = "execution_stopped"
    STATE_MODIFIED = "state_modified"
    WORKFLOW_CREATED = "workflow_created"
    BATCH_TEST_CREATED = "batch_test_created"
    BATCH_TEST_CANCELLED = "batch_test_cancelled"
    
    # Batch coordination events (v1.8 - 2026-02-05)
    EXECUTION_FORKED = "execution_forked"


class OutboxStatus(Enum):
    """Outbox event processing status."""
    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


@dataclass
class OutboxEvent:
    """
    Outbox Event Entity.
    
    Used to record operations that need cross-database synchronization,
    ensuring eventual consistency.
    
    Design Philosophy:
    - Written in the same transaction as business data
    - Guarantees idempotency via event_id AND idempotency_key
    - Supports retry and error tracking
    
    Attributes:
        id: Database primary key (auto-generated)
        event_id: UUID for event identity
        event_type: Type of cross-database operation
        aggregate_type: Entity type (e.g., 'Execution', 'NodeBoundary')
        aggregate_id: Entity ID
        payload: JSON data for the operation
        idempotency_key: Optional client-provided key for duplicate detection
        status: Current processing status
        retry_count: Number of processing attempts
        max_retries: Maximum retry attempts
        created_at: When the event was created
        processed_at: When the event was successfully processed
        last_error: Last error message if failed
        
    Idempotency (ISSUE-OB-002):
        The idempotency_key enables clients to safely retry requests without
        creating duplicate events. If provided, the system will reject events
        with duplicate idempotency keys within the deduplication window.
    """
    
    id: Optional[int] = None
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_type: OutboxEventType = OutboxEventType.CHECKPOINT_CREATE
    aggregate_type: str = ""
    aggregate_id: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    
    # Idempotency key for duplicate detection (ISSUE-OB-002)
    idempotency_key: Optional[str] = None
    
    status: OutboxStatus = OutboxStatus.PENDING
    retry_count: int = 0
    max_retries: int = 5
    
    created_at: datetime = field(default_factory=datetime.now)
    processed_at: Optional[datetime] = None
    published_at: Optional[datetime] = None  # For event publishing use case
    last_error: Optional[str] = None
    
    def can_retry(self) -> bool:
        """Check if the event can be retried."""
        return (
            self.status in [OutboxStatus.PENDING, OutboxStatus.FAILED]
            and self.retry_count < self.max_retries
        )
    
    def mark_processing(self) -> None:
        """Mark as currently being processed."""
        self.status = OutboxStatus.PROCESSING
    
    def mark_processed(self) -> None:
        """Mark as successfully processed."""
        self.status = OutboxStatus.PROCESSED
        self.processed_at = datetime.now()
    
    def mark_published(self) -> None:
        """Mark as successfully published (for event publishing)."""
        self.status = OutboxStatus.PROCESSED
        self.published_at = datetime.now()
        self.processed_at = datetime.now()
    
    def mark_failed(self, error: str) -> None:
        """Mark as failed with error message."""
        self.status = OutboxStatus.FAILED
        self.retry_count += 1
        self.last_error = error
    
    def reset_for_retry(self) -> None:
        """Reset event for manual retry."""
        self.status = OutboxStatus.PENDING
        self.retry_count = 0
        self.last_error = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "payload": self.payload,
            "idempotency_key": self.idempotency_key,
            "status": self.status.value,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "processed_at": self.processed_at.isoformat() if self.processed_at else None,
            "last_error": self.last_error,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OutboxEvent":
        """Create from dictionary."""
        return cls(
            id=data.get("id"),
            event_id=data.get("event_id", str(uuid.uuid4())),
            event_type=OutboxEventType(data.get("event_type", "checkpoint_create")),
            aggregate_type=data.get("aggregate_type", ""),
            aggregate_id=data.get("aggregate_id", ""),
            payload=data.get("payload", {}),
            idempotency_key=data.get("idempotency_key"),
            status=OutboxStatus(data.get("status", "pending")),
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 5),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else datetime.now(),
            processed_at=datetime.fromisoformat(data["processed_at"]) if data.get("processed_at") else None,
            last_error=data.get("last_error"),
        )
    
    @classmethod
    def create(
        cls,
        event_type: OutboxEventType,
        aggregate_id: str,
        payload: Dict[str, Any],
        aggregate_type: str = "",
        idempotency_key: Optional[str] = None,
    ) -> "OutboxEvent":
        """
        Generic factory method for creating outbox events.
        
        Args:
            event_type: Type of event
            aggregate_id: ID of the aggregate (entity) this event relates to
            payload: Event payload data
            aggregate_type: Optional aggregate type name
            idempotency_key: Optional client-provided key for duplicate detection
            
        Returns:
            New OutboxEvent instance
            
        ISSUE-OB-002: Now supports idempotency keys for safe retries.
        """
        return cls(
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload=payload,
            idempotency_key=idempotency_key,
        )
    
    @classmethod
    def create_checkpoint_verify(
        cls,
        execution_id: str,
        checkpoint_id: int,
        node_id: str,
        internal_session_id: int,
        is_entry: bool = False,
        is_exit: bool = False
    ) -> "OutboxEvent":
        """Factory method for checkpoint verification events."""
        return cls(
            event_type=OutboxEventType.CHECKPOINT_VERIFY,
            aggregate_type="Execution",
            aggregate_id=execution_id,
            payload={
                "checkpoint_id": checkpoint_id,
                "node_id": node_id,
                "internal_session_id": internal_session_id,
                "is_entry": is_entry,
                "is_exit": is_exit,
            }
        )
    
    @classmethod
    def create_file_commit_verify(
        cls,
        execution_id: str,
        checkpoint_id: int,
        file_commit_id: str,
        node_id: str
    ) -> "OutboxEvent":
        """Factory method for file commit verification events."""
        return cls(
            event_type=OutboxEventType.FILE_COMMIT_VERIFY,
            aggregate_type="Execution",
            aggregate_id=execution_id,
            payload={
                "checkpoint_id": checkpoint_id,
                "file_commit_id": file_commit_id,
                "node_id": node_id,
            }
        )
    
    @classmethod
    def create_checkpoint_file_link_verify(
        cls,
        execution_id: str,
        checkpoint_id: int,
        file_commit_id: str
    ) -> "OutboxEvent":
        """Factory method for checkpoint-file link verification events."""
        return cls(
            event_type=OutboxEventType.CHECKPOINT_FILE_LINK_VERIFY,
            aggregate_type="CheckpointFile",
            aggregate_id=f"{checkpoint_id}_{file_commit_id}",
            payload={
                "execution_id": execution_id,
                "checkpoint_id": checkpoint_id,
                "file_commit_id": file_commit_id,
            }
        )
    
    @classmethod
    def create_file_batch_verify(
        cls,
        execution_id: str,
        commit_ids: list,
        expected_total_files: int,
        verify_blobs: bool = True,
    ) -> "OutboxEvent":
        """Factory method for batch file verification events."""
        return cls(
            event_type=OutboxEventType.FILE_BATCH_VERIFY,
            aggregate_type="BatchTest",
            aggregate_id=execution_id,
            payload={
                "commit_ids": commit_ids,
                "expected_total_files": expected_total_files,
                "verify_blobs": verify_blobs,
            }
        )
    
    @classmethod
    def create_file_integrity_check(
        cls,
        commit_id: str,
        file_hashes: dict,
        verify_content: bool = False,
    ) -> "OutboxEvent":
        """Factory method for file integrity verification events."""
        return cls(
            event_type=OutboxEventType.FILE_INTEGRITY_CHECK,
            aggregate_type="FileCommit",
            aggregate_id=commit_id,
            payload={
                "file_hashes": file_hashes,
                "verify_content": verify_content,
            }
        )
    
    @classmethod
    def create_file_restore_verify(
        cls,
        execution_id: str,
        checkpoint_id: int,
        commit_id: str,
        restored_paths: list,
    ) -> "OutboxEvent":
        """Factory method for file restore verification events."""
        return cls(
            event_type=OutboxEventType.FILE_RESTORE_VERIFY,
            aggregate_type="Execution",
            aggregate_id=execution_id,
            payload={
                "checkpoint_id": checkpoint_id,
                "commit_id": commit_id,
                "restored_paths": restored_paths,
            }
        )
    
    @classmethod
    def create_rollback_file_restore(
        cls,
        execution_id: str,
        source_checkpoint_id: int,
        target_checkpoint_id: int,
        source_commit_id: str,
    ) -> "OutboxEvent":
        """Factory method for rollback file restore events."""
        return cls(
            event_type=OutboxEventType.ROLLBACK_FILE_RESTORE,
            aggregate_type="Execution",
            aggregate_id=execution_id,
            payload={
                "source_checkpoint_id": source_checkpoint_id,
                "target_checkpoint_id": target_checkpoint_id,
                "source_commit_id": source_commit_id,
            }
        )
    
    @classmethod
    def create_rollback_verify(
        cls,
        execution_id: str,
        checkpoint_id: int,
        restored_files_count: int,
        state_verified: bool = False,
    ) -> "OutboxEvent":
        """Factory method for rollback verification events."""
        return cls(
            event_type=OutboxEventType.ROLLBACK_VERIFY,
            aggregate_type="Execution",
            aggregate_id=execution_id,
            payload={
                "checkpoint_id": checkpoint_id,
                "restored_files_count": restored_files_count,
                "state_verified": state_verified,
            }
        )

