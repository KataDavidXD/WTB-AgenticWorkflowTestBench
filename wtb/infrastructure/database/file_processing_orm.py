"""
File Processing ORM Models.

SQLAlchemy ORM models for file processing persistence.
Maps domain models to database tables with proper relationships.

Tables:
- file_commits: Commit metadata
- file_mementos: File snapshots within commits
- file_blobs: Blob content metadata
- checkpoint_file_links: WTB checkpoint to commit links

Design Principles:
- ORM models are infrastructure concerns
- Domain models are reconstructed from ORM data
- Relationships defined for efficient loading
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

# Import Base from the sibling models.py file
from wtb.infrastructure.database.models import Base

# ═══════════════════════════════════════════════════════════════════════════════
# File Blob ORM
# ═══════════════════════════════════════════════════════════════════════════════


class FileBlobORM(Base):
    """
    ORM model for blob content metadata.
    
    Content-addressable storage: Primary key is SHA-256 hash.
    Actual content stored on filesystem at storage_location.
    
    Attributes:
        content_hash: SHA-256 hash (primary key)
        storage_location: Filesystem path to content
        size: Content size in bytes
        created_at: Creation timestamp
        reference_count: Number of mementos referencing this blob
    """
    __tablename__ = "file_blobs"
    
    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    storage_location: Mapped[str] = mapped_column(String(512), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    reference_count: Mapped[int] = mapped_column(Integer, default=1)
    
    # Indexes for common queries
    __table_args__ = (
        Index("ix_file_blobs_created_at", "created_at"),
    )
    
    def __repr__(self) -> str:
        return f"<FileBlobORM hash={self.content_hash[:8]}... size={self.size}>"


# ═══════════════════════════════════════════════════════════════════════════════
# File Commit ORM
# ═══════════════════════════════════════════════════════════════════════════════


class FileCommitORM(Base):
    """
    ORM model for file commits.
    
    Aggregate root: Has many FileMementoORM children.
    
    Attributes:
        commit_id: UUID primary key
        message: Optional commit message
        timestamp: Creation timestamp
        status: pending, finalized, verified
        created_by: Creator identifier
        execution_id: WTB execution correlation
        checkpoint_id: WTB checkpoint link
    """
    __tablename__ = "file_commits"
    
    commit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checkpoint_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    
    # Relationship to mementos (cascade delete)
    mementos: Mapped[list["FileMementoORM"]] = relationship(
        "FileMementoORM",
        back_populates="commit",
        cascade="all, delete-orphan",
        lazy="select",
    )
    
    # Indexes
    __table_args__ = (
        Index("ix_file_commits_timestamp", "timestamp"),
        Index("ix_file_commits_execution_id", "execution_id"),
        Index("ix_file_commits_checkpoint_id", "checkpoint_id"),
        Index("ix_file_commits_status", "status"),
    )
    
    @property
    def file_count(self) -> int:
        """Number of mementos in this commit."""
        return len(self.mementos) if self.mementos else 0
    
    @property
    def total_size(self) -> int:
        """Total size of all files."""
        return sum(m.file_size for m in self.mementos) if self.mementos else 0
    
    def __repr__(self) -> str:
        return f"<FileCommitORM id={self.commit_id[:8]}... files={self.file_count}>"


# ═══════════════════════════════════════════════════════════════════════════════
# File Memento ORM
# ═══════════════════════════════════════════════════════════════════════════════


class FileMementoORM(Base):
    """
    ORM model for file mementos (snapshots).
    
    Child of FileCommitORM. References blob by hash.
    
    Attributes:
        id: Auto-increment primary key
        file_path: Original file path
        file_hash: SHA-256 hash (references file_blobs)
        file_size: File size in bytes
        commit_id: Parent commit (foreign key)
    """
    __tablename__ = "file_mementos"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    commit_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("file_commits.commit_id", ondelete="CASCADE"),
        nullable=False,
    )
    
    # Relationship to parent commit
    commit: Mapped["FileCommitORM"] = relationship(
        "FileCommitORM",
        back_populates="mementos",
    )
    
    # Indexes and constraints
    __table_args__ = (
        Index("ix_file_mementos_commit_id", "commit_id"),
        Index("ix_file_mementos_file_hash", "file_hash"),
        UniqueConstraint("file_path", "commit_id", name="uq_file_mementos_path_commit"),
    )
    
    def __repr__(self) -> str:
        return f"<FileMementoORM path={self.file_path} hash={self.file_hash[:8]}...>"


# ═══════════════════════════════════════════════════════════════════════════════
# Checkpoint File Link ORM
# ═══════════════════════════════════════════════════════════════════════════════


class CheckpointFileLinkORM(Base):
    """
    ORM model for checkpoint-file links.
    
    Links WTB checkpoints to file commits for coordinated rollback.
    
    Attributes:
        checkpoint_id: WTB checkpoint ID (primary key)
        commit_id: FileTracker commit ID
        linked_at: Link creation timestamp
        file_count: Number of files in linked commit
        total_size_bytes: Total size of files
    """
    __tablename__ = "checkpoint_file_links"
    
    checkpoint_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    commit_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("file_commits.commit_id", ondelete="CASCADE"),
        nullable=False,
    )
    linked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    
    # Indexes
    __table_args__ = (
        Index("ix_checkpoint_file_links_commit_id", "commit_id"),
    )
    
    def __repr__(self) -> str:
        return f"<CheckpointFileLinkORM cp={self.checkpoint_id} commit={self.commit_id[:8]}...>"


# ═══════════════════════════════════════════════════════════════════════════════
# Export
# ═══════════════════════════════════════════════════════════════════════════════

__all__ = [
    "FileBlobORM",
    "FileCommitORM",
    "FileMementoORM",
    "CheckpointFileLinkORM",
]
