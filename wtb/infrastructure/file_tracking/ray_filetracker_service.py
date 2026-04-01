"""
Ray-compatible FileTracker Service.

Wrapper around FileTrackerService with serializable configuration
for use in Ray actors. Handles lazy initialization to avoid
serialization issues when passing to Ray workers.

Design Principles:
- Serializable: Config passed as dict, service created on worker
- Lazy init: Heavy deps initialized on first use in actor
- Thread-safe: Each actor gets isolated instance

Architecture:
- RayBatchTestRunner passes FileTrackingConfig.to_dict() to actors
- VariantExecutionActor creates RayFileTrackerService from dict
- Service lazily initializes FileTrackerService on first operation

Usage:
    # In Ray actor initialization
    @ray.remote
    class VariantExecutionActor:
        def __init__(self, filetracker_config: Optional[Dict] = None):
            self._file_tracking = None
            if filetracker_config and filetracker_config.get("enabled"):
                self._file_tracking = RayFileTrackerService(filetracker_config)
        
        def _track_output_files(self, checkpoint_id: str, files: List[str]):
            if self._file_tracking:
                return self._file_tracking.track_and_link(checkpoint_id, files)
"""

import logging
from typing import Dict, List, Optional, Any

from wtb.config import FileTrackingConfig
from wtb.domain.interfaces.file_tracking import (
    IFileTrackingService,
    TrackedFile,
    FileTrackingResult,
    FileRestoreResult,
    FileTrackingLink,
    FileTrackingError,
)

logger = logging.getLogger(__name__)


class RayFileTrackerService(IFileTrackingService):
    """
    Ray-compatible wrapper for FileTrackerService.
    
    Takes serializable dict config and lazily creates FileTrackerService
    on first operation. Designed for use in Ray actors where heavy
    dependencies must be initialized inside the actor process.
    
    Key Design Decisions:
    - Config stored as dict (serializable for Ray transmission)
    - FileTrackerService created lazily (avoids serialization issues)
    - Thread-safe (each actor gets isolated service instance)
    - Falls back gracefully when FileTracker unavailable
    
    Serialization:
    - Only config dict is serialized when actor is created
    - Database connections created inside actor process
    - No pickle of complex objects across Ray boundary
    
    Attributes:
        _config_dict: Serializable configuration dictionary
        _service: Lazily initialized FileTrackerService
        _initialized: Flag to track initialization state
    """
    
    def __init__(self, config_dict: Dict[str, Any]):
        """
        Initialize with serializable config dict.
        
        Args:
            config_dict: FileTrackingConfig.to_dict() output
        """
        self._config_dict = config_dict
        self._service: Optional["FileTrackerService"] = None
        self._initialized = False
        self._initialization_error: Optional[str] = None
    
    def _ensure_initialized(self):
        """
        Lazily initialize FileTrackerService.
        
        Called on first operation. Creates FileTrackerService
        with config reconstructed from dict.
        """
        if self._initialized:
            return
        
        if self._initialization_error:
            # Don't retry failed initialization
            raise FileTrackingError(self._initialization_error)
        
        try:
            # Reconstruct config from dict
            config = FileTrackingConfig.from_dict(self._config_dict)
            
            if not config.enabled:
                self._initialized = True
                return
            
            # Import and create service
            from wtb.infrastructure.file_tracking.filetracker_service import FileTrackerService
            self._service = FileTrackerService(config)
            
            self._initialized = True
            logger.info("RayFileTrackerService initialized successfully")
            
        except Exception as e:
            self._initialization_error = str(e)
            logger.error(f"RayFileTrackerService initialization failed: {e}")
            raise FileTrackingError(f"Failed to initialize: {e}") from e
    
    @property
    def enabled(self) -> bool:
        """Check if file tracking is enabled in config."""
        return self._config_dict.get("enabled", False)
    
    def track_files(
        self,
        file_paths: List[str],
        message: Optional[str] = None,
    ) -> FileTrackingResult:
        """
        Track specified files and create a commit.
        
        Delegates to underlying FileTrackerService.
        """
        if not self.enabled:
            return self._create_disabled_result()
        
        self._ensure_initialized()
        
        if self._service is None:
            return self._create_disabled_result()
        
        return self._service.track_files(file_paths, message)
    
    def track_and_link(
        self,
        checkpoint_id: str,
        file_paths: List[str],
        message: Optional[str] = None,
    ) -> FileTrackingResult:
        """
        Track files AND link to checkpoint in single operation.
        """
        if not self.enabled:
            return self._create_disabled_result()
        
        self._ensure_initialized()
        
        if self._service is None:
            return self._create_disabled_result()
        
        return self._service.track_and_link(checkpoint_id, file_paths, message)
    
    def link_to_checkpoint(
        self,
        checkpoint_id: str,
        commit_id: str,
    ) -> FileTrackingLink:
        """
        Link existing commit to checkpoint.
        """
        if not self.enabled:
            from wtb.domain.interfaces.file_tracking import CheckpointLinkError
            raise CheckpointLinkError("File tracking is disabled")
        
        self._ensure_initialized()
        
        if self._service is None:
            from wtb.domain.interfaces.file_tracking import CheckpointLinkError
            raise CheckpointLinkError("FileTracker service not available")
        
        return self._service.link_to_checkpoint(checkpoint_id, commit_id)
    
    def restore_from_checkpoint(
        self,
        checkpoint_id: str,
    ) -> FileRestoreResult:
        """
        Restore files from checkpoint's linked commit.
        """
        if not self.enabled:
            return FileRestoreResult(
                commit_id="",
                files_restored=0,
                total_size_bytes=0,
                restored_paths=[],
                success=False,
                error_message="File tracking is disabled",
            )
        
        self._ensure_initialized()
        
        if self._service is None:
            return FileRestoreResult(
                commit_id="",
                files_restored=0,
                total_size_bytes=0,
                restored_paths=[],
                success=False,
                error_message="FileTracker service not available",
            )
        
        return self._service.restore_from_checkpoint(checkpoint_id)
    
    def restore_commit(
        self,
        commit_id: str,
    ) -> FileRestoreResult:
        """
        Restore files from a specific commit.
        """
        if not self.enabled:
            return FileRestoreResult(
                commit_id=commit_id,
                files_restored=0,
                total_size_bytes=0,
                restored_paths=[],
                success=False,
                error_message="File tracking is disabled",
            )
        
        self._ensure_initialized()
        
        if self._service is None:
            return FileRestoreResult(
                commit_id=commit_id,
                files_restored=0,
                total_size_bytes=0,
                restored_paths=[],
                success=False,
                error_message="FileTracker service not available",
            )
        
        return self._service.restore_commit(commit_id)
    
    def get_commit_for_checkpoint(
        self,
        checkpoint_id: str,
    ) -> Optional[str]:
        """
        Get the file commit ID linked to a checkpoint.
        """
        if not self.enabled:
            return None
        
        try:
            self._ensure_initialized()
        except FileTrackingError:
            return None
        
        if self._service is None:
            return None
        
        return self._service.get_commit_for_checkpoint(checkpoint_id)
    
    def get_tracked_files(
        self,
        commit_id: str,
    ) -> List[TrackedFile]:
        """
        Get list of tracked files for a commit.
        """
        if not self.enabled:
            return []
        
        self._ensure_initialized()
        
        if self._service is None:
            return []
        
        return self._service.get_tracked_files(commit_id)
    
    def get_files_at_checkpoint(self, checkpoint_id: str) -> List[str]:
        """Get file paths that existed at a specific checkpoint."""
        if not self.enabled:
            return []
        self._ensure_initialized()
        if self._service is None:
            return []
        return self._service.get_files_at_checkpoint(checkpoint_id)

    def is_available(self) -> bool:
        """
        Check if file tracking service is available.
        """
        if not self.enabled:
            return False


        try:
            self._ensure_initialized()
            return self._service is not None and self._service.is_available()
        except FileTrackingError:
            return False
    
    def _create_disabled_result(self) -> FileTrackingResult:
        """Create a result for disabled/unavailable service."""
        return FileTrackingResult(
            commit_id="",
            files_tracked=0,
            total_size_bytes=0,
            file_hashes={},
            message="File tracking disabled or unavailable",
        )
    
    def close(self):
        """Close underlying service if initialized."""
        if self._service is not None:
            self._service.close()
            self._service = None
        self._initialized = False


def create_file_tracking_service(
    config: Optional[FileTrackingConfig] = None,
    config_dict: Optional[Dict[str, Any]] = None,
) -> IFileTrackingService:
    """
    Factory function to create appropriate file tracking service.
    
    Selects implementation based on config:
    - If config.enabled is False: MockFileTrackingService
    - If in Ray worker (config_dict provided): RayFileTrackerService
    - Otherwise: FileTrackerService
    
    Args:
        config: FileTrackingConfig object
        config_dict: Serialized config dict (for Ray workers)
        
    Returns:
        IFileTrackingService implementation
    """
    from wtb.infrastructure.file_tracking.mock_service import MockFileTrackingService
    
    # If dict provided, use Ray-compatible service
    if config_dict is not None:
        if not config_dict.get("enabled", False):
            return MockFileTrackingService()
        return RayFileTrackerService(config_dict)
    
    # If config provided, use appropriate service
    if config is not None:
        if not config.enabled:
            return MockFileTrackingService()
        
        from wtb.infrastructure.file_tracking.filetracker_service import FileTrackerService
        return FileTrackerService(config)
    
    # Default to mock
    return MockFileTrackingService()
