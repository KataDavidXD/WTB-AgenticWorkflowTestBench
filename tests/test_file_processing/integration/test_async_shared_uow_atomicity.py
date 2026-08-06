"""Regression tests for async file tracking transaction boundaries."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from wtb.application.services.async_execution_controller import (
    AsyncExecutionController,
    AsyncExecutionControllerFactory,
)
from wtb.domain.interfaces.async_file_tracking import (
    FileTrackingResult,
    IAsyncFileTrackingService,
)
from wtb.domain.models.file_processing import CheckpointFileLink, FileCommit
from wtb.domain.models.workflow import Execution, ExecutionState, ExecutionStatus
from wtb.infrastructure.database.async_unit_of_work import AsyncSQLAlchemyUnitOfWork
from wtb.infrastructure.database.file_processing_orm import (
    CheckpointFileLinkORM,
    FileBlobORM,
    FileCommitORM,
)
from wtb.infrastructure.database.models import Base, ExecutionORM, WorkflowORM
from wtb.infrastructure.file_tracking.async_filetracker_service import (
    AsyncFileTrackerService,
)


@pytest_asyncio.fixture
async def async_uow_factory(tmp_path):
    db_path = tmp_path / "shared-uow.db"
    blob_path = tmp_path / "blobs"
    db_url = f"sqlite:///{db_path}"
    async_db_url = f"sqlite+aiosqlite:///{db_path}"
    probe = AsyncSQLAlchemyUnitOfWork(db_url, str(blob_path))
    engine = probe.get_engine(async_db_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    def factory():
        return AsyncSQLAlchemyUnitOfWork(db_url, str(blob_path))

    yield factory
    await engine.dispose()


class _ControllerUoW:
    """Expose real file repositories and simple execution/outbox test doubles."""

    def __init__(self, factory, execution, *, fail_commit):
        self._factory = factory
        self._execution = execution
        self._fail_commit = fail_commit
        self._inner = None

    async def __aenter__(self):
        self._inner = await self._factory().__aenter__()
        self.blobs = self._inner.blobs
        self.file_commits = self._inner.file_commits
        self.checkpoint_file_links = self._inner.checkpoint_file_links
        self.executions = MagicMock()
        self.executions.aget = AsyncMock(return_value=self._execution)
        self.executions.aupdate = AsyncMock()
        self.outbox = MagicMock()
        self.outbox.aadd = AsyncMock()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return await self._inner.__aexit__(exc_type, exc_value, traceback)

    async def acommit(self):
        await self._inner._session.flush()
        if self._fail_commit:
            raise RuntimeError("forced main commit failure")
        await self._inner.acommit()

    async def arollback(self):
        await self._inner.arollback()


class _ConcurrentControllerUoW:
    def __init__(self, executions):
        self.executions = MagicMock()
        self.executions.aget = AsyncMock(side_effect=executions.get)
        self.executions.aupdate = AsyncMock()
        self.outbox = MagicMock()
        self.outbox.aadd = AsyncMock()
        self.acommit = AsyncMock()
        self.arollback = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None


class _YieldingSessionAdapter:
    """Expose shared mutable session state across await boundaries."""

    def __init__(self):
        self.current_execution_id = None

    async def ainitialize_session(self, execution_id, state):
        self.current_execution_id = execution_id
        await asyncio.sleep(0)
        return f"session-{execution_id}"

    async def aexecute(self, initial_state):
        await asyncio.sleep(0)
        return {"owner": self.current_execution_id}

    async def aset_current_session(self, session_id, execution_id=None):
        self.current_execution_id = execution_id or session_id.removeprefix("session-")
        await asyncio.sleep(0)
        return True

    async def aget_current_state(self):
        return {"_checkpoint_id": f"checkpoint-{self.current_execution_id}"}


class _YieldingStreamAdapter(_YieldingSessionAdapter):
    async def astream(self, initial_state, stream_mode):
        yield {"node": {"owner": self.current_execution_id}}
        await asyncio.sleep(60)


class _BlockingExecutionAdapter(_YieldingSessionAdapter):
    def __init__(self):
        super().__init__()
        self.execution_started = asyncio.Event()

    async def aexecute(self, initial_state):
        self.execution_started.set()
        await asyncio.Event().wait()


class _FailingExecutionAdapter(_YieldingSessionAdapter):
    async def aexecute(self, initial_state):
        raise RuntimeError("primary execution failed")


@pytest.mark.asyncio
async def test_concurrent_arun_calls_isolate_shared_adapter_session_state():
    executions = {
        execution_id: Execution(
            id=execution_id,
            workflow_id="workflow-1",
            state=ExecutionState(
                current_node_id="start",
                workflow_variables={"owner": execution_id},
            ),
        )
        for execution_id in ("async-e1", "async-e2")
    }
    adapter = _YieldingSessionAdapter()
    controller = AsyncExecutionController(
        adapter,
        uow_factory=lambda: _ConcurrentControllerUoW(executions),
    )

    async def run_and_read(execution_id):
        result = await controller.arun(execution_id)
        current_state = await controller.aget_current_state()
        return result, current_state

    (first, first_state), (second, second_state) = await asyncio.gather(
        run_and_read("async-e1"),
        run_and_read("async-e2"),
    )

    assert first.final_state == {"owner": "async-e1"}
    assert first.checkpoint_id == "checkpoint-async-e1"
    assert first_state == {"_checkpoint_id": "checkpoint-async-e1"}
    assert second.final_state == {"owner": "async-e2"}
    assert second.checkpoint_id == "checkpoint-async-e2"
    assert second_state == {"_checkpoint_id": "checkpoint-async-e2"}

    with pytest.raises(RuntimeError, match="No active async execution"):
        await controller.aget_current_state()


@pytest.mark.asyncio
async def test_controllers_sharing_one_adapter_serialize_session_mutation():
    executions = {
        execution_id: Execution(
            id=execution_id,
            workflow_id="workflow-1",
            state=ExecutionState(workflow_variables={"owner": execution_id}),
        )
        for execution_id in ("controller-e1", "controller-e2")
    }
    adapter = _YieldingSessionAdapter()
    first_controller = AsyncExecutionController(
        adapter,
        uow_factory=lambda: _ConcurrentControllerUoW(executions),
    )
    second_controller = AsyncExecutionController(
        adapter,
        uow_factory=lambda: _ConcurrentControllerUoW(executions),
    )

    first, second = await asyncio.gather(
        first_controller.arun("controller-e1"),
        second_controller.arun("controller-e2"),
    )

    assert first.final_state == {"owner": "controller-e1"}
    assert first.checkpoint_id == "checkpoint-controller-e1"
    assert second.final_state == {"owner": "controller-e2"}
    assert second.checkpoint_id == "checkpoint-controller-e2"


@pytest.mark.asyncio
async def test_closing_stream_early_persists_cancelled_terminal_state():
    execution = Execution(
        id="stream-close",
        workflow_id="workflow-1",
        state=ExecutionState(workflow_variables={"value": 1}),
    )
    controller = AsyncExecutionController(
        _YieldingStreamAdapter(),
        uow_factory=lambda: _ConcurrentControllerUoW({execution.id: execution}),
    )

    stream = controller.astream(execution.id)
    first_event = await anext(stream)
    assert first_event.event_type == "update"
    await stream.aclose()

    assert execution.status is ExecutionStatus.CANCELLED
    assert execution.completed_at is not None


@pytest.mark.asyncio
async def test_cancelling_arun_during_adapter_execution_persists_cancelled():
    execution = Execution(
        id="cancel-running-arun",
        workflow_id="workflow-1",
        state=ExecutionState(workflow_variables={"value": 1}),
    )
    adapter = _BlockingExecutionAdapter()
    controller = AsyncExecutionController(
        adapter,
        uow_factory=lambda: _ConcurrentControllerUoW({execution.id: execution}),
    )

    task = asyncio.create_task(controller.arun(execution.id))
    await adapter.execution_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert execution.status is ExecutionStatus.CANCELLED
    assert execution.completed_at is not None


@pytest.mark.asyncio
async def test_cancelling_arun_while_waiting_for_adapter_lock_persists_cancelled():
    execution = Execution(
        id="cancel-waiting-arun",
        workflow_id="workflow-1",
        state=ExecutionState(workflow_variables={"value": 1}),
    )
    controller = AsyncExecutionController(
        _YieldingSessionAdapter(),
        uow_factory=lambda: _ConcurrentControllerUoW({execution.id: execution}),
    )
    await controller._state_adapter_lock.acquire()
    task = asyncio.create_task(controller.arun(execution.id))
    await asyncio.sleep(0)

    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        controller._state_adapter_lock.release()

    assert execution.status is ExecutionStatus.CANCELLED
    assert execution.completed_at is not None


@pytest.mark.asyncio
async def test_execution_failure_with_durable_failed_state_returns_failed_result():
    execution = Execution(
        id="durable-failed-result",
        workflow_id="workflow-1",
        state=ExecutionState(workflow_variables={"value": 1}),
    )
    controller = AsyncExecutionController(
        _FailingExecutionAdapter(),
        uow_factory=lambda: _ConcurrentControllerUoW({execution.id: execution}),
    )

    result = await controller.arun(execution.id)

    assert result.status is ExecutionStatus.FAILED
    assert result.error_message == "primary execution failed"
    assert execution.status is ExecutionStatus.FAILED


@pytest.mark.asyncio
async def test_failed_state_persistence_failure_reraises_primary_error_with_note():
    execution = Execution(
        id="failed-persistence-unavailable",
        workflow_id="workflow-1",
        state=ExecutionState(workflow_variables={"value": 1}),
    )
    uow_calls = 0

    def uow_factory():
        nonlocal uow_calls
        uow_calls += 1
        uow = _ConcurrentControllerUoW({execution.id: execution})
        if uow_calls == 2:
            uow.acommit = AsyncMock(
                side_effect=RuntimeError("failure persistence unavailable")
            )
        return uow

    controller = AsyncExecutionController(
        _FailingExecutionAdapter(),
        uow_factory=uow_factory,
    )

    with pytest.raises(RuntimeError, match="primary execution failed") as caught:
        await controller.arun(execution.id)

    assert any(
        "failure persistence unavailable" in note
        for note in getattr(caught.value, "__notes__", ())
    )


@pytest.mark.asyncio
async def test_async_factory_constructs_memory_langgraph_config():
    factory = AsyncExecutionControllerFactory(
        uow_factory=lambda: _ConcurrentControllerUoW({}),
    )

    controller = await factory.acreate_with_langgraph(checkpointer_type="memory")

    assert controller._state_adapter._config.connection_string is None


@pytest.mark.asyncio
async def test_controller_main_commit_failure_rolls_back_all_file_metadata(
    async_uow_factory,
    tmp_path,
):
    output_file = tmp_path / "answer.txt"
    output_file.write_text("answer", encoding="utf-8")
    execution = Execution(
        id="shared-uow-main-failure",
        workflow_id="workflow-1",
        state=ExecutionState(
            current_node_id="start",
            workflow_variables={"value": 1},
        ),
    )
    adapter = MagicMock()
    adapter.ainitialize_session = AsyncMock(return_value="thread-1")
    adapter.aexecute = AsyncMock(return_value={"answer": "done"})
    adapter.aget_current_state = AsyncMock(
        return_value={"_checkpoint_id": "checkpoint-main-failure"}
    )
    tracker = AsyncFileTrackerService(async_uow_factory)
    uow_calls = 0

    def controller_uow_factory():
        nonlocal uow_calls
        uow_calls += 1
        return _ControllerUoW(
            async_uow_factory,
            execution,
            fail_commit=uow_calls == 1,
        )

    controller = AsyncExecutionController(
        adapter,
        controller_uow_factory,
        file_tracking_service=tracker,
    )

    result = await controller.arun(
        execution.id,
        track_output_files=[str(output_file)],
    )

    assert result.status == ExecutionStatus.FAILED
    assert result.error_message == "forced main commit failure"
    async with async_uow_factory() as check_uow:
        for orm_type in (FileBlobORM, FileCommitORM, CheckpointFileLinkORM):
            count = await check_uow._session.scalar(
                select(func.count()).select_from(orm_type)
            )
            assert count == 0


@pytest.mark.asyncio
async def test_duplicate_blob_conflict_does_not_rollback_outer_execution(
    async_uow_factory,
):
    content = b"shared blob"
    async with async_uow_factory() as seed_uow:
        blob_id = await seed_uow.blobs.asave(content)
        await seed_uow.acommit()

    async with async_uow_factory() as uow:
        uow._session.add(
            WorkflowORM(
                id="workflow-preserved",
                name="Preserved workflow",
                definition="{}",
            )
        )
        uow._session.add(
            ExecutionORM(
                id="execution-preserved",
                workflow_id="workflow-preserved",
                status="running",
            )
        )
        original_get = uow._session.get
        forced_miss = False

        async def get_with_one_forced_miss(entity, identifier, *args, **kwargs):
            nonlocal forced_miss
            if entity is FileBlobORM and not forced_miss:
                forced_miss = True
                return None
            return await original_get(entity, identifier, *args, **kwargs)

        uow._session.get = get_with_one_forced_miss
        assert await uow.blobs.asave(content) == blob_id
        await uow.acommit()

    async with async_uow_factory() as check_uow:
        assert await check_uow._session.get(
            ExecutionORM,
            "execution-preserved",
        ) is not None
        stored_blob = await check_uow._session.get(FileBlobORM, blob_id.value)
        assert stored_blob.reference_count == 2


@pytest.mark.asyncio
async def test_checkpoint_link_is_idempotent_for_the_same_commit(
    async_uow_factory,
):
    commit = FileCommit.create(message="immutable link")
    first_link = CheckpointFileLink.create_from_values(
        checkpoint_id="checkpoint-immutable",
        commit_id=commit.commit_id,
        file_count=1,
        total_size_bytes=4,
    )
    async with async_uow_factory() as uow:
        await uow.file_commits.asave(commit)
        await uow.checkpoint_file_links.aadd(first_link)
        await uow.acommit()

    duplicate_link = CheckpointFileLink.create_from_values(
        checkpoint_id="checkpoint-immutable",
        commit_id=commit.commit_id,
        file_count=99,
        total_size_bytes=999,
    )
    async with async_uow_factory() as uow:
        await uow.checkpoint_file_links.aadd(duplicate_link)
        await uow.acommit()

    async with async_uow_factory() as check_uow:
        stored = await check_uow.checkpoint_file_links.aget_by_checkpoint(
            "checkpoint-immutable"
        )
        assert stored.commit_id == commit.commit_id
        assert stored.linked_at == first_link.linked_at
        assert stored.file_count == 1
        assert stored.total_size_bytes == 4


@pytest.mark.asyncio
async def test_checkpoint_link_rejects_a_different_commit(async_uow_factory):
    first_commit = FileCommit.create(message="first")
    second_commit = FileCommit.create(message="second")
    first_link = CheckpointFileLink.create_from_values(
        checkpoint_id="checkpoint-conflict",
        commit_id=first_commit.commit_id,
        file_count=0,
        total_size_bytes=0,
    )
    async with async_uow_factory() as uow:
        await uow.file_commits.asave(first_commit)
        await uow.file_commits.asave(second_commit)
        await uow.checkpoint_file_links.aadd(first_link)
        await uow.acommit()

    conflicting_link = CheckpointFileLink.create_from_values(
        checkpoint_id="checkpoint-conflict",
        commit_id=second_commit.commit_id,
        file_count=0,
        total_size_bytes=0,
    )
    with pytest.raises(ValueError, match="already linked"):
        async with async_uow_factory() as uow:
            await uow.checkpoint_file_links.aadd(conflicting_link)
            await uow.acommit()

    async with async_uow_factory() as check_uow:
        stored = await check_uow.checkpoint_file_links.aget_by_checkpoint(
            "checkpoint-conflict"
        )
        assert stored.commit_id == first_commit.commit_id


class _InUoWTracker:
    def __init__(self):
        self.calls = []

    async def atrack_and_link_in_uow(self, uow, **kwargs):
        self.calls.append((uow, kwargs))
        return FileTrackingResult("commit-1", 1, 4)

    async def atrack_and_link(self, **kwargs):
        raise AssertionError("external transaction path must not be used")


class _LegacyInterfaceTracker(IAsyncFileTrackingService):
    """A valid legacy implementation without the shared-UoW capability."""

    def __init__(self):
        self.external_calls = []

    async def atrack_files(self, file_paths, message, checkpoint_id=None):
        raise AssertionError("not used")

    async def arestore_files(self, checkpoint_id, output_dir):
        raise AssertionError("not used")

    async def atrack_and_link(self, **kwargs):
        self.external_calls.append(kwargs)
        return FileTrackingResult("external-commit", 1, 4)


@pytest.mark.asyncio
async def test_save_checkpoint_reuses_main_uow_when_tracker_has_capability():
    execution = Execution(
        id="checkpoint-shared-uow",
        workflow_id="workflow-1",
        session_id="session-checkpoint-shared-uow",
        state=ExecutionState(workflow_variables={"value": 1}),
    )
    uow = MagicMock()
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=None)
    uow.executions.aupdate = AsyncMock()
    uow.acommit = AsyncMock()
    adapter = MagicMock()
    adapter.aset_current_session = AsyncMock(return_value=True)
    adapter.aget_current_state = AsyncMock(return_value={"value": 2})
    adapter.asave_checkpoint = AsyncMock(return_value="checkpoint-shared")
    tracker = _InUoWTracker()
    controller = AsyncExecutionController(
        adapter,
        uow_factory=lambda: uow,
        file_tracking_service=tracker,
    )
    controller._current_execution_var.set(execution)

    checkpoint_id = await controller.asave_checkpoint(
        "node-1",
        track_files=["answer.txt"],
    )

    assert checkpoint_id == "checkpoint-shared"
    assert tracker.calls == [
        (
            uow,
            {
                "checkpoint_id": "checkpoint-shared",
                "file_paths": ["answer.txt"],
                "message": "Checkpoint node-1 files",
            },
        )
    ]


@pytest.mark.asyncio
async def test_legacy_tracker_fails_closed_without_opening_its_own_transaction():
    execution = Execution(
        id="checkpoint-legacy-tracker",
        workflow_id="workflow-1",
        session_id="session-checkpoint-legacy-tracker",
        state=ExecutionState(workflow_variables={"value": 1}),
    )
    uow = MagicMock()
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=None)
    uow.executions.aupdate = AsyncMock()
    uow.acommit = AsyncMock(side_effect=RuntimeError("main commit failed"))
    adapter = MagicMock()
    adapter.aset_current_session = AsyncMock(return_value=True)
    adapter.aget_current_state = AsyncMock(return_value={"value": 2})
    adapter.asave_checkpoint = AsyncMock(return_value="checkpoint-legacy")
    tracker = _LegacyInterfaceTracker()
    controller = AsyncExecutionController(
        adapter,
        uow_factory=lambda: uow,
        file_tracking_service=tracker,
    )
    controller._current_execution_var.set(execution)

    with pytest.raises(RuntimeError, match="shared unit of work"):
        await controller.asave_checkpoint(
            "node-1",
            track_files=["answer.txt"],
        )

    assert tracker.external_calls == []
    uow.acommit.assert_not_awaited()
