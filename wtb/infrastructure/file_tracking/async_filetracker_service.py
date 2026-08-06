
import logging
import os
import stat
from collections.abc import Callable
from pathlib import Path

import aiofiles
import aiofiles.os

from wtb.domain.interfaces.async_file_tracking import (
    FileTrackingResult,
    IAsyncFileTrackingService,
)
from wtb.domain.interfaces.async_unit_of_work import IAsyncUnitOfWork
from wtb.domain.models.file_processing import (
    CheckpointFileLink,
    DuplicateFileError,
    FileCommit,
    FileMemento,
)

logger = logging.getLogger(__name__)

class AsyncFileTrackerService(IAsyncFileTrackingService):
    """
    Async File Tracking Service using Repository Pattern.
    
    ARCHITECTURE ALIGNMENT (FILE_TRACKING_ARCHITECTURE_DECISION.md):
    - Uses IAsyncBlobRepository for content storage
    - Uses IAsyncFileCommitRepository for commit management  
    - Uses IAsyncCheckpointFileLinkRepository for checkpoint links
    - All operations through UoW for ACID compliance
    """
    
    def __init__(
        self,
        uow_factory: Callable[[], IAsyncUnitOfWork],
    ):
        self._uow_factory = uow_factory
    
    async def _path_exists(self, path: Path) -> bool:
        """
        Check if path exists asynchronously.
        """
        try:
            await aiofiles.os.stat(path)
            return True
        except FileNotFoundError:
            return False

    async def _aprepare_files(
        self,
        file_paths: list[str],
    ) -> list[tuple[Path, bytes]]:
        """Validate and read every input before opening the storage UoW."""
        validated_paths: list[Path] = []
        seen_paths = set()

        for file_path in file_paths:
            if not isinstance(file_path, (str, os.PathLike)):
                raise TypeError("file_path must be a string or path-like value")
            if not str(file_path).strip():
                raise ValueError("file_path cannot be empty")

            path = Path(file_path)
            try:
                path_stat = await aiofiles.os.stat(path)
            except FileNotFoundError as exc:
                raise FileNotFoundError(f"File not found: {file_path}") from exc

            if not stat.S_ISREG(path_stat.st_mode):
                raise ValueError(f"Path is not a regular file: {file_path}")

            try:
                canonical_path = path.resolve(strict=True)
            except FileNotFoundError as exc:
                raise FileNotFoundError(f"File not found: {file_path}") from exc
            duplicate_key = os.path.normcase(str(canonical_path))
            if duplicate_key in seen_paths:
                raise DuplicateFileError(f"Duplicate file path: {file_path}")
            seen_paths.add(duplicate_key)
            validated_paths.append(path)

        # Read the complete request before the first CAS write. A file that is
        # removed or becomes unreadable after stat therefore still fails with
        # zero blob, commit, or checkpoint-link writes.
        prepared_files = []
        for path in validated_paths:
            async with aiofiles.open(path, "rb") as file_handle:
                prepared_files.append((path, await file_handle.read()))
        return prepared_files
    
    async def atrack_files(
        self,
        file_paths: list[str],
        message: str,
        checkpoint_id: str | None = None,
    ) -> FileTrackingResult:
        """
        Track files asynchronously with content-addressable storage.
        """
        prepared_files = await self._aprepare_files(file_paths)

        async with self._uow_factory() as uow:
            result = await self._atrack_prepared_files_in_uow(
                uow,
                prepared_files=prepared_files,
                message=message,
                checkpoint_id=checkpoint_id,
            )
            await uow.acommit()  # ATOMIC: blobs + commit + link
            return result

    async def _atrack_prepared_files_in_uow(
        self,
        uow: IAsyncUnitOfWork,
        prepared_files: list[tuple[Path, bytes]],
        message: str,
        checkpoint_id: str | None = None,
    ) -> FileTrackingResult:
        """Persist prepared files through the supplied UoW without committing it."""
        mementos = []
        total_size = 0

        for path, content in prepared_files:
            blob_id = await uow.blobs.asave(content)
            mementos.append(FileMemento(
                file_path=str(path),
                file_hash=blob_id,
                file_size=len(content),
            ))
            total_size += len(content)

        commit = FileCommit.create(message=message)
        for memento in mementos:
            commit.add_memento(memento)
        await uow.file_commits.asave(commit)

        if checkpoint_id is not None:
            normalized_checkpoint_id = str(checkpoint_id).strip()
            if not normalized_checkpoint_id:
                raise ValueError("checkpoint_id cannot be empty")
            link = CheckpointFileLink.create_from_values(
                checkpoint_id=normalized_checkpoint_id,
                commit_id=commit.commit_id,
                file_count=len(mementos),
                total_size_bytes=total_size,
            )
            await uow.checkpoint_file_links.aadd(link)

        return FileTrackingResult(
            commit_id=commit.commit_id.value,
            files_tracked=len(mementos),
            total_size_bytes=total_size,
        )

    async def atrack_and_link_in_uow(
        self,
        uow: IAsyncUnitOfWork,
        checkpoint_id: str,
        file_paths: list[str],
        message: str,
    ) -> FileTrackingResult:
        """Track and link files in a caller-owned transaction."""
        prepared_files = await self._aprepare_files(file_paths)
        return await self._atrack_prepared_files_in_uow(
            uow,
            prepared_files=prepared_files,
            message=message,
            checkpoint_id=checkpoint_id,
        )

    async def arestore_files(
        self,
        checkpoint_id: str,
        output_dir: Path,
    ) -> int:
        """
        Restore files from checkpoint asynchronously.
        """
        async with self._uow_factory() as uow:
            link = await uow.checkpoint_file_links.aget_by_checkpoint(
                str(checkpoint_id)
            )
            if not link:
                return 0
            commit = await uow.file_commits.aget_by_id(link.commit_id)
            if not commit:
                return 0
            
            resolved_output_dir = Path(output_dir).resolve(strict=False)
            planned_restores = []
            seen_names = set()
            for memento in commit.mementos:
                output_name = Path(memento.file_path).name
                normalized_name = output_name.casefold()
                if not output_name or normalized_name in seen_names:
                    raise ValueError(
                        f"Checkpoint contains duplicate output filename: {output_name}"
                    )
                seen_names.add(normalized_name)

                output_path = resolved_output_dir / output_name
                resolved_output_path = output_path.resolve(strict=False)
                try:
                    resolved_output_path.relative_to(resolved_output_dir)
                except ValueError as exc:
                    raise ValueError(
                        f"Checkpoint restore path escapes output directory: {output_name}"
                    ) from exc
                planned_restores.append((memento, output_path))

            # Preflight every blob before creating the destination or writing a
            # single file, so missing content cannot leave a partial restore.
            for memento, _ in planned_restores:
                if not await uow.blobs.aexists(memento.file_hash):
                    raise FileNotFoundError(f"Blob not found: {memento.file_hash}")

            await aiofiles.os.makedirs(resolved_output_dir, exist_ok=True)
            restored_count = 0
            for memento, output_path in planned_restores:
                await uow.blobs.arestore_to_file(
                    blob_id=memento.file_hash,
                    output_path=output_path,
                )
                restored_count += 1

            return restored_count
    
    async def atrack_and_link(
        self,
        checkpoint_id: str,
        file_paths: list[str],
        message: str,
    ) -> FileTrackingResult:
        """
        Convenience method: Track files and link to checkpoint in one call.
        """
        prepared_files = await self._aprepare_files(file_paths)
        async with self._uow_factory() as uow:
            result = await self._atrack_prepared_files_in_uow(
                uow,
                prepared_files=prepared_files,
                message=message,
                checkpoint_id=checkpoint_id,
            )
            await uow.acommit()
            return result
