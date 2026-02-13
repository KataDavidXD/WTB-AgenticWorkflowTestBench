"""
SQLite-based File Tracking Service.

Lightweight implementation of IFileTrackingService using SQLite for metadata
and local directory for blob storage. No PostgreSQL required.

Design Principles:
- SOLID: Single responsibility (file tracking operations)
- ACID: Uses SQLite transactions for consistency
- DIP: Implements IFileTrackingService abstraction

Usage:
    from wtb.infrastructure.file_tracking import SqliteFileTrackingService
    from pathlib import Path
    
    service = SqliteFileTrackingService(
        workspace_path=Path("./workspace"),
        db_name="filetrack.db"
    )
    
    # Track files and link to checkpoint
    result = service.track_and_link(checkpoint_id=42, file_paths=["model.pkl"])
    
    # Restore files from checkpoint
    service.restore_from_checkpoint(checkpoint_id=42)

Storage Layout:
    {workspace}/.filetrack/
    ├── blobs/              # Content-addressed file storage
    │   ├── ab/
    │   │   └── cdef1234... # SHA256 hash prefix directory
    │   └── ...
    └── filetrack.db        # SQLite: commits, mementos, checkpoint_links
"""

import hashlib
import logging
import os
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from wtb.domain.interfaces.file_tracking import (
    IFileTrackingService,
    TrackedFile,
    FileTrackingResult,
    FileRestoreResult,
    FileTrackingLink,
    FileTrackingError,
    FileNotFoundError as FTFileNotFoundError,
    CommitNotFoundError,
    CheckpointLinkError,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# SQL Schema
# ═══════════════════════════════════════════════════════════════════════════════

SCHEMA_SQL = """
-- Commits table (aggregate root)
CREATE TABLE IF NOT EXISTS commits (
    commit_id TEXT PRIMARY KEY,
    message TEXT,
    created_at TEXT NOT NULL,
    file_count INTEGER NOT NULL DEFAULT 0,
    total_size_bytes INTEGER NOT NULL DEFAULT 0
);

-- File mementos table (snapshots within commits)
CREATE TABLE IF NOT EXISTS mementos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    blob_hash TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (commit_id) REFERENCES commits(commit_id) ON DELETE CASCADE,
    UNIQUE(commit_id, file_path)
);

-- Checkpoint-commit links table
CREATE TABLE IF NOT EXISTS checkpoint_links (
    checkpoint_id INTEGER PRIMARY KEY,
    commit_id TEXT NOT NULL,
    linked_at TEXT NOT NULL,
    file_count INTEGER NOT NULL DEFAULT 0,
    total_size_bytes INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (commit_id) REFERENCES commits(commit_id) ON DELETE CASCADE
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_mementos_commit ON mementos(commit_id);
CREATE INDEX IF NOT EXISTS idx_mementos_blob ON mementos(blob_hash);
CREATE INDEX IF NOT EXISTS idx_checkpoint_links_commit ON checkpoint_links(commit_id);
"""


class SqliteFileTrackingService(IFileTrackingService):
    """
    SQLite-based file tracking service for single-run execution.
    
    Provides lightweight file tracking without PostgreSQL dependency.
    Uses content-addressed blob storage (like Git) for file deduplication.
    
    Thread-safe with connection-per-thread pattern and RLock protection.
    
    Storage Layout:
        {workspace}/.filetrack/
        ├── blobs/              # Content-addressed file storage (sha256)
        │   ├── ab/
        │   │   └── cdef1234...
        │   └── ...
        └── filetrack.db        # SQLite metadata
    
    ACID Compliance:
    - Atomic: SQLite transactions for commit creation
    - Consistent: SHA256 hashes validate file content
    - Isolated: Per-thread connections
    - Durable: SQLite with WAL mode
    """
    
    def __init__(
        self,
        workspace_path: Path,
        db_name: str = "filetrack.db",
        enable_wal: bool = True,
    ):
        """
        Initialize SQLite file tracking service.
        
        Args:
            workspace_path: Base workspace directory
            db_name: SQLite database filename
            enable_wal: Enable WAL mode for concurrent access
        """
        self._workspace = Path(workspace_path)
        self._filetrack_dir = self._workspace / ".filetrack"
        self._blob_dir = self._filetrack_dir / "blobs"
        self._db_path = self._filetrack_dir / db_name
        self._enable_wal = enable_wal
        
        # Thread safety
        self._lock = threading.RLock()
        self._local = threading.local()
        
        # Initialize storage
        self._init_storage()
    
    def _init_storage(self):
        """Initialize storage directories and database schema."""
        with self._lock:
            # Create directories
            self._filetrack_dir.mkdir(parents=True, exist_ok=True)
            self._blob_dir.mkdir(parents=True, exist_ok=True)
            
            # Initialize database schema
            conn = self._get_connection()
            try:
                conn.executescript(SCHEMA_SQL)
                conn.commit()
                logger.debug(f"Initialized SQLite file tracking at {self._db_path}")
            finally:
                # Don't close - connection is cached per thread
                pass
    
    def _get_connection(self) -> sqlite3.Connection:
        """
        Get thread-local database connection.
        
        Returns:
            SQLite connection for current thread
        """
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
            )
            self._local.conn.row_factory = sqlite3.Row
            
            # Enable WAL mode for concurrent access
            if self._enable_wal:
                self._local.conn.execute("PRAGMA journal_mode=WAL")
            
            # Enable foreign keys
            self._local.conn.execute("PRAGMA foreign_keys=ON")
            
        return self._local.conn
    
    def _hash_file(self, file_path: str) -> str:
        """
        Compute SHA256 hash of file content.
        
        Args:
            file_path: Path to file
            
        Returns:
            64-character hex hash string
        """
        sha256 = hashlib.sha256()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                sha256.update(chunk)
        return sha256.hexdigest()
    
    def _get_blob_path(self, blob_hash: str) -> Path:
        """
        Get blob storage path for a hash (Git-like: objects/{hash[:2]}/{hash[2:]}).
        
        Args:
            blob_hash: SHA256 hash string
            
        Returns:
            Path to blob file
        """
        return self._blob_dir / blob_hash[:2] / blob_hash[2:]
    
    def _store_blob(self, file_path: str) -> tuple[str, int]:
        """
        Store file content in blob storage.
        
        Content-addressed: same content = same hash = stored once.
        
        Args:
            file_path: Path to source file
            
        Returns:
            Tuple of (blob_hash, file_size)
        """
        file_path = Path(file_path)
        blob_hash = self._hash_file(str(file_path))
        blob_path = self._get_blob_path(blob_hash)
        file_size = file_path.stat().st_size
        
        # Only store if not already present (deduplication)
        if not blob_path.exists():
            blob_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(file_path), str(blob_path))
            logger.debug(f"Stored blob {blob_hash[:8]}... ({file_size} bytes)")
        else:
            logger.debug(f"Blob {blob_hash[:8]}... already exists (dedup)")
        
        return blob_hash, file_size
    
    def _restore_blob(self, blob_hash: str, target_path: str) -> bool:
        """
        Restore file from blob storage.
        
        Args:
            blob_hash: SHA256 hash of blob
            target_path: Destination path
            
        Returns:
            True if restored successfully
        """
        blob_path = self._get_blob_path(blob_hash)
        if not blob_path.exists():
            logger.warning(f"Blob {blob_hash[:8]}... not found")
            return False
        
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(blob_path), str(target))
        logger.debug(f"Restored blob {blob_hash[:8]}... to {target_path}")
        return True
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # IFileTrackingService Implementation
    # ═══════════════════════════════════════════════════════════════════════════════
    
    def track_files(
        self,
        file_paths: List[str],
        message: Optional[str] = None,
    ) -> FileTrackingResult:
        """
        Track specified files and create a commit.
        
        Args:
            file_paths: List of file paths to track (absolute or relative)
            message: Optional commit message
            
        Returns:
            FileTrackingResult with commit info and file hashes
            
        Raises:
            FileNotFoundError: If any file does not exist
        """
        with self._lock:
            # Validate all files exist
            for path in file_paths:
                if not os.path.exists(path):
                    raise FTFileNotFoundError(f"File not found: {path}")
            
            conn = self._get_connection()
            cursor = conn.cursor()
            
            try:
                commit_id = str(uuid.uuid4())
                created_at = datetime.now().isoformat()
                file_hashes = {}
                total_size = 0
                mementos_data = []
                
                # Store blobs and collect hashes FIRST
                for path in file_paths:
                    blob_hash, file_size = self._store_blob(path)
                    file_hashes[path] = blob_hash
                    total_size += file_size
                    mementos_data.append((commit_id, path, blob_hash, file_size, created_at))
                
                # Insert commit FIRST (parent table)
                cursor.execute(
                    """
                    INSERT INTO commits (commit_id, message, created_at, file_count, total_size_bytes)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (commit_id, message, created_at, len(file_paths), total_size)
                )
                
                # Then insert mementos (child table with FK to commits)
                for memento in mementos_data:
                    cursor.execute(
                        """
                        INSERT INTO mementos (commit_id, file_path, blob_hash, file_size, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        memento
                    )
                
                conn.commit()
                
                logger.info(
                    f"Created file commit {commit_id[:8]}... "
                    f"({len(file_paths)} files, {total_size} bytes)"
                )
                
                return FileTrackingResult(
                    commit_id=commit_id,
                    files_tracked=len(file_paths),
                    total_size_bytes=total_size,
                    file_hashes=file_hashes,
                    message=message,
                    created_at=datetime.fromisoformat(created_at),
                )
                
            except Exception as e:
                conn.rollback()
                raise FileTrackingError(f"Failed to track files: {e}") from e
    
    def track_and_link(
        self,
        checkpoint_id: int,
        file_paths: List[str],
        message: Optional[str] = None,
    ) -> FileTrackingResult:
        """
        Track files AND link to checkpoint in single atomic operation.
        
        Args:
            checkpoint_id: WTB checkpoint ID to link to
            file_paths: Files to track
            message: Optional commit message
            
        Returns:
            FileTrackingResult with commit info
        """
        with self._lock:
            # Track files first
            result = self.track_files(file_paths, message)
            
            # Link to checkpoint
            conn = self._get_connection()
            cursor = conn.cursor()
            
            try:
                # Delete existing link if any (replace)
                cursor.execute(
                    "DELETE FROM checkpoint_links WHERE checkpoint_id = ?",
                    (checkpoint_id,)
                )
                
                # Insert new link
                cursor.execute(
                    """
                    INSERT INTO checkpoint_links 
                    (checkpoint_id, commit_id, linked_at, file_count, total_size_bytes)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        checkpoint_id,
                        result.commit_id,
                        datetime.now().isoformat(),
                        result.files_tracked,
                        result.total_size_bytes,
                    )
                )
                
                conn.commit()
                
                logger.info(
                    f"Linked checkpoint {checkpoint_id} to commit {result.commit_id[:8]}..."
                )
                
                return result
                
            except Exception as e:
                conn.rollback()
                raise CheckpointLinkError(f"Failed to link checkpoint: {e}") from e
    
    def link_to_checkpoint(
        self,
        checkpoint_id: int,
        commit_id: str,
    ) -> FileTrackingLink:
        """
        Link existing commit to checkpoint.
        
        Args:
            checkpoint_id: WTB checkpoint ID
            commit_id: FileTracker commit ID
            
        Returns:
            FileTrackingLink with link details
            
        Raises:
            CommitNotFoundError: If commit does not exist
        """
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # Verify commit exists
            cursor.execute(
                "SELECT file_count, total_size_bytes FROM commits WHERE commit_id = ?",
                (commit_id,)
            )
            row = cursor.fetchone()
            if row is None:
                raise CommitNotFoundError(f"Commit not found: {commit_id}")
            
            file_count = row['file_count']
            total_size = row['total_size_bytes']
            linked_at = datetime.now()
            
            try:
                # Replace existing link
                cursor.execute(
                    "DELETE FROM checkpoint_links WHERE checkpoint_id = ?",
                    (checkpoint_id,)
                )
                
                cursor.execute(
                    """
                    INSERT INTO checkpoint_links 
                    (checkpoint_id, commit_id, linked_at, file_count, total_size_bytes)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (checkpoint_id, commit_id, linked_at.isoformat(), file_count, total_size)
                )
                
                conn.commit()
                
                return FileTrackingLink(
                    checkpoint_id=checkpoint_id,
                    commit_id=commit_id,
                    linked_at=linked_at,
                    file_count=file_count,
                    total_size_bytes=total_size,
                )
                
            except Exception as e:
                conn.rollback()
                raise CheckpointLinkError(f"Failed to link: {e}") from e
    
    def restore_from_checkpoint(
        self,
        checkpoint_id: int,
    ) -> FileRestoreResult:
        """
        Restore files from checkpoint's linked commit.
        
        Args:
            checkpoint_id: Checkpoint to restore from
            
        Returns:
            FileRestoreResult with restore details
            
        Raises:
            CheckpointLinkError: If checkpoint has no linked commit
        """
        with self._lock:
            commit_id = self.get_commit_for_checkpoint(checkpoint_id)
            if commit_id is None:
                raise CheckpointLinkError(
                    f"No commit linked to checkpoint {checkpoint_id}"
                )
            
            return self.restore_commit(commit_id)
    
    def restore_commit(
        self,
        commit_id: str,
    ) -> FileRestoreResult:
        """
        Restore files from a specific commit.
        
        Args:
            commit_id: FileTracker commit ID
            
        Returns:
            FileRestoreResult with restore details
            
        Raises:
            CommitNotFoundError: If commit not found
        """
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # Verify commit exists
            cursor.execute(
                "SELECT commit_id FROM commits WHERE commit_id = ?",
                (commit_id,)
            )
            if cursor.fetchone() is None:
                raise CommitNotFoundError(f"Commit not found: {commit_id}")
            
            # Get all mementos for this commit
            cursor.execute(
                """
                SELECT file_path, blob_hash, file_size 
                FROM mementos WHERE commit_id = ?
                """,
                (commit_id,)
            )
            rows = cursor.fetchall()
            
            restored_paths = []
            total_size = 0
            errors = []
            
            for row in rows:
                file_path = row['file_path']
                blob_hash = row['blob_hash']
                file_size = row['file_size']
                
                if self._restore_blob(blob_hash, file_path):
                    restored_paths.append(file_path)
                    total_size += file_size
                else:
                    errors.append(f"Failed to restore {file_path}")
            
            success = len(errors) == 0
            error_message = "; ".join(errors) if errors else None
            
            logger.info(
                f"Restored {len(restored_paths)}/{len(rows)} files from commit {commit_id[:8]}..."
            )
            
            return FileRestoreResult(
                commit_id=commit_id,
                files_restored=len(restored_paths),
                total_size_bytes=total_size,
                restored_paths=restored_paths,
                success=success,
                error_message=error_message,
            )
    
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
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute(
                "SELECT commit_id FROM checkpoint_links WHERE checkpoint_id = ?",
                (checkpoint_id,)
            )
            row = cursor.fetchone()
            return row['commit_id'] if row else None
    
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
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # Verify commit exists
            cursor.execute(
                "SELECT commit_id FROM commits WHERE commit_id = ?",
                (commit_id,)
            )
            if cursor.fetchone() is None:
                raise CommitNotFoundError(f"Commit not found: {commit_id}")
            
            # Get mementos
            cursor.execute(
                """
                SELECT file_path, blob_hash, file_size, created_at 
                FROM mementos WHERE commit_id = ?
                """,
                (commit_id,)
            )
            
            return [
                TrackedFile(
                    file_path=row['file_path'],
                    file_hash=row['blob_hash'],
                    size_bytes=row['file_size'],
                    tracked_at=datetime.fromisoformat(row['created_at']),
                )
                for row in cursor.fetchall()
            ]
    
    def is_available(self) -> bool:
        """
        Check if file tracking service is available.
        
        Returns:
            True if database is accessible
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            return True
        except Exception:
            return False
    
    def get_files_at_checkpoint(
        self,
        checkpoint_id: int,
    ) -> List[str]:
        """
        Get file paths that existed at a specific checkpoint.
        
        Uses existing checkpoint_links -> commit_id -> mementos relationship.
        NO new table required - leverages existing schema.
        
        Design Decision (2026-02-13, v1.9):
        - Added for rollback cleanup feature
        - Uses existing tables to avoid data duplication
        - Returns only file paths (not full TrackedFile objects) for efficiency
        
        Args:
            checkpoint_id: Checkpoint to query
            
        Returns:
            List of file paths that existed at the checkpoint
        """
        with self._lock:
            # Get commit ID for checkpoint
            commit_id = self.get_commit_for_checkpoint(checkpoint_id)
            if commit_id is None:
                logger.debug(f"No commit linked to checkpoint {checkpoint_id}")
                return []
            
            # Get file paths from mementos
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute(
                "SELECT file_path FROM mementos WHERE commit_id = ?",
                (commit_id,)
            )
            
            file_paths = [row['file_path'] for row in cursor.fetchall()]
            logger.debug(
                f"Found {len(file_paths)} files at checkpoint {checkpoint_id} "
                f"(commit {commit_id[:8]}...)"
            )
            
            return file_paths
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # Additional Methods
    # ═══════════════════════════════════════════════════════════════════════════════
    
    def get_commit_count(self) -> int:
        """Get total number of commits."""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as cnt FROM commits")
            return cursor.fetchone()['cnt']
    
    def get_link_count(self) -> int:
        """Get total number of checkpoint links."""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as cnt FROM checkpoint_links")
            return cursor.fetchone()['cnt']
    
    def get_storage_stats(self) -> Dict:
        """Get storage statistics."""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute("SELECT COUNT(*) as cnt, SUM(total_size_bytes) as total FROM commits")
            commits_row = cursor.fetchone()
            
            cursor.execute("SELECT COUNT(DISTINCT blob_hash) as cnt FROM mementos")
            blobs_row = cursor.fetchone()
            
            # Calculate actual blob storage size
            blob_size = 0
            for root, dirs, files in os.walk(self._blob_dir):
                for f in files:
                    blob_size += os.path.getsize(os.path.join(root, f))
            
            return {
                "commit_count": commits_row['cnt'],
                "total_tracked_bytes": commits_row['total'] or 0,
                "unique_blob_count": blobs_row['cnt'],
                "actual_storage_bytes": blob_size,
                "dedup_ratio": (
                    (commits_row['total'] or 0) / blob_size 
                    if blob_size > 0 else 1.0
                ),
            }
    
    def close(self):
        """Close database connection for current thread."""
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
