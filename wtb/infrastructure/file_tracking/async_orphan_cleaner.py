
import logging
import time
from collections.abc import Callable
from pathlib import Path

import aiofiles.os

from wtb.domain.interfaces.async_unit_of_work import IAsyncUnitOfWork

logger = logging.getLogger(__name__)


class AsyncBlobOrphanCleaner:
    """
    Async orphan blob detection and cleanup.
    """
    
    def __init__(
        self,
        blobs_dir: Path,
        uow_factory: Callable[[], IAsyncUnitOfWork],
        grace_period_minutes: int = 10,
    ):
        self._blobs_dir = blobs_dir
        self._uow_factory = uow_factory
        self._grace_period_minutes = grace_period_minutes
    
    async def aclean_orphans(self, dry_run: bool = True) -> list[str]:
        """
        Find and optionally delete orphaned blob files.
        """
        orphans = []
        
        # Get all blobs from filesystem
        fs_blobs = await self._list_blob_files_async()
        logger.info(f"Found {len(fs_blobs)} blob files on filesystem")
        
        # Get all known blobs from DB
        async with self._uow_factory() as uow:
            known_hashes_list = await uow.blobs.alist_all_hashes()
            known_hashes: set[str] = set(h.value for h in known_hashes_list)
        logger.info(f"Found {len(known_hashes)} blob hashes in database")
        
        # Find orphans (on filesystem but not in DB)
        for blob_path in fs_blobs:
            blob_hash = self._extract_hash(blob_path)
            if blob_hash not in known_hashes:
                # Check grace period (skip recently created files)
                if await self._is_within_grace_period(blob_path):
                    logger.debug(f"Skipping recent blob: {blob_hash}")
                    continue
                
                orphans.append(str(blob_path))
                if not dry_run:
                    await aiofiles.os.remove(blob_path)
                    logger.info(f"Deleted orphan blob: {blob_hash}")
        
        logger.info(
            f"Found {len(orphans)} orphaned blobs "
            f"({'DRY RUN - not deleted' if dry_run else 'DELETED'})"
        )
        return orphans
    
    async def _list_blob_files_async(self) -> list[Path]:
        """List all blob files in storage directory."""
        blobs = []
        if not self._blobs_dir.exists():
            return blobs
        
        # Blob storage uses sharding: blobs_dir/ab/cdef123...
        # Iteration via Path is synchronous, but fast for directory listing usually.
        # For async directory listing, we can use aiofiles.os.scandir if available (newer versions)
        # or stick to sync pathlib for simplicity if directory size is manageable.
        # Given "Async", ideally we shouldn't block.
        # But aiofiles.os doesn't have listdir/scandir widely available/consistent across versions?
        # Actually aiofiles.os.scandir was added recently.
        # Let's use sync pathlib for now as listing directories is rarely the bottleneck compared to I/O
        # unless millions of files.
        
        for prefix_dir in self._blobs_dir.iterdir():
            if prefix_dir.is_dir() and len(prefix_dir.name) == 2:
                for blob_file in prefix_dir.iterdir():
                    if blob_file.is_file():
                        blobs.append(blob_file)
        return blobs
    
    def _extract_hash(self, blob_path: Path) -> str:
        """Extract blob hash from path: prefix_dir/rest → prefix + rest."""
        return blob_path.parent.name + blob_path.name
    
    async def _is_within_grace_period(self, blob_path: Path) -> bool:
        """Check if blob was created within grace period."""
        stat = await aiofiles.os.stat(blob_path)
        age_minutes = (time.time() - stat.st_ctime) / 60
        return age_minutes < self._grace_period_minutes
