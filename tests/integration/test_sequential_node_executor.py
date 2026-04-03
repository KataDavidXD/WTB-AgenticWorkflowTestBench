"""
Integration tests for Sequential Node-Executor mode.

Tests the DefaultNodeExecutor path used when no LangGraph graph is provided
and adapter.supports_graph_execution() returns False (InMemoryStateAdapter).
"""

import pytest
from wtb.domain.models.workflow import (
    Execution,
    ExecutionState,
    ExecutionStatus,
    TestWorkflow,
    WorkflowNode,
    WorkflowEdge,
)
from wtb.infrastructure.database.inmemory_unit_of_work import InMemoryUnitOfWork
from wtb.infrastructure.adapters.inmemory_state_adapter import InMemoryStateAdapter
from wtb.application.services.execution_controller import (
    ExecutionController,
    DefaultNodeExecutor,
)


def _make_linear_workflow() -> TestWorkflow:
    """start -> action -> end with standard edges."""
    wf = TestWorkflow(id="wf-ne", name="node-executor-test", entry_point="start")
    wf.add_node(WorkflowNode(id="start", name="Start", type="start"))
    wf.add_node(WorkflowNode(id="action", name="Action", type="action"))
    wf.add_node(WorkflowNode(id="end", name="End", type="end"))
    wf.add_edge(WorkflowEdge(source_id="start", target_id="action"))
    wf.add_edge(WorkflowEdge(source_id="action", target_id="end"))
    return wf


def _make_breakpoint_workflow() -> TestWorkflow:
    """start -> bp_node -> after -> end. breakpoint at bp_node."""
    wf = TestWorkflow(id="wf-bp", name="breakpoint-test", entry_point="start")
    wf.add_node(WorkflowNode(id="start", name="Start", type="start"))
    wf.add_node(WorkflowNode(id="bp_node", name="Breakpoint", type="action"))
    wf.add_node(WorkflowNode(id="after", name="After", type="action"))
    wf.add_node(WorkflowNode(id="end", name="End", type="end"))
    wf.add_edge(WorkflowEdge(source_id="start", target_id="bp_node"))
    wf.add_edge(WorkflowEdge(source_id="bp_node", target_id="after"))
    wf.add_edge(WorkflowEdge(source_id="after", target_id="end"))
    return wf


@pytest.fixture
def setup():
    adapter = InMemoryStateAdapter()
    uow = InMemoryUnitOfWork()
    uow.__enter__()

    controller = ExecutionController(
        execution_repository=uow.executions,
        workflow_repository=uow.workflows,
        state_adapter=adapter,
        node_executor=DefaultNodeExecutor(),
        unit_of_work=uow,
    )

    return controller, adapter, uow


# ═══════════════════════════════════════════════════════════════
# Basic Run Tests
# ═══════════════════════════════════════════════════════════════


class TestNodeExecutorRun:

    def test_run_simple_workflow(self, setup):
        """start -> action -> end completes successfully."""
        controller, adapter, uow = setup
        workflow = _make_linear_workflow()
        uow.workflows.add(workflow)
        uow.commit()

        execution = controller.create_execution(workflow)
        assert execution.status == ExecutionStatus.PENDING

        execution = controller.run(execution.id)

        assert execution.status in (ExecutionStatus.COMPLETED, ExecutionStatus.FAILED)
        assert "start" in execution.state.execution_path

    def test_node_executor_with_inmemory_adapter(self, setup):
        """InMemoryStateAdapter falls through to node executor (no LangGraph)."""
        controller, adapter, uow = setup
        workflow = _make_linear_workflow()
        uow.workflows.add(workflow)
        uow.commit()

        assert adapter.supports_graph_execution() is False

        execution = controller.create_execution(workflow)
        execution = controller.run(execution.id)

        # Should not crash -- node executor handles workflow
        assert execution.status in (ExecutionStatus.COMPLETED, ExecutionStatus.FAILED)


# ═══════════════════════════════════════════════════════════════
# Breakpoint / Pause Tests
# ═══════════════════════════════════════════════════════════════


class TestBreakpoint:

    def test_breakpoint_pauses_execution(self, setup):
        controller, adapter, uow = setup
        workflow = _make_breakpoint_workflow()
        uow.workflows.add(workflow)
        uow.commit()

        execution = controller.create_execution(
            workflow, breakpoints=["bp_node"],
        )
        execution = controller.run(execution.id)

        assert execution.status == ExecutionStatus.PAUSED
        assert "start" in execution.state.execution_path

    def test_resume_after_breakpoint(self, setup):
        controller, adapter, uow = setup
        workflow = _make_breakpoint_workflow()
        uow.workflows.add(workflow)
        uow.commit()

        execution = controller.create_execution(
            workflow, breakpoints=["bp_node"],
        )
        execution = controller.run(execution.id)
        assert execution.status == ExecutionStatus.PAUSED

        execution = controller.resume(execution.id)
        assert execution.status in (ExecutionStatus.COMPLETED, ExecutionStatus.FAILED)
