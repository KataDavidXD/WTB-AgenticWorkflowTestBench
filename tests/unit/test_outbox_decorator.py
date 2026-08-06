"""
Unit tests for OutboxExecutionControllerDecorator.

Verifies outbox events are emitted for all lifecycle operations.
"""

import pytest
from unittest.mock import MagicMock, call

from wtb.domain.models.workflow import (
    Execution,
    ExecutionState,
    ExecutionStatus,
    TestWorkflow,
    WorkflowNode,
    WorkflowEdge,
)
from wtb.domain.models.outbox import OutboxEventType
from wtb.application.services.outbox_controller_decorator import (
    OutboxExecutionControllerDecorator,
)
from wtb.application.services.execution_controller import ExecutionController
from wtb.infrastructure.adapters.inmemory_state_adapter import InMemoryStateAdapter
from wtb.infrastructure.database.inmemory_unit_of_work import InMemoryUnitOfWork


def _make_execution(status=ExecutionStatus.PENDING, exec_id="exec-1") -> Execution:
    return Execution(
        id=exec_id,
        workflow_id="wf-1",
        status=status,
        state=ExecutionState(
            current_node_id="start",
            workflow_variables={},
        ),
    )


def _make_workflow() -> TestWorkflow:
    wf = TestWorkflow(id="wf-1", name="test", entry_point="start")
    wf.add_node(WorkflowNode(id="start", name="Start", type="start"))
    wf.add_node(WorkflowNode(id="end", name="End", type="end"))
    wf.add_edge(WorkflowEdge(source_id="start", target_id="end"))
    return wf


def _make_atomic_decorator(inner, outbox_repo):
    return OutboxExecutionControllerDecorator(
        inner,
        outbox_repo,
        commit_fn=MagicMock(),
        rollback_fn=MagicMock(),
    )


class TestOutboxDecoratorEvents:

    @pytest.mark.parametrize(
        ("method_name", "args"),
        [
            ("create_execution", (_make_workflow(),)),
            ("run", ("exec-1",)),
            ("pause", ("exec-1",)),
            ("resume", ("exec-1",)),
            ("stop", ("exec-1",)),
            ("rollback", ("exec-1", "cp-1")),
            ("fork", ("exec-1", "cp-1")),
        ],
    )
    def test_inner_mutation_error_rolls_back_once(self, method_name, args):
        inner = MagicMock()
        primary_error = RuntimeError(f"{method_name} failed")
        getattr(inner, method_name).side_effect = primary_error
        outbox_repo = MagicMock()
        commit_fn = MagicMock()
        rollback_fn = MagicMock()
        decorator = OutboxExecutionControllerDecorator(
            inner,
            outbox_repo,
            commit_fn=commit_fn,
            rollback_fn=rollback_fn,
        )

        with pytest.raises(RuntimeError) as exc_info:
            getattr(decorator, method_name)(*args)

        assert exc_info.value is primary_error
        rollback_fn.assert_called_once_with()
        outbox_repo.add.assert_not_called()
        commit_fn.assert_not_called()

    def test_inner_error_is_preserved_when_rollback_also_fails(self):
        inner = MagicMock()
        primary_error = RuntimeError("inner failed")
        inner.run.side_effect = primary_error
        rollback_fn = MagicMock(side_effect=RuntimeError("rollback failed"))
        decorator = OutboxExecutionControllerDecorator(
            inner,
            outbox_repo=MagicMock(),
            commit_fn=MagicMock(),
            rollback_fn=rollback_fn,
        )

        with pytest.raises(RuntimeError) as exc_info:
            decorator.run("exec-1")

        assert exc_info.value is primary_error
        rollback_fn.assert_called_once_with()

    @pytest.mark.parametrize(
        ("method_name", "args", "inner_result", "event_type"),
        [
            (
                "rollback_to_node",
                ("exec-1", "node-a"),
                _make_execution(status=ExecutionStatus.PAUSED),
                OutboxEventType.ROLLBACK_PERFORMED,
            ),
            (
                "update_execution_state",
                ("exec-1", {"value": 2}),
                True,
                OutboxEventType.STATE_MODIFIED,
            ),
        ],
    )
    def test_extended_mutation_success_emits_and_commits_once(
        self,
        method_name,
        args,
        inner_result,
        event_type,
    ):
        inner = MagicMock()
        getattr(inner, method_name).return_value = inner_result
        outbox_repo = MagicMock()
        commit_fn = MagicMock()
        rollback_fn = MagicMock()
        decorator = OutboxExecutionControllerDecorator(
            inner,
            outbox_repo,
            commit_fn=commit_fn,
            rollback_fn=rollback_fn,
        )

        result = getattr(decorator, method_name)(*args)

        assert result is inner_result
        event = outbox_repo.add.call_args.args[0]
        assert event.event_type == event_type
        commit_fn.assert_called_once_with()
        rollback_fn.assert_not_called()

    @pytest.mark.parametrize(
        ("method_name", "args", "inner_result"),
        [
            (
                "rollback_to_node",
                ("exec-1", "node-a"),
                _make_execution(status=ExecutionStatus.PAUSED),
            ),
            ("update_execution_state", ("exec-1", {"value": 2}), True),
        ],
    )
    @pytest.mark.parametrize("failure_stage", ["inner", "emit", "commit"])
    def test_extended_mutation_failure_rolls_back_once_and_preserves_error(
        self,
        method_name,
        args,
        inner_result,
        failure_stage,
    ):
        inner = MagicMock()
        inner_method = getattr(inner, method_name)
        primary_error = RuntimeError(f"{failure_stage} failed")
        if failure_stage == "inner":
            inner_method.side_effect = primary_error
        else:
            inner_method.return_value = inner_result
        outbox_repo = MagicMock()
        commit_fn = MagicMock()
        if failure_stage == "emit":
            outbox_repo.add.side_effect = primary_error
        elif failure_stage == "commit":
            commit_fn.side_effect = primary_error
        rollback_fn = MagicMock()
        decorator = OutboxExecutionControllerDecorator(
            inner,
            outbox_repo,
            commit_fn=commit_fn,
            rollback_fn=rollback_fn,
        )

        with pytest.raises(RuntimeError) as exc_info:
            getattr(decorator, method_name)(*args)

        assert exc_info.value is primary_error
        rollback_fn.assert_called_once_with()
        if failure_stage == "inner":
            outbox_repo.add.assert_not_called()
            commit_fn.assert_not_called()
        elif failure_stage == "emit":
            commit_fn.assert_not_called()

    def test_run_completed_emits_started_and_completed(self):
        inner = MagicMock()
        execution = _make_execution(status=ExecutionStatus.COMPLETED)
        inner.run.return_value = execution

        outbox_repo = MagicMock()
        decorator = _make_atomic_decorator(inner, outbox_repo)

        result = decorator.run("exec-1", graph=None)

        assert result.status == ExecutionStatus.COMPLETED
        assert outbox_repo.add.call_count == 2
        events = [call[0][0] for call in outbox_repo.add.call_args_list]
        assert events[0].event_type == OutboxEventType.EXECUTION_STARTED
        assert events[1].event_type == OutboxEventType.EXECUTION_COMPLETED

    def test_run_failed_emits_started_and_failed(self):
        inner = MagicMock()
        execution = _make_execution(status=ExecutionStatus.FAILED)
        execution.error_message = "something broke"
        inner.run.return_value = execution

        outbox_repo = MagicMock()
        decorator = _make_atomic_decorator(inner, outbox_repo)

        result = decorator.run("exec-1")

        assert outbox_repo.add.call_count == 2
        events = [call[0][0] for call in outbox_repo.add.call_args_list]
        assert events[0].event_type == OutboxEventType.EXECUTION_STARTED
        assert events[1].event_type == OutboxEventType.EXECUTION_FAILED
        assert "something broke" in events[1].payload.get("error", "")

    def test_pause_emits_execution_paused(self):
        inner = MagicMock()
        execution = _make_execution(status=ExecutionStatus.PAUSED)
        inner.pause.return_value = execution

        outbox_repo = MagicMock()
        decorator = _make_atomic_decorator(inner, outbox_repo)

        decorator.pause("exec-1")

        outbox_repo.add.assert_called_once()
        event = outbox_repo.add.call_args[0][0]
        assert event.event_type == OutboxEventType.EXECUTION_PAUSED

    def test_resume_emits_execution_resumed(self):
        inner = MagicMock()
        execution = _make_execution(status=ExecutionStatus.RUNNING)
        inner.resume.return_value = execution

        outbox_repo = MagicMock()
        decorator = _make_atomic_decorator(inner, outbox_repo)

        decorator.resume("exec-1")

        outbox_repo.add.assert_called_once()
        event = outbox_repo.add.call_args[0][0]
        assert event.event_type == OutboxEventType.EXECUTION_RESUMED

    def test_stop_emits_execution_stopped(self):
        inner = MagicMock()
        execution = _make_execution(status=ExecutionStatus.CANCELLED)
        inner.stop.return_value = execution

        outbox_repo = MagicMock()
        decorator = _make_atomic_decorator(inner, outbox_repo)

        decorator.stop("exec-1")

        outbox_repo.add.assert_called_once()
        event = outbox_repo.add.call_args[0][0]
        assert event.event_type == OutboxEventType.EXECUTION_STOPPED

    def test_rollback_emits_rollback_performed(self):
        inner = MagicMock()
        execution = _make_execution(status=ExecutionStatus.PAUSED)
        inner.rollback.return_value = execution

        outbox_repo = MagicMock()
        decorator = _make_atomic_decorator(inner, outbox_repo)

        decorator.rollback("exec-1", "cp-123")

        outbox_repo.add.assert_called_once()
        event = outbox_repo.add.call_args[0][0]
        assert event.event_type == OutboxEventType.ROLLBACK_PERFORMED
        assert event.payload["checkpoint_id"] == "cp-123"

    def test_create_execution_emits_execution_created(self):
        inner = MagicMock()
        execution = _make_execution(status=ExecutionStatus.PENDING)
        inner.create_execution.return_value = execution

        outbox_repo = MagicMock()
        decorator = _make_atomic_decorator(inner, outbox_repo)

        workflow = _make_workflow()
        decorator.create_execution(workflow)

        inner.create_execution.assert_called_once_with(workflow, None, None)
        outbox_repo.add.assert_called_once()
        event = outbox_repo.add.call_args[0][0]
        assert event.event_type == OutboxEventType.EXECUTION_CREATED

    def test_create_execution_forwards_optional_stable_id_and_metadata(self):
        inner = MagicMock()
        execution = _make_execution(exec_id="stable-execution-id")
        inner.create_execution.return_value = execution
        outbox_repo = MagicMock()
        decorator = _make_atomic_decorator(inner, outbox_repo)
        workflow = _make_workflow()
        initial_state = {"request": "stable"}
        breakpoints = ["end"]
        metadata = {"actor_id": "actor-1"}

        result = decorator.create_execution(
            workflow,
            initial_state=initial_state,
            breakpoints=breakpoints,
            metadata=metadata,
            execution_id="stable-execution-id",
        )

        assert result is execution
        inner.create_execution.assert_called_once_with(
            workflow,
            initial_state,
            breakpoints,
            metadata=metadata,
            execution_id="stable-execution-id",
        )
        event = outbox_repo.add.call_args[0][0]
        assert event.aggregate_id == "stable-execution-id"
        assert event.payload["execution_id"] == "stable-execution-id"

    def test_no_outbox_repo_does_not_raise(self):
        inner = MagicMock()
        execution = _make_execution(status=ExecutionStatus.COMPLETED)
        inner.run.return_value = execution

        decorator = OutboxExecutionControllerDecorator(inner, outbox_repo=None)
        result = decorator.run("exec-1")

        assert result.status == ExecutionStatus.COMPLETED

    def test_outbox_error_raises_and_rolls_back_shared_uow(self):
        inner = MagicMock()
        execution = _make_execution(status=ExecutionStatus.COMPLETED)
        inner.run.return_value = execution

        outbox_repo = MagicMock()
        outbox_repo.add.side_effect = RuntimeError("DB error")
        commit_fn = MagicMock()
        rollback_fn = MagicMock()

        decorator = OutboxExecutionControllerDecorator(
            inner,
            outbox_repo,
            commit_fn=commit_fn,
            rollback_fn=rollback_fn,
        )

        with pytest.raises(RuntimeError, match="DB error"):
            decorator.run("exec-1")

        rollback_fn.assert_called_once_with()
        commit_fn.assert_not_called()

    def test_commit_error_raises_and_rolls_back_shared_uow(self):
        inner = MagicMock()
        inner.create_execution.return_value = _make_execution()
        outbox_repo = MagicMock()
        commit_fn = MagicMock(side_effect=RuntimeError("commit failed"))
        rollback_fn = MagicMock()
        decorator = OutboxExecutionControllerDecorator(
            inner,
            outbox_repo,
            commit_fn=commit_fn,
            rollback_fn=rollback_fn,
        )

        with pytest.raises(RuntimeError, match="commit failed"):
            decorator.create_execution(_make_workflow())

        rollback_fn.assert_called_once_with()

    def test_real_inmemory_uow_rolls_back_business_row_when_outbox_add_fails(self):
        uow = InMemoryUnitOfWork()
        workflow = _make_workflow()
        uow.workflows.add(workflow)
        uow.commit()
        inner = ExecutionController(
            execution_repository=uow.executions,
            workflow_repository=uow.workflows,
            state_adapter=InMemoryStateAdapter(),
            unit_of_work=uow,
        )
        broken_outbox = MagicMock()
        broken_outbox.add.side_effect = RuntimeError("outbox add failed")
        decorator = OutboxExecutionControllerDecorator(
            inner,
            broken_outbox,
            commit_fn=uow.commit,
            rollback_fn=uow.rollback,
        )

        with pytest.raises(RuntimeError, match="outbox add failed"):
            decorator.create_execution(workflow)

        assert uow.executions.list() == []
        assert uow.workflows.get(workflow.id) == workflow

    def test_rollback_error_does_not_mask_outbox_error(self):
        inner = MagicMock()
        inner.run.return_value = _make_execution(status=ExecutionStatus.COMPLETED)
        outbox_repo = MagicMock()
        primary_error = RuntimeError("outbox failed")
        outbox_repo.add.side_effect = primary_error
        rollback_fn = MagicMock(side_effect=RuntimeError("rollback failed"))
        decorator = OutboxExecutionControllerDecorator(
            inner,
            outbox_repo,
            commit_fn=MagicMock(),
            rollback_fn=rollback_fn,
        )

        with pytest.raises(RuntimeError) as exc_info:
            decorator.run("exec-1")

        assert exc_info.value is primary_error
        rollback_fn.assert_called_once_with()

    def test_get_state_delegates_directly(self):
        inner = MagicMock()
        state = ExecutionState(workflow_variables={"x": 1})
        inner.get_state.return_value = state

        decorator = OutboxExecutionControllerDecorator(inner, outbox_repo=None)
        result = decorator.get_state("exec-1")

        assert result == state
        inner.get_state.assert_called_once_with("exec-1")

    def test_getattr_delegates_to_inner(self):
        inner = MagicMock()
        inner.supports_time_travel.return_value = True

        decorator = OutboxExecutionControllerDecorator(inner, outbox_repo=None)
        assert decorator.supports_time_travel() is True
