"""
Async File Processing Repository Implementations.

SQLAlchemy-based implementations of async file processing repository interfaces.
Provides ACID-compliant persistence for file commits, mementos, and blobs.

Design Principles:
- Repository Pattern: Encapsulates data access
- Dependency Inversion: Implements domain interfaces
- ACID: Uses SQLAlchemy session transactions
- Async I/O: Uses aiofiles and async DB drivers
- DRY: Uses shared mappers from wtb.infrastructure.database.mappers

Updated: 2026-01-28 - Refactored to use BlobStorageCore for shared logic (ISSUE-FS-001)
"""

import asyncio
import logging
import os
import uuid
from pathlib import Path

import aiofiles
import aiofiles.os
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from wtb.domain.interfaces.async_repositories import (
    IAsyncBlobRepository,
    IAsyncCheckpointFileLinkRepository,
    IAsyncFileCommitRepository,
)
from wtb.domain.models.file_processing import (
    BlobId,
    CheckpointFileLink,
    CommitId,
    FileCommit,
)
from wtb.infrastructure.database.file_processing_orm import (
    CheckpointFileLinkORM,
    FileBlobORM,
    FileCommitORM,
    FileMementoORM,
)
from wtb.infrastructure.database.mappers import (
    BlobStorageCore,
    CheckpointFileLinkMapper,
    FileCommitMapper,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Async SQLAlchemy Blob Repository
# ═══════════════════════════════════════════════════════════════════════════════


class AsyncSQLAlchemyBlobRepository(IAsyncBlobRepository):
    """
    Async SQLAlchemy implementation of IAsyncBlobRepository.
    
    Stores blob metadata in database and content in filesystem.
    Uses content-addressable storage (like Git).
    
    Two-Phase Write Pattern:
    1. Filesystem write (async via aiofiles)
    2. DB record in session
    3. Commit → both or neither persisted
    
    DRY:
    - Uses BlobStorageCore for shared logic with sync repository
    """
    
    def __init__(self, session: AsyncSession, storage_path: str):
        """
        Initialize blob repository.
        
        Args:
            session: AsyncSQLAlchemy session
            storage_path: Base path for blob file storage
        """
        self._session = session
        self._storage_path, self._objects_path = BlobStorageCore.setup_storage_structure(
            Path(storage_path)
        )
    
    async def asave(self, content: bytes) -> BlobId:
        """
        Save content to blob storage asynchronously.
        
        Idempotent: Returns same BlobId for same content.
        Updates reference count if blob already exists.
        
        Handles concurrent writes gracefully via content-addressable storage.
        Uses BlobStorageCore for consistent path/hash computation.
        """
        from sqlalchemy.exc import IntegrityError
        
        blob_id = BlobId.from_content(content)
        
        # Check if blob already exists
        existing = await self._session.get(FileBlobORM, blob_id.value)
        if existing:
            # Increment reference count
            existing.reference_count += 1
            return blob_id
        
        # Write content to filesystem asynchronously
        storage_location = await self._write_content_async(blob_id, content)
        
        # Create database record using shared mapper
        orm_dict = BlobStorageCore.create_orm_dict(
            blob_id=blob_id.value,
            storage_location=str(storage_location),
            content_size=len(content),
        )

        # Besides closing the read-then-insert race, this UPDATE ensures the
        # database has a real outer write transaction before a SAVEPOINT is
        # opened. That matters on SQLite, where releasing an outermost
        # SAVEPOINT can otherwise make the insert survive a later rollback.
        increment_result = await self._session.execute(
            update(FileBlobORM)
            .where(FileBlobORM.content_hash == blob_id.value)
            .values(reference_count=FileBlobORM.reference_count + 1)
        )
        if increment_result.rowcount and increment_result.rowcount > 0:
            return blob_id

        blob_orm = FileBlobORM(**orm_dict)

        # Isolate a concurrent unique-key conflict to a savepoint. Rolling back
        # the whole session here would discard unrelated caller-owned changes.
        try:
            async with self._session.begin_nested():
                self._session.add(blob_orm)
                await self._session.flush()
        except IntegrityError:
            # Concurrent write of same content - another transaction committed first
            existing = await self._session.get(FileBlobORM, blob_id.value)
            if existing:
                existing.reference_count += 1
            else:
                # Re-raise if still not found (unexpected state)
                raise
        
        logger.debug(f"Saved blob {BlobStorageCore.get_short_id(blob_id.value)}... ({len(content)} bytes)")
        return blob_id
    
    async def aget(self, blob_id: BlobId) -> bytes | None:
        """Retrieve content by blob ID asynchronously."""
        blob_orm = await self._session.get(FileBlobORM, blob_id.value)
        if not blob_orm:
            return None
        
        return await self._read_content_async(blob_orm.storage_location)
    
    async def aexists(self, blob_id: BlobId) -> bool:
        """
        Check if blob exists asynchronously.
        
        Checks both database and filesystem.
        """
        blob_orm = await self._session.get(FileBlobORM, blob_id.value)
        if not blob_orm:
            return False
        
        # Also verify file exists (async stat)
        try:
            await aiofiles.os.stat(blob_orm.storage_location)
            return True
        except FileNotFoundError:
            return False
    
    async def adelete(self, blob_id: BlobId) -> bool:
        """
        Delete blob from storage asynchronously.
        
        Decrements reference count. Only deletes when count reaches 0.
        """
        blob_orm = await self._session.get(FileBlobORM, blob_id.value)
        if not blob_orm:
            return False
        
        blob_orm.reference_count -= 1
        
        if blob_orm.reference_count <= 0:
            # Delete file asynchronously
            try:
                await aiofiles.os.remove(blob_orm.storage_location)
            except FileNotFoundError:
                pass  # Already gone
            
            # Delete database record
            await self._session.delete(blob_orm)
            logger.debug(f"Deleted blob {blob_id.short}...")
            return True
        
        return False
    
    async def arestore_to_file(self, blob_id: BlobId, output_path: Path) -> None:
        """
        Restore blob content to filesystem asynchronously.
        """
        content = await self.aget(blob_id)
        if content is None:
            raise FileNotFoundError(f"Blob not found: {blob_id}")
        
        # Ensure parent directory exists (async not available for makedirs in older aiofiles, 
        # but modern versions have it. Fallback to os.makedirs if needed or run in executor)
        # Assuming aiofiles.os.makedirs exists (v23.2.1+)
        # If not, use sync or executor
        try:
            await aiofiles.os.makedirs(output_path.parent, exist_ok=True)
        except AttributeError:
            # Fallback for older aiofiles
            output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Atomic write: write to temp, then rename
        temp_path = output_path.with_suffix(".tmp")
        async with aiofiles.open(temp_path, 'wb') as f:
            await f.write(content)
        
        # Use os.replace for cross-platform atomic move (works on Windows even if target exists)
        await asyncio.to_thread(os.replace, str(temp_path), str(output_path))
        
        logger.debug(f"Restored blob {blob_id.short}... to {output_path}")
    
    async def alist_all_hashes(self) -> list[BlobId]:
        """List all blob hashes in storage."""
        stmt = select(FileBlobORM.content_hash)
        result = await self._session.execute(stmt)
        return [BlobId(h) for h in result.scalars().all()]
    
    async def _write_content_async(self, blob_id: BlobId, content: bytes) -> Path:
        """
        Write content to content-addressable storage asynchronously.
        
        Handles concurrent writes gracefully - if target already exists
        (due to concurrent write of same content), returns existing path.
        Uses BlobStorageCore for consistent path computation.
        """
        # Compute paths using shared logic
        dir_path = BlobStorageCore.compute_directory_path(blob_id.value, self._objects_path)
        # Sync mkdir is fast enough and safe
        dir_path.mkdir(exist_ok=True)
        
        file_path = BlobStorageCore.compute_storage_path(blob_id.value, self._objects_path)
        
        # Check if target already exists (concurrent write of same content)
        if file_path.exists():
            return file_path
        
        # Atomic write with unique temp file to avoid concurrent conflicts
        unique_suffix = uuid.uuid4().hex[:8]
        temp_path = BlobStorageCore.compute_temp_path(file_path, unique_suffix)
        
        try:
            async with aiofiles.open(temp_path, 'wb') as f:
                await f.write(content)
            
            try:
                # Use os.replace for cross-platform atomic move
                await asyncio.to_thread(os.replace, str(temp_path), str(file_path))
            except (PermissionError, FileExistsError, OSError):
                # Another concurrent write completed first - file exists now
                # Clean up our temp file and use the existing file
                try:
                    await aiofiles.os.remove(temp_path)
                except FileNotFoundError:
                    pass
                # Verify the target exists now
                if not file_path.exists():
                    raise
        except Exception:
            # Clean up temp file on any error
            try:
                await aiofiles.os.remove(temp_path)
            except FileNotFoundError:
                pass
            raise
        
        return file_path
    
    async def _read_content_async(self, storage_location: str) -> bytes | None:
        """Read content from filesystem asynchronously."""
        path = Path(storage_location)
        try:
            async with aiofiles.open(path, 'rb') as f:
                return await f.read()
        except FileNotFoundError:
            logger.warning(f"Blob file missing: {storage_location}")
            return None


# ═══════════════════════════════════════════════════════════════════════════════
# Async SQLAlchemy File Commit Repository
# ═══════════════════════════════════════════════════════════════════════════════


class AsyncSQLAlchemyFileCommitRepository(IAsyncFileCommitRepository):
    """
    Async SQLAlchemy implementation of IAsyncFileCommitRepository.
    """
    
    def __init__(self, session: AsyncSession):
        self._session = session
    
    async def asave(self, commit: FileCommit) -> None:
        """
        Save commit with all mementos asynchronously.
        """
        # Check for existing
        existing = await self._session.get(FileCommitORM, commit.commit_id.value)
        
        if existing:
            # Update existing
            existing.message = commit.message
            existing.status = commit.status.value
            existing.execution_id = commit.execution_id
            existing.checkpoint_id = commit.checkpoint_id
            
            # For relationships, we need to handle async loading or eager loading
            # Here we assume eager loading or simple replacement if possible.
            # However, direct list manipulation on async relationship might trigger lazy load error.
            # Safe way: delete old mementos explicitly and add new ones.
            
            # Load mementos if not loaded (awaitable in async session)
            # But simpler to just delete all mementos for this commit and re-add
            # Or assume they are loaded.
            
            # Since we can't easily clear() the relationship without loading it first,
            # we'll use a direct delete query for mementos
            # But ORM relationship management is better.
            
            # Let's try direct attribute manipulation, assuming session handles it on commit
            # WARNING: This might fail if mementos are not loaded.
            # Better approach for async update:
            # 1. Fetch with mementos eagerly
            # 2. Update list
            
            # Re-fetch with mementos to be safe
            stmt = select(FileCommitORM).where(FileCommitORM.commit_id == commit.commit_id.value).execution_options(populate_existing=True)
            # We need to make sure we load mementos
            # For now, let's just create new ORM objects and rely on session merge? No, merge is tricky.
            
            # Let's assume we can just overwrite attributes.
            # But mementos is a list.
            
            # Strategy: Delete existing mementos for this commit ID via query
            # Then add new ones.
            # Note: This breaks the ORM relationship link in memory if 'existing' is used later, 
            # but for save() it should be fine.
            
            # However, standard SQLAlchemy pattern:
            # existing.mementos = [] -> this requires mementos to be loaded.
            
            # Let's try to load it first if we are updating.
            # Actually, `existing` from `get` might not have mementos loaded.
            # We can use `select(FileCommitORM).options(selectinload(FileCommitORM.mementos))`
            
            # Simplified: Since file commits are usually immutable or append-only,
            # updates are rare. If they happen, we accept the cost of loading.
            
            # For this implementation, I'll stick to basic add (create) which is 99% of cases.
            # If update is needed, we'll implement it properly.
            pass # Already updated scalar fields above.
            
            # Handle mementos - clearing old ones
            # (Requires loading)
            await self._session.refresh(existing, ['mementos'])
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
    
    async def aget_by_id(self, commit_id: CommitId) -> FileCommit | None:
        """Get commit by ID with mementos loaded."""
        stmt = (
            select(FileCommitORM)
            .where(FileCommitORM.commit_id == commit_id.value)
            # Eager load mementos
            # Note: Need to import selectinload or similar if we want optimization,
            # but default relationship loading in async requires explicit options or await on access.
            # We will use explicit join or options.
        )
        # To avoid lazy load error, we should use selectinload
        from sqlalchemy.orm import selectinload
        stmt = stmt.options(selectinload(FileCommitORM.mementos))
        
        result = await self._session.execute(stmt)
        commit_orm = result.scalar_one_or_none()
        
        if not commit_orm:
            return None
        
        return self._orm_to_domain(commit_orm, load_mementos=True)
    
    async def aget_by_id_without_mementos(self, commit_id: CommitId) -> FileCommit | None:
        """Get commit without loading mementos."""
        commit_orm = await self._session.get(FileCommitORM, commit_id.value)
        if not commit_orm:
            return None
        
        return self._orm_to_domain(commit_orm, load_mementos=False)
    
    async def aget_by_execution_id(self, execution_id: str) -> list[FileCommit]:
        """Get commits for an execution."""
        stmt = (
            select(FileCommitORM)
            .where(FileCommitORM.execution_id == execution_id)
            .order_by(FileCommitORM.timestamp.desc())
        )
        result = await self._session.execute(stmt)
        return [self._orm_to_domain(orm, load_mementos=False) for orm in result.scalars().all()]
    
    async def aget_by_checkpoint_id(self, checkpoint_id: str) -> FileCommit | None:
        """Get commit linked to checkpoint."""
        # Convert checkpoint_id to int if necessary (ORM uses int for now based on previous file)
        # But wait, CheckpointFileLinkORM uses checkpoint_id: str?
        # Let's check FILE_TRACKING_ARCHITECTURE_DECISION.md or ORM.
        # file_processing_orm.py: checkpoint_id: Mapped[int]
        # But in LangGraph it's usually string. WTB uses int for SQL checkpoints?
        # The prompt examples use string for checkpoint_id in some places and int in others.
        # CheckpointFileLinkORM uses int.
        # Let's try to cast or keep as is.
        
        try:
            cp_id_int = int(checkpoint_id)
            stmt = (
                select(FileCommitORM)
                .where(FileCommitORM.checkpoint_id == cp_id_int)
            )
            # Eager load mementos
            from sqlalchemy.orm import selectinload
            stmt = stmt.options(selectinload(FileCommitORM.mementos))
            
            result = await self._session.execute(stmt)
            commit_orm = result.scalar_one_or_none()
            
            if not commit_orm:
                return None
            
            return self._orm_to_domain(commit_orm, load_mementos=True)
        except ValueError:
            # If checkpoint_id is not int, return None or handle appropriately
            return None

    async def adelete(self, commit_id: CommitId) -> bool:
        """Delete commit asynchronously."""
        commit_orm = await self._session.get(FileCommitORM, commit_id.value)
        if not commit_orm:
            return False
        
        await self._session.delete(commit_orm)
        return True
    
    async def acount(self) -> int:
        """Get total commit count."""
        stmt = select(func.count(FileCommitORM.commit_id))
        result = await self._session.execute(stmt)
        return result.scalar() or 0
    
    def _orm_to_domain(self, orm: FileCommitORM, load_mementos: bool) -> FileCommit:
        """Convert ORM model to domain entity using shared mapper."""
        return FileCommitMapper.orm_to_domain(orm, load_mementos)


# ═══════════════════════════════════════════════════════════════════════════════
# Async SQLAlchemy Checkpoint File Link Repository
# ═══════════════════════════════════════════════════════════════════════════════


class AsyncSQLAlchemyCheckpointFileLinkRepository(IAsyncCheckpointFileLinkRepository):
    """
    Async SQLAlchemy implementation of IAsyncCheckpointFileLinkRepository.
    """
    
    def __init__(self, session: AsyncSession):
        self._session = session
    
    async def aadd(self, link: CheckpointFileLink) -> None:
        """Add an immutable checkpoint-file link asynchronously."""
        checkpoint_id = str(link.checkpoint_id)
        existing = await self._session.get(CheckpointFileLinkORM, checkpoint_id)
        
        if existing:
            if existing.commit_id != link.commit_id.value:
                raise ValueError(
                    f"Checkpoint {checkpoint_id} is already linked to another commit"
                )
            return
        else:
            link_orm = CheckpointFileLinkORM(
                checkpoint_id=checkpoint_id,
                commit_id=link.commit_id.value,
                linked_at=link.linked_at,
                file_count=link.file_count,
                total_size_bytes=link.total_size_bytes,
            )
            self._session.add(link_orm)
    
    async def aget_by_checkpoint(self, checkpoint_id: str) -> CheckpointFileLink | None:
        """Get link by checkpoint ID."""
        link_orm = await self._session.get(CheckpointFileLinkORM, str(checkpoint_id))
        if not link_orm:
            return None
        return self._orm_to_domain(link_orm)
    
    async def aget_by_commit(self, commit_id: CommitId) -> list[CheckpointFileLink]:
        """Get all links for a commit."""
        stmt = (
            select(CheckpointFileLinkORM)
            .where(CheckpointFileLinkORM.commit_id == commit_id.value)
        )
        result = await self._session.execute(stmt)
        return [self._orm_to_domain(orm) for orm in result.scalars().all()]
    
    async def adelete_by_checkpoint(self, checkpoint_id: str) -> bool:
        """Delete link by checkpoint ID."""
        link_orm = await self._session.get(CheckpointFileLinkORM, str(checkpoint_id))
        if not link_orm:
            return False

        await self._session.delete(link_orm)
        return True
    
    async def adelete_by_commit(self, commit_id: CommitId) -> int:
        """Delete all links for a commit."""
        stmt = (
            select(CheckpointFileLinkORM)
            .where(CheckpointFileLinkORM.commit_id == commit_id.value)
        )
        result = await self._session.execute(stmt)
        links = result.scalars().all()
        count = len(links)
        
        for link in links:
            await self._session.delete(link)
            
        return count
    
    def _orm_to_domain(self, orm: CheckpointFileLinkORM) -> CheckpointFileLink:
        """Convert ORM to domain model using shared mapper."""
        return CheckpointFileLinkMapper.orm_to_domain(orm)
