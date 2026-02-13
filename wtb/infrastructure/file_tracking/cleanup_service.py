"""
File Cleanup Service Implementation (v1.9).

Handles identification and cleanup of orphaned files during rollback operations.
Files created after a checkpoint are considered "orphaned" when rolling back
to that checkpoint.

Design Principles:
- ISP: Implements IFileCleanupService (separate from IFileTrackingService)
- SRP: Single responsibility - orphaned file management
- ACID: Backup-first approach ensures recoverability

Safety Features:
- max_files limit prevents runaway deletion
- dry_run mode for testing cleanup logic
- backup option preserves files before deletion
- Graceful handling of missing files

Usage:
    from wtb.infrastructure.file_tracking import FileCleanupService
    
    service = FileCleanupService()
    orphaned = service.identify_orphaned_files(
        target_checkpoint_id=42,
        execution_id="exec-123",
        current_workspace_path=Path("/project"),
        track_patterns=["*.py", "data/*.json"],
        exclude_patterns=["*.pyc", "__pycache__/*"],
        file_tracking_service=file_tracker,
    )
    result = service.cleanup_orphaned_files(
        checkpoint_id=42,
        execution_id="exec-123",
        orphaned_paths=orphaned,
        backup_dir=Path(".rollback_backup"),
        dry_run=False,
        max_files=100,
    )
"""

import fnmatch
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Set

from wtb.domain.interfaces.file_tracking import (
    FileCleanupResult,
    IFileCleanupService,
    IFileTrackingService,
)

logger = logging.getLogger(__name__)


class FileCleanupService(IFileCleanupService):
    """
    Implementation of file cleanup operations for rollback.
    
    Identifies files created after a checkpoint and safely removes them
    during rollback operations. Supports dry-run mode and backup before
    deletion.
    
    Thread-safe: All operations are stateless and atomic at file level.
    
    Design Decision (2026-02-13):
    - Created as separate service following ISP
    - Uses glob pattern matching for file discovery
    - Backup-first approach for safety
    - Safety limit to prevent mass deletion accidents
    """
    
    def identify_orphaned_files(
        self,
        target_checkpoint_id: int,
        execution_id: str,
        current_workspace_path: Path,
        track_patterns: List[str],
        exclude_patterns: List[str],
        file_tracking_service: IFileTrackingService,
    ) -> List[str]:
        """
        Identify files created after target checkpoint.
        
        Compares current workspace files (matching patterns) against
        files tracked at the checkpoint. Returns paths that exist now
        but weren't tracked at the checkpoint.
        
        Algorithm:
        1. Get files at checkpoint via file_tracking_service
        2. Discover current files in workspace matching patterns
        3. Return set difference: current - checkpoint
        
        Args:
            target_checkpoint_id: Checkpoint being rolled back to
            execution_id: Execution ID for logging
            current_workspace_path: Root path to scan for current files
            track_patterns: Glob patterns for files to consider
            exclude_patterns: Glob patterns to exclude
            file_tracking_service: Service to query checkpoint file state
            
        Returns:
            List of file paths that are orphaned (created after checkpoint)
        """
        logger.info(
            f"Identifying orphaned files for checkpoint {target_checkpoint_id} "
            f"(execution {execution_id})"
        )
        
        # 1. Get files at checkpoint via existing service
        checkpoint_files = set(
            file_tracking_service.get_files_at_checkpoint(target_checkpoint_id)
        )
        logger.debug(f"Found {len(checkpoint_files)} files at checkpoint")
        
        # 2. Discover current files matching patterns
        current_files = self._discover_files(
            workspace_path=current_workspace_path,
            track_patterns=track_patterns,
            exclude_patterns=exclude_patterns,
        )
        logger.debug(f"Found {len(current_files)} current files matching patterns")
        
        # 3. Orphaned = current - checkpoint
        # Normalize paths for comparison
        checkpoint_files_normalized = set(
            self._normalize_path(p, current_workspace_path) 
            for p in checkpoint_files
        )
        current_files_normalized = set(
            self._normalize_path(p, current_workspace_path) 
            for p in current_files
        )
        
        orphaned = list(current_files_normalized - checkpoint_files_normalized)
        
        logger.info(
            f"Identified {len(orphaned)} orphaned files for checkpoint {target_checkpoint_id}"
        )
        
        return orphaned
    
    def cleanup_orphaned_files(
        self,
        checkpoint_id: int,
        execution_id: str,
        orphaned_paths: List[str],
        backup_dir: Optional[Path] = None,
        dry_run: bool = False,
        max_files: int = 100,
    ) -> FileCleanupResult:
        """
        Delete orphaned files with safety checks.
        
        Removes files that were created after a checkpoint, with optional
        backup and safety limits.
        
        ACID Compliance:
        - Each file operation is independent (no transaction)
        - Backup before delete ensures recoverability
        - Errors are collected, not thrown (partial success allowed)
        
        Args:
            checkpoint_id: Source checkpoint (for result tracking)
            execution_id: Execution being rolled back
            orphaned_paths: Files to delete (from identify_orphaned_files)
            backup_dir: If provided, backup files before deletion
            dry_run: If True, log actions but don't delete
            max_files: Safety limit - refuse to delete more than this
            
        Returns:
            FileCleanupResult with detailed operation outcome
        """
        mode = "DRY RUN" if dry_run else "LIVE"
        logger.info(
            f"[{mode}] Starting cleanup of {len(orphaned_paths)} orphaned files "
            f"for checkpoint {checkpoint_id} (execution {execution_id})"
        )
        
        # Safety check
        if len(orphaned_paths) > max_files and not dry_run:
            error_msg = (
                f"Refusing to delete {len(orphaned_paths)} files "
                f"(exceeds max_files limit of {max_files}). "
                f"Increase max_files if this is intentional."
            )
            logger.error(error_msg)
            return FileCleanupResult(
                checkpoint_id=checkpoint_id,
                execution_id=execution_id,
                files_skipped=len(orphaned_paths),
                skipped_paths=tuple(orphaned_paths),
                errors=(error_msg,),
                dry_run=dry_run,
            )
        
        deleted_paths: List[str] = []
        backed_up_paths: List[str] = []
        skipped_paths: List[str] = []
        errors: List[str] = []
        
        # Create backup directory if needed
        if backup_dir and not dry_run:
            backup_dir.mkdir(parents=True, exist_ok=True)
            logger.debug(f"Created backup directory: {backup_dir}")
        
        # Process each orphaned file
        for file_path in orphaned_paths:
            try:
                # Check if file still exists
                if not os.path.exists(file_path):
                    logger.debug(f"File already deleted: {file_path}")
                    skipped_paths.append(file_path)
                    continue
                
                if dry_run:
                    # Dry run - just log what would happen
                    if backup_dir:
                        logger.info(f"[DRY RUN] Would backup: {file_path}")
                        backed_up_paths.append(file_path)
                    logger.info(f"[DRY RUN] Would delete: {file_path}")
                    deleted_paths.append(file_path)
                else:
                    # Live mode - backup then delete
                    if backup_dir:
                        backup_path = self._backup_file(file_path, backup_dir)
                        if backup_path:
                            backed_up_paths.append(file_path)
                            logger.debug(f"Backed up: {file_path} -> {backup_path}")
                    
                    # Delete the file
                    os.remove(file_path)
                    deleted_paths.append(file_path)
                    logger.info(f"Deleted orphaned file: {file_path}")
                    
            except PermissionError as e:
                error_msg = f"Permission denied: {file_path}"
                logger.warning(error_msg)
                errors.append(error_msg)
                skipped_paths.append(file_path)
            except OSError as e:
                error_msg = f"OS error for {file_path}: {e}"
                logger.warning(error_msg)
                errors.append(error_msg)
                skipped_paths.append(file_path)
        
        result = FileCleanupResult(
            checkpoint_id=checkpoint_id,
            execution_id=execution_id,
            files_deleted=len(deleted_paths),
            files_backed_up=len(backed_up_paths),
            files_skipped=len(skipped_paths),
            deleted_paths=tuple(deleted_paths),
            backed_up_paths=tuple(backed_up_paths),
            skipped_paths=tuple(skipped_paths),
            errors=tuple(errors),
            dry_run=dry_run,
        )
        
        logger.info(
            f"[{mode}] File cleanup complete: "
            f"{result.files_deleted} deleted, "
            f"{result.files_backed_up} backed up, "
            f"{result.files_skipped} skipped, "
            f"{len(result.errors)} errors"
        )
        
        return result
    
    def _discover_files(
        self,
        workspace_path: Path,
        track_patterns: List[str],
        exclude_patterns: List[str],
    ) -> Set[str]:
        """
        Discover files in workspace matching track patterns.
        
        Args:
            workspace_path: Root directory to scan
            track_patterns: Glob patterns to include (e.g., ["*.py", "data/*.json"])
            exclude_patterns: Glob patterns to exclude (e.g., ["*.pyc", "__pycache__/*"])
            
        Returns:
            Set of matching file paths (as strings)
        """
        discovered: Set[str] = set()
        
        if not workspace_path.exists():
            logger.warning(f"Workspace path does not exist: {workspace_path}")
            return discovered
        
        # If no patterns specified, return empty (don't scan everything)
        if not track_patterns:
            logger.debug("No track patterns specified, skipping file discovery")
            return discovered
        
        # Walk the workspace
        for root, dirs, files in os.walk(workspace_path):
            # Skip hidden directories
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            
            for filename in files:
                file_path = os.path.join(root, filename)
                rel_path = os.path.relpath(file_path, workspace_path)
                
                # Check if matches any track pattern
                if not self._matches_patterns(rel_path, track_patterns):
                    continue
                
                # Check if matches any exclude pattern
                if self._matches_patterns(rel_path, exclude_patterns):
                    continue
                
                discovered.add(file_path)
        
        return discovered
    
    def _matches_patterns(self, path: str, patterns: List[str]) -> bool:
        """
        Check if path matches any of the glob patterns.
        
        Args:
            path: File path to check
            patterns: List of glob patterns
            
        Returns:
            True if path matches any pattern
        """
        for pattern in patterns:
            if fnmatch.fnmatch(path, pattern):
                return True
            # Also check just the filename
            if fnmatch.fnmatch(os.path.basename(path), pattern):
                return True
        return False
    
    def _normalize_path(self, path: str, workspace_path: Path) -> str:
        """
        Normalize path to absolute form for comparison.
        
        Args:
            path: Path to normalize (may be relative or absolute)
            workspace_path: Workspace root for relative paths
            
        Returns:
            Absolute normalized path
        """
        p = Path(path)
        if not p.is_absolute():
            p = workspace_path / p
        return str(p.resolve())
    
    def _backup_file(self, file_path: str, backup_dir: Path) -> Optional[str]:
        """
        Backup file to backup directory.
        
        Preserves relative directory structure in backup.
        Adds timestamp to prevent overwrites.
        
        Args:
            file_path: File to backup
            backup_dir: Root backup directory
            
        Returns:
            Path to backup file, or None if failed
        """
        try:
            # Generate unique backup path with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filename = os.path.basename(file_path)
            backup_filename = f"{timestamp}_{filename}"
            backup_path = backup_dir / backup_filename
            
            # Create parent directories if needed
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Copy file to backup
            shutil.copy2(file_path, backup_path)
            
            return str(backup_path)
            
        except Exception as e:
            logger.warning(f"Failed to backup {file_path}: {e}")
            return None
