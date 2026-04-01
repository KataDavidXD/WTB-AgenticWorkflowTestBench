"""
File Processing Entities.

Domain entities for file version control following DDD principles.
Implements the Memento Pattern for file state snapshots.

Design Principles:
- Aggregate Root: FileCommit controls FileMemento lifecycle
- Rich Domain Model: Business logic in entities, not services
- Immutable Memento: Thread-safe file snapshots
- ACID: Transaction-safe through invariant enforcement

Entities:
- FileMemento: Immutable file snapshot (Memento Pattern)
- FileCommit: Aggregate root for file commits
- CommitStatus: Enum for commit lifecycle
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any

from .value_objects import BlobId, CommitId
from .exceptions import CommitAlreadyFinalized, DuplicateFileError


class CommitStatus(Enum):
    """
    Status of a file commit.
    
    Lifecycle:
        PENDING -> FINALIZED -> VERIFIED
    """
    PENDING = "pending"       # Being constructed
    FINALIZED = "finalized"   # Saved to repository
    VERIFIED = "verified"     # Verified by outbox processor


@dataclass(frozen=True)
class FileMemento:
    """
    Immutable snapshot of a file's state (Memento Pattern).
    
    Captures file metadata without storing content. Content is stored
    separately in blob storage, referenced by file_hash.
    
    Thread-safe due to immutability (frozen dataclass).
    
    Attributes:
        file_path: Original file path (relative or absolute)
        file_hash: Content-addressable blob reference
        file_size: File size in bytes
        
    Design Decisions:
    - Immutable: Thread-safe, value object semantics
    - No content: Content stored in BlobRepository
    - Hash reference: Links to content-addressable storage
    
    Examples:
        >>> memento, content = FileMemento.from_file("data.csv")
        >>> blob_repo.save(content)  # Caller saves content
        
        >>> memento = FileMemento.from_path_and_hash("data.csv", blob_id, 1024)
    """
    file_path: str
    file_hash: BlobId
    file_size: int
    
    def __post_init__(self):
        """Validate memento fields."""
        if not self.file_path:
            raise ValueError("file_path cannot be empty")
        if self.file_size < 0:
            raise ValueError(f"file_size cannot be negative: {self.file_size}")
    
    @classmethod
    def from_path_and_hash(
        cls,
        file_path: str,
        file_hash: BlobId,
        file_size: int,
    ) -> "FileMemento":
        """
        Create memento from pre-computed values.
        
        Use when blob has already been saved to storage.
        
        Args:
            file_path: File path
            file_hash: Pre-computed blob ID
            file_size: File size in bytes
            
        Returns:
            New FileMemento instance
        """
        return cls(
            file_path=file_path,
            file_hash=file_hash,
            file_size=file_size,
        )
    
    @classmethod
    def from_file(cls, file_path: str) -> tuple["FileMemento", bytes]:
        """
        Create memento from file, returning content for blob storage.
        
        This method reads the file and computes the hash, but does NOT
        save to blob storage. Caller is responsible for saving content.
        
        Args:
            file_path: Path to file to snapshot
            
        Returns:
            Tuple of (FileMemento, file_content_bytes)
            
        Raises:
            FileNotFoundError: If file does not exist
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        
        content = path.read_bytes()
        blob_id = BlobId.from_content(content)
        
        return cls(
            file_path=str(file_path),
            file_hash=blob_id,
            file_size=len(content),
        ), content
    
    @property
    def filename(self) -> str:
        """Extract filename from path."""
        return Path(self.file_path).name
    
    @property
    def extension(self) -> str:
        """Extract file extension."""
        return Path(self.file_path).suffix
    
    @property
    def size_kb(self) -> float:
        """File size in kilobytes."""
        return self.file_size / 1024
    
    @property
    def size_mb(self) -> float:
        """File size in megabytes."""
        return self.file_size / (1024 * 1024)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "file_path": self.file_path,
            "file_hash": self.file_hash.value,
            "file_size": self.file_size,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FileMemento":
        """Deserialize from dictionary."""
        return cls(
            file_path=data["file_path"],
            file_hash=BlobId(data["file_hash"]),
            file_size=data["file_size"],
        )
    
    def __repr__(self) -> str:
        return f"FileMemento(path={self.filename}, hash={self.file_hash.short}..., size={self.file_size})"


@dataclass
class FileCommit:
    """
    Aggregate root for file version commits.
    
    Represents a point-in-time snapshot of one or more files.
    Similar to Git commits but focused on file content tracking.
    
    Design:
    - Aggregate root: Controls access to FileMemento children
    - Rich domain model: Business logic in entity
    - Status tracking: Lifecycle management
    
    Invariants:
    - commit_id is immutable after creation
    - mementos list is append-only (no remove/modify)
    - Once finalized, commit cannot be modified
    - No duplicate file paths within a commit
    
    Usage:
        >>> commit = FileCommit.create(message="Snapshot before processing")
        >>> commit.add_memento(memento1)
        >>> commit.add_memento(memento2)
        >>> commit.finalize()  # Mark as ready for persistence
    """
    commit_id: CommitId
    message: Optional[str]
    timestamp: datetime
    status: CommitStatus = CommitStatus.PENDING
    _mementos: List[FileMemento] = field(default_factory=list)
    
    # Metadata for audit
    created_by: Optional[str] = None
    execution_id: Optional[str] = None
    checkpoint_id: Optional[str] = None
    
    @classmethod
    def create(
        cls,
        message: Optional[str] = None,
        commit_id: Optional[CommitId] = None,
        created_by: Optional[str] = None,
        execution_id: Optional[str] = None,
    ) -> "FileCommit":
        """
        Factory method to create a new commit.
        
        Args:
            message: Optional commit message for audit
            commit_id: Optional specific ID (generates new if not provided)
            created_by: Optional creator identifier
            execution_id: Optional WTB execution ID for correlation
            
        Returns:
            New FileCommit instance
        """
        return cls(
            commit_id=commit_id or CommitId.generate(),
            message=message,
            timestamp=datetime.now(),
            status=CommitStatus.PENDING,
            _mementos=[],
            created_by=created_by,
            execution_id=execution_id,
        )
    
    @classmethod
    def reconstitute(
        cls,
        commit_id: str,
        message: Optional[str],
        timestamp: datetime,
        status: str,
        mementos: List[FileMemento],
        created_by: Optional[str] = None,
        execution_id: Optional[str] = None,
        checkpoint_id: Optional[str] = None,
    ) -> "FileCommit":
        """
        Reconstitute commit from persistence.
        
        Used by repository to rebuild entity from stored data.
        Bypasses validation since data is already validated.
        
        Args:
            commit_id: Stored commit ID string
            message: Stored message
            timestamp: Stored timestamp
            status: Stored status string
            mementos: List of FileMemento instances
            created_by: Stored creator
            execution_id: Stored execution correlation ID
            checkpoint_id: Stored checkpoint link
            
        Returns:
            Reconstituted FileCommit
        """
        commit = object.__new__(cls)
        commit.commit_id = CommitId(commit_id)
        commit.message = message
        commit.timestamp = timestamp
        commit.status = CommitStatus(status)
        commit._mementos = list(mementos)
        commit.created_by = created_by
        commit.execution_id = execution_id
        commit.checkpoint_id = checkpoint_id
        return commit
    
    @property
    def mementos(self) -> List[FileMemento]:
        """
        Get copy of mementos list.
        
        Returns a copy to prevent external modification.
        
        Returns:
            List of FileMemento instances
        """
        return list(self._mementos)
    
    @property
    def file_count(self) -> int:
        """Number of files in this commit."""
        return len(self._mementos)
    
    @property
    def total_size(self) -> int:
        """Total size of all files in bytes."""
        return sum(m.file_size for m in self._mementos)
    
    @property
    def total_size_mb(self) -> float:
        """Total size in megabytes."""
        return self.total_size / (1024 * 1024)
    
    @property
    def file_paths(self) -> List[str]:
        """List of all file paths in this commit."""
        return [m.file_path for m in self._mementos]
    
    @property
    def file_hashes(self) -> Dict[str, str]:
        """Dictionary mapping file paths to hashes."""
        return {m.file_path: m.file_hash.value for m in self._mementos}
    
    @property
    def is_pending(self) -> bool:
        """Check if commit is still being constructed."""
        return self.status == CommitStatus.PENDING
    
    @property
    def is_finalized(self) -> bool:
        """Check if commit has been finalized."""
        return self.status in (CommitStatus.FINALIZED, CommitStatus.VERIFIED)
    
    def add_memento(self, memento: FileMemento) -> None:
        """
        Add a file memento to this commit.
        
        Domain method enforcing invariants:
        - No duplicates (same file path)
        - Cannot add to finalized commit
        
        Args:
            memento: FileMemento to add
            
        Raises:
            CommitAlreadyFinalized: If commit is already finalized
            DuplicateFileError: If file path already exists in commit
        """
        if self.is_finalized:
            raise CommitAlreadyFinalized(
                f"Cannot add memento to finalized commit {self.commit_id}"
            )
        
        if memento.file_path in self.file_paths:
            raise DuplicateFileError(
                f"File already tracked in this commit: {memento.file_path}"
            )
        
        self._mementos.append(memento)
    
    def finalize(self) -> None:
        """
        Mark commit as finalized (ready for persistence).
        
        After finalization, no more mementos can be added.
        
        Raises:
            CommitAlreadyFinalized: If already finalized
            ValueError: If commit has no mementos
        """
        if self.is_finalized:
            raise CommitAlreadyFinalized(f"Commit {self.commit_id} already finalized")
        
        if not self._mementos:
            raise ValueError("Cannot finalize commit with no mementos")
        
        self.status = CommitStatus.FINALIZED
    
    def mark_verified(self) -> None:
        """
        Mark commit as verified by outbox processor.
        
        Called after cross-database verification succeeds.
        """
        if self.status != CommitStatus.FINALIZED:
            raise ValueError(f"Can only verify finalized commits, got {self.status}")
        self.status = CommitStatus.VERIFIED
    
    def get_memento(self, file_path: str) -> Optional[FileMemento]:
        """
        Get memento for specific file path.
        
        Args:
            file_path: File path to look up
            
        Returns:
            FileMemento if found, None otherwise
        """
        for memento in self._mementos:
            if memento.file_path == file_path:
                return memento
        return None
    
    def link_to_checkpoint(self, checkpoint_id: str) -> None:
        """
        Link this commit to a WTB checkpoint.
        
        Args:
            checkpoint_id: WTB checkpoint ID
        """
        self.checkpoint_id = checkpoint_id
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "commit_id": self.commit_id.value,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "status": self.status.value,
            "file_count": self.file_count,
            "total_size": self.total_size,
            "mementos": [m.to_dict() for m in self._mementos],
            "created_by": self.created_by,
            "execution_id": self.execution_id,
            "checkpoint_id": self.checkpoint_id,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FileCommit":
        """Deserialize from dictionary."""
        mementos = [FileMemento.from_dict(m) for m in data.get("mementos", [])]
        return cls.reconstitute(
            commit_id=data["commit_id"],
            message=data.get("message"),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            status=data.get("status", "pending"),
            mementos=mementos,
            created_by=data.get("created_by"),
            execution_id=data.get("execution_id"),
            checkpoint_id=data.get("checkpoint_id"),
        )
    
    def __repr__(self) -> str:
        return (
            f"FileCommit(id={self.commit_id.short}..., "
            f"files={self.file_count}, "
            f"size={self.total_size_mb:.2f}MB, "
            f"status={self.status.value})"
        )


__all__ = [
    "CommitStatus",
    "FileMemento",
    "FileCommit",
]
