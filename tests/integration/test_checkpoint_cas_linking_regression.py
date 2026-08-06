"""Regression coverage for checkpoint-centric CAS file linking."""

from __future__ import annotations

import operator
import uuid
from pathlib import Path
from typing import Annotated, TypedDict
from unittest.mock import AsyncMock, MagicMock

import pytest

from wtb.application.services.execution_controller import (
    DefaultNodeExecutor,
    ExecutionController,
)
from wtb.domain.interfaces.async_file_tracking import FileTrackingResult
from wtb.domain.interfaces.file_tracking import CheckpointLinkError
from wtb.domain.interfaces.state_adapter import CheckpointTrigger
from wtb.domain.models.workflow import (
    Execution,
    ExecutionState,
    ExecutionStatus,
    TestWorkflow,
    WorkflowEdge,
    WorkflowNode,
)
from wtb.infrastructure.adapters.inmemory_state_adapter import InMemoryStateAdapter
from wtb.infrastructure.database.inmemory_unit_of_work import InMemoryUnitOfWork
from wtb.infrastructure.file_tracking.sqlite_service import SqliteFileTrackingService

try:
    from langgraph.graph import END, StateGraph

    from wtb.infrastructure.adapters.langgraph_state_adapter import (
        LANGGRAPH_AVAILABLE,
        LangGraphConfig,
        LangGraphStateAdapter,
    )
except ImportError:
    END = None
    StateGraph = None
    LANGGRAPH_AVAILABLE = False
    LangGraphConfig = None
    LangGraphStateAdapter = None


class FileState(TypedDict, total=False):
    value: int
    messages: Annotated[list[str], operator.add]
    _output_files: dict[str, str]
    branch: str | None
    answer: str
    observed_file: str


def _draft_node(state: FileState) -> FileState:
    return {
        "value": state.get("value", 0) + 1,
        "messages": ["draft"],
        "_output_files": {
            "chapter.txt": "draft-v1",
            "nested/info.txt": "draft metadata",
        },
    }


def _revise_node(state: FileState) -> FileState:
    suffix = state.get("branch") or "main"
    return {
        "value": state.get("value", 0) + 1,
        "messages": ["revise"],
        "_output_files": {
            "chapter.txt": f"revise-v2-{suffix}",
            "nested/info.txt": f"revise metadata {suffix}",
        },
        "answer": f"done-{suffix}",
    }


def _file_output_graph():
    graph = StateGraph(FileState)
    graph.add_node("draft", _draft_node)
    graph.add_node("revise", _revise_node)
    graph.set_entry_point("draft")
    graph.add_edge("draft", "revise")
    graph.add_edge("revise", END)
    return graph


def _prefix_file_reader_graph(output_file: Path, node_calls: list[str]):
    def materialize_prefix(state: FileState) -> FileState:
        node_calls.append("materialize")
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text("prefix-v1", encoding="utf-8")
        return {
            "messages": ["materialize"],
            "_output_files": {"prefix.txt": "prefix-v1"},
        }

    def consume_prefix(state: FileState) -> FileState:
        node_calls.append("consume")
        return {
            "messages": ["consume"],
            "observed_file": output_file.read_text(encoding="utf-8"),
        }

    graph = StateGraph(FileState)
    graph.add_node("materialize", materialize_prefix)
    graph.add_node("consume", consume_prefix)
    graph.set_entry_point("materialize")
    graph.add_edge("materialize", "consume")
    graph.add_edge("consume", END)
    return graph


def _workflow(wf_id: str = "checkpoint-cas-wf") -> TestWorkflow:
    workflow = TestWorkflow(id=wf_id, name=wf_id, entry_point="start")
    workflow.add_node(WorkflowNode(id="start", name="Start", type="start"))
    workflow.add_node(WorkflowNode(id="end", name="End", type="end"))
    workflow.add_edge(WorkflowEdge(source_id="start", target_id="end"))
    return workflow


def _langgraph_controller(tmp_path: Path):
    uow = InMemoryUnitOfWork()
    uow.__enter__()
    adapter = LangGraphStateAdapter(LangGraphConfig.for_testing())
    file_tracking = SqliteFileTrackingService(workspace_path=tmp_path)
    controller = ExecutionController(
        execution_repository=uow.executions,
        workflow_repository=uow.workflows,
        state_adapter=adapter,
        node_executor=DefaultNodeExecutor(),
        unit_of_work=uow,
        file_tracking_service=file_tracking,
        output_dir=str(tmp_path / "outputs"),
    )
    workflow = _workflow()
    uow.workflows.add(workflow)
    uow.commit()
    return controller, adapter, file_tracking, workflow


def _output_checkpoints(adapter, file_tracking):
    result = []
    for checkpoint in adapter.get_checkpoint_history():
        checkpoint_id = checkpoint["checkpoint_id"]
        values = checkpoint.get("values") or {}
        output_files = values.get("_output_files")
        if isinstance(output_files, dict) and output_files:
            commit_id = file_tracking.get_commit_for_checkpoint(checkpoint_id)
            result.append((checkpoint, output_files, commit_id))
    return result


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not installed")
def test_langgraph_checkpoint_history_links_each_output_checkpoint_to_cas(tmp_path):
    controller, adapter, file_tracking, workflow = _langgraph_controller(tmp_path)

    execution = controller.create_execution(
        workflow,
        initial_state={"value": 0, "messages": []},
    )
    execution = controller.run(execution.id, graph=_file_output_graph())

    assert execution.status == ExecutionStatus.COMPLETED
    linked_checkpoints = _output_checkpoints(adapter, file_tracking)
    assert len(linked_checkpoints) >= 2

    for checkpoint, output_files, commit_id in linked_checkpoints:
        assert commit_id, f"missing CAS commit for {checkpoint['checkpoint_id']}"
        tracked = file_tracking.get_tracked_files(commit_id)
        assert len(tracked) == len(output_files)

    draft_cp, _, _ = next(
        item
        for item in linked_checkpoints
        if item[1].get("chapter.txt") == "draft-v1"
    )
    draft_restore = tmp_path / "restore-draft"
    restored = file_tracking.restore_checkpoint_to_workspace(
        draft_cp["checkpoint_id"],
        str(draft_restore),
    )
    assert restored.success is True
    assert (draft_restore / "chapter.txt").read_text(encoding="utf-8") == "draft-v1"


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not installed")
def test_rollback_resume_and_fork_resume_use_checkpoint_file_links(tmp_path):
    controller, adapter, file_tracking, workflow = _langgraph_controller(tmp_path)

    execution = controller.create_execution(
        workflow,
        initial_state={"value": 0, "messages": []},
    )
    execution = controller.run(execution.id, graph=_file_output_graph())
    linked_checkpoints = _output_checkpoints(adapter, file_tracking)
    draft_cp, _, _ = next(
        item
        for item in linked_checkpoints
        if item[1].get("chapter.txt") == "draft-v1"
    )
    checkpoint_id = draft_cp["checkpoint_id"]

    rolled_back = controller.rollback(execution.id, checkpoint_id)
    assert rolled_back.status == ExecutionStatus.PAUSED
    assert rolled_back.state.workflow_variables["_file_restore_status"]["success"] is True
    assert (tmp_path / "outputs" / "chapter.txt").read_text(encoding="utf-8") == "draft-v1"

    resumed = controller.resume(execution.id)
    assert resumed.status == ExecutionStatus.COMPLETED

    forked = controller.fork(
        execution.id,
        checkpoint_id,
        new_initial_state={"branch": "fork"},
    )
    assert forked.status == ExecutionStatus.PAUSED

    advanced = controller.resume(forked.id)
    assert advanced.status == ExecutionStatus.COMPLETED
    assert advanced.state.workflow_variables["answer"] == "done-fork"


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not installed")
@pytest.mark.parametrize("restored_count", [0, 1])
def test_rollback_fails_closed_when_declared_files_are_not_fully_restored(
    tmp_path,
    monkeypatch,
    restored_count,
):
    controller, adapter, file_tracking, workflow = _langgraph_controller(tmp_path)
    execution = controller.create_execution(
        workflow,
        initial_state={"value": 0, "messages": []},
    )
    execution = controller.run(execution.id, graph=_file_output_graph())
    checkpoint, output_files, _ = next(
        item
        for item in _output_checkpoints(adapter, file_tracking)
        if item[1].get("chapter.txt") == "draft-v1"
    )
    assert len(output_files) == 2

    rollback_spy = MagicMock(wraps=adapter.rollback)
    monkeypatch.setattr(adapter, "rollback", rollback_spy)
    incomplete = MagicMock(files_restored=restored_count, error_message=None)
    monkeypatch.setattr(
        file_tracking,
        "restore_from_checkpoint",
        MagicMock(return_value=incomplete),
    )

    with pytest.raises(RuntimeError, match="Expected 2 checkpoint files"):
        controller.rollback(execution.id, checkpoint["checkpoint_id"])

    rollback_spy.assert_not_called()
    persisted = controller._exec_repo.get(execution.id)
    assert persisted.status == ExecutionStatus.COMPLETED
    assert "resume_checkpoint_id" not in persisted.metadata


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not installed")
def test_two_restore_targets_materialize_different_checkpoints_without_overwrite(tmp_path):
    controller, adapter, file_tracking, workflow = _langgraph_controller(tmp_path)

    execution = controller.create_execution(
        workflow,
        initial_state={"value": 0, "messages": []},
    )
    controller.run(execution.id, graph=_file_output_graph())

    linked_checkpoints = _output_checkpoints(adapter, file_tracking)
    draft_cp, _, _ = next(
        item
        for item in linked_checkpoints
        if item[1].get("chapter.txt") == "draft-v1"
    )
    revise_cp, _, _ = next(
        item
        for item in linked_checkpoints
        if item[1].get("chapter.txt", "").startswith("revise-v2")
    )

    first_target = tmp_path / "process-a"
    second_target = tmp_path / "process-b"
    file_tracking.restore_checkpoint_to_workspace(draft_cp["checkpoint_id"], str(first_target))
    file_tracking.restore_checkpoint_to_workspace(revise_cp["checkpoint_id"], str(second_target))

    assert (first_target / "chapter.txt").read_text(encoding="utf-8") == "draft-v1"
    assert (second_target / "chapter.txt").read_text(encoding="utf-8").startswith("revise-v2")
    assert (tmp_path / "outputs" / "chapter.txt").read_text(encoding="utf-8").startswith("revise-v2")


def test_missing_checkpoint_file_link_fails_rollback_and_fork_loudly(tmp_path):
    uow = InMemoryUnitOfWork()
    uow.__enter__()
    adapter = InMemoryStateAdapter()
    file_tracking = SqliteFileTrackingService(workspace_path=tmp_path)
    controller = ExecutionController(
        execution_repository=uow.executions,
        workflow_repository=uow.workflows,
        state_adapter=adapter,
        node_executor=DefaultNodeExecutor(),
        unit_of_work=uow,
        file_tracking_service=file_tracking,
        output_dir=str(tmp_path / "outputs"),
    )
    workflow = _workflow("missing-link-wf")
    uow.workflows.add(workflow)
    uow.commit()

    execution = controller.create_execution(workflow)
    execution.status = ExecutionStatus.COMPLETED
    uow.executions.update(execution)
    checkpoint_id = adapter.save_checkpoint(
        state=ExecutionState(
            current_node_id="start",
            workflow_variables={"_output_files": {"lost.txt": "not linked"}},
        ),
        node_id="start",
        trigger=CheckpointTrigger.AUTO,
    )

    with pytest.raises(RuntimeError, match="no linked file commit"):
        controller.rollback(execution.id, checkpoint_id)

    with pytest.raises(RuntimeError, match="no linked file commit"):
        controller.fork(execution.id, checkpoint_id)

    with pytest.raises(CheckpointLinkError):
        file_tracking.restore_checkpoint_to_workspace(
            checkpoint_id,
            str(tmp_path / "restore-missing"),
        )


def test_state_only_checkpoint_does_not_require_cas_link_for_time_travel(tmp_path):
    uow = InMemoryUnitOfWork()
    uow.__enter__()
    adapter = InMemoryStateAdapter()
    file_tracking = SqliteFileTrackingService(workspace_path=tmp_path)
    controller = ExecutionController(
        execution_repository=uow.executions,
        workflow_repository=uow.workflows,
        state_adapter=adapter,
        node_executor=DefaultNodeExecutor(),
        unit_of_work=uow,
        file_tracking_service=file_tracking,
        output_dir=str(tmp_path / "outputs"),
    )
    workflow = _workflow("state-only-wf")
    uow.workflows.add(workflow)
    uow.commit()

    execution = controller.create_execution(workflow, initial_state={"value": 1})
    execution.status = ExecutionStatus.COMPLETED
    uow.executions.update(execution)
    checkpoint_id = adapter.save_checkpoint(
        state=ExecutionState(
            current_node_id="start",
            workflow_variables={"value": 2},
        ),
        node_id="start",
        trigger=CheckpointTrigger.AUTO,
    )

    rolled_back = controller.rollback(execution.id, checkpoint_id)
    assert rolled_back.status == ExecutionStatus.PAUSED
    assert rolled_back.state.workflow_variables["value"] == 2
    resumed = controller.resume(execution.id)
    assert resumed.status == ExecutionStatus.COMPLETED

    forked = controller.fork(execution.id, checkpoint_id)
    assert forked.status == ExecutionStatus.PAUSED
    advanced = controller.resume(
        forked.id,
        modified_state={
            "_output_files": {"new.txt": "created only after fork"}
        },
    )
    assert advanced.status == ExecutionStatus.COMPLETED


@pytest.mark.parametrize(
    "unsafe_name",
    ["../escape.txt", "nested/../../escape.txt"],
)
def test_output_file_paths_are_prevalidated_before_any_write(tmp_path, unsafe_name):
    controller, _, _, _ = _langgraph_controller(tmp_path)
    output_dir = tmp_path / "outputs"

    with pytest.raises(ValueError, match="output file path"):
        controller._write_output_files(
            {
                "safe.txt": "must-not-be-written",
                unsafe_name: "escape",
            }
        )

    assert not (output_dir / "safe.txt").exists()
    assert not (tmp_path / "escape.txt").exists()


def test_absolute_output_file_path_is_rejected_before_any_write(tmp_path):
    controller, _, _, _ = _langgraph_controller(tmp_path)
    output_dir = tmp_path / "outputs"
    outside = tmp_path / "outside.txt"

    with pytest.raises(ValueError, match="output file path"):
        controller._write_output_files(
            {
                "safe.txt": "must-not-be-written",
                str(outside.resolve()): "escape",
            }
        )

    assert not (output_dir / "safe.txt").exists()
    assert not outside.exists()


def test_output_file_symlink_redirect_is_rejected_before_any_write(tmp_path):
    controller, _, _, _ = _langgraph_controller(tmp_path)
    output_dir = tmp_path / "outputs"
    output_dir.mkdir(parents=True)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    redirect = output_dir / "redirect"
    try:
        redirect.symlink_to(outside_dir, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    with pytest.raises(ValueError, match="output file path"):
        controller._write_output_files(
            {
                "safe.txt": "must-not-be-written",
                "redirect/escape.txt": "escape",
            }
        )

    assert not (output_dir / "safe.txt").exists()
    assert not (outside_dir / "escape.txt").exists()


def test_output_file_contents_are_preencoded_before_any_write(tmp_path):
    controller, _, _, _ = _langgraph_controller(tmp_path)
    output_dir = tmp_path / "outputs"

    with pytest.raises(TypeError):
        controller._write_output_files(
            {
                "safe.txt": "must-not-be-written",
                "invalid.json": object(),
            }
        )

    assert not (output_dir / "safe.txt").exists()
    assert not (output_dir / "invalid.json").exists()


def _async_uow_for(execution: Execution):
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
async def test_async_checkpoint_fork_execution_starts_paused():
    from wtb.application.services.async_execution_controller import (
        AsyncExecutionController,
    )

    source = Execution(
        id="source-exec",
        workflow_id="workflow-1",
        session_id="session-source-exec",
        status=ExecutionStatus.PAUSED,
        state=ExecutionState(workflow_variables={"value": 1}),
    )
    adapter = MagicMock()
    adapter.aset_current_session = AsyncMock(return_value=True)
    adapter.acreate_fork = AsyncMock(return_value=MagicMock())
    adapter.aload_checkpoint = AsyncMock(
        return_value=ExecutionState(workflow_variables={"value": 1})
    )

    uow = MagicMock()
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=None)
    uow.executions.aadd = AsyncMock()
    uow.acommit = AsyncMock()

    controller = AsyncExecutionController(adapter, uow_factory=lambda: uow)
    controller._current_execution_var.set(source)

    await controller.afork(str(uuid.uuid4()), from_checkpoint_id="cp-1")

    forked_execution = uow.executions.aadd.call_args.args[0]
    assert forked_execution.status == ExecutionStatus.PAUSED


@pytest.mark.asyncio
async def test_async_run_links_files_to_real_checkpoint_from_history():
    from wtb.application.services.async_execution_controller import (
        AsyncExecutionController,
    )

    execution = Execution(
        id="async-history-exec",
        workflow_id="workflow-1",
        state=ExecutionState(workflow_variables={"value": 1}),
    )
    adapter = MagicMock()
    adapter.ainitialize_session = AsyncMock(return_value="thread-1")
    adapter.aexecute = AsyncMock(return_value={"answer": "done"})
    adapter.aget_current_state = AsyncMock(return_value={"answer": "done"})
    adapter.aget_checkpoints = AsyncMock(
        return_value=[{"checkpoint_id": "langgraph-final", "values": {"answer": "done"}}]
    )
    adapter.asave_checkpoint = AsyncMock()
    file_tracking = MagicMock()
    file_tracking.atrack_files = AsyncMock()
    file_tracking.atrack_and_link_in_uow = AsyncMock(
        return_value=FileTrackingResult("cas-commit", 1, 4)
    )
    uow = _async_uow_for(execution)
    controller = AsyncExecutionController(
        adapter,
        uow_factory=lambda: uow,
        file_tracking_service=file_tracking,
    )

    result = await controller.arun(
        execution.id,
        track_output_files=["answer.txt"],
    )

    assert result.status == ExecutionStatus.COMPLETED
    assert result.checkpoint_id == "langgraph-final"
    assert execution.checkpoint_id == "langgraph-final"
    file_tracking.atrack_and_link_in_uow.assert_awaited_once_with(
        uow,
        checkpoint_id="langgraph-final",
        file_paths=["answer.txt"],
        message=f"Execution {execution.id} output files",
    )
    file_tracking.atrack_files.assert_not_awaited()
    adapter.asave_checkpoint.assert_not_awaited()
    verify_event = uow.outbox.aadd.call_args.args[0]
    assert verify_event.payload["checkpoint_id"] == "langgraph-final"


@pytest.mark.asyncio
@pytest.mark.parametrize("restored_count", [0, 1])
async def test_async_rollback_fails_closed_on_incomplete_file_restore(
    tmp_path,
    restored_count,
):
    from wtb.application.services.async_execution_controller import (
        AsyncExecutionController,
    )

    execution = Execution(
        id="async-rollback-incomplete",
        workflow_id="workflow-1",
        session_id="session-async-rollback-incomplete",
        status=ExecutionStatus.COMPLETED,
        state=ExecutionState(workflow_variables={"value": 1}),
    )
    restored_state = ExecutionState(
        current_node_id="start",
        workflow_variables={
            "_output_files": {"one.txt": "one", "two.txt": "two"},
        },
    )
    adapter = MagicMock()
    adapter.aset_current_session = AsyncMock(return_value=True)
    adapter.aload_checkpoint = AsyncMock(return_value=restored_state)
    adapter.arollback = AsyncMock(return_value=restored_state)
    file_tracking = MagicMock()
    file_tracking.arestore_files = AsyncMock(return_value=restored_count)
    uow = _async_uow_for(execution)
    controller = AsyncExecutionController(
        adapter,
        uow_factory=lambda: uow,
        file_tracking_service=file_tracking,
    )
    controller._current_execution_var.set(execution)

    with pytest.raises(RuntimeError, match="Expected 2 checkpoint files"):
        await controller.arollback_to_checkpoint(
            "checkpoint-1",
            restore_output_dir=tmp_path / "restore",
        )

    adapter.aload_checkpoint.assert_awaited_once_with("checkpoint-1")
    adapter.arollback.assert_not_awaited()
    uow.executions.aupdate.assert_not_awaited()
    uow.acommit.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_rollback_restores_files_before_committing_state(tmp_path):
    from wtb.application.services.async_execution_controller import (
        AsyncExecutionController,
    )

    execution = Execution(
        id="async-rollback-success",
        workflow_id="workflow-1",
        session_id="session-async-rollback-success",
        status=ExecutionStatus.COMPLETED,
        state=ExecutionState(workflow_variables={"value": 1}),
    )
    restored_state = ExecutionState(
        current_node_id="start",
        workflow_variables={"_output_files": {"one.txt": "one"}},
    )
    adapter = MagicMock()
    adapter.aset_current_session = AsyncMock(return_value=True)
    adapter.aload_checkpoint = AsyncMock(return_value=restored_state)
    adapter.arollback = AsyncMock(return_value=restored_state)
    file_tracking = MagicMock()
    file_tracking.arestore_files = AsyncMock(return_value=1)
    uow = _async_uow_for(execution)
    controller = AsyncExecutionController(
        adapter,
        uow_factory=lambda: uow,
        file_tracking_service=file_tracking,
    )
    controller._current_execution_var.set(execution)
    restore_dir = tmp_path / "restore"

    result = await controller.arollback_to_checkpoint(
        "checkpoint-1",
        restore_output_dir=restore_dir,
    )

    assert result is restored_state
    file_tracking.arestore_files.assert_awaited_once_with(
        "checkpoint-1",
        restore_dir,
    )
    uow.executions.aupdate.assert_awaited_once_with(execution)
    uow.acommit.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_run_saves_fallback_checkpoint_when_history_is_empty():
    from wtb.application.services.async_execution_controller import (
        AsyncExecutionController,
    )

    execution = Execution(
        id="async-fallback-exec",
        workflow_id="workflow-1",
        state=ExecutionState(current_node_id="start", workflow_variables={"value": 1}),
    )
    adapter = MagicMock()
    adapter.ainitialize_session = AsyncMock(return_value="thread-2")
    adapter.aexecute = AsyncMock(return_value={"answer": "done"})
    adapter.aget_current_state = AsyncMock(return_value={"control": "metadata"})
    adapter.aget_checkpoints = AsyncMock(return_value=[])
    adapter.asave_checkpoint = AsyncMock(return_value="langgraph-saved")
    uow = _async_uow_for(execution)
    controller = AsyncExecutionController(adapter, uow_factory=lambda: uow)

    result = await controller.arun(execution.id)

    assert result.status == ExecutionStatus.COMPLETED
    assert result.checkpoint_id == "langgraph-saved"
    saved_state = adapter.asave_checkpoint.call_args.kwargs["state"]
    assert saved_state.workflow_variables == {"answer": "done"}
    assert adapter.asave_checkpoint.call_args.kwargs["node_id"] == "start"
    verify_event = uow.outbox.aadd.call_args.args[0]
    assert verify_event.payload["checkpoint_id"] == "langgraph-saved"


@pytest.mark.asyncio
async def test_async_run_fails_closed_when_fallback_has_no_current_node():
    from wtb.application.services.async_execution_controller import (
        AsyncExecutionController,
    )

    execution = Execution(
        id="async-no-node-exec",
        workflow_id="workflow-1",
        state=ExecutionState(workflow_variables={"value": 1}),
    )
    adapter = MagicMock()
    adapter.ainitialize_session = AsyncMock(return_value="thread-3")
    adapter.aexecute = AsyncMock(return_value={"answer": "done"})
    adapter.aget_current_state = AsyncMock(return_value={})
    adapter.aget_checkpoints = AsyncMock(return_value=[])
    adapter.asave_checkpoint = AsyncMock(return_value="must-not-be-used")
    uow = _async_uow_for(execution)
    controller = AsyncExecutionController(adapter, uow_factory=lambda: uow)

    result = await controller.arun(execution.id)

    assert result.status == ExecutionStatus.FAILED
    assert "current node" in result.error_message
    adapter.asave_checkpoint.assert_not_awaited()
    uow.outbox.aadd.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_run_fails_closed_when_file_link_has_no_commit():
    from wtb.application.services.async_execution_controller import (
        AsyncExecutionController,
    )

    execution = Execution(
        id="async-link-failure-exec",
        workflow_id="workflow-1",
        state=ExecutionState(current_node_id="start", workflow_variables={"value": 1}),
    )
    adapter = MagicMock()
    adapter.ainitialize_session = AsyncMock(return_value="thread-4")
    adapter.aexecute = AsyncMock(return_value={"answer": "done"})
    adapter.aget_current_state = AsyncMock(return_value={"answer": "done"})
    adapter.aget_checkpoints = AsyncMock(
        return_value=[{"checkpoint_id": "langgraph-final"}]
    )
    adapter.asave_checkpoint = AsyncMock()
    file_tracking = MagicMock()
    file_tracking.atrack_and_link_in_uow = AsyncMock(
        return_value=FileTrackingResult("", 0, 0)
    )
    uow = _async_uow_for(execution)
    controller = AsyncExecutionController(
        adapter,
        uow_factory=lambda: uow,
        file_tracking_service=file_tracking,
    )

    result = await controller.arun(
        execution.id,
        track_output_files=["answer.txt"],
    )

    assert result.status == ExecutionStatus.FAILED
    assert "file commit" in result.error_message
    assert execution.status == ExecutionStatus.FAILED
    uow.outbox.aadd.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_run_commit_failure_uses_clean_uow_to_persist_failed_state():
    from wtb.application.services.async_execution_controller import (
        AsyncExecutionController,
    )

    execution = Execution(
        id="async-main-commit-failure",
        workflow_id="workflow-1",
        state=ExecutionState(current_node_id="start", workflow_variables={"value": 1}),
    )
    adapter = MagicMock()
    adapter.ainitialize_session = AsyncMock(return_value="thread-commit-failure")
    adapter.aexecute = AsyncMock(return_value={"answer": "done"})
    adapter.aget_current_state = AsyncMock(return_value={"_checkpoint_id": "cp-final"})
    adapter.aget_checkpoints = AsyncMock()
    events = []
    primary_uow = _async_uow_for(execution)

    async def fail_primary_commit():
        events.append("primary_commit")
        raise RuntimeError("primary commit failed")

    async def rollback_primary():
        events.append("primary_rollback")

    primary_uow.acommit = AsyncMock(side_effect=fail_primary_commit)
    primary_uow.arollback = AsyncMock(side_effect=rollback_primary)
    failure_uow = _async_uow_for(execution)
    failure_uow.executions.aget = AsyncMock(
        side_effect=lambda execution_id: events.append("failure_load") or execution
    )
    failure_uow.executions.aupdate = AsyncMock(
        side_effect=lambda entity: events.append("failure_update")
    )
    failure_uow.acommit = AsyncMock(
        side_effect=lambda: events.append("failure_commit")
    )
    uows = iter([primary_uow, failure_uow])
    controller = AsyncExecutionController(adapter, uow_factory=lambda: next(uows))

    result = await controller.arun(execution.id)

    assert result.status == ExecutionStatus.FAILED
    assert result.error_message == "primary commit failed"
    assert execution.status == ExecutionStatus.FAILED
    assert execution.error_message == "primary commit failed"
    assert events == [
        "primary_commit",
        "primary_rollback",
        "failure_load",
        "failure_update",
        "failure_commit",
    ]


@pytest.mark.asyncio
async def test_async_run_secondary_failure_does_not_mask_primary_commit_error():
    from wtb.application.services.async_execution_controller import (
        AsyncExecutionController,
    )

    execution = Execution(
        id="async-secondary-commit-failure",
        workflow_id="workflow-1",
        state=ExecutionState(current_node_id="start", workflow_variables={"value": 1}),
    )
    adapter = MagicMock()
    adapter.ainitialize_session = AsyncMock(return_value="thread-secondary-failure")
    adapter.aexecute = AsyncMock(return_value={"answer": "done"})
    adapter.aget_current_state = AsyncMock(return_value={"_checkpoint_id": "cp-final"})
    primary_uow = _async_uow_for(execution)
    primary_uow.acommit = AsyncMock(
        side_effect=RuntimeError("primary commit failed")
    )
    failure_uow = _async_uow_for(execution)
    failure_uow.acommit = AsyncMock(
        side_effect=RuntimeError("failure persistence failed")
    )
    uows = iter([primary_uow, failure_uow])
    controller = AsyncExecutionController(adapter, uow_factory=lambda: next(uows))

    with pytest.raises(RuntimeError, match="primary commit failed") as raised:
        await controller.arun(execution.id)

    assert any(
        "failure persistence failed" in note
        for note in getattr(raised.value, "__notes__", ())
    )
    primary_uow.arollback.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not installed")
async def test_async_run_uses_checkpoint_id_from_real_langgraph_history():
    from wtb.application.services.async_execution_controller import (
        AsyncExecutionController,
    )
    from wtb.infrastructure.adapters.async_langgraph_state_adapter import (
        AsyncLangGraphStateAdapter,
    )

    execution = Execution(
        id="async-real-langgraph-exec",
        workflow_id="workflow-1",
        state=ExecutionState(workflow_variables={"value": 0, "messages": []}),
    )
    adapter = AsyncLangGraphStateAdapter(LangGraphConfig.for_testing())
    uow = _async_uow_for(execution)
    controller = AsyncExecutionController(adapter, uow_factory=lambda: uow)

    result = await controller.arun(
        execution.id,
        graph=_file_output_graph(),
    )

    history = await adapter.aget_checkpoints(limit=1)
    assert result.status == ExecutionStatus.COMPLETED
    assert history
    assert result.checkpoint_id == history[0]["checkpoint_id"]
    assert execution.checkpoint_id == history[0]["checkpoint_id"]
    assert isinstance(result.checkpoint_id, str)
    assert result.checkpoint_id
    verify_event = uow.outbox.aadd.call_args.args[0]
    assert verify_event.payload["checkpoint_id"] == history[0]["checkpoint_id"]


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not installed")
def test_fork_resume_restores_checkpoint_files_before_successor_reads(tmp_path):
    controller, adapter, file_tracking, workflow = _langgraph_controller(tmp_path)
    output_file = tmp_path / "outputs" / "prefix.txt"
    node_calls: list[str] = []

    execution = controller.create_execution(
        workflow,
        initial_state={"messages": []},
    )
    execution = controller.run(
        execution.id,
        graph=_prefix_file_reader_graph(output_file, node_calls),
    )
    assert execution.status == ExecutionStatus.COMPLETED
    assert node_calls == ["materialize", "consume"]

    prefix_checkpoint, _, _ = next(
        item
        for item in _output_checkpoints(adapter, file_tracking)
        if item[1].get("prefix.txt") == "prefix-v1"
        and item[0].get("next") == ["consume"]
    )
    checkpoint_id = prefix_checkpoint["checkpoint_id"]

    output_file.write_text("corrupted-after-prefix", encoding="utf-8")
    node_calls.clear()
    forked = controller.fork(execution.id, checkpoint_id)
    resumed = controller.resume(
        forked.id,
        modified_state={"_output_files": {}},
    )

    assert resumed.status == ExecutionStatus.COMPLETED
    assert resumed.state.workflow_variables["observed_file"] == "prefix-v1"
    assert node_calls == ["consume"]


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not installed")
@pytest.mark.parametrize("failing_method", ["create_fork", "update_state"])
def test_fork_setup_failure_does_not_persist_paused_execution(
    tmp_path,
    monkeypatch,
    failing_method,
):
    controller, adapter, file_tracking, workflow = _langgraph_controller(tmp_path)
    execution = controller.create_execution(
        workflow,
        initial_state={"value": 0, "messages": []},
    )
    execution = controller.run(execution.id, graph=_file_output_graph())
    checkpoint_id = _output_checkpoints(adapter, file_tracking)[0][0]["checkpoint_id"]
    before_ids = {item.id for item in controller._exec_repo.list()}

    def fail_setup(*args, **kwargs):
        raise RuntimeError(f"{failing_method} failed")

    monkeypatch.setattr(adapter, failing_method, fail_setup)

    with pytest.raises(RuntimeError, match=f"{failing_method} failed"):
        controller.fork(
            execution.id,
            checkpoint_id,
            new_initial_state={"branch": "fork"},
        )

    assert {item.id for item in controller._exec_repo.list()} == before_ids


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not installed")
def test_fork_update_state_false_does_not_persist_paused_execution(
    tmp_path,
    monkeypatch,
):
    controller, adapter, file_tracking, workflow = _langgraph_controller(tmp_path)
    execution = controller.create_execution(
        workflow,
        initial_state={"value": 0, "messages": []},
    )
    execution = controller.run(execution.id, graph=_file_output_graph())
    checkpoint_id = _output_checkpoints(adapter, file_tracking)[0][0]["checkpoint_id"]
    before_ids = {item.id for item in controller._exec_repo.list()}

    def reject_update(*args, **kwargs):
        return False

    monkeypatch.setattr(adapter, "update_state", reject_update)

    with pytest.raises(RuntimeError, match="continuation state"):
        controller.fork(
            execution.id,
            checkpoint_id,
            new_initial_state={"branch": "fork"},
        )

    assert {item.id for item in controller._exec_repo.list()} == before_ids


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not installed")
@pytest.mark.parametrize(
    "failing_stage",
    ["initialize_session", "metadata", "repository_add", "commit"],
)
def test_fork_failure_restores_source_session_and_removes_uncommitted_execution(
    tmp_path,
    monkeypatch,
    failing_stage,
):
    controller, adapter, file_tracking, workflow = _langgraph_controller(tmp_path)
    execution = controller.create_execution(
        workflow,
        initial_state={"value": 0, "messages": []},
    )
    execution = controller.run(execution.id, graph=_file_output_graph())
    checkpoint_id = _output_checkpoints(adapter, file_tracking)[0][0]["checkpoint_id"]
    before_ids = {item.id for item in controller._exec_repo.list()}
    source_session_id = execution.session_id

    if failing_stage == "initialize_session":
        original_initialize = adapter.initialize_session

        def fail_after_switch(execution_id, initial_state):
            session_id = original_initialize(execution_id, initial_state)
            if execution_id != execution.id:
                raise RuntimeError("initialize_session failed")
            return session_id

        monkeypatch.setattr(adapter, "initialize_session", fail_after_switch)
    elif failing_stage == "metadata":
        def fail_metadata(*args, **kwargs):
            raise RuntimeError("metadata failed")

        monkeypatch.setattr(
            controller,
            "_sync_external_cache_metadata",
            fail_metadata,
        )
    elif failing_stage == "repository_add":
        original_add = controller._exec_repo.add

        def fail_after_add(entity):
            original_add(entity)
            raise RuntimeError("repository_add failed")

        monkeypatch.setattr(controller._exec_repo, "add", fail_after_add)
    else:
        def fail_commit():
            raise RuntimeError("commit failed")

        monkeypatch.setattr(controller, "_commit", fail_commit)

    with pytest.raises(RuntimeError, match=f"{failing_stage} failed"):
        controller.fork(
            execution.id,
            checkpoint_id,
            new_initial_state={"branch": "fork"},
        )

    assert adapter.get_current_session_id() == source_session_id
    assert {item.id for item in controller._exec_repo.list()} == before_ids


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not installed")
def test_fork_restore_session_failure_is_reported_and_removes_committed_fork(
    tmp_path,
    monkeypatch,
):
    controller, adapter, file_tracking, workflow = _langgraph_controller(tmp_path)
    execution = controller.create_execution(
        workflow,
        initial_state={"value": 0, "messages": []},
    )
    execution = controller.run(execution.id, graph=_file_output_graph())
    checkpoint_id = _output_checkpoints(adapter, file_tracking)[0][0]["checkpoint_id"]
    before_ids = {item.id for item in controller._exec_repo.list()}
    source_session_id = execution.session_id
    original_set_current = adapter.set_current_session

    def fail_source_restore(session_id, execution_id=None):
        if session_id == source_session_id:
            raise RuntimeError("source session restore failed")
        return original_set_current(session_id, execution_id=execution_id)

    monkeypatch.setattr(adapter, "set_current_session", fail_source_restore)

    with pytest.raises(RuntimeError, match="source session restore failed"):
        controller.fork(
            execution.id,
            checkpoint_id,
            new_initial_state={"branch": "fork"},
        )

    assert {item.id for item in controller._exec_repo.list()} == before_ids


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not installed")
def test_fork_restore_session_false_is_reported_and_removes_committed_fork(
    tmp_path,
    monkeypatch,
):
    controller, adapter, file_tracking, workflow = _langgraph_controller(tmp_path)
    execution = controller.create_execution(
        workflow,
        initial_state={"value": 0, "messages": []},
    )
    execution = controller.run(execution.id, graph=_file_output_graph())
    checkpoint_id = _output_checkpoints(adapter, file_tracking)[0][0]["checkpoint_id"]
    before_ids = {item.id for item in controller._exec_repo.list()}
    source_session_id = execution.session_id
    original_set_current = adapter.set_current_session

    def reject_source_restore(session_id, execution_id=None):
        if session_id == source_session_id:
            return False
        return original_set_current(session_id, execution_id=execution_id)

    monkeypatch.setattr(adapter, "set_current_session", reject_source_restore)

    with pytest.raises(RuntimeError, match="restore source session"):
        controller.fork(
            execution.id,
            checkpoint_id,
            new_initial_state={"branch": "fork"},
        )

    assert {item.id for item in controller._exec_repo.list()} == before_ids


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not installed")
def test_fork_cleanup_commits_after_a_persisted_repository_failure(
    tmp_path,
    monkeypatch,
):
    from wtb.infrastructure.database.unit_of_work import SQLAlchemyUnitOfWork

    database_path = tmp_path / "fork-cleanup.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    uow = SQLAlchemyUnitOfWork(
        database_url,
        blob_storage_path=str(tmp_path / "sql-blobs"),
    )
    uow.__enter__()
    adapter = LangGraphStateAdapter(LangGraphConfig.for_testing())
    file_tracking = SqliteFileTrackingService(workspace_path=tmp_path / "sql-files")
    controller = ExecutionController(
        execution_repository=uow.executions,
        workflow_repository=uow.workflows,
        state_adapter=adapter,
        node_executor=DefaultNodeExecutor(),
        unit_of_work=uow,
        file_tracking_service=file_tracking,
        output_dir=str(tmp_path / "sql-output"),
    )
    workflow = _workflow("sql-fork-cleanup-wf")
    uow.workflows.add(workflow)
    uow.commit()
    execution = controller.create_execution(
        workflow,
        initial_state={"value": 0, "messages": []},
    )
    execution = controller.run(execution.id, graph=_file_output_graph())
    checkpoint_id = _output_checkpoints(adapter, file_tracking)[0][0]["checkpoint_id"]
    before_ids = {item.id for item in uow.executions.list()}
    original_add = uow.executions.add

    def persist_then_fail(entity):
        original_add(entity)
        uow.commit()
        raise RuntimeError("repository failed after persistence")

    monkeypatch.setattr(uow.executions, "add", persist_then_fail)

    with pytest.raises(RuntimeError, match="repository failed after persistence"):
        controller.fork(execution.id, checkpoint_id)

    with SQLAlchemyUnitOfWork(
        database_url,
        blob_storage_path=str(tmp_path / "verify-blobs"),
    ) as verification_uow:
        assert {item.id for item in verification_uow.executions.list()} == before_ids


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not installed")
def test_fork_restore_failure_does_not_mask_original_setup_error(
    tmp_path,
    monkeypatch,
):
    controller, adapter, file_tracking, workflow = _langgraph_controller(tmp_path)
    execution = controller.create_execution(
        workflow,
        initial_state={"value": 0, "messages": []},
    )
    execution = controller.run(execution.id, graph=_file_output_graph())
    checkpoint_id = _output_checkpoints(adapter, file_tracking)[0][0]["checkpoint_id"]
    before_ids = {item.id for item in controller._exec_repo.list()}
    source_session_id = execution.session_id
    original_set_current = adapter.set_current_session
    source_session_sets = 0

    def fail_update(*args, **kwargs):
        raise RuntimeError("fork update failed")

    def fail_source_restore(session_id, execution_id=None):
        nonlocal source_session_sets
        if session_id == source_session_id:
            source_session_sets += 1
            if source_session_sets > 1:
                raise RuntimeError("source session restore failed")
        return original_set_current(session_id, execution_id=execution_id)

    monkeypatch.setattr(adapter, "update_state", fail_update)
    monkeypatch.setattr(adapter, "set_current_session", fail_source_restore)

    with pytest.raises(RuntimeError, match="fork update failed") as raised:
        controller.fork(
            execution.id,
            checkpoint_id,
            new_initial_state={"branch": "fork"},
        )

    notes = getattr(raised.value, "__notes__", [])
    assert any("source session restore failed" in note for note in notes)
    assert {item.id for item in controller._exec_repo.list()} == before_ids


def test_ray_batch_runner_fails_when_outputs_have_no_checkpoint_id():
    import inspect

    from wtb.application.services import ray_batch_runner

    source = inspect.getsource(ray_batch_runner._create_variant_execution_actor_class)
    assert "Execution produced output files but has no checkpoint_id" in source
    assert "raise" in source
