"""
Blob Storage Core - Shared logic for sync/async blob repositories.

Created: 2026-01-28
Reference: ARCHITECTURE_REVIEW_2026_01_28.md - ISSUE-FS-001

Purpose:
- Single source of truth for blob storage business logic
- Used by both sync and async blob repositories
- Handles path computation, hash operations, and pure logic
- DRY principle compliance: ~200 lines of duplicated code eliminated

Design:
- Static methods for pure functions (no I/O)
- No async/sync dependencies - can be used by either
- Content-addressable storage using SHA-256 hashes
"""

import hashlib
from pathlib import Path
from typing import Any


class BlobStorageCore:
    """
    Pure logic for blob storage operations.
    
    This class extracts the shared logic that was duplicated between:
    - SQLAlchemyBlobRepository (sync)
    - AsyncSQLAlchemyBlobRepository (async)
    
    All methods are static/class methods with no I/O, enabling:
    - Easy testing without mocking
    - Reuse across sync and async implementations
    - Clear separation of business logic from I/O operations
    
    Usage:
        # In sync or async repository
        from wtb.infrastructure.database.mappers import BlobStorageCore
        
        blob_id = BlobStorageCore.compute_blob_id(content)
        storage_path = BlobStorageCore.compute_storage_path(blob_id, base_path)
    """
    
    HASH_ALGORITHM = "sha256"
    DIR_PREFIX_LENGTH = 2  # Git-like: objects/ab/cdef...
    
    @staticmethod
    def compute_blob_id(content: bytes) -> str:
        """
        Compute content-addressable blob ID from content.
        
        Uses SHA-256 for consistent hashing across all implementations.
        
        Args:
            content: Binary content to hash
            
        Returns:
            SHA-256 hex digest string
        """
        return hashlib.sha256(content).hexdigest()
    
    @staticmethod
    def compute_storage_path(blob_id: str, objects_path: Path) -> Path:
        """
        Compute filesystem path for blob storage.
        
        Uses Git-like directory sharding: objects/{hash[:2]}/{hash[2:]}
        This prevents too many files in a single directory.
        
        Args:
            blob_id: SHA-256 hash string
            objects_path: Base path for objects storage
            
        Returns:
            Full path to blob file
        """
        dir_name = blob_id[:BlobStorageCore.DIR_PREFIX_LENGTH]
        file_name = blob_id[BlobStorageCore.DIR_PREFIX_LENGTH:]
        return objects_path / dir_name / file_name
    
    @staticmethod
    def compute_directory_path(blob_id: str, objects_path: Path) -> Path:
        """
        Compute directory path for a blob (without filename).
        
        Args:
            blob_id: SHA-256 hash string
            objects_path: Base path for objects storage
            
        Returns:
            Directory path for the blob
        """
        dir_name = blob_id[:BlobStorageCore.DIR_PREFIX_LENGTH]
        return objects_path / dir_name
    
    @staticmethod
    def compute_temp_path(storage_path: Path, suffix: str | None = None) -> Path:
        """
        Compute temporary path for atomic writes.
        
        Args:
            storage_path: Target storage path
            suffix: Optional unique suffix (e.g., UUID for concurrent writes)
            
        Returns:
            Temporary file path
        """
        if suffix:
            return storage_path.with_suffix(f".tmp.{suffix}")
        return storage_path.with_suffix(".tmp")
    
    @staticmethod
    def validate_blob_id(blob_id: str) -> bool:
        """
        Validate that a blob ID is a valid SHA-256 hash.
        
        Args:
            blob_id: String to validate
            
        Returns:
            True if valid SHA-256 hex string
        """
        if not blob_id:
            return False
        if len(blob_id) != 64:  # SHA-256 produces 64 hex chars
            return False
        try:
            int(blob_id, 16)
            return True
        except ValueError:
            return False
    
    @staticmethod
    def get_short_id(blob_id: str, length: int = 8) -> str:
        """
        Get shortened blob ID for display/logging.
        
        Args:
            blob_id: Full SHA-256 hash
            length: Number of characters to return
            
        Returns:
            Shortened hash string
        """
        return blob_id[:length] if blob_id else ""
    
    @staticmethod
    def verify_content_matches_id(content: bytes, blob_id: str) -> bool:
        """
        Verify that content hash matches expected blob ID.
        
        Args:
            content: Binary content
            blob_id: Expected SHA-256 hash
            
        Returns:
            True if content hash matches blob_id
        """
        computed_hash = BlobStorageCore.compute_blob_id(content)
        return computed_hash == blob_id
    
    @staticmethod
    def create_orm_dict(
        blob_id: str,
        storage_location: str,
        content_size: int,
        reference_count: int = 1,
    ) -> dict[str, Any]:
        """
        Create dictionary for ORM model creation.
        
        Args:
            blob_id: Content hash
            storage_location: Filesystem path as string
            content_size: Size in bytes
            reference_count: Initial reference count
            
        Returns:
            Dictionary suitable for FileBlobORM creation
        """
        from datetime import datetime
        
        return {
            "content_hash": blob_id,
            "storage_location": storage_location,
            "size": content_size,
            "created_at": datetime.now(),
            "reference_count": reference_count,
        }
    
    @staticmethod
    def compute_stats(blob_count: int, total_size: int) -> dict[str, Any]:
        """
        Compute storage statistics dictionary.
        
        Args:
            blob_count: Number of blobs
            total_size: Total size in bytes
            
        Returns:
            Statistics dictionary
        """
        return {
            "blob_count": blob_count,
            "total_size_bytes": total_size,
            "total_size_mb": total_size / (1024 * 1024) if total_size else 0,
        }
    
    @classmethod
    def setup_storage_structure(cls, storage_path: Path) -> tuple[Path, Path]:
        """
        Setup storage directory structure.
        
        Creates necessary directories for blob storage.
        
        Args:
            storage_path: Base storage path
            
        Returns:
            Tuple of (storage_path, objects_path)
        """
        objects_path = storage_path / "objects"
        objects_path.mkdir(parents=True, exist_ok=True)
        return storage_path, objects_path


class FileCommitMapper:
    """
    Mapper for FileCommit domain model ↔ FileCommitORM.
    
    Extracts the conversion logic shared between sync and async repositories.
    """
    
    @staticmethod
    def orm_to_domain(orm, load_mementos: bool = True):
        """
        Convert ORM model to domain entity.
        
        Args:
            orm: FileCommitORM instance
            load_mementos: Whether to include mementos
            
        Returns:
            FileCommit domain model
        """
        from wtb.domain.models.file_processing import (
            BlobId,
            FileCommit,
            FileMemento,
        )
        
        mementos = []
        if load_mementos and hasattr(orm, 'mementos'):
            mementos = [
                FileMemento(
                    file_path=m.file_path,
                    file_hash=BlobId(m.file_hash),
                    file_size=m.file_size,
                )
                for m in orm.mementos
            ]
        
        return FileCommit.reconstitute(
            commit_id=orm.commit_id,
            message=orm.message,
            timestamp=orm.timestamp,
            status=orm.status,
            mementos=mementos,
            created_by=orm.created_by,
            execution_id=orm.execution_id,
            checkpoint_id=orm.checkpoint_id,
        )
    
    @staticmethod
    def domain_to_orm_mementos(commit, orm_class):
        """
        Convert domain mementos to ORM models.
        
        Args:
            commit: FileCommit domain model
            orm_class: FileMementoORM class
            
        Returns:
            List of FileMementoORM instances
        """
        return [
            orm_class(
                file_path=memento.file_path,
                file_hash=memento.file_hash.value,
                file_size=memento.file_size,
                commit_id=commit.commit_id.value,
            )
            for memento in commit.mementos
        ]


class CheckpointFileLinkMapper:
    """
    Mapper for CheckpointFileLink domain model ↔ CheckpointFileLinkORM.
    """
    
    @staticmethod
    def orm_to_domain(orm):
        """
        Convert ORM to domain model.
        
        Args:
            orm: CheckpointFileLinkORM instance
            
        Returns:
            CheckpointFileLink domain model
        """
        from wtb.domain.models.file_processing import CheckpointFileLink, CommitId
        
        return CheckpointFileLink(
            checkpoint_id=str(orm.checkpoint_id),
            commit_id=CommitId(orm.commit_id),
            linked_at=orm.linked_at,
            file_count=orm.file_count,
            total_size_bytes=orm.total_size_bytes,
        )


__all__ = [
    "BlobStorageCore",
    "FileCommitMapper",
    "CheckpointFileLinkMapper",
]
