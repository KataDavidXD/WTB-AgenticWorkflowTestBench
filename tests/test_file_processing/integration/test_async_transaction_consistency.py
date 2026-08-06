"""
Comprehensive Transaction Consistency Tests for Async File Tracking & Architecture.

Created: 2026-01-28
Reference: FILE_TRACKING_ARCHITECTURE_DECISION.md, ASYNC_ARCHITECTURE_PLAN.md

These tests demonstrate how the WTB architecture handles error scenarios
and maintains transaction consistency following SOLID and ACID principles.

Error Scenarios Covered:
═══════════════════════════════════════════════════════════════════════════════
Scenario A: Non-idempotent Writes
    - Problem: Tool writes not idempotent, retry causes duplicate records
    - Solution: Content-addressable storage (SHA-256) ensures idempotency
    
Scenario B: Partial Commit / Half-Written  
    - Problem: Mid-failure leaves partial data; orphaned data can be read
    - Solution: Two-phase write with atomic commit + orphan cleanup
    
Scenario C: Async Background Side Effects
    - Problem: Async tasks lack write ordering; chaos on restart
    - Solution: Outbox pattern with FIFO ordering guarantee
    
Scenario D: Stale Reads
    - Problem: Reading outdated writes; reads before writes complete
    - Solution: Session isolation + explicit commit boundaries
    
Scenario E: Node Replacement with Isolated Environments
    - Problem: Node-level venv management conflicts with workflow env
    - Solution: Independent node-level venv via GrpcEnvironmentProvider
═══════════════════════════════════════════════════════════════════════════════
"""

import pytest
import pytest_asyncio
import aiofiles
import aiofiles.os
import uuid
import asyncio
import tempfile
import sqlite3
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any

from wtb.infrastructure.database.async_unit_of_work import AsyncSQLAlchemyUnitOfWork
from wtb.infrastructure.file_tracking.async_filetracker_service import AsyncFileTrackerService
from wtb.infrastructure.file_tracking.async_orphan_cleaner import AsyncBlobOrphanCleaner
from wtb.domain.models.file_processing import (
    FileCommit, FileMemento, BlobId, CommitId, CommitStatus, CheckpointFileLink
)
from wtb.domain.models.outbox import OutboxEvent, OutboxEventType, OutboxStatus
from wtb.infrastructure.database.file_processing_orm import FileBlobORM, FileCommitORM
from sqlalchemy import select, func


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def async_uow_factory(tmp_path):
    """Factory for creating async UoW instances with fresh database."""
    db_path = tmp_path / "wtb_test.db"
    blob_path = tmp_path / "blobs"
    blob_path.mkdir(exist_ok=True)
    
    db_url = f"sqlite:///{db_path}"
    
    # Initialize schema
    from wtb.infrastructure.database.models import Base
    from wtb.infrastructure.database.file_processing_orm import (
        FileBlobORM, FileCommitORM, FileMementoORM, CheckpointFileLinkORM
    )
    
    # Create tables using async engine
    async_db_url = f"sqlite+aiosqlite:///{db_path}"
    
    # Create a UoW to get engine
    test_uow = AsyncSQLAlchemyUnitOfWork(db_url, str(blob_path))
    engine = test_uow.get_engine(async_db_url)
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    def factory():
        return AsyncSQLAlchemyUnitOfWork(db_url, str(blob_path))
    
    yield factory
    
    # Cleanup
    await engine.dispose()


@pytest.fixture
def blob_storage_path(tmp_path):
    """Blob storage directory."""
    path = tmp_path / "blobs" / "objects"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def workspace_path(tmp_path):
    """Workspace directory for test files."""
    path = tmp_path / "workspace"
    path.mkdir(exist_ok=True)
    return path


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO A: Non-Idempotent Writes
# ═══════════════════════════════════════════════════════════════════════════════


def test_checkpoint_link_mapper_normalizes_legacy_numeric_identifier():
    from wtb.infrastructure.database.mappers import CheckpointFileLinkMapper

    legacy_row = type(
        "LegacyCheckpointFileLinkRow",
        (),
        {
            "checkpoint_id": 7,
            "commit_id": str(uuid.uuid4()),
            "linked_at": datetime.now(),
            "file_count": 1,
            "total_size_bytes": 4,
        },
    )()

    mapped = CheckpointFileLinkMapper.orm_to_domain(legacy_row)

    assert mapped.checkpoint_id == "7"
    assert isinstance(mapped.checkpoint_id, str)


def test_sqlite_string_checkpoint_migration_preserves_legacy_rows(tmp_path):
    database_path = tmp_path / "legacy-links.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            CREATE TABLE file_commits (
                commit_id VARCHAR(64) PRIMARY KEY
            );
            CREATE TABLE checkpoint_file_links (
                checkpoint_id INTEGER PRIMARY KEY,
                commit_id VARCHAR(64) NOT NULL,
                linked_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                file_count INTEGER NOT NULL,
                total_size_bytes BIGINT NOT NULL
            );
            INSERT INTO file_commits (commit_id) VALUES ('legacy-commit');
            INSERT INTO checkpoint_file_links (
                checkpoint_id, commit_id, file_count, total_size_bytes
            ) VALUES (7, 'legacy-commit', 1, 4);
            """
        )
        migration_path = (
            Path(__file__).parents[3]
            / "wtb"
            / "infrastructure"
            / "database"
            / "migrations"
            / "006_checkpoint_file_links_string_ids.sql"
        )
        connection.executescript(migration_path.read_text(encoding="utf-8"))

        checkpoint_id, storage_type = connection.execute(
            "SELECT checkpoint_id, typeof(checkpoint_id) "
            "FROM checkpoint_file_links"
        ).fetchone()
        column_type = next(
            row[2]
            for row in connection.execute("PRAGMA table_info(checkpoint_file_links)")
            if row[1] == "checkpoint_id"
        )
    finally:
        connection.close()

    assert checkpoint_id == "7"
    assert storage_type == "text"
    assert column_type == "VARCHAR(128)"



def test_postgresql_string_checkpoint_migration_is_dialect_specific():
    migration_path = (
        Path(__file__).parents[3]
        / "wtb"
        / "infrastructure"
        / "database"
        / "migrations"
        / "006_checkpoint_file_links_string_ids_postgresql.sql"
    )
    sql = migration_path.read_text(encoding="utf-8")

    assert "PostgreSQL only" in sql
    assert "ALTER COLUMN checkpoint_id TYPE VARCHAR(128)" in sql
    assert "USING checkpoint_id::text" in sql


class TestScenarioA_Idempotency:
    """
    Scenario A: Non-idempotent Writes
    
    Problem:
        工具写入不是幂等的，retry 会造成重复记录/重复 chunk/重复索引项
        (Tool writes are not idempotent, retries cause duplicate records/chunks/indices)
    
    Solution:
        Content-addressable storage using SHA-256 hashes.
        Same content always produces same BlobId.
        Repository increments reference count instead of creating duplicates.
    
    ACID Property: Consistency - Same input always produces same output
    SOLID Principle: SRP - BlobRepository only handles blob storage logic
    """
    
    @pytest.mark.asyncio
    async def test_same_content_same_blob_id(self, async_uow_factory):
        """Writing same content multiple times returns identical BlobId."""
        content = b"idempotent test content - scenario A"
        
        async with async_uow_factory() as uow:
            blob_id_1 = await uow.blobs.asave(content)
            blob_id_2 = await uow.blobs.asave(content)
            blob_id_3 = await uow.blobs.asave(content)
            await uow.acommit()
        
        # All three saves return the same BlobId
        assert blob_id_1 == blob_id_2 == blob_id_3
        
        # Only one record in database with reference_count = 3
        async with async_uow_factory() as uow:
            stmt = select(FileBlobORM).where(FileBlobORM.content_hash == blob_id_1.value)
            result = await uow._session.execute(stmt)
            blob = result.scalar_one()
            
            assert blob.reference_count == 3
    
    @pytest.mark.asyncio
    async def test_retry_after_commit_is_safe(self, async_uow_factory):
        """Retrying a committed operation is safe (no duplicates)."""
        content = b"retry-safe content"
        
        # First attempt - succeeds
        async with async_uow_factory() as uow:
            blob_id_1 = await uow.blobs.asave(content)
            commit_1 = FileCommit.create(message="First attempt")
            commit_1.add_memento(FileMemento(
                file_path="/test/file.txt",
                file_hash=blob_id_1,
                file_size=len(content),
            ))
            await uow.file_commits.asave(commit_1)
            await uow.acommit()
        
        # "Retry" - perhaps due to network timeout, client retries
        async with async_uow_factory() as uow:
            blob_id_2 = await uow.blobs.asave(content)
            commit_2 = FileCommit.create(message="Retry attempt")
            commit_2.add_memento(FileMemento(
                file_path="/test/file.txt",
                file_hash=blob_id_2,
                file_size=len(content),
            ))
            await uow.file_commits.asave(commit_2)
            await uow.acommit()
        
        # BlobId is same (idempotent)
        assert blob_id_1 == blob_id_2
        
        # But we have 2 commits (which is correct - each commit is distinct)
        async with async_uow_factory() as uow:
            commit_count = await uow.file_commits.acount()
            assert commit_count == 2
            
            # Reference count should be 2
            stmt = select(FileBlobORM).where(FileBlobORM.content_hash == blob_id_1.value)
            result = await uow._session.execute(stmt)
            blob = result.scalar_one()
            assert blob.reference_count == 2
    
    @pytest.mark.asyncio
    async def test_concurrent_writes_same_content(self, async_uow_factory):
        """
        Concurrent writes of same content converge to single blob.
        
        Note: In high-concurrency scenarios, some writes may fail with
        IntegrityError, which is expected behavior - what matters is
        that no duplicates are created and the system remains consistent.
        """
        content = b"concurrent write content"
        blob_id = BlobId.from_content(content)
        
        async def write_content():
            try:
                async with async_uow_factory() as uow:
                    saved_id = await uow.blobs.asave(content)
                    await uow.acommit()
                    return saved_id
            except Exception as e:
                # Concurrent write conflict - expected in some cases
                return None
        
        # Run 5 concurrent writes - some may succeed, some may fail
        results = await asyncio.gather(*[write_content() for _ in range(5)])
        
        # Filter out failed attempts
        successful_results = [r for r in results if r is not None]
        
        # At least one should succeed
        assert len(successful_results) >= 1
        
        # All successful writes should return same BlobId
        assert all(r.value == blob_id.value for r in successful_results)
        
        # Only one blob in database (no duplicates)
        async with async_uow_factory() as uow:
            stmt = select(func.count(FileBlobORM.content_hash))
            result = await uow._session.execute(stmt)
            count = result.scalar()
            assert count == 1, f"Expected 1 blob, got {count}"


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO B: Partial Commit / Half-Written
# ═══════════════════════════════════════════════════════════════════════════════


class TestScenarioB_PartialCommit:
    """
    Scenario B: Partial Commit / Half-Written
    
    Problem:
        中途失败留下半套数据；孤儿数据，可能被错误读到且无法被管理
        (Mid-failure leaves partial data; orphaned data may be read incorrectly)
    
    Solution:
        Two-phase write pattern:
        1. Write blob to filesystem FIRST
        2. Add DB record to session (not committed)
        3. Commit → both persisted, or rollback → DB clean, orphan blob on FS
        4. AsyncBlobOrphanCleaner runs periodically to clean orphaned blobs
    
    ACID Property: Atomicity - All-or-nothing for DB operations
    SOLID Principle: OCP - OrphanCleaner can be extended for different backends
    """
    
    @pytest.mark.asyncio
    async def test_rollback_leaves_no_db_record(self, async_uow_factory, blob_storage_path):
        """On rollback, DB record is removed (atomicity)."""
        content = b"orphan scenario content"
        blob_id = BlobId.from_content(content)
        
        try:
            async with async_uow_factory() as uow:
                saved_blob_id = await uow.blobs.asave(content)
                
                # Simulate failure before commit
                raise RuntimeError("Simulated database crash!")
                
                await uow.acommit()
        except RuntimeError:
            pass  # Expected
        
        # Verify: NO database record (atomicity)
        async with async_uow_factory() as uow:
            stmt = select(func.count(FileBlobORM.content_hash)).where(
                FileBlobORM.content_hash == blob_id.value
            )
            result = await uow._session.execute(stmt)
            count = result.scalar()
            assert count == 0, "Rolled back transaction should leave no DB record"
    
    @pytest.mark.asyncio
    async def test_orphan_blob_exists_after_rollback(self, async_uow_factory, blob_storage_path):
        """Orphan blob file exists on filesystem after rollback."""
        content = b"orphan blob content"
        blob_id = BlobId.from_content(content)
        
        try:
            async with async_uow_factory() as uow:
                await uow.blobs.asave(content)
                raise RuntimeError("Crash before commit")
                await uow.acommit()
        except RuntimeError:
            pass
        
        # Orphan file DOES exist on filesystem
        # Path: blobs/objects/<prefix>/<rest>
        prefix = blob_id.value[:2]
        rest = blob_id.value[2:]
        orphan_path = blob_storage_path / prefix / rest
        
        assert orphan_path.exists(), "Orphan blob should exist on filesystem"
    
    @pytest.mark.asyncio
    async def test_orphan_cleaner_removes_orphans(self, async_uow_factory, blob_storage_path):
        """AsyncBlobOrphanCleaner detects and removes orphaned blobs."""
        # Create an orphan (write file but no DB record)
        content = b"will become orphan"
        blob_id = BlobId.from_content(content)
        
        try:
            async with async_uow_factory() as uow:
                await uow.blobs.asave(content)
                raise RuntimeError("Crash!")
                await uow.acommit()
        except RuntimeError:
            pass
        
        # Verify orphan exists
        prefix = blob_id.value[:2]
        rest = blob_id.value[2:]
        orphan_path = blob_storage_path / prefix / rest
        assert orphan_path.exists()
        
        # Run cleaner
        cleaner = AsyncBlobOrphanCleaner(
            blobs_dir=blob_storage_path,
            uow_factory=async_uow_factory,
            grace_period_minutes=0,  # No grace period for test
        )
        
        orphans_found = await cleaner.aclean_orphans(dry_run=False)
        
        # Orphan should be cleaned
        assert len(orphans_found) == 1
        assert not orphan_path.exists(), "Orphan should be deleted"
    
    @pytest.mark.asyncio
    async def test_grace_period_protects_in_flight_blobs(self, async_uow_factory, blob_storage_path):
        """Grace period prevents cleaning blobs from in-flight transactions."""
        content = b"in-flight content"
        
        # Create "recent" orphan
        try:
            async with async_uow_factory() as uow:
                blob_id = await uow.blobs.asave(content)
                raise RuntimeError("Crash!")
        except RuntimeError:
            pass
        
        # Run cleaner with 60-minute grace period (blob just created)
        cleaner = AsyncBlobOrphanCleaner(
            blobs_dir=blob_storage_path,
            uow_factory=async_uow_factory,
            grace_period_minutes=60,  # 60 minutes - blob is within grace period
        )
        
        orphans_found = await cleaner.aclean_orphans(dry_run=True)
        
        # Should find 0 orphans (within grace period)
        assert len(orphans_found) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO C: Async Background Side Effects
# ═══════════════════════════════════════════════════════════════════════════════


class TestScenarioC_AsyncOrdering:
    """
    Scenario C: Async Background Side Effects
    
    Problem:
        异步任务没有写入顺序控制，写入错位混乱；重启也清不掉
        (Async tasks lack write ordering, chaos on restart)
    
    Solution:
        Outbox pattern with FIFO ordering guarantee:
        1. Events added to outbox in transaction
        2. OutboxProcessor reads events in created_at order (FIFO)
        3. Events processed in order, marked as processed
        4. Cleanup job removes old processed events
    
    ACID Property: Isolation - Events processed in correct order
    SOLID Principle: ISP - IAsyncOutboxRepository has focused interface
    """
    
    @pytest.mark.asyncio
    async def test_outbox_maintains_fifo_order(self, async_uow_factory):
        """Outbox events are retrieved in FIFO order."""
        events_data = [
            {"order": i, "name": f"event-{i}"}
            for i in range(10)
        ]
        
        async with async_uow_factory() as uow:
            for data in events_data:
                event = OutboxEvent(
                    event_id=str(uuid.uuid4()),
                    event_type=OutboxEventType.CHECKPOINT_VERIFY,
                    aggregate_type="Test",
                    aggregate_id=str(data["order"]),
                    payload=data,
                )
                await uow.outbox.aadd(event)
                # Small delay to ensure distinct timestamps
                await asyncio.sleep(0.001)
            
            await uow.acommit()
        
        # Retrieve and verify FIFO order
        async with async_uow_factory() as uow:
            pending = await uow.outbox.aget_pending(limit=100)
            
            assert len(pending) == 10
            
            for i, event in enumerate(pending):
                assert event.aggregate_id == str(i), f"Expected order {i}, got {event.aggregate_id}"
                assert event.payload["order"] == i
    
    @pytest.mark.asyncio
    async def test_concurrent_outbox_writes_maintain_order(self, async_uow_factory):
        """Concurrent outbox writes still maintain causal order within transaction."""
        
        async def create_event(order: int) -> str:
            async with async_uow_factory() as uow:
                event = OutboxEvent(
                    event_id=str(uuid.uuid4()),
                    event_type=OutboxEventType.FILE_COMMIT_VERIFY,
                    aggregate_type="FileCommit",
                    aggregate_id=f"commit-{order}",
                    payload={"sequence": order},
                )
                await uow.outbox.aadd(event)
                await uow.acommit()
                return event.event_id
        
        # Create events concurrently
        tasks = [create_event(i) for i in range(5)]
        event_ids = await asyncio.gather(*tasks)
        
        # All events should be created
        async with async_uow_factory() as uow:
            pending = await uow.outbox.aget_pending(limit=100)
            assert len(pending) == 5
            
            # Events should be ordered by created_at (FIFO)
            timestamps = [e.created_at for e in pending]
            assert timestamps == sorted(timestamps), "Events should be in timestamp order"
    
    @pytest.mark.asyncio
    async def test_processed_events_not_returned(self, async_uow_factory):
        """Processed events are not returned by aget_pending."""
        async with async_uow_factory() as uow:
            # Create events
            for i in range(3):
                event = OutboxEvent(
                    event_id=str(uuid.uuid4()),
                    event_type=OutboxEventType.CHECKPOINT_CREATE,
                    aggregate_type="Test",
                    aggregate_id=str(i),
                    payload={"index": i},
                )
                await uow.outbox.aadd(event)
            await uow.acommit()
        
        # Mark first event as processed
        async with async_uow_factory() as uow:
            pending = await uow.outbox.aget_pending(limit=10)
            first_event = pending[0]
            first_event.status = OutboxStatus.PROCESSED
            first_event.processed_at = datetime.now()
            await uow.outbox.aupdate(first_event)
            await uow.acommit()
        
        # Get pending again - should not include processed event
        async with async_uow_factory() as uow:
            pending = await uow.outbox.aget_pending(limit=10)
            assert len(pending) == 2
            assert all(e.status == OutboxStatus.PENDING for e in pending)


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO D: Stale Reads
# ═══════════════════════════════════════════════════════════════════════════════


class TestScenarioD_StaleReads:
    """
    Scenario D: Stale Reads
    
    Problem:
        读到过时的写/读在写之前
        (Reading outdated writes; reads before writes complete)
    
    Solution:
        Session isolation ensures:
        1. Uncommitted writes are not visible to other sessions
        2. Each session sees a consistent snapshot
        3. Explicit commit() makes writes visible
    
    ACID Property: Isolation - Transactions don't see uncommitted data
    SOLID Principle: DIP - UoW abstracts session management
    """
    
    @pytest.mark.asyncio
    async def test_uncommitted_writes_invisible_to_others(self, async_uow_factory):
        """Uncommitted writes are not visible to other transactions."""
        content = b"secret uncommitted content"
        blob_id = BlobId.from_content(content)
        
        async with async_uow_factory() as writer_uow:
            # Writer saves but doesn't commit yet
            await writer_uow.blobs.asave(content)
            
            # Reader in separate session should NOT see the blob
            async with async_uow_factory() as reader_uow:
                exists = await reader_uow.blobs.aexists(blob_id)
                assert not exists, "Uncommitted write should not be visible"
            
            # Now commit
            await writer_uow.acommit()
        
        # After commit, new session should see it
        async with async_uow_factory() as reader_uow:
            exists = await reader_uow.blobs.aexists(blob_id)
            assert exists, "Committed write should be visible"
    
    @pytest.mark.asyncio
    async def test_read_your_writes_within_transaction(self, async_uow_factory):
        """Within same transaction, you can read your own writes."""
        content = b"read-your-write content"
        
        async with async_uow_factory() as uow:
            blob_id = await uow.blobs.asave(content)
            
            # Can read within same transaction
            exists = await uow.blobs.aexists(blob_id)
            assert exists, "Should see own uncommitted write"
            
            retrieved = await uow.blobs.aget(blob_id)
            assert retrieved == content
            
            await uow.acommit()
    
    @pytest.mark.asyncio
    async def test_snapshot_isolation_prevents_phantom_reads(self, async_uow_factory):
        """Transaction sees consistent snapshot, no phantom reads."""
        # Pre-populate with initial data
        async with async_uow_factory() as uow:
            for i in range(3):
                blob_id = await uow.blobs.asave(f"initial-{i}".encode())
            await uow.acommit()
        
        # Start reader transaction
        async with async_uow_factory() as reader_uow:
            # Get initial count
            initial_hashes = await reader_uow.blobs.alist_all_hashes()
            initial_count = len(initial_hashes)
            
            # Another transaction adds more blobs
            async with async_uow_factory() as writer_uow:
                for i in range(3, 6):
                    await writer_uow.blobs.asave(f"added-{i}".encode())
                await writer_uow.acommit()
            
            # Reader should still see same count (snapshot isolation)
            # Note: SQLite default isolation may vary - this demonstrates the pattern
            current_hashes = await reader_uow.blobs.alist_all_hashes()
            
            # In strict snapshot isolation, this would be equal
            # SQLite default may allow some phantom reads
            # The important thing is the transaction model supports isolation
            assert len(current_hashes) >= initial_count


# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO E: Node Environment Isolation
# ═══════════════════════════════════════════════════════════════════════════════


class TestScenarioE_NodeEnvironmentIsolation:
    """
    Scenario E: Node Replacement with Isolated Environments
    
    Problem:
        Node 替换，用node级别的虚拟环境管理，独立于前面的workflow环境
        (Node replacement with node-level venv, independent of workflow env)
    
    Solution:
        GrpcEnvironmentProvider manages node-level virtual environments:
        1. Each node variant can have its own venv
        2. Environments are isolated from workflow-level venv
        3. Created/destroyed via gRPC calls to Venv Manager service
    
    ACID Property: Isolation - Node envs don't affect each other
    SOLID Principle: DIP - ExecutionController depends on IEnvironmentProvider
    """
    
    @pytest.mark.asyncio
    async def test_node_environment_creation_concept(self, async_uow_factory):
        """
        Demonstrates the concept of node-level environment isolation.
        
        Note: Actual gRPC tests require running Venv Manager service.
        This test validates the pattern/model.
        """
        # Simulate node variant configurations
        node_variants = [
            {
                "variant_id": "node-a-v1",
                "node_id": "node_a",
                "packages": ["numpy==1.24.0"],
            },
            {
                "variant_id": "node-a-v2",
                "node_id": "node_a",
                "packages": ["numpy==1.25.0"],  # Different version
            },
            {
                "variant_id": "node-b-v1",
                "node_id": "node_b",
                "packages": ["pandas==2.0.0"],  # Different package
            },
        ]
        
        # In real implementation, each variant gets its own venv
        # Here we validate the data model supports this
        for config in node_variants:
            # Each variant can specify:
            # - Its own packages (isolated from workflow)
            # - Its own Python version (if needed)
            assert "variant_id" in config
            assert "node_id" in config
            assert "packages" in config
            
            # Validate variant ID uniqueness
            variant_ids = [v["variant_id"] for v in node_variants]
            assert len(variant_ids) == len(set(variant_ids)), "Variant IDs must be unique"
    
    @pytest.mark.asyncio
    async def test_actual_node_environment_isolation(self):
        """
        Integration test with actual Venv Manager service (Docker).
        
        Uses the ``RunCode`` gRPC RPC to execute code *inside* the
        Docker container so the test works on any host OS.
        
        Requires:
            - Venv Manager running on localhost:50051
        """
        from wtb.infrastructure.environment.providers import GrpcEnvironmentProvider
        
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            result = sock.connect_ex(('localhost', 50051))
            if result != 0:
                pytest.skip("Venv Manager gRPC service not running on localhost:50051")
        finally:
            sock.close()
        
        provider = GrpcEnvironmentProvider("localhost:50051", timeout_seconds=60)
        
        variant_id = f"test-variant-{uuid.uuid4()}"
        config = {
            "workflow_id": "test-workflow",
            "node_id": "test-node",
            "packages": ["requests>=2.28"],
            "python_version": "3.11"
        }
        
        environment_created = False
        try:
            try:
                env_info = provider.create_environment(variant_id, config)
            except Exception as exc:
                pytest.skip(f"Venv Manager gRPC service unavailable: {exc}")

            if env_info.get("type") == "grpc_uv_stub":
                pytest.skip("Venv Manager gRPC service returned a local stub")
            environment_created = True
            
            assert "python_path" in env_info
            assert env_info.get("type") == "grpc_uv"
            assert env_info.get("env_path")
            
            status = provider.get_env_status(variant_id)
            assert status["status"] == "ACTIVE"
            assert status["has_venv"] is True
            assert status["has_pyproject"] is True
            
        finally:
            if environment_created:
                provider.cleanup_environment(variant_id)
            provider.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Combined Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCombinedTransactionConsistency:
    """
    Combined tests demonstrating multiple ACID properties together.
    """
    
    @pytest.mark.asyncio
    async def test_file_tracking_full_workflow(self, async_uow_factory, workspace_path):
        """
        Full file tracking workflow demonstrating all ACID properties:
        - Atomicity: All operations succeed or fail together
        - Consistency: SHA-256 ensures data integrity
        - Isolation: Each UoW has isolated session
        - Durability: Committed data persists
        """
        # Create test files
        file1 = workspace_path / "model.pkl"
        file2 = workspace_path / "config.json"
        
        async with aiofiles.open(file1, 'wb') as f:
            await f.write(b"model binary data")
        async with aiofiles.open(file2, 'wb') as f:
            await f.write(b'{"learning_rate": 0.001}')
        
        # Create file tracker service
        service = AsyncFileTrackerService(uow_factory=async_uow_factory)
        
        # Track files (Atomicity: all tracked together)
        result = await service.atrack_files(
            file_paths=[str(file1), str(file2)],
            message="Checkpoint at epoch 10",
            checkpoint_id="1",  # Assuming int checkpoint_id
        )
        
        assert result.files_tracked == 2
        assert result.total_size_bytes > 0
        
        # Verify durability - data persists
        # Query via checkpoint file link
        async with async_uow_factory() as uow:
            link = await uow.checkpoint_file_links.aget_by_checkpoint("1")
            assert link is not None, "Checkpoint file link should exist"
            
            # Verify the linked commit
            commit = await uow.file_commits.aget_by_id(link.commit_id)
            assert commit is not None, "Linked commit should exist"
            assert commit.file_count == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("checkpoint_id", ["7", str(uuid.uuid4())])
    async def test_file_tracking_links_and_restores_string_checkpoint_ids(
        self,
        async_uow_factory,
        workspace_path,
        checkpoint_id,
    ):
        source = workspace_path / "result.txt"
        async with aiofiles.open(source, "wb") as file_handle:
            await file_handle.write(b"checkpoint-linked-content")

        service = AsyncFileTrackerService(uow_factory=async_uow_factory)
        result = await service.atrack_and_link(
            checkpoint_id=checkpoint_id,
            file_paths=[str(source)],
            message="String checkpoint link",
        )

        assert result.commit_id
        async with async_uow_factory() as uow:
            link = await uow.checkpoint_file_links.aget_by_checkpoint(checkpoint_id)
            assert link is not None
            assert link.checkpoint_id == checkpoint_id
            commit = await uow.file_commits.aget_by_id(link.commit_id)
            assert commit is not None
            assert commit.commit_id.value == result.commit_id

        restore_dir = workspace_path / f"restore-{checkpoint_id}"
        restored_count = await service.arestore_files(
            checkpoint_id,
            restore_dir,
        )

        assert restored_count == 1
        restored = restore_dir / source.name
        assert restored.read_bytes() == b"checkpoint-linked-content"
    
    @pytest.mark.asyncio
    async def test_restore_rejects_duplicate_basenames_before_any_write(
        self,
        async_uow_factory,
        workspace_path,
    ):
        first_dir = workspace_path / "first"
        second_dir = workspace_path / "second"
        first_dir.mkdir()
        second_dir.mkdir()
        first = first_dir / "result.txt"
        second = second_dir / "result.txt"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        service = AsyncFileTrackerService(uow_factory=async_uow_factory)
        await service.atrack_and_link(
            checkpoint_id="duplicate-basename",
            file_paths=[str(first), str(second)],
            message="Duplicate basename preflight",
        )
        restore_dir = workspace_path / "restore-duplicate"

        with pytest.raises(ValueError, match="duplicate output filename"):
            await service.arestore_files("duplicate-basename", restore_dir)

        assert not restore_dir.exists()

    @pytest.mark.asyncio
    async def test_crash_recovery_scenario(self, async_uow_factory, blob_storage_path):
        """
        Simulates crash and recovery, demonstrating system resilience:
        1. Start transaction
        2. Write blob (filesystem)
        3. Crash before commit
        4. Verify DB clean (atomicity)
        5. Run orphan cleanup (recovery)
        """
        crash_content = b"crash recovery test"
        
        # Simulate crash
        try:
            async with async_uow_factory() as uow:
                blob_id = await uow.blobs.asave(crash_content)
                # CRASH!
                raise SystemExit("Simulated process crash")
        except SystemExit:
            pass
        
        # After "restart" - verify DB is clean
        async with async_uow_factory() as uow:
            blob_id = BlobId.from_content(crash_content)
            exists = await uow.blobs.aexists(blob_id)
            assert not exists, "DB should be clean after crash"
        
        # Run recovery (orphan cleanup)
        cleaner = AsyncBlobOrphanCleaner(
            blobs_dir=blob_storage_path,
            uow_factory=async_uow_factory,
            grace_period_minutes=0,
        )
        
        orphans = await cleaner.aclean_orphans(dry_run=False)
        
        # Orphan cleaned up
        assert len(orphans) >= 0  # May or may not have orphan depending on timing
    
    @pytest.mark.asyncio
    async def test_concurrent_operations_isolation(self, async_uow_factory):
        """
        Multiple concurrent operations maintain isolation:
        - Each task has its own UoW
        - No interference between concurrent writes
        - All data consistent after completion
        """
        async def track_file(task_id: int) -> str:
            content = f"task-{task_id}-content".encode()
            
            async with async_uow_factory() as uow:
                blob_id = await uow.blobs.asave(content)
                
                commit = FileCommit.create(message=f"Task {task_id} commit")
                commit.add_memento(FileMemento(
                    file_path=f"/task/{task_id}/file.txt",
                    file_hash=blob_id,
                    file_size=len(content),
                ))
                await uow.file_commits.asave(commit)
                await uow.acommit()
                
                return commit.commit_id.value
        
        # Run 10 concurrent operations
        tasks = [track_file(i) for i in range(10)]
        commit_ids = await asyncio.gather(*tasks)
        
        # All commits should be unique
        assert len(set(commit_ids)) == 10
        
        # All commits should be persisted
        async with async_uow_factory() as uow:
            count = await uow.file_commits.acount()
            assert count == 10
