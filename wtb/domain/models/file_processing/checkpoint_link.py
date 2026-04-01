"""
Checkpoint File Link - Association Entity.

Links WTB checkpoints to FileTracker commits for coordinated rollback.
This is the PRIMARY checkpoint-file linking mechanism.

Design Principles:
- Single Source of Truth: Only use this for checkpoint-file links
- Rich Value Objects: Uses CommitId instead of primitive string
- Cross-Database Consistency: Verified by outbox processor

Note:
- DEPRECATED: wtb/domain/models/checkpoint_file.py (use this instead)
- FileTrackingLink in interfaces is for ACL boundary (primitives)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Any, TYPE_CHECKING

from .value_objects import CommitId

if TYPE_CHECKING:
    from .entities import FileCommit


@dataclass
class CheckpointFileLink:
    """
    Link between WTB checkpoint and file commit.
    
    Association entity for cross-database consistency.
    Links AgentGit/LangGraph checkpoints to FileTracker commits.
    
    Attributes:
        checkpoint_id: WTB checkpoint ID
        commit_id: FileTracker commit ID (rich value object)
        linked_at: Timestamp of link creation
        file_count: Number of files in linked commit
        total_size_bytes: Total size of files
        
    Design:
    - Stored in WTB database (checkpoint_file_links table)
    - Verified by outbox processor for consistency
    - Uses CommitId value object for type safety
    
    Usage:
        >>> link = CheckpointFileLink.create(checkpoint_id=42, commit=file_commit)
        >>> repo.add(link)
        
        >>> link = repo.get_by_checkpoint(42)
        >>> print(link.commit_id)
    """
    checkpoint_id: str
    commit_id: CommitId
    linked_at: datetime
    file_count: int
    total_size_bytes: int
    
    @classmethod
    def create(
        cls,
        checkpoint_id: str,
        commit: "FileCommit",
    ) -> "CheckpointFileLink":
        """
        Create link from checkpoint and commit.
        
        Factory method ensures proper initialization with
        denormalized summary data from commit.
        
        Args:
            checkpoint_id: WTB checkpoint ID (UUID string)
            commit: FileCommit to link
            
        Returns:
            New CheckpointFileLink
        """
        return cls(
            checkpoint_id=checkpoint_id,
            commit_id=commit.commit_id,
            linked_at=datetime.now(),
            file_count=commit.file_count,
            total_size_bytes=commit.total_size,
        )
    
    @classmethod
    def create_from_values(
        cls,
        checkpoint_id: str,
        commit_id: CommitId,
        file_count: int,
        total_size_bytes: int,
    ) -> "CheckpointFileLink":
        """
        Create link from individual values.
        
        Use when commit object is not available.
        
        Args:
            checkpoint_id: WTB checkpoint ID
            commit_id: File commit ID
            file_count: Number of files
            total_size_bytes: Total size
            
        Returns:
            New CheckpointFileLink
        """
        return cls(
            checkpoint_id=checkpoint_id,
            commit_id=commit_id,
            linked_at=datetime.now(),
            file_count=file_count,
            total_size_bytes=total_size_bytes,
        )
    
    @property
    def total_size_mb(self) -> float:
        """Total size in megabytes."""
        return self.total_size_bytes / (1024 * 1024)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "checkpoint_id": self.checkpoint_id,
            "commit_id": self.commit_id.value,
            "linked_at": self.linked_at.isoformat(),
            "file_count": self.file_count,
            "total_size_bytes": self.total_size_bytes,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CheckpointFileLink":
        """Deserialize from dictionary."""
        return cls(
            checkpoint_id=data["checkpoint_id"],
            commit_id=CommitId(data["commit_id"]),
            linked_at=datetime.fromisoformat(data["linked_at"]),
            file_count=data["file_count"],
            total_size_bytes=data["total_size_bytes"],
        )
    
    def __repr__(self) -> str:
        return (
            f"CheckpointFileLink(checkpoint={self.checkpoint_id}, "
            f"commit={self.commit_id.short}..., "
            f"files={self.file_count})"
        )


__all__ = [
    "CheckpointFileLink",
]
