from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from wtb.application.services.async_execution_controller import (
    AsyncExecutionController,
)
from wtb.domain.interfaces.async_file_tracking import FileTrackingResult
from wtb.domain.models.file_processing import BlobId, DuplicateFileError
from wtb.domain.models.workflow import Execution, ExecutionState, ExecutionStatus
from wtb.infrastructure.file_tracking.async_filetracker_service import (
    AsyncFileTrackerService,
)


def _tracking_uow():
    uow = MagicMock()
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=None)
    uow.blobs.asave = AsyncMock(
        side_effect=lambda content: BlobId.from_content(content)
    )
    uow.file_commits.asave = AsyncMock()
    uow.checkpoint_file_links.aadd = AsyncMock()
    uow.acommit = AsyncMock()
    return uow


@pytest.mark.asyncio
async def test_atrack_files_preflights_missing_path_before_any_storage_write(
    tmp_path,
):
    existing = tmp_path / "existing.txt"
    existing.write_text("must remain unstored", encoding="utf-8")
    missing = tmp_path / "missing.txt"
    uow = _tracking_uow()
    uow_factory = MagicMock(return_value=uow)
    service = AsyncFileTrackerService(uow_factory=uow_factory)

    with pytest.raises(FileNotFoundError):
        await service.atrack_files(
            [str(existing), str(missing)],
            message="missing-path preflight",
            checkpoint_id="checkpoint-missing",
        )

    uow_factory.assert_not_called()
    uow.blobs.asave.assert_not_awaited()
    uow.file_commits.asave.assert_not_awaited()
    uow.checkpoint_file_links.aadd.assert_not_awaited()
    uow.acommit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_kind", ["directory", "empty"])
async def test_atrack_files_preflights_non_file_paths_before_storage_write(
    tmp_path,
    invalid_kind,
):
    existing = tmp_path / "existing.txt"
    existing.write_text("must remain unstored", encoding="utf-8")
    invalid_path = str(tmp_path) if invalid_kind == "directory" else ""
    uow = _tracking_uow()
    uow_factory = MagicMock(return_value=uow)
    service = AsyncFileTrackerService(uow_factory=uow_factory)

    with pytest.raises((OSError, ValueError)):
        await service.atrack_files(
            [str(existing), invalid_path],
            message="non-file preflight",
            checkpoint_id="checkpoint-non-file",
        )

    uow_factory.assert_not_called()
    uow.blobs.asave.assert_not_awaited()
    uow.file_commits.asave.assert_not_awaited()
    uow.checkpoint_file_links.aadd.assert_not_awaited()
    uow.acommit.assert_not_awaited()


@pytest.mark.asyncio
async def test_atrack_files_preflights_duplicate_path_before_storage_write(
    tmp_path,
):
    existing = tmp_path / "existing.txt"
    existing.write_text("must remain unstored", encoding="utf-8")
    uow = _tracking_uow()
    uow_factory = MagicMock(return_value=uow)
    service = AsyncFileTrackerService(uow_factory=uow_factory)

    with pytest.raises(DuplicateFileError):
        await service.atrack_files(
            [str(existing), str(existing)],
            message="duplicate preflight",
            checkpoint_id="checkpoint-duplicate",
        )

    uow_factory.assert_not_called()
    uow.blobs.asave.assert_not_awaited()
    uow.file_commits.asave.assert_not_awaited()
    uow.checkpoint_file_links.aadd.assert_not_awaited()
    uow.acommit.assert_not_awaited()


def _execution_uow(execution: Execution):
    uow = MagicMock()
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=None)
    uow.executions.aget = AsyncMock(return_value=execution)
    uow.executions.aupdate = AsyncMock()
    uow.outbox.aadd = AsyncMock()
    uow.acommit = AsyncMock()
    uow.arollback = AsyncMock()
    return uow


@pytest.mark.asyncio
@pytest.mark.parametrize("files_tracked", [0, 1])
async def test_async_controller_fails_when_file_tracking_is_incomplete(
    files_tracked,
):
    execution = Execution(
        id="async-incomplete-files",
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
        return_value={"_checkpoint_id": "checkpoint-final"}
    )
    file_tracking = MagicMock()
    file_tracking.atrack_and_link_in_uow = AsyncMock(
        return_value=FileTrackingResult(
            commit_id="file-commit",
            files_tracked=files_tracked,
            total_size_bytes=4,
        )
    )
    uow = _execution_uow(execution)
    controller = AsyncExecutionController(
        adapter,
        uow_factory=lambda: uow,
        file_tracking_service=file_tracking,
    )

    result = await controller.arun(
        execution.id,
        track_output_files=["one.txt", "two.txt"],
    )

    assert result.status == ExecutionStatus.FAILED
    assert "Expected 2 files to be tracked" in result.error_message
    assert execution.status == ExecutionStatus.FAILED
    uow.outbox.aadd.assert_not_awaited()
