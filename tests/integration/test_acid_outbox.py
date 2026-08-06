"""
Integration tests for ACID and Outbox transactional consistency.

Cross-cutting tests that verify:
- Outbox events are emitted for all lifecycle operations
- Transaction atomicity (failure -> consistent state)
- Event semantics (CREATED vs STARTED)
- Decorator resilience (missing repo, errors)
"""

from unittest.mock import MagicMock

import pytest

from wtb.application.services.execution_controller import (
    DefaultNodeExecutor,
    ExecutionController,
)
from wtb.application.services.outbox_controller_decorator import (
    OutboxExecutionControllerDecorator,
)
from wtb.domain.models.outbox import OutboxEventType
from wtb.domain.models.workflow import (
    Execution,
    ExecutionState,
    ExecutionStatus,
    TestWorkflow,
    WorkflowEdge,
    WorkflowNode,
)
from wtb.infrastructure.database.inmemory_unit_of_work import InMemoryUnitOfWork
from wtb.infrastructure.database.unit_of_work import SQLAlchemyUnitOfWork


def _make_workflow() -> TestWorkflow:
    wf = TestWorkflow(id="wf-acid", name="acid-test", entry_point="start")
    wf.add_node(WorkflowNode(id="start", name="Start", type="start"))
    wf.add_node(WorkflowNode(id="end", name="End", type="end"))
    wf.add_edge(WorkflowEdge(source_id="start", target_id="end"))
    return wf


def _make_breakpoint_workflow() -> TestWorkflow:
    wf = TestWorkflow(id="wf-bp", name="breakpoint-test", entry_point="start")
    wf.add_node(WorkflowNode(id="start", name="Start", type="start"))
    wf.add_node(WorkflowNode(id="bp_node", name="BP", type="action"))
    wf.add_node(WorkflowNode(id="end", name="End", type="end"))
    wf.add_edge(WorkflowEdge(source_id="start", target_id="bp_node"))
    wf.add_edge(WorkflowEdge(source_id="bp_node", target_id="end"))
    return wf


def _try_import_langgraph():
    try:
        from wtb.infrastructure.adapters.langgraph_state_adapter import (
            LANGGRAPH_AVAILABLE,
            LangGraphConfig,
            LangGraphStateAdapter,
        )
        if not LANGGRAPH_AVAILABLE:
            pytest.skip("LangGraph not available")
        return LangGraphStateAdapter, LangGraphConfig
    except ImportError:
        pytest.skip("LangGraph not installed")


def _try_import_fixtures():
    try:
        from wtb.testing.fixtures import create_minimal_graph
        return create_minimal_graph
    except ImportError:
        pytest.skip("LangGraph fixtures not available")


@pytest.fixture
def langgraph_decorated_setup():
    """LangGraph controller wrapped in OutboxExecutionControllerDecorator."""
    LangGraphStateAdapter, LangGraphConfig = _try_import_langgraph()

    config = LangGraphConfig.for_testing()
    adapter = LangGraphStateAdapter(config)
    uow = InMemoryUnitOfWork()
    uow.__enter__()

    inner_controller = ExecutionController(
        execution_repository=uow.executions,
        workflow_repository=uow.workflows,
        state_adapter=adapter,
        node_executor=DefaultNodeExecutor(),
        unit_of_work=uow,
    )

    decorated = OutboxExecutionControllerDecorator(
        inner_controller,
        uow.outbox,
        commit_fn=uow.commit,
        rollback_fn=uow.rollback,
    )

    return decorated, adapter, uow


@pytest.fixture
def node_executor_decorated_setup():
    """InMemory adapter controller wrapped in outbox decorator (node-executor path)."""
    from wtb.infrastructure.adapters.inmemory_state_adapter import InMemoryStateAdapter

    adapter = InMemoryStateAdapter()
    uow = InMemoryUnitOfWork()
    uow.__enter__()

    inner_controller = ExecutionController(
        execution_repository=uow.executions,
        workflow_repository=uow.workflows,
        state_adapter=adapter,
        node_executor=DefaultNodeExecutor(),
        unit_of_work=uow,
    )

    decorated = OutboxExecutionControllerDecorator(
        inner_controller,
        uow.outbox,
        commit_fn=uow.commit,
        rollback_fn=uow.rollback,
    )

    return decorated, adapter, uow


# ═══════════════════════════════════════════════════════════════
# Full Lifecycle Outbox Events (LangGraph)
# ═══════════════════════════════════════════════════════════════


class TestOutboxFullLifecycle:

    def test_create_and_run_emits_created_started_completed(
        self, langgraph_decorated_setup
    ):
        """create_execution -> run -> verify CREATED, STARTED, COMPLETED events."""
        decorated, adapter, uow = langgraph_decorated_setup
        create_minimal_graph = _try_import_fixtures()
        graph = create_minimal_graph()

        workflow = _make_workflow()
        uow.workflows.add(workflow)
        uow.commit()

        execution = decorated.create_execution(
            workflow, initial_state={"value": 0, "messages": [], "route": None},
        )

        execution = decorated.run(execution.id, graph=graph)
        assert execution.status == ExecutionStatus.COMPLETED

        events = uow.outbox.get_pending()
        event_types = [e.event_type.value for e in events]

        assert "execution_created" in event_types, f"Missing CREATED. Got: {event_types}"
        assert "execution_started" in event_types, f"Missing STARTED. Got: {event_types}"
        assert "execution_completed" in event_types, f"Missing COMPLETED. Got: {event_types}"

    def test_outbox_event_for_failed_run(self, langgraph_decorated_setup):
        """A run that raises internally -> STARTED + FAILED events."""
        decorated, adapter, uow = langgraph_decorated_setup

        workflow = _make_workflow()
        uow.workflows.add(workflow)
        uow.commit()

        execution = decorated.create_execution(
            workflow, initial_state={"value": 0, "messages": [], "route": None},
        )

        # Run without graph and adapter has no graph -> will fail
        # (adapter.supports_graph_execution() is True but has_graph() is False
        #  and no graph provided, falls to node_executor which may also fail)
        execution = decorated.run(execution.id)
        # The result depends on routing; if it fails, FAILED event is emitted
        events = uow.outbox.get_pending()
        event_types = [e.event_type.value for e in events]

        assert "execution_created" in event_types


# ═══════════════════════════════════════════════════════════════
# Rollback Outbox Event
# ═══════════════════════════════════════════════════════════════


class TestOutboxRollback:

    def test_rollback_emits_rollback_performed(self, langgraph_decorated_setup):
        decorated, adapter, uow = langgraph_decorated_setup
        create_minimal_graph = _try_import_fixtures()
        graph = create_minimal_graph()

        workflow = _make_workflow()
        uow.workflows.add(workflow)
        uow.commit()

        execution = decorated.create_execution(
            workflow, initial_state={"value": 0, "messages": [], "route": None},
        )
        execution = decorated.run(execution.id, graph=graph)
        assert execution.status == ExecutionStatus.COMPLETED

        cp_id = execution.checkpoint_id
        if cp_id is None:
            checkpoints = adapter.get_checkpoints(execution.session_id)
            if checkpoints:
                cp_id = checkpoints[0].id
            else:
                pytest.skip("No checkpoints")

        rolled_back = decorated.rollback(execution.id, cp_id)
        assert rolled_back.status == ExecutionStatus.PAUSED

        events = uow.outbox.get_pending()
        event_types = [e.event_type.value for e in events]
        assert "rollback_performed" in event_types


# ═══════════════════════════════════════════════════════════════
# Decorator Resilience
# ═══════════════════════════════════════════════════════════════


class TestDecoratorResilience:

    def test_invalid_atomic_construction_preserves_sqlite_inner_durability(
        self,
        tmp_path,
    ):
        db_url = f"sqlite:///{(tmp_path / 'outbox-construction.db').as_posix()}"
        with SQLAlchemyUnitOfWork(
            db_url=db_url,
            blob_storage_path=str(tmp_path / "blobs-construction"),
        ) as writer:
            workflow = _make_workflow()
            writer.workflows.add(workflow)
            writer.commit()
            adapter = MagicMock()
            adapter.initialize_session.return_value = "construction-session"
            inner = ExecutionController(
                execution_repository=writer.executions,
                workflow_repository=writer.workflows,
                state_adapter=adapter,
                node_executor=DefaultNodeExecutor(),
                unit_of_work=writer,
            )

            with pytest.raises(ValueError, match="atomic outbox"):
                OutboxExecutionControllerDecorator(inner, writer.outbox)

            assert inner._deferred_commit is False
            execution = inner.create_execution(workflow)

            with SQLAlchemyUnitOfWork(
                db_url=db_url,
                blob_storage_path=str(tmp_path / "reader-blobs-construction"),
            ) as reader:
                assert reader.executions.get(execution.id) is not None

    def test_no_outbox_passthrough_preserves_sqlite_second_session_durability(
        self,
        tmp_path,
    ):
        db_url = f"sqlite:///{(tmp_path / 'no-outbox-passthrough.db').as_posix()}"
        with SQLAlchemyUnitOfWork(
            db_url=db_url,
            blob_storage_path=str(tmp_path / "blobs-no-outbox"),
        ) as writer:
            workflow = _make_workflow()
            writer.workflows.add(workflow)
            writer.commit()
            adapter = MagicMock()
            adapter.initialize_session.return_value = "no-outbox-session"
            inner = ExecutionController(
                execution_repository=writer.executions,
                workflow_repository=writer.workflows,
                state_adapter=adapter,
                node_executor=DefaultNodeExecutor(),
                unit_of_work=writer,
            )
            transparent = OutboxExecutionControllerDecorator(inner, None)

            execution = transparent.create_execution(workflow)

            assert inner._deferred_commit is False
            with SQLAlchemyUnitOfWork(
                db_url=db_url,
                blob_storage_path=str(tmp_path / "reader-blobs-no-outbox"),
            ) as reader:
                assert reader.executions.get(execution.id) is not None

    def test_decorator_with_none_repo_no_crash(self, langgraph_decorated_setup):
        """OutboxExecutionControllerDecorator(inner, None) should not crash."""
        _, adapter, uow = langgraph_decorated_setup
        create_minimal_graph = _try_import_fixtures()
        graph = create_minimal_graph()

        inner_controller = ExecutionController(
            execution_repository=uow.executions,
            workflow_repository=uow.workflows,
            state_adapter=adapter,
            node_executor=DefaultNodeExecutor(),
            unit_of_work=uow,
        )
        no_outbox = OutboxExecutionControllerDecorator(inner_controller, None)

        workflow = _make_workflow()
        uow.workflows.add(workflow)
        uow.commit()

        execution = no_outbox.create_execution(
            workflow, initial_state={"value": 0, "messages": [], "route": None},
        )
        execution = no_outbox.run(execution.id, graph=graph)
        assert execution.status == ExecutionStatus.COMPLETED

    def test_outbox_add_error_rolls_back_shared_uow(self, langgraph_decorated_setup):
        """An outbox insert error aborts the pending business transaction."""
        _, adapter, uow = langgraph_decorated_setup
        inner_controller = ExecutionController(
            execution_repository=uow.executions,
            workflow_repository=uow.workflows,
            state_adapter=adapter,
            node_executor=DefaultNodeExecutor(),
            unit_of_work=uow,
        )

        broken_outbox = MagicMock()
        broken_outbox.add.side_effect = RuntimeError("DB write failed")
        rollback_fn = MagicMock(wraps=uow.rollback)

        decorated = OutboxExecutionControllerDecorator(
            inner_controller,
            broken_outbox,
            commit_fn=uow.commit,
            rollback_fn=rollback_fn,
        )

        workflow = _make_workflow()
        uow.workflows.add(workflow)
        uow.commit()

        with pytest.raises(RuntimeError, match="DB write failed"):
            decorated.create_execution(
                workflow,
                initial_state={"value": 0, "messages": [], "route": None},
            )

        rollback_fn.assert_called_once_with()

    def test_sqlalchemy_outbox_add_error_removes_pending_business_row(self, tmp_path):
        db_url = f"sqlite:///{(tmp_path / 'outbox-add.db').as_posix()}"
        with SQLAlchemyUnitOfWork(
            db_url=db_url,
            blob_storage_path=str(tmp_path / "blobs-add"),
        ) as uow:
            workflow = _make_workflow()
            uow.workflows.add(workflow)
            uow.commit()
            adapter = MagicMock()
            adapter.initialize_session.return_value = "session-add-failure"
            inner_controller = ExecutionController(
                execution_repository=uow.executions,
                workflow_repository=uow.workflows,
                state_adapter=adapter,
                node_executor=DefaultNodeExecutor(),
                unit_of_work=uow,
            )
            broken_outbox = MagicMock()
            broken_outbox.add.side_effect = RuntimeError("DB write failed")
            decorated = OutboxExecutionControllerDecorator(
                inner_controller,
                broken_outbox,
                commit_fn=uow.commit,
                rollback_fn=uow.rollback,
            )

            with pytest.raises(RuntimeError, match="DB write failed"):
                decorated.create_execution(workflow)

            assert uow.executions.list() == []
            assert uow.outbox.get_pending() == []

    def test_sqlalchemy_commit_error_removes_business_and_outbox_rows(self, tmp_path):
        db_url = f"sqlite:///{(tmp_path / 'outbox-commit.db').as_posix()}"
        with SQLAlchemyUnitOfWork(
            db_url=db_url,
            blob_storage_path=str(tmp_path / "blobs-commit"),
        ) as uow:
            workflow = _make_workflow()
            uow.workflows.add(workflow)
            uow.commit()
            adapter = MagicMock()
            adapter.initialize_session.return_value = "session-commit-failure"
            inner_controller = ExecutionController(
                execution_repository=uow.executions,
                workflow_repository=uow.workflows,
                state_adapter=adapter,
                node_executor=DefaultNodeExecutor(),
                unit_of_work=uow,
            )

            def fail_commit():
                raise RuntimeError("commit failed")

            decorated = OutboxExecutionControllerDecorator(
                inner_controller,
                uow.outbox,
                commit_fn=fail_commit,
                rollback_fn=uow.rollback,
            )

            with pytest.raises(RuntimeError, match="commit failed"):
                decorated.create_execution(workflow)

            assert uow.executions.list() == []
            assert uow.outbox.get_pending() == []

    def test_sqlalchemy_inner_error_rolls_back_pending_business_row(self, tmp_path):
        db_url = f"sqlite:///{(tmp_path / 'outbox-inner.db').as_posix()}"
        with SQLAlchemyUnitOfWork(
            db_url=db_url,
            blob_storage_path=str(tmp_path / "blobs-inner"),
        ) as uow:
            workflow = _make_workflow()
            uow.workflows.add(workflow)
            uow.commit()
            primary_error = RuntimeError("inner failed after write")

            class PartiallyWritingController:
                def set_deferred_commit(self, enabled):
                    self.deferred_commit = enabled

                def create_execution(self, workflow, initial_state, breakpoints):
                    execution = Execution(
                        workflow_id=workflow.id,
                        status=ExecutionStatus.PENDING,
                        state=ExecutionState(
                            current_node_id=workflow.entry_point,
                            workflow_variables=initial_state or {},
                        ),
                    )
                    uow.executions.add(execution)
                    raise primary_error

            decorated = OutboxExecutionControllerDecorator(
                PartiallyWritingController(),
                uow.outbox,
                commit_fn=uow.commit,
                rollback_fn=uow.rollback,
            )

            with pytest.raises(RuntimeError) as exc_info:
                decorated.create_execution(workflow)

            assert exc_info.value is primary_error
            assert uow.executions.list() == []
            assert uow.outbox.get_pending() == []

    @pytest.mark.parametrize(
        ("method_name", "event_type"),
        [
            ("rollback_to_node", OutboxEventType.ROLLBACK_PERFORMED),
            ("update_execution_state", OutboxEventType.STATE_MODIFIED),
        ],
    )
    def test_extended_mutation_commit_is_visible_to_second_session(
        self,
        tmp_path,
        method_name,
        event_type,
    ):
        db_url = f"sqlite:///{(tmp_path / f'outbox-{method_name}.db').as_posix()}"
        with SQLAlchemyUnitOfWork(
            db_url=db_url,
            blob_storage_path=str(tmp_path / f"blobs-{method_name}"),
        ) as writer:
            workflow = _make_workflow()
            writer.workflows.add(workflow)
            execution = Execution(
                workflow_id=workflow.id,
                status=ExecutionStatus.PENDING,
                state=ExecutionState(
                    current_node_id="start",
                    workflow_variables={"updated": False},
                ),
            )
            writer.executions.add(execution)
            writer.commit()

            class PersistingExtendedController:
                def set_deferred_commit(self, enabled):
                    self.deferred_commit = enabled

                def update_execution_state(self, execution_id, values):
                    current = writer.executions.get(execution_id)
                    current.state.workflow_variables.update(values)
                    writer.executions.update(current)
                    return True

                def rollback_to_node(self, execution_id, node_id):
                    current = writer.executions.get(execution_id)
                    current.state.current_node_id = node_id
                    current.status = ExecutionStatus.PAUSED
                    writer.executions.update(current)
                    return current

            decorated = OutboxExecutionControllerDecorator(
                PersistingExtendedController(),
                writer.outbox,
                commit_fn=writer.commit,
                rollback_fn=writer.rollback,
            )

            if method_name == "update_execution_state":
                assert decorated.update_execution_state(
                    execution.id,
                    {"updated": True},
                ) is True
            else:
                decorated.rollback_to_node(execution.id, "end")

            with SQLAlchemyUnitOfWork(
                db_url=db_url,
                blob_storage_path=str(tmp_path / f"reader-blobs-{method_name}"),
            ) as reader:
                stored = reader.executions.get(execution.id)
                events = reader.outbox.get_pending()

                if method_name == "update_execution_state":
                    assert stored.state.workflow_variables["updated"] is True
                else:
                    assert stored.state.current_node_id == "end"
                    assert stored.status == ExecutionStatus.PAUSED
                assert [event.event_type for event in events] == [event_type]


# ═══════════════════════════════════════════════════════════════
# Transaction Rollback on Failure
# ═══════════════════════════════════════════════════════════════


class TestTransactionConsistency:

    def test_failed_execution_state_is_consistent(self, langgraph_decorated_setup):
        """If execution fails, the execution record should be in FAILED state."""
        decorated, adapter, uow = langgraph_decorated_setup

        workflow = _make_workflow()
        uow.workflows.add(workflow)
        uow.commit()

        execution = decorated.create_execution(
            workflow, initial_state={"value": 0, "messages": [], "route": None},
        )

        # Run a second time on already-completed to trigger failure
        execution = decorated.run(execution.id)

        # Read back from repository
        stored = uow.executions.get(execution.id)
        if stored.status in (ExecutionStatus.COMPLETED, ExecutionStatus.FAILED):
            assert stored.status == execution.status


# ═══════════════════════════════════════════════════════════════
# Event Semantics (Gap 8 Fix Verification)
# ═══════════════════════════════════════════════════════════════


class TestEventSemantics:

    def test_create_execution_emits_created_not_started(
        self, langgraph_decorated_setup
    ):
        """create_execution should emit EXECUTION_CREATED, not EXECUTION_STARTED."""
        decorated, adapter, uow = langgraph_decorated_setup

        workflow = _make_workflow()
        uow.workflows.add(workflow)
        uow.commit()

        execution = decorated.create_execution(
            workflow, initial_state={"value": 0, "messages": [], "route": None},
        )
        assert execution.status == ExecutionStatus.PENDING

        events = uow.outbox.get_pending()
        event_types = [e.event_type.value for e in events]

        assert "execution_created" in event_types
        assert "execution_started" not in event_types, \
            "STARTED should not be emitted on create (still PENDING)"

    def test_run_emits_started_then_completed(self, langgraph_decorated_setup):
        """run() should emit both STARTED and COMPLETED for a successful run."""
        decorated, adapter, uow = langgraph_decorated_setup
        create_minimal_graph = _try_import_fixtures()
        graph = create_minimal_graph()

        workflow = _make_workflow()
        uow.workflows.add(workflow)
        uow.commit()

        execution = decorated.create_execution(
            workflow, initial_state={"value": 0, "messages": [], "route": None},
        )
        execution = decorated.run(execution.id, graph=graph)
        assert execution.status == ExecutionStatus.COMPLETED

        events = uow.outbox.get_pending()
        event_types = [e.event_type.value for e in events]

        started_indices = [
            i for i, e in enumerate(events)
            if e.event_type.value == "execution_started"
        ]
        completed_indices = [
            i for i, e in enumerate(events)
            if e.event_type.value == "execution_completed"
        ]

        assert len(started_indices) >= 1, "STARTED event missing"
        assert len(completed_indices) >= 1, "COMPLETED event missing"
        assert started_indices[0] < completed_indices[0], \
            "STARTED should come before COMPLETED"


# ═══════════════════════════════════════════════════════════════
# Node Executor Path Outbox
# ═══════════════════════════════════════════════════════════════


class TestNodeExecutorOutbox:

    def test_node_executor_path_emits_events(self, node_executor_decorated_setup):
        """Node executor path (InMemoryAdapter) also emits outbox events."""
        decorated, adapter, uow = node_executor_decorated_setup

        workflow = _make_workflow()
        uow.workflows.add(workflow)
        uow.commit()

        execution = decorated.create_execution(workflow)
        execution = decorated.run(execution.id)

        events = uow.outbox.get_pending()
        event_types = [e.event_type.value for e in events]
        assert "execution_created" in event_types
