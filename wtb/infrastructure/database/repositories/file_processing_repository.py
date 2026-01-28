"""
File Processing Repository Implementations.

SQLAlchemy-based implementations of file processing repository interfaces.
Provides ACID-compliant persistence for file commits, mementos, and blobs.

Design Principles:
- Repository Pattern: Encapsulates data access
- Dependency Inversion: Implements domain interfaces
- ACID: Uses SQLAlchemy session transactions
- DRY: Uses shared mappers from wtb.infrastructure.database.mappers

Updated: 2026-01-28 - Refactored to use BlobStorageCore for shared logic (ISSUE-FS-001)

Usage:
    from wtb.infrastructure.database.repositories.file_processing_repository import (
        SQLAlchemyFileCommitRepository,
        SQLAlchemyBlobRepository,
    )
    
    # Within UoW context
    with uow:
        repo = SQLAlchemyFileCommitRepository(uow.session)
        repo.save(commit)
        uow.commit()
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

from sqlalchemy.orm import Session
from sqlalchemy import select, func

from wtb.domain.models.file_processing import (
    FileCommit,
    FileMemento,
    BlobId,
    CommitId,
    CheckpointFileLink,
    CommitStatus,
)
from wtb.domain.interfaces.file_processing_repository import (
    IBlobRepository,
    IFileCommitRepository,
    ICheckpointFileLinkRepository,
)
from wtb.infrastructure.database.file_processing_orm import (
    FileBlobORM,
    FileCommitORM,
    FileMementoORM,
    CheckpointFileLinkORM,
)
from wtb.infrastructure.database.mappers import (
    BlobStorageCore,
    FileCommitMapper,
    CheckpointFileLinkMapper,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# SQLAlchemy Blob Repository
# ═══════════════════════════════════════════════════════════════════════════════


class SQLAlchemyBlobRepository(IBlobRepository):
    """
    SQLAlchemy implementation of IBlobRepository.
    
    Stores blob metadata in database and content in filesystem.
    Uses content-addressable storage (like Git).
    
    Thread Safety:
    - Session should be per-thread
    - File operations are atomic per blob
    
    ACID:
    - Database operations within session transaction
    - File writes are atomic (write to temp, rename)
    
    DRY:
    - Uses BlobStorageCore for shared logic with async repository
    """
    
    def __init__(self, session: Session, storage_path: str):
        """
        Initialize blob repository.
        
        Args:
            session: SQLAlchemy session
            storage_path: Base path for blob file storage
        """
        self._session = session
        self._storage_path, self._objects_path = BlobStorageCore.setup_storage_structure(
            Path(storage_path)
        )
    
    def save(self, content: bytes) -> BlobId:
        """
        Save content to blob storage.
        
        Idempotent: Returns same BlobId for same content.
        Updates reference count if blob already exists.
        
        Args:
            content: Binary content
            
        Returns:
            BlobId (SHA-256 hash)
        """
        blob_id = BlobId.from_content(content)
        
        # Check if blob already exists
        existing = self._session.get(FileBlobORM, blob_id.value)
        if existing:
            # Increment reference count
            existing.reference_count += 1
            return blob_id
        
        # Write content to filesystem using shared logic
        storage_location = self._write_content(blob_id, content)
        
        # Create database record using shared mapper
        orm_dict = BlobStorageCore.create_orm_dict(
            blob_id=blob_id.value,
            storage_location=str(storage_location),
            content_size=len(content),
        )
        blob_orm = FileBlobORM(**orm_dict)
        self._session.add(blob_orm)
        
        logger.debug(f"Saved blob {BlobStorageCore.get_short_id(blob_id.value)}... ({len(content)} bytes)")
        return blob_id
    
    def get(self, blob_id: BlobId) -> Optional[bytes]:
        """
        Retrieve content by blob ID.
        
        Args:
            blob_id: Blob identifier
            
        Returns:
            Binary content if found, None otherwise
        """
        blob_orm = self._session.get(FileBlobORM, blob_id.value)
        if not blob_orm:
            return None
        
        return self._read_content(blob_orm.storage_location)
    
    def exists(self, blob_id: BlobId) -> bool:
        """
        Check if blob exists.
        
        Checks both database and filesystem.
        
        Args:
            blob_id: Blob identifier
            
        Returns:
            True if blob exists in both DB and filesystem
        """
        blob_orm = self._session.get(FileBlobORM, blob_id.value)
        if not blob_orm:
            return False
        
        # Also verify file exists
        return Path(blob_orm.storage_location).exists()
    
    def delete(self, blob_id: BlobId) -> bool:
        """
        Delete blob from storage.
        
        Decrements reference count. Only deletes when count reaches 0.
        
        Args:
            blob_id: Blob to delete
            
        Returns:
            True if blob was deleted (ref count = 0), False otherwise
        """
        blob_orm = self._session.get(FileBlobORM, blob_id.value)
        if not blob_orm:
            return False
        
        blob_orm.reference_count -= 1
        
        if blob_orm.reference_count <= 0:
            # Delete file
            file_path = Path(blob_orm.storage_location)
            if file_path.exists():
                file_path.unlink()
            
            # Delete database record
            self._session.delete(blob_orm)
            logger.debug(f"Deleted blob {blob_id.short}...")
            return True
        
        return False
    
    def restore_to_file(self, blob_id: BlobId, output_path: str) -> None:
        """
        Restore blob content to filesystem.
        
        Creates parent directories if needed.
        
        Args:
            blob_id: Blob to restore
            output_path: Destination path
            
        Raises:
            FileNotFoundError: If blob not found
        """
        content = self.get(blob_id)
        if content is None:
            raise FileNotFoundError(f"Blob not found: {blob_id}")
        
        # Ensure parent directory exists
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        
        # Atomic write: write to temp, then rename
        temp_path = output.with_suffix(".tmp")
        temp_path.write_bytes(content)
        os.replace(str(temp_path), str(output))  # Cross-platform atomic move
        
        logger.debug(f"Restored blob {blob_id.short}... to {output_path}")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get storage statistics."""
        result = self._session.execute(
            select(
                func.count(FileBlobORM.content_hash),
                func.sum(FileBlobORM.size),
            )
        ).one()
        
        count = result[0] or 0
        total_size = result[1] or 0
        
        return BlobStorageCore.compute_stats(count, total_size)
    
    def _write_content(self, blob_id: BlobId, content: bytes) -> Path:
        """
        Write content to content-addressable storage.
        
        Uses Git-like path structure: objects/{hash[:2]}/{hash[2:]}
        Computed by BlobStorageCore for consistency with async repository.
        """
        # Compute paths using shared logic
        dir_path = BlobStorageCore.compute_directory_path(blob_id.value, self._objects_path)
        dir_path.mkdir(exist_ok=True)
        
        file_path = BlobStorageCore.compute_storage_path(blob_id.value, self._objects_path)
        
        # Atomic write using shared temp path logic
        # Use os.replace() for cross-platform atomic move (works on Windows even if target exists)
        temp_path = BlobStorageCore.compute_temp_path(file_path)
        temp_path.write_bytes(content)
        os.replace(str(temp_path), str(file_path))
        
        return file_path
    
    def _read_content(self, storage_location: str) -> Optional[bytes]:
        """Read content from filesystem."""
        path = Path(storage_location)
        if not path.exists():
            logger.warning(f"Blob file missing: {storage_location}")
            return None
        return path.read_bytes()


# ═══════════════════════════════════════════════════════════════════════════════
# SQLAlchemy File Commit Repository
# ═══════════════════════════════════════════════════════════════════════════════


class SQLAlchemyFileCommitRepository(IFileCommitRepository):
    """
    SQLAlchemy implementation of IFileCommitRepository.
    
    Manages FileCommit aggregate with cascade to FileMementoORM.
    
    Transaction Boundary:
    - save() adds commit + mementos to session
    - Actual persistence on session.commit()
    """
    
    def __init__(self, session: Session):
        """
        Initialize commit repository.
        
        Args:
            session: SQLAlchemy session
        """
        self._session = session
    
    def save(self, commit: FileCommit) -> None:
        """
        Save commit with all mementos.
        
        Upsert behavior: Updates if commit_id exists.
        
        Args:
            commit: FileCommit to persist
        """
        # Check for existing
        existing = self._session.get(FileCommitORM, commit.commit_id.value)
        
        if existing:
            # Update existing
            existing.message = commit.message
            existing.status = commit.status.value
            existing.execution_id = commit.execution_id
            existing.checkpoint_id = commit.checkpoint_id
            
            # Update mementos (delete old, add new)
            existing.mementos.clear()
            for memento in commit.mementos:
                memento_orm = FileMementoORM(
                    file_path=memento.file_path,
                    file_hash=memento.file_hash.value,
                    file_size=memento.file_size,
                    commit_id=commit.commit_id.value,
                )
                existing.mementos.append(memento_orm)
        else:
            # Create new
            commit_orm = FileCommitORM(
                commit_id=commit.commit_id.value,
                message=commit.message,
                timestamp=commit.timestamp,
                status=commit.status.value,
                created_by=commit.created_by,
                execution_id=commit.execution_id,
                checkpoint_id=commit.checkpoint_id,
            )
            
            # Add mementos
            for memento in commit.mementos:
                memento_orm = FileMementoORM(
                    file_path=memento.file_path,
                    file_hash=memento.file_hash.value,
                    file_size=memento.file_size,
                    commit_id=commit.commit_id.value,
                )
                commit_orm.mementos.append(memento_orm)
            
            self._session.add(commit_orm)
        
        logger.debug(f"Saved commit {commit.commit_id.short}... ({commit.file_count} files)")
    
    def get_by_id(self, commit_id: CommitId) -> Optional[FileCommit]:
        """
        Get commit by ID with mementos loaded.
        
        Args:
            commit_id: Commit identifier
            
        Returns:
            FileCommit with mementos, or None
        """
        commit_orm = self._session.get(FileCommitORM, commit_id.value)
        if not commit_orm:
            return None
        
        return self._orm_to_domain(commit_orm, load_mementos=True)
    
    def get_by_id_without_mementos(self, commit_id: CommitId) -> Optional[FileCommit]:
        """
        Get commit without loading mementos.
        
        Performance optimization for listings.
        """
        commit_orm = self._session.get(FileCommitORM, commit_id.value)
        if not commit_orm:
            return None
        
        return self._orm_to_domain(commit_orm, load_mementos=False)
    
    def get_all(self, limit: int = 100, offset: int = 0) -> List[FileCommit]:
        """
        Get all commits, ordered by timestamp descending.
        
        Does not load mementos for performance.
        """
        stmt = (
            select(FileCommitORM)
            .order_by(FileCommitORM.timestamp.desc())
            .limit(limit)
            .offset(offset)
        )
        
        results = self._session.execute(stmt).scalars().all()
        return [self._orm_to_domain(orm, load_mementos=False) for orm in results]
    
    def get_by_execution_id(self, execution_id: str) -> List[FileCommit]:
        """Get commits for a WTB execution."""
        stmt = (
            select(FileCommitORM)
            .where(FileCommitORM.execution_id == execution_id)
            .order_by(FileCommitORM.timestamp.desc())
        )
        
        results = self._session.execute(stmt).scalars().all()
        return [self._orm_to_domain(orm, load_mementos=False) for orm in results]
    
    def get_by_checkpoint_id(self, checkpoint_id: int) -> Optional[FileCommit]:
        """Get commit linked to checkpoint."""
        stmt = (
            select(FileCommitORM)
            .where(FileCommitORM.checkpoint_id == checkpoint_id)
        )
        
        result = self._session.execute(stmt).scalar_one_or_none()
        if not result:
            return None
        
        return self._orm_to_domain(result, load_mementos=True)
    
    def delete(self, commit_id: CommitId) -> bool:
        """
        Delete commit and cascade to mementos.
        
        Does NOT delete blob content (may be shared).
        """
        commit_orm = self._session.get(FileCommitORM, commit_id.value)
        if not commit_orm:
            return False
        
        self._session.delete(commit_orm)
        logger.debug(f"Deleted commit {commit_id.short}...")
        return True
    
    def count(self) -> int:
        """Get total commit count."""
        result = self._session.execute(
            select(func.count(FileCommitORM.commit_id))
        ).scalar()
        return result or 0
    
    def _orm_to_domain(self, orm: FileCommitORM, load_mementos: bool) -> FileCommit:
        """Convert ORM model to domain entity using shared mapper."""
        return FileCommitMapper.orm_to_domain(orm, load_mementos)


# ═══════════════════════════════════════════════════════════════════════════════
# SQLAlchemy Checkpoint File Link Repository
# ═══════════════════════════════════════════════════════════════════════════════


class SQLAlchemyCheckpointFileLinkRepository(ICheckpointFileLinkRepository):
    """
    SQLAlchemy implementation of ICheckpointFileLinkRepository.
    
    Manages checkpoint-commit links for rollback coordination.
    """
    
    def __init__(self, session: Session):
        """
        Initialize link repository.
        
        Args:
            session: SQLAlchemy session
        """
        self._session = session
    
    def add(self, link: CheckpointFileLink) -> None:
        """
        Add or update checkpoint-file link.
        
        Upsert behavior: Updates if checkpoint_id exists.
        """
        existing = self._session.get(CheckpointFileLinkORM, link.checkpoint_id)
        
        if existing:
            existing.commit_id = link.commit_id.value
            existing.linked_at = link.linked_at
            existing.file_count = link.file_count
            existing.total_size_bytes = link.total_size_bytes
        else:
            link_orm = CheckpointFileLinkORM(
                checkpoint_id=link.checkpoint_id,
                commit_id=link.commit_id.value,
                linked_at=link.linked_at,
                file_count=link.file_count,
                total_size_bytes=link.total_size_bytes,
            )
            self._session.add(link_orm)
        
        logger.debug(f"Linked checkpoint {link.checkpoint_id} to commit {link.commit_id.short}...")
    
    def get_by_checkpoint(self, checkpoint_id: int) -> Optional[CheckpointFileLink]:
        """Get link by checkpoint ID."""
        link_orm = self._session.get(CheckpointFileLinkORM, checkpoint_id)
        if not link_orm:
            return None
        
        return self._orm_to_domain(link_orm)
    
    def get_by_commit(self, commit_id: CommitId) -> List[CheckpointFileLink]:
        """Get all links for a commit."""
        stmt = (
            select(CheckpointFileLinkORM)
            .where(CheckpointFileLinkORM.commit_id == commit_id.value)
        )
        
        results = self._session.execute(stmt).scalars().all()
        return [self._orm_to_domain(orm) for orm in results]
    
    def delete_by_checkpoint(self, checkpoint_id: int) -> bool:
        """Delete link by checkpoint ID."""
        link_orm = self._session.get(CheckpointFileLinkORM, checkpoint_id)
        if not link_orm:
            return False
        
        self._session.delete(link_orm)
        return True
    
    def delete_by_commit(self, commit_id: CommitId) -> int:
        """Delete all links for a commit."""
        stmt = (
            select(CheckpointFileLinkORM)
            .where(CheckpointFileLinkORM.commit_id == commit_id.value)
        )
        
        results = self._session.execute(stmt).scalars().all()
        count = len(results)
        
        for link_orm in results:
            self._session.delete(link_orm)
        
        return count
    
    def list_all(self, limit: int = 10000) -> List[CheckpointFileLink]:
        """List all checkpoint file links."""
        stmt = select(CheckpointFileLinkORM).limit(limit)
        results = self._session.execute(stmt).scalars().all()
        return [self._orm_to_domain(orm) for orm in results]
    
    def _orm_to_domain(self, orm: CheckpointFileLinkORM) -> CheckpointFileLink:
        """Convert ORM to domain model using shared mapper."""
        return CheckpointFileLinkMapper.orm_to_domain(orm)


# ═══════════════════════════════════════════════════════════════════════════════
# In-Memory Implementations (for testing)
# ═══════════════════════════════════════════════════════════════════════════════


class InMemoryBlobRepository(IBlobRepository):
    """
    In-memory blob repository for testing.
    
    Stores blobs in a dictionary. No filesystem involved.
    """
    
    def __init__(self):
        self._blobs: Dict[str, bytes] = {}
        self._metadata: Dict[str, Dict] = {}
    
    def save(self, content: bytes) -> BlobId:
        blob_id = BlobId.from_content(content)
        
        if blob_id.value not in self._blobs:
            self._blobs[blob_id.value] = content
            self._metadata[blob_id.value] = {
                "size": len(content),
                "created_at": datetime.now(),
                "reference_count": 1,
            }
        else:
            self._metadata[blob_id.value]["reference_count"] += 1
        
        return blob_id
    
    def get(self, blob_id: BlobId) -> Optional[bytes]:
        return self._blobs.get(blob_id.value)
    
    def exists(self, blob_id: BlobId) -> bool:
        return blob_id.value in self._blobs
    
    def delete(self, blob_id: BlobId) -> bool:
        if blob_id.value not in self._blobs:
            return False
        
        self._metadata[blob_id.value]["reference_count"] -= 1
        
        if self._metadata[blob_id.value]["reference_count"] <= 0:
            del self._blobs[blob_id.value]
            del self._metadata[blob_id.value]
            return True
        
        return False
    
    def restore_to_file(self, blob_id: BlobId, output_path: str) -> None:
        content = self.get(blob_id)
        if content is None:
            raise FileNotFoundError(f"Blob not found: {blob_id}")
        
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(content)
    
    def get_stats(self) -> Dict[str, Any]:
        total_size = sum(len(b) for b in self._blobs.values())
        return {
            "blob_count": len(self._blobs),
            "total_size_bytes": total_size,
            "total_size_mb": total_size / (1024 * 1024) if total_size else 0,
        }


class InMemoryFileCommitRepository(IFileCommitRepository):
    """
    In-memory file commit repository for testing.
    """
    
    def __init__(self):
        self._commits: Dict[str, FileCommit] = {}
    
    def save(self, commit: FileCommit) -> None:
        self._commits[commit.commit_id.value] = commit
    
    def get_by_id(self, commit_id: CommitId) -> Optional[FileCommit]:
        return self._commits.get(commit_id.value)
    
    def get_by_id_without_mementos(self, commit_id: CommitId) -> Optional[FileCommit]:
        return self._commits.get(commit_id.value)
    
    def get_all(self, limit: int = 100, offset: int = 0) -> List[FileCommit]:
        commits = sorted(
            self._commits.values(),
            key=lambda c: c.timestamp,
            reverse=True,
        )
        return commits[offset:offset + limit]
    
    def get_by_execution_id(self, execution_id: str) -> List[FileCommit]:
        return [
            c for c in self._commits.values()
            if c.execution_id == execution_id
        ]
    
    def get_by_checkpoint_id(self, checkpoint_id: int) -> Optional[FileCommit]:
        for commit in self._commits.values():
            if commit.checkpoint_id == checkpoint_id:
                return commit
        return None
    
    def delete(self, commit_id: CommitId) -> bool:
        if commit_id.value in self._commits:
            del self._commits[commit_id.value]
            return True
        return False
    
    def count(self) -> int:
        return len(self._commits)


class InMemoryCheckpointFileLinkRepository(ICheckpointFileLinkRepository):
    """
    In-memory checkpoint file link repository for testing.
    """
    
    def __init__(self):
        self._links: Dict[int, CheckpointFileLink] = {}
    
    def add(self, link: CheckpointFileLink) -> None:
        self._links[link.checkpoint_id] = link
    
    def get_by_checkpoint(self, checkpoint_id: int) -> Optional[CheckpointFileLink]:
        return self._links.get(checkpoint_id)
    
    def get_by_commit(self, commit_id: CommitId) -> List[CheckpointFileLink]:
        return [
            link for link in self._links.values()
            if link.commit_id.value == commit_id.value
        ]
    
    def delete_by_checkpoint(self, checkpoint_id: int) -> bool:
        if checkpoint_id in self._links:
            del self._links[checkpoint_id]
            return True
        return False
    
    def delete_by_commit(self, commit_id: CommitId) -> int:
        to_delete = [
            cp_id for cp_id, link in self._links.items()
            if link.commit_id.value == commit_id.value
        ]
        for cp_id in to_delete:
            del self._links[cp_id]
        return len(to_delete)
    
    def list_all(self, limit: int = 10000) -> List[CheckpointFileLink]:
        """List all checkpoint file links."""
        links = list(self._links.values())
        return links[:limit]


# ═══════════════════════════════════════════════════════════════════════════════
# Export
# ═══════════════════════════════════════════════════════════════════════════════

__all__ = [
    # SQLAlchemy implementations
    "SQLAlchemyBlobRepository",
    "SQLAlchemyFileCommitRepository",
    "SQLAlchemyCheckpointFileLinkRepository",
    # In-memory implementations
    "InMemoryBlobRepository",
    "InMemoryFileCommitRepository",
    "InMemoryCheckpointFileLinkRepository",
]
