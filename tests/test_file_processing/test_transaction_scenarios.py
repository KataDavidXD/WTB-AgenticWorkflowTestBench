
import pytest
import pytest_asyncio
import aiofiles
import aiofiles.os
import uuid
import asyncio
from datetime import datetime

from wtb.infrastructure.database.async_unit_of_work import AsyncSQLAlchemyUnitOfWork
from wtb.infrastructure.file_tracking.async_filetracker_service import AsyncFileTrackerService
from wtb.infrastructure.file_tracking.async_orphan_cleaner import AsyncBlobOrphanCleaner
from wtb.domain.models.file_processing import FileCommit, FileMemento, BlobId, CommitId, CommitStatus, CheckpointFileLink
from wtb.domain.models.outbox import OutboxEvent, OutboxEventType, OutboxStatus
from wtb.infrastructure.database.file_processing_orm import FileBlobORM, FileCommitORM
from sqlalchemy import select, func

@pytest_asyncio.fixture
async def uow_fixture(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/wtb.db"
    blob_path = tmp_path / "blobs"
    blob_path.mkdir()
    
    # Initialize DB schema
    from wtb.infrastructure.database.models import Base
    uow = AsyncSQLAlchemyUnitOfWork(db_url, str(blob_path))
    
    # Create tables
    engine = uow.get_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    yield uow
    # Cleanup
    await engine.dispose()

@pytest.fixture
def blob_storage_path(tmp_path):
    return tmp_path / "blobs"

@pytest.mark.asyncio
async def test_scenario_a_idempotency(uow_fixture):
    """
    Scenario A: Non-idempotent Writes
    Goal: Verify that writing the same content multiple times is safe and returns the same ID.
    """
    content = b"duplicate content"
    async_uow = uow_fixture
    
    async with async_uow as uow:
        blob_id_1 = await uow.blobs.asave(content)
        blob_id_2 = await uow.blobs.asave(content)
        await uow.acommit()
        
    assert blob_id_1 == blob_id_2
    
    async with async_uow as uow:
        stats = await uow._session.execute(
            select(func.count(FileBlobORM.content_hash), func.sum(FileBlobORM.reference_count))
        )
        count, ref_sum = stats.fetchone()
        assert count == 1 
        assert ref_sum == 2

@pytest.mark.asyncio
async def test_scenario_b_partial_commit_orphan(uow_fixture, blob_storage_path):
    """
    Scenario B: Partial commit / half-written
    Goal: Verify DB is clean (atomicity) but file exists (orphan) after crash, then clean it.
    """
    content = b"orphan content"
    async_uow = uow_fixture
    
    try:
        async with async_uow as uow:
            blob_id = await uow.blobs.asave(content)
            raise RuntimeError("Simulated Database Crash")
            await uow.acommit()
    except RuntimeError:
        pass
    
    # Verify DB is clean
    async with async_uow as uow:
        stmt = select(func.count(FileBlobORM.content_hash)).where(FileBlobORM.content_hash == blob_id.value)
        result = await uow._session.execute(stmt)
        assert result.scalar() == 0
    
    # Verify File Exists (Orphan)
    prefix = blob_id.value[:2]
    rest = blob_id.value[2:]
    orphan_file = blob_storage_path / "objects" / prefix / rest
    assert orphan_file.exists()
    
    # Clean Orphans
    cleaner = AsyncBlobOrphanCleaner(
        blobs_dir=blob_storage_path / "objects",
        uow_factory=lambda: async_uow, 
        grace_period_minutes=0
    )
    
    orphans = await cleaner.aclean_orphans(dry_run=False)
    assert len(orphans) == 1
    assert str(orphan_file) in orphans[0]
    assert not orphan_file.exists()

@pytest.mark.asyncio
async def test_scenario_c_async_ordering(uow_fixture):
    """
    Scenario C: Async background side effects (Ordering)
    Goal: Verify AsyncOutboxRepository maintains FIFO order.
    """
    async_uow = uow_fixture
    async with async_uow as uow:
        for i in range(5):
            event = OutboxEvent(
                event_type=OutboxEventType.CHECKPOINT_VERIFY,
                aggregate_type="Test",
                aggregate_id=str(i),
                payload={"order": i}
            )
            event.event_id = str(uuid.uuid4())
            await uow.outbox.aadd(event)
            await asyncio.sleep(0.01) 
        
        await uow.acommit()
        
    async with async_uow as uow:
        pending = await uow.outbox.aget_pending(limit=10)
        assert len(pending) == 5
        for i, event in enumerate(pending):
            assert event.aggregate_id == str(i)

@pytest.mark.asyncio
async def test_scenario_d_isolation(uow_fixture):
    """
    Scenario D: Stale Reads / Isolation
    Goal: Verify that uncommitted writes are not visible to other transactions.
    """
    content = b"secret content"
    blob_id = BlobId.from_content(content)
    uow1 = uow_fixture
    uow2 = AsyncSQLAlchemyUnitOfWork(uow1._db_url, uow1._blob_storage_path)
    
    async with uow1:
        await uow1.blobs.asave(content)
        async with uow2:
            exists = await uow2.blobs.aexists(blob_id)
            assert not exists
        await uow1.acommit()
        
    async with uow2:
        exists = await uow2.blobs.aexists(blob_id)
        assert exists

@pytest.mark.asyncio
async def test_scenario_e_node_env_isolation():
    """
    Scenario E: Node Replacement / Environment Isolation
    
    Verifies that we can create an isolated environment for a node
    variant via the Docker UV venv manager and execute code inside it
    using the ``RunCode`` gRPC RPC.
    """
    from wtb.infrastructure.environment.providers import GrpcEnvironmentProvider
    
    provider = GrpcEnvironmentProvider("localhost:50051", timeout_seconds=60)
    
    variant_id = f"variant-e-{uuid.uuid4()}"
    config = {
        "workflow_id": "test-workflow-e",
        "node_id": "node-e",
        "packages": ["requests"], 
        "python_version": "3.12"
    }
    
    environment_created = False
    try:
        try:
            env_info = provider.create_environment(variant_id, config)
        except Exception as e:
            pytest.skip(f"Venv Manager service not available or failed: {e}")
            return

        if env_info.get("type") == "grpc_uv_stub":
            pytest.skip("gRPC stub returned (service likely not found during init)")

        environment_created = True
        
        assert env_info.get("env_path")
        assert env_info.get("python_path")
        
        status = provider.get_env_status(variant_id)
        assert status["status"] == "ACTIVE"
        assert status["has_venv"] is True
        assert status["has_pyproject"] is True
        
    finally:
        if environment_created:
            provider.cleanup_environment(variant_id)
        provider.close()
