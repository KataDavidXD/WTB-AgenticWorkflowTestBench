"""Fork graph-selection metadata must follow explicit variant overlays."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from wtb.application.services.execution_controller import ExecutionController
from wtb.domain.models.workflow import Execution, ExecutionState, ExecutionStatus
from wtb.sdk import WorkflowProject, WTBTestBench


def _fork_with_overlay(new_initial_state):
    source = Execution(
        id="source-execution",
        workflow_id="workflow",
        status=ExecutionStatus.COMPLETED,
        state=ExecutionState(workflow_variables={"answer": "source"}),
        metadata={
            "_variant_config": {"retrieve": "variant-a"},
            "_workflow_variant": "workflow-a",
            "source_only": {"nested": [1]},
        },
    )
    source.session_id = "wtb-source-execution"
    execution_repository = MagicMock()
    execution_repository.get.return_value = source
    workflow_repository = MagicMock()
    workflow_repository.get.return_value = SimpleNamespace(entry_point="start")
    adapter = MagicMock()
    adapter.set_current_session.return_value = True
    adapter.supports_graph_execution.return_value = False
    adapter.load_checkpoint.return_value = ExecutionState(
        current_node_id="start",
        workflow_variables={"answer": "checkpoint"},
    )
    adapter.initialize_session.return_value = "wtb-fork-execution"
    controller = ExecutionController(
        execution_repository=execution_repository,
        workflow_repository=workflow_repository,
        state_adapter=adapter,
        unit_of_work=MagicMock(),
    )

    forked = controller.fork(
        source.id,
        "checkpoint-id",
        new_initial_state=new_initial_state,
    )
    return forked, source


def test_fork_variant_overlay_replaces_graph_metadata_with_deep_copies():
    variant_config = {"retrieve": "variant-b", "rerank": "variant-b"}
    arbitrary_state = {"nested": ["state-only"]}
    forked, source = _fork_with_overlay(
        {
            "_variant_config": variant_config,
            "_workflow_variant": "workflow-b",
            "arbitrary_state": arbitrary_state,
        }
    )

    assert forked.metadata["_variant_config"] == variant_config
    assert forked.metadata["_variant_config"] is not variant_config
    assert forked.metadata["_workflow_variant"] == "workflow-b"
    assert "arbitrary_state" not in forked.metadata
    assert forked.state.workflow_variables["arbitrary_state"] == arbitrary_state
    assert source.metadata["_variant_config"] == {"retrieve": "variant-a"}
    assert source.metadata["_workflow_variant"] == "workflow-a"

    project = WorkflowProject(
        id="workflow",
        name="workflow",
        graph_factory=MagicMock(),
    )
    project.build_graph = MagicMock(return_value=object())
    bench = WTBTestBench(
        project_service=MagicMock(),
        variant_service=MagicMock(),
        execution_controller=MagicMock(),
    )
    bench._project_cache = {project.name: project}

    bench._resolve_graph_for_execution(forked.id, execution=forked)

    project.build_graph.assert_called_once_with(
        variant_config={"retrieve": "variant-b", "rerank": "variant-b"},
        workflow_variant="workflow-b",
    )


def test_fork_without_variant_overlay_inherits_source_graph_metadata():
    forked, source = _fork_with_overlay({"arbitrary_state": "fork-only"})

    assert forked.metadata["_variant_config"] == {"retrieve": "variant-a"}
    assert (
        forked.metadata["_variant_config"]
        is not source.metadata["_variant_config"]
    )
    assert forked.metadata["_workflow_variant"] == "workflow-a"
    assert "arbitrary_state" not in forked.metadata
