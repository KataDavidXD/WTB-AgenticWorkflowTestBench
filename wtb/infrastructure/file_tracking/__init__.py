"""
File Tracking Infrastructure.

Provides implementations of IFileTrackingService for different deployment modes.

Implementations:
- MockFileTrackingService: In-memory mock for testing (no real file restore)
- SqliteFileTrackingService: Lightweight SQLite-based for single-run execution (NEW)
- FileTrackerService: Real FileTracker integration (PostgreSQL)
- RayFileTrackerService: Ray-compatible wrapper with serializable config
- FileCleanupService: Rollback file cleanup (v1.9)

The services integrate with the FileTracker project at:
  file_processing/file_processing/FileTracker/

Usage:
    # For testing (no real file operations)
    from wtb.infrastructure.file_tracking import MockFileTrackingService
    service = MockFileTrackingService()
    
    # For single-run execution (SQLite, no PostgreSQL)
    from wtb.infrastructure.file_tracking import SqliteFileTrackingService
    from pathlib import Path
    
    service = SqliteFileTrackingService(
        workspace_path=Path("./workspace"),
        db_name="filetrack.db"
    )
    
    # For production with real FileTracker (PostgreSQL)
    from wtb.infrastructure.file_tracking import FileTrackerService
    from wtb.config import FileTrackingConfig
    
    config = FileTrackingConfig.for_production(
        postgres_url="postgresql://user:pass@localhost/filetracker",
        storage_path="/data/file_storage",
        wtb_db_url="postgresql://...",
    )
    service = FileTrackerService(config)
    
    # For Ray actors
    from wtb.infrastructure.file_tracking import RayFileTrackerService
    service = RayFileTrackerService(config.to_dict())
    
    # For rollback cleanup (v1.9)
    from wtb.infrastructure.file_tracking import FileCleanupService
    cleanup_service = FileCleanupService()
"""

from wtb.infrastructure.file_tracking.mock_service import MockFileTrackingService
from wtb.infrastructure.file_tracking.sqlite_service import SqliteFileTrackingService
from wtb.infrastructure.file_tracking.filetracker_service import FileTrackerService
from wtb.infrastructure.file_tracking.ray_filetracker_service import RayFileTrackerService
from wtb.infrastructure.file_tracking.cleanup_service import FileCleanupService

__all__ = [
    "MockFileTrackingService",
    "SqliteFileTrackingService",
    "FileTrackerService",
    "RayFileTrackerService",
    "FileCleanupService",
]
