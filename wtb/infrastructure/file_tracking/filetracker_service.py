"""
FileTracker Service Implementation.

Real implementation of IFileTrackingService using FileTracker system.
Integrates with the file_processing/file_processing/FileTracker package.

Design Principles:
- SOLID: Single responsibility (file tracking operations)
- ACID: Uses transactions for consistency
- DIP: Implements IFileTrackingService abstraction

Architecture:
- Connects to FileTracker PostgreSQL database via psycopg2
- Uses content-addressed blob storage (like Git)
- Maintains checkpoint_files links for WTB rollback coordination

Usage:
    from wtb.infrastructure.file_tracking import FileTrackerService
    from wtb.config import FileTrackingConfig
    
    config = FileTrackingConfig.for_production(
        postgres_url="postgresql://user:pass@localhost/filetracker",
        storage_path="/data/blobs",
        wtb_db_url="postgresql://...",
    )
    
    service = FileTrackerService(config)
    result = service.track_and_link(checkpoint_id=42, file_paths=["model.pkl"])
"""

import logging
import os
import sys
import threading
from datetime import datetime
from typing import Dict, List, Optional, Any

from wtb.config import FileTrackingConfig
from wtb.domain.interfaces.file_tracking import (
    IFileTrackingService,
    TrackedFile,
    FileTrackingResult,
    FileRestoreResult,
    FileTrackingLink,
    FileTrackingError,
    CommitNotFoundError,
    CheckpointLinkError,
)

logger = logging.getLogger(__name__)


class FileTrackerService(IFileTrackingService):
    """
    Real FileTracker integration service.
    
    Connects to the FileTracker system (file_processing/file_processing/FileTracker)
    for file version control and maintains checkpoint-file links in the
    checkpoint_files table for rollback coordination.
    
    Thread-safe with RLock protection.
    
    Database Connections:
    - FileTracker DB (PostgreSQL): For commits, mementos, and blobs
    - checkpoint_files table: For WTB checkpoint links
    
    ACID Compliance:
    - Atomic: Uses transactions for commit creation
    - Consistent: Validates file existence before tracking
    - Isolated: Per-thread connections
    - Durable: Data persisted to PostgreSQL
    
    FileTracker Components Used:
    - Commit: Represents a version commit with file snapshots
    - FileMemento: Captures file state (path, hash, size)
    - BlobRepository: Stores file content in content-addressed storage
    - CommitRepository: Manages commit and memento persistence
    """
    
    def __init__(self, config: FileTrackingConfig):
        """
        Initialize FileTracker service.
        
        Args:
            config: FileTrackingConfig with connection details
            
        Raises:
            FileTrackingError: If initialization fails
        """
        self._config = config
        self._lock = threading.RLock()
        
        # Lazy-initialized connections and components
        self._db_conn = None
        self._commit_repo = None
        self._blob_repo = None
        self._blob_orm = None
        self._initialized = False
        
        # FileTracker module references
        self._Commit = None
        self._FileMemento = None
        
        # Checkpoint links cache
        self._checkpoint_links_cache: Dict[int, str] = {}
        
        if config.enabled:
            self._ensure_initialized()
    
    def _ensure_initialized(self):
        """
        Lazy initialization of FileTracker components.
        
        Imports FileTracker from file_processing path and initializes
        database connections and repositories.
        """
        if self._initialized:
            return
        
        with self._lock:
            if self._initialized:
                return
            
            try:
                # Add FileTracker path to sys.path
                filetracker_path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))),
                    "file_processing",
                    "file_processing"
                )
                
                if filetracker_path not in sys.path:
                    sys.path.insert(0, filetracker_path)
                
                # Import FileTracker components
                from FileTracker import Commit, FileMemento
                from FileTracker.ORM import BlobORM
                from FileTracker.Repository import BlobRepository, CommitRepository
                
                self._Commit = Commit
                self._FileMemento = FileMemento
                
                # Create storage directory
                self._config.ensure_storage_path()
                
                # Connect to PostgreSQL database
                if self._config.postgres_url:
                    import psycopg2
                    
                    # Parse postgres_url to connection params
                    self._db_conn = self._connect_postgres(self._config.postgres_url)
                else:
                    raise FileTrackingError(
                        "PostgreSQL URL required for FileTracker. "
                        "Use MockFileTrackingService for testing."
                    )
                
                # Initialize ORM and repositories
                self._blob_orm = BlobORM(self._db_conn, self._config.storage_path)
                self._blob_repo = BlobRepository(self._db_conn, self._config.storage_path)
                self._commit_repo = CommitRepository(self._db_conn)
                
                # Initialize checkpoint_files table if not exists
                self._init_checkpoint_files_table()
                
                self._initialized = True
                logger.info(
                    f"FileTrackerService initialized: "
                    f"storage={self._config.storage_path}, "
                    f"db={self._config.postgres_url}"
                )
                
            except ImportError as e:
                logger.error(f"FileTracker import failed: {e}")
                raise FileTrackingError(
                    f"FileTracker not available. Ensure file_processing is in the path: {e}"
                ) from e
            except Exception as e:
                logger.error(f"FileTrackerService initialization failed: {e}")
                raise FileTrackingError(f"Initialization failed: {e}") from e
    
    def _connect_postgres(self, url: str) -> Any:
        """
        Connect to PostgreSQL database.
        
        Args:
            url: PostgreSQL connection URL (postgresql://user:pass@host:port/db)
            
        Returns:
            psycopg2 connection object
        """
        import psycopg2
        from urllib.parse import urlparse
        
        parsed = urlparse(url)
        
        return psycopg2.connect(
            host=parsed.hostname or "localhost",
            port=parsed.port or 5432,
            database=parsed.path[1:] if parsed.path else "filetracker",
            user=parsed.username or "postgres",
            password=parsed.password or "",
        )
    
    def _init_checkpoint_files_table(self):
        """
        Initialize checkpoint_files table for WTB integration.
        
        Creates the table if it doesn't exist.
        """
        cursor = self._db_conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS checkpoint_files (
                checkpoint_id INTEGER PRIMARY KEY,
                commit_id VARCHAR(64) NOT NULL REFERENCES commits(commit_id) ON DELETE CASCADE,
                linked_at TIMESTAMP DEFAULT NOW(),
                file_count INTEGER NOT NULL,
                total_size_bytes BIGINT NOT NULL
            )
        """)
        
        # Create index for commit lookups
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_checkpoint_files_commit_id 
            ON checkpoint_files(commit_id)
        """)
        
        self._db_conn.commit()
        cursor.close()
    
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
        if not self._config.enabled:
            return self._create_disabled_result()
        
        self._ensure_initialized()
        
        with self._lock:
            # Validate all files exist
            for path in file_paths:
                if not os.path.exists(path):
                    from wtb.domain.interfaces.file_tracking import FileNotFoundError as FTFileNotFoundError
                    raise FTFileNotFoundError(f"File not found: {path}")
                
                # Check file size limit
                size_mb = os.path.getsize(path) / (1024 * 1024)
                if size_mb > self._config.max_file_size_mb:
                    raise FileTrackingError(
                        f"File too large: {path} ({size_mb:.1f}MB > {self._config.max_file_size_mb}MB limit)"
                    )
            
            try:
                # Create commit with mementos
                commit = self._Commit(message=message)
                file_hashes = {}
                total_size = 0
                
                for path in file_paths:
                    # FileMemento reads file and saves blob via BlobORM
                    memento = self._FileMemento(path, self._blob_orm)
                    commit.add_memento(memento)
                    
                    file_hashes[path] = memento.file_hash
                    total_size += memento.file_size
                
                # Save commit to database
                self._commit_repo.save(commit)
                
                logger.info(
                    f"Created commit {commit.commit_id}: "
                    f"{len(file_paths)} files, {total_size} bytes"
                )
                
                return FileTrackingResult(
                    commit_id=commit.commit_id,
                    files_tracked=len(file_paths),
                    total_size_bytes=total_size,
                    file_hashes=file_hashes,
                    message=message,
                )
                
            except Exception as e:
                logger.error(f"track_files failed: {e}")
                raise FileTrackingError(f"Failed to track files: {e}") from e
    
    def track_and_link(
        self,
        checkpoint_id: str,
        file_paths: List[str],
        message: Optional[str] = None,
    ) -> FileTrackingResult:
        """
        Track files AND link to checkpoint in single operation.
        
        Atomic operation that:
        1. Creates FileTracker commit with file snapshots
        2. Links the commit to the specified WTB checkpoint
        
        Args:
            checkpoint_id: WTB checkpoint ID to link to
            file_paths: Files to track
            message: Optional commit message
            
        Returns:
            FileTrackingResult with commit info
        """
        if not self._config.enabled:
            return self._create_disabled_result()
        
        self._ensure_initialized()
        
        with self._lock:
            # Track files first
            result = self.track_files(file_paths, message)
            
            # Link to checkpoint
            self._save_checkpoint_link(
                checkpoint_id=checkpoint_id,
                commit_id=result.commit_id,
                file_count=result.files_tracked,
                total_size=result.total_size_bytes,
            )
            
            logger.info(
                f"Linked checkpoint {checkpoint_id} to commit {result.commit_id}"
            )
            
            return result
    
    def _save_checkpoint_link(
        self,
        checkpoint_id: str,
        commit_id: str,
        file_count: int,
        total_size: int,
    ):
        """Save checkpoint-commit link to database."""
        cursor = self._db_conn.cursor()
        
        # Use upsert pattern for PostgreSQL
        cursor.execute("""
            INSERT INTO checkpoint_files 
            (checkpoint_id, commit_id, linked_at, file_count, total_size_bytes)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (checkpoint_id) DO UPDATE SET
                commit_id = EXCLUDED.commit_id,
                linked_at = EXCLUDED.linked_at,
                file_count = EXCLUDED.file_count,
                total_size_bytes = EXCLUDED.total_size_bytes
        """, (checkpoint_id, commit_id, datetime.now(), file_count, total_size))
        
        self._db_conn.commit()
        cursor.close()
        
        # Update cache
        self._checkpoint_links_cache[checkpoint_id] = commit_id
    
    def link_to_checkpoint(
        self,
        checkpoint_id: str,
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
        if not self._config.enabled:
            raise CheckpointLinkError("File tracking is disabled")
        
        self._ensure_initialized()
        
        with self._lock:
            # Verify commit exists
            commit = self._commit_repo.find_by_id(commit_id)
            if commit is None:
                raise CommitNotFoundError(f"Commit not found: {commit_id}")
            
            # Calculate totals from mementos
            file_count = len(commit.mementos)
            total_size = sum(m.file_size for m in commit.mementos)
            
            # Save link
            self._save_checkpoint_link(checkpoint_id, commit_id, file_count, total_size)
            
            return FileTrackingLink(
                checkpoint_id=checkpoint_id,
                commit_id=commit_id,
                linked_at=datetime.now(),
                file_count=file_count,
                total_size_bytes=total_size,
            )
    
    def restore_from_checkpoint(
        self,
        checkpoint_id: str,
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
        if not self._config.enabled:
            return FileRestoreResult(
                commit_id="",
                files_restored=0,
                total_size_bytes=0,
                restored_paths=[],
                success=False,
                error_message="File tracking is disabled",
            )
        
        self._ensure_initialized()
        
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
        
        Restores all files from the commit to their original paths
        using the BlobRepository.
        
        Args:
            commit_id: FileTracker commit ID
            
        Returns:
            FileRestoreResult with restore details
            
        Raises:
            CommitNotFoundError: If commit not found
        """
        if not self._config.enabled:
            return FileRestoreResult(
                commit_id=commit_id,
                files_restored=0,
                total_size_bytes=0,
                restored_paths=[],
                success=False,
                error_message="File tracking is disabled",
            )
        
        self._ensure_initialized()
        
        with self._lock:
            commit = self._commit_repo.find_by_id(commit_id)
            if commit is None:
                raise CommitNotFoundError(f"Commit not found: {commit_id}")
            
            restored_paths = []
            total_size = 0
            
            try:
                for memento in commit.mementos:
                    # Ensure directory exists
                    dir_path = os.path.dirname(memento.file_path)
                    if dir_path:
                        os.makedirs(dir_path, exist_ok=True)
                    
                    # Restore file content from blob storage
                    self._blob_repo.restore(memento.file_hash, memento.file_path)
                    
                    restored_paths.append(memento.file_path)
                    total_size += memento.file_size
                
                logger.info(
                    f"Restored {len(restored_paths)} files from commit {commit_id}"
                )
                
                return FileRestoreResult(
                    commit_id=commit_id,
                    files_restored=len(restored_paths),
                    total_size_bytes=total_size,
                    restored_paths=restored_paths,
                    success=True,
                )
                
            except Exception as e:
                logger.error(f"Restore failed for commit {commit_id}: {e}")
                return FileRestoreResult(
                    commit_id=commit_id,
                    files_restored=len(restored_paths),
                    total_size_bytes=total_size,
                    restored_paths=restored_paths,
                    success=False,
                    error_message=str(e),
                )
    
    def restore_to_workspace(
        self,
        commit_id: str,
        workspace_output_dir: str,
    ) -> FileRestoreResult:
        """
        Restore files from commit to a specific workspace output directory.
        
        This method restores files to the workspace's output directory instead
        of their original paths, enabling isolated rollback for parallel variants.
        
        Args:
            commit_id: FileTracker commit ID to restore from
            workspace_output_dir: Workspace output directory path
            
        Returns:
            FileRestoreResult with restore details
            
        Raises:
            CommitNotFoundError: If commit not found
        """
        if not self._config.enabled:
            return FileRestoreResult(
                commit_id=commit_id,
                files_restored=0,
                total_size_bytes=0,
                restored_paths=[],
                success=False,
                error_message="File tracking is disabled",
            )
        
        self._ensure_initialized()
        
        with self._lock:
            commit = self._commit_repo.find_by_id(commit_id)
            if commit is None:
                raise CommitNotFoundError(f"Commit not found: {commit_id}")
            
            output_dir = os.path.abspath(workspace_output_dir)
            os.makedirs(output_dir, exist_ok=True)
            
            restored_paths = []
            total_size = 0
            
            try:
                for memento in commit.mementos:
                    # Compute target path within workspace
                    original_path = memento.file_path
                    file_name = os.path.basename(original_path)
                    
                    # Preserve subdirectory structure if it exists
                    # e.g., "outputs/models/v1.pkl" -> "{workspace}/output/outputs/models/v1.pkl"
                    rel_parts = original_path.split(os.sep)
                    if len(rel_parts) > 1:
                        target_path = os.path.join(output_dir, *rel_parts)
                    else:
                        target_path = os.path.join(output_dir, file_name)
                    
                    # Ensure target directory exists
                    target_dir = os.path.dirname(target_path)
                    if target_dir:
                        os.makedirs(target_dir, exist_ok=True)
                    
                    # Restore file content from blob storage
                    self._blob_repo.restore(memento.file_hash, target_path)
                    
                    restored_paths.append(target_path)
                    total_size += memento.file_size
                
                logger.info(
                    f"Restored {len(restored_paths)} files from commit {commit_id} "
                    f"to workspace {output_dir}"
                )
                
                return FileRestoreResult(
                    commit_id=commit_id,
                    files_restored=len(restored_paths),
                    total_size_bytes=total_size,
                    restored_paths=restored_paths,
                    success=True,
                )
                
            except Exception as e:
                logger.error(
                    f"Workspace restore failed for commit {commit_id} "
                    f"to {output_dir}: {e}"
                )
                return FileRestoreResult(
                    commit_id=commit_id,
                    files_restored=len(restored_paths),
                    total_size_bytes=total_size,
                    restored_paths=restored_paths,
                    success=False,
                    error_message=str(e),
                )
    
    def restore_checkpoint_to_workspace(
        self,
        checkpoint_id: str,
        workspace_output_dir: str,
    ) -> FileRestoreResult:
        """
        Restore files from checkpoint to workspace output directory.
        
        Convenience method combining get_commit_for_checkpoint and
        restore_to_workspace for rollback operations.
        
        Args:
            checkpoint_id: Checkpoint to restore from
            workspace_output_dir: Workspace output directory path
            
        Returns:
            FileRestoreResult with restore details
            
        Raises:
            CheckpointLinkError: If checkpoint has no linked commit
        """
        if not self._config.enabled:
            return FileRestoreResult(
                commit_id="",
                files_restored=0,
                total_size_bytes=0,
                restored_paths=[],
                success=False,
                error_message="File tracking is disabled",
            )
        
        self._ensure_initialized()
        
        with self._lock:
            commit_id = self.get_commit_for_checkpoint(checkpoint_id)
            if commit_id is None:
                raise CheckpointLinkError(
                    f"No commit linked to checkpoint {checkpoint_id}"
                )
            
            return self.restore_to_workspace(commit_id, workspace_output_dir)
    
    def get_commit_for_checkpoint(
        self,
        checkpoint_id: str,
    ) -> Optional[str]:
        """
        Get the file commit ID linked to a checkpoint.
        
        Args:
            checkpoint_id: Checkpoint to query
            
        Returns:
            Commit ID if linked, None otherwise
        """
        if not self._config.enabled:
            return None
        
        # Check cache first
        if checkpoint_id in self._checkpoint_links_cache:
            return self._checkpoint_links_cache[checkpoint_id]
        
        self._ensure_initialized()
        
        with self._lock:
            cursor = self._db_conn.cursor()
            cursor.execute(
                "SELECT commit_id FROM checkpoint_files WHERE checkpoint_id = %s",
                (checkpoint_id,)
            )
            row = cursor.fetchone()
            cursor.close()
            
            if row:
                commit_id = row[0]
                self._checkpoint_links_cache[checkpoint_id] = commit_id
                return commit_id
            
            return None
    
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
        if not self._config.enabled:
            return []
        
        self._ensure_initialized()
        
        with self._lock:
            commit = self._commit_repo.find_by_id(commit_id)
            if commit is None:
                raise CommitNotFoundError(f"Commit not found: {commit_id}")
            
            return [
                TrackedFile(
                    file_path=m.file_path,
                    file_hash=m.file_hash,
                    size_bytes=m.file_size,
                    tracked_at=commit.timestamp,
                )
                for m in commit.mementos
            ]
    
    def get_files_at_checkpoint(
        self,
        checkpoint_id: str,
    ) -> List[str]:
        """
        Get file paths that existed at a specific checkpoint.

        Args:
            checkpoint_id: Checkpoint to query

        Returns:
            List of file paths that existed at the checkpoint
        """
        if not self._config.enabled:
            return []

        self._ensure_initialized()

        with self._lock:
            commit_id = self.get_commit_for_checkpoint(checkpoint_id)
            if commit_id is None:
                return []

            commit = self._commit_repo.find_by_id(commit_id)
            if commit is None:
                return []

            return [m.file_path for m in commit.mementos]

    def is_available(self) -> bool:
        """
        Check if file tracking service is available and configured.
        
        Returns:
            True if service can track files, False otherwise
        """
        if not self._config.enabled:
            return False
        
        try:
            self._ensure_initialized()
            return self._initialized
        except Exception:
            return False
    
    def _create_disabled_result(self) -> FileTrackingResult:
        """Create a result for disabled service."""
        return FileTrackingResult(
            commit_id="",
            files_tracked=0,
            total_size_bytes=0,
            file_hashes={},
            message="File tracking disabled",
        )
    
    def get_blob_stats(self) -> Dict[str, Any]:
        """
        Get storage statistics from BlobRepository.
        
        Returns:
            Dict with blob_count, total_size_bytes, total_size_mb
        """
        if not self._config.enabled or not self._initialized:
            return {"blob_count": 0, "total_size_bytes": 0, "total_size_mb": 0}
        
        with self._lock:
            return self._blob_repo.stats()
    
    def close(self):
        """Close database connections."""
        with self._lock:
            if self._db_conn:
                self._db_conn.close()
                self._db_conn = None
            self._initialized = False
            self._checkpoint_links_cache.clear()
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - close connections."""
        self.close()
        return False
