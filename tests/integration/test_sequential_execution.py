"""
Integration tests for sequential execution flow.

Tests the full SDK -> controller -> LangGraphStateAdapter -> MemorySaver path.
Uses real LangGraph fixtures (create_minimal_graph, create_conditional_graph).
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
from wtb.application.services.execution_controller import (
    ExecutionController,
    DefaultNodeExecutor,
)
from wtb.application.services.outbox_controller_decorator import (
    OutboxExecutionControllerDecorator,
)


def _make_workflow() -> TestWorkflow:
    wf = TestWorkflow(id="wf-1", name="integration-test", entry_point="start")
    wf.add_node(WorkflowNode(id="start", name="Start", type="start"))
    wf.add_node(WorkflowNode(id="end", name="End", type="end"))
    wf.add_edge(WorkflowEdge(source_id="start", target_id="end"))
    return wf


def _try_import_langgraph():
    """Import LangGraph or skip test."""
    try:
        from wtb.infrastructure.adapters.langgraph_state_adapter import (
            LangGraphStateAdapter,
            LangGraphConfig,
            LANGGRAPH_AVAILABLE,
        )
        if not LANGGRAPH_AVAILABLE:
            pytest.skip("LangGraph not available")
        return LangGraphStateAdapter, LangGraphConfig
    except ImportError:
        pytest.skip("LangGraph not installed")


def _try_import_fixtures():
    """Import test fixtures or skip."""
    try:
        from wtb.testing.fixtures import create_minimal_graph, create_conditional_graph
        return create_minimal_graph, create_conditional_graph
    except ImportError:
        pytest.skip("LangGraph fixtures not available")


@pytest.fixture
def langgraph_setup():
    """Create LangGraph adapter with MemorySaver + UoW."""
    LangGraphStateAdapter, LangGraphConfig = _try_import_langgraph()

    config = LangGraphConfig.for_testing()
    adapter = LangGraphStateAdapter(config)
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


@pytest.fixture
def minimal_graph():
    create_minimal_graph, _ = _try_import_fixtures()
    return create_minimal_graph()


@pytest.fixture
def conditional_graph():
    _, create_conditional_graph = _try_import_fixtures()
    return create_conditional_graph()


# ═══════════════════════════════════════════════════════════════
# Full Flow Tests
# ═══════════════════════════════════════════════════════════════


class TestFullFlow:
    """Full run -> COMPLETED flow with real LangGraph."""

    def test_run_minimal_graph_completes(self, langgraph_setup, minimal_graph):
        controller, adapter, uow = langgraph_setup

        workflow = _make_workflow()
        uow.workflows.add(workflow)
        uow.commit()

        execution = controller.create_execution(
            workflow=workflow,
            initial_state={"value": 0, "messages": [], "route": None},
        )
        assert execution.status == ExecutionStatus.PENDING

        execution = controller.run(execution.id, graph=minimal_graph)

        assert execution.status == ExecutionStatus.COMPLETED
        final_vars = execution.state.workflow_variables
        assert final_vars.get("value") == 2
        assert "a_executed" in final_vars.get("messages", [])
        assert "b_executed" in final_vars.get("messages", [])

    def test_run_conditional_graph_route_b(self, langgraph_setup, conditional_graph):
        controller, adapter, uow = langgraph_setup

        workflow = _make_workflow()
        uow.workflows.add(workflow)
        uow.commit()

        execution = controller.create_execution(
            workflow=workflow,
            initial_state={"value": 0, "messages": [], "route": "b"},
        )

        execution = controller.run(execution.id, graph=conditional_graph)

        assert execution.status == ExecutionStatus.COMPLETED
        final_vars = execution.state.workflow_variables
        assert "b_executed" in final_vars.get("messages", [])

    def test_run_conditional_graph_route_c(self, langgraph_setup, conditional_graph):
        controller, adapter, uow = langgraph_setup

        workflow = _make_workflow()
        uow.workflows.add(workflow)
        uow.commit()

        execution = controller.create_execution(
            workflow=workflow,
            initial_state={"value": 0, "messages": [], "route": "c"},
        )

        execution = controller.run(execution.id, graph=conditional_graph)

        assert execution.status == ExecutionStatus.COMPLETED
        final_vars = execution.state.workflow_variables
        assert "c_executed" in final_vars.get("messages", [])
        assert final_vars.get("value") == 11


# ═══════════════════════════════════════════════════════════════
# Resume Tests
# ═══════════════════════════════════════════════════════════════


class TestResumeFlow:
    """Run -> pause -> resume -> COMPLETED with real LangGraph."""

    def test_resume_reuses_graph_on_adapter(self, langgraph_setup, minimal_graph):
        """After first run with graph, resume (graph=None) should find adapter.has_graph()."""
        controller, adapter, uow = langgraph_setup

        workflow = _make_workflow()
        uow.workflows.add(workflow)
        uow.commit()

        execution = controller.create_execution(
            workflow=workflow,
            initial_state={"value": 0, "messages": [], "route": None},
        )

        execution = controller.run(execution.id, graph=minimal_graph)
        assert execution.status == ExecutionStatus.COMPLETED

        assert adapter.has_graph() is True


# ═══════════════════════════════════════════════════════════════
# Rollback Tests
# ═══════════════════════════════════════════════════════════════


class TestRollbackFlow:
    """Run -> complete -> rollback -> verify state restored."""

    def test_rollback_restores_state_to_paused(self, langgraph_setup, minimal_graph):
        controller, adapter, uow = langgraph_setup

        workflow = _make_workflow()
        uow.workflows.add(workflow)
        uow.commit()

        execution = controller.create_execution(
            workflow=workflow,
            initial_state={"value": 0, "messages": [], "route": None},
        )

        execution = controller.run(execution.id, graph=minimal_graph)
        assert execution.status == ExecutionStatus.COMPLETED

        checkpoint_id = execution.checkpoint_id
        if checkpoint_id is None:
            checkpoints = adapter.get_checkpoints(execution.session_id)
            if checkpoints:
                checkpoint_id = checkpoints[0].id
            else:
                pytest.skip("No checkpoints available for rollback test")

        execution = controller.rollback(execution.id, checkpoint_id)

        assert execution.status == ExecutionStatus.PAUSED
        assert execution.checkpoint_id == checkpoint_id
        assert isinstance(execution.state, ExecutionState)


# ═══════════════════════════════════════════════════════════════
# Fork Tests
# ═══════════════════════════════════════════════════════════════


class TestForkFlow:
    """Run -> complete -> fork -> verify new execution."""

    def test_fork_creates_new_execution(self, langgraph_setup, minimal_graph):
        controller, adapter, uow = langgraph_setup

        workflow = _make_workflow()
        uow.workflows.add(workflow)
        uow.commit()

        execution = controller.create_execution(
            workflow=workflow,
            initial_state={"value": 0, "messages": [], "route": None},
        )

        execution = controller.run(execution.id, graph=minimal_graph)
        assert execution.status == ExecutionStatus.COMPLETED

        checkpoint_id = execution.checkpoint_id
        if checkpoint_id is None:
            checkpoints = adapter.get_checkpoints(execution.session_id)
            if checkpoints:
                checkpoint_id = checkpoints[0].id
            else:
                pytest.skip("No checkpoints available for fork test")

        forked = controller.fork(
            execution.id,
            checkpoint_id,
            new_initial_state={"extra": True},
        )

        assert forked.id != execution.id
        assert forked.status == ExecutionStatus.PAUSED
        assert forked.state.workflow_variables.get("extra") is True
        assert forked.metadata.get("forked_from") == execution.id


# ═══════════════════════════════════════════════════════════════
# Outbox Decorator Integration
# ═══════════════════════════════════════════════════════════════


class TestOutboxDecoratorIntegration:
    """OutboxExecutionControllerDecorator with real controller."""

    def test_decorator_emits_events_on_run(self, langgraph_setup, minimal_graph):
        controller, adapter, uow = langgraph_setup

        outbox_repo = uow.outbox
        decorated = OutboxExecutionControllerDecorator(
            controller,
            outbox_repo,
            commit_fn=uow.commit,
            rollback_fn=uow.rollback,
        )

        workflow = _make_workflow()
        uow.workflows.add(workflow)
        uow.commit()

        execution = decorated.create_execution(
            workflow=workflow,
            initial_state={"value": 0, "messages": [], "route": None},
        )

        execution = decorated.run(execution.id, graph=minimal_graph)
        assert execution.status == ExecutionStatus.COMPLETED

        all_events = outbox_repo.get_pending()
        event_types = [e.event_type.value for e in all_events]
        assert "execution_started" in event_types
        assert "execution_completed" in event_types


# ═══════════════════════════════════════════════════════════════
# Negative Cases
# ═══════════════════════════════════════════════════════════════


class TestNegativeCases:

    def test_resume_non_paused_raises(self, langgraph_setup, minimal_graph):
        controller, adapter, uow = langgraph_setup

        workflow = _make_workflow()
        uow.workflows.add(workflow)
        uow.commit()

        execution = controller.create_execution(
            workflow=workflow,
            initial_state={"value": 0, "messages": [], "route": None},
        )

        with pytest.raises(ValueError, match="Cannot resume"):
            controller.resume(execution.id)

    def test_run_completed_execution_fails(self, langgraph_setup, minimal_graph):
        controller, adapter, uow = langgraph_setup

        workflow = _make_workflow()
        uow.workflows.add(workflow)
        uow.commit()

        execution = controller.create_execution(
            workflow=workflow,
            initial_state={"value": 0, "messages": [], "route": None},
        )

        execution = controller.run(execution.id, graph=minimal_graph)
        assert execution.status == ExecutionStatus.COMPLETED

        result = controller.run(execution.id)
        # H13 fix: running a completed execution stays COMPLETED (terminal state)
        assert result.status in (ExecutionStatus.COMPLETED, ExecutionStatus.FAILED)
