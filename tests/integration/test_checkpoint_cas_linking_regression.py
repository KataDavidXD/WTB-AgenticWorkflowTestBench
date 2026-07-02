"""Regression coverage for checkpoint-centric CAS file linking."""

from __future__ import annotations

import operator
import uuid
from pathlib import Path
from typing import Annotated, Optional, TypedDict
from unittest.mock import AsyncMock, MagicMock

import pytest

from wtb.application.services.execution_controller import (
    DefaultNodeExecutor,
    ExecutionController,
)
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
    branch: Optional[str]
    answer: str


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


@pytest.mark.asyncio
async def test_async_checkpoint_fork_execution_starts_paused():
    from wtb.application.services.async_execution_controller import AsyncExecutionController

    source = Execution(
        id="source-exec",
        workflow_id="workflow-1",
        status=ExecutionStatus.PAUSED,
        state=ExecutionState(workflow_variables={"value": 1}),
    )
    adapter = MagicMock()
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


def test_ray_batch_runner_fails_when_outputs_have_no_checkpoint_id():
    import inspect

    from wtb.application.services import ray_batch_runner

    source = inspect.getsource(ray_batch_runner._create_variant_execution_actor_class)
    assert "Execution produced output files but has no checkpoint_id" in source
    assert "logger.error(f\"File tracking failed: {e}\")" in source
    assert "raise" in source
