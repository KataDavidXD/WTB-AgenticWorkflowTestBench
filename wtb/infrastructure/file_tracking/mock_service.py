"""
Mock File Tracking Service.

In-memory implementation of IFileTrackingService for testing.
Does not require FileTracker or database connections.

Design Principles:
- Provides full interface implementation for testing
- Thread-safe with lock protection
- Tracks all operations for verification in tests

Usage:
    from wtb.infrastructure.file_tracking import MockFileTrackingService
    
    service = MockFileTrackingService()
    
    # Track files
    result = service.track_files(["data/output.csv"])
    
    # Verify tracking
    assert service.get_commit_count() == 1
    assert "data/output.csv" in service.get_all_tracked_paths()
"""

import hashlib
import os
import threading
import uuid
from datetime import datetime
from typing import Dict, List, Optional

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


class MockFileTrackingService(IFileTrackingService):
    """
    In-memory mock implementation of IFileTrackingService.
    
    Provides full interface implementation for unit testing without
    requiring FileTracker database or file system operations.
    
    Thread-safe with RLock protection for all state mutations.
    
    Features:
    - Simulates file hashing (uses path-based fake hash if file doesn't exist)
    - Tracks checkpoint-commit links
    - Records all operations for test verification
    - Supports restore simulation
    
    Attributes:
        _commits: Dict mapping commit_id to commit data
        _checkpoint_links: Dict mapping checkpoint_id to commit_id
        _tracked_files: Dict mapping commit_id to list of TrackedFile
        _operation_log: List of operation records for verification
    """
    
    def __init__(self, simulate_file_existence: bool = False):
        """
        Initialize mock service.
        
        Args:
            simulate_file_existence: If True, files must exist for tracking.
                                     If False, fake hashes are generated.
        """
        self._simulate_file_existence = simulate_file_existence
        self._lock = threading.RLock()
        
        # State storage
        self._commits: Dict[str, Dict] = {}  # commit_id -> commit data
        self._checkpoint_links: Dict[int, str] = {}  # checkpoint_id -> commit_id
        self._tracked_files: Dict[str, List[TrackedFile]] = {}  # commit_id -> files
        
        # Operation log for test verification
        self._operation_log: List[Dict] = []
    
    def _log_operation(self, operation: str, **kwargs):
        """Log operation for test verification."""
        self._operation_log.append({
            "operation": operation,
            "timestamp": datetime.now().isoformat(),
            **kwargs,
        })
    
    def _generate_hash(self, file_path: str) -> str:
        """
        Generate hash for a file.
        
        If simulate_file_existence is True, reads actual file.
        Otherwise, generates deterministic fake hash from path.
        """
        if self._simulate_file_existence and os.path.exists(file_path):
            with open(file_path, 'rb') as f:
                return hashlib.sha256(f.read()).hexdigest()
        else:
            # Generate deterministic fake hash from path
            return hashlib.sha256(file_path.encode()).hexdigest()
    
    def _get_file_size(self, file_path: str) -> int:
        """Get file size or simulate it."""
        if self._simulate_file_existence and os.path.exists(file_path):
            return os.path.getsize(file_path)
        else:
            # Return fake size based on path length
            return len(file_path) * 100
    
    def track_files(
        self,
        file_paths: List[str],
        message: Optional[str] = None,
    ) -> FileTrackingResult:
        """
        Track specified files and create a commit.
        
        Args:
            file_paths: List of file paths to track
            message: Optional commit message
            
        Returns:
            FileTrackingResult with commit info
        """
        with self._lock:
            if self._simulate_file_existence:
                for path in file_paths:
                    if not os.path.exists(path):
                        from wtb.domain.interfaces.file_tracking import FileNotFoundError as FTFileNotFoundError
                        raise FTFileNotFoundError(f"File not found: {path}")
            
            commit_id = str(uuid.uuid4())
            tracked = []
            file_hashes = {}
            total_size = 0
            
            for path in file_paths:
                file_hash = self._generate_hash(path)
                size = self._get_file_size(path)
                
                tracked_file = TrackedFile(
                    file_path=path,
                    file_hash=file_hash,
                    size_bytes=size,
                    tracked_at=datetime.now(),
                )
                tracked.append(tracked_file)
                file_hashes[path] = file_hash
                total_size += size
            
            # Store commit
            self._commits[commit_id] = {
                "message": message,
                "created_at": datetime.now(),
                "files": file_paths,
            }
            self._tracked_files[commit_id] = tracked
            
            result = FileTrackingResult(
                commit_id=commit_id,
                files_tracked=len(file_paths),
                total_size_bytes=total_size,
                file_hashes=file_hashes,
                message=message,
            )
            
            self._log_operation(
                "track_files",
                commit_id=commit_id,
                file_count=len(file_paths),
                file_paths=file_paths,
            )
            
            return result
    
    def track_and_link(
        self,
        checkpoint_id: str,
        file_paths: List[str],
        message: Optional[str] = None,
    ) -> FileTrackingResult:
        """
        Track files AND link to checkpoint in single operation.
        
        Args:
            checkpoint_id: WTB checkpoint ID to link to
            file_paths: Files to track
            message: Optional commit message
            
        Returns:
            FileTrackingResult with commit info
        """
        with self._lock:
            # Track files
            result = self.track_files(file_paths, message)
            
            # Create link
            self._checkpoint_links[checkpoint_id] = result.commit_id
            
            self._log_operation(
                "track_and_link",
                checkpoint_id=checkpoint_id,
                commit_id=result.commit_id,
                file_count=len(file_paths),
            )
            
            return result
    
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
        with self._lock:
            if commit_id not in self._commits:
                raise CommitNotFoundError(f"Commit not found: {commit_id}")
            
            self._checkpoint_links[checkpoint_id] = commit_id
            
            tracked = self._tracked_files.get(commit_id, [])
            total_size = sum(f.size_bytes for f in tracked)
            
            link = FileTrackingLink(
                checkpoint_id=checkpoint_id,
                commit_id=commit_id,
                linked_at=datetime.now(),
                file_count=len(tracked),
                total_size_bytes=total_size,
            )
            
            self._log_operation(
                "link_to_checkpoint",
                checkpoint_id=checkpoint_id,
                commit_id=commit_id,
            )
            
            return link
    
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
        with self._lock:
            commit_id = self._checkpoint_links.get(checkpoint_id)
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
            if commit_id not in self._commits:
                raise CommitNotFoundError(f"Commit not found: {commit_id}")
            
            tracked = self._tracked_files.get(commit_id, [])
            paths = [f.file_path for f in tracked]
            total_size = sum(f.size_bytes for f in tracked)
            
            result = FileRestoreResult(
                commit_id=commit_id,
                files_restored=len(tracked),
                total_size_bytes=total_size,
                restored_paths=paths,
                success=True,
            )
            
            self._log_operation(
                "restore_commit",
                commit_id=commit_id,
                files_restored=len(tracked),
            )
            
            return result
    
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
        with self._lock:
            return self._checkpoint_links.get(checkpoint_id)
    
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
            if commit_id not in self._commits:
                raise CommitNotFoundError(f"Commit not found: {commit_id}")
            
            return self._tracked_files.get(commit_id, []).copy()
    
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
        with self._lock:
            commit_id = self._checkpoint_links.get(checkpoint_id)
            if commit_id is None:
                return []
            tracked = self._tracked_files.get(commit_id, [])
            return [f.file_path for f in tracked]

    def is_available(self) -> bool:
        """
        Check if file tracking service is available.
        
        Returns:
            Always True for mock service
        """
        return True
    
    # ═══════════════════════════════════════════════════════════════
    # Test Helper Methods
    # ═══════════════════════════════════════════════════════════════
    
    def get_commit_count(self) -> int:
        """Get total number of commits (for testing)."""
        with self._lock:
            return len(self._commits)
    
    def get_link_count(self) -> int:
        """Get total number of checkpoint links (for testing)."""
        with self._lock:
            return len(self._checkpoint_links)
    
    def get_all_tracked_paths(self) -> List[str]:
        """Get all tracked file paths (for testing)."""
        with self._lock:
            paths = []
            for tracked in self._tracked_files.values():
                paths.extend(f.file_path for f in tracked)
            return paths
    
    def get_operation_log(self) -> List[Dict]:
        """Get operation log (for testing)."""
        with self._lock:
            return self._operation_log.copy()
    
    def clear(self):
        """Clear all state (for testing)."""
        with self._lock:
            self._commits.clear()
            self._checkpoint_links.clear()
            self._tracked_files.clear()
            self._operation_log.clear()
    
    def get_commit(self, commit_id: str) -> Optional[Dict]:
        """Get commit data by ID (for testing)."""
        with self._lock:
            return self._commits.get(commit_id)
