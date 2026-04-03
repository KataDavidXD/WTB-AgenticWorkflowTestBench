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


class TestOutboxDecoratorEvents:

    def test_run_completed_emits_started_and_completed(self):
        inner = MagicMock()
        execution = _make_execution(status=ExecutionStatus.COMPLETED)
        inner.run.return_value = execution

        outbox_repo = MagicMock()
        decorator = OutboxExecutionControllerDecorator(inner, outbox_repo)

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
        decorator = OutboxExecutionControllerDecorator(inner, outbox_repo)

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
        decorator = OutboxExecutionControllerDecorator(inner, outbox_repo)

        decorator.pause("exec-1")

        outbox_repo.add.assert_called_once()
        event = outbox_repo.add.call_args[0][0]
        assert event.event_type == OutboxEventType.EXECUTION_PAUSED

    def test_resume_emits_execution_resumed(self):
        inner = MagicMock()
        execution = _make_execution(status=ExecutionStatus.RUNNING)
        inner.resume.return_value = execution

        outbox_repo = MagicMock()
        decorator = OutboxExecutionControllerDecorator(inner, outbox_repo)

        decorator.resume("exec-1")

        outbox_repo.add.assert_called_once()
        event = outbox_repo.add.call_args[0][0]
        assert event.event_type == OutboxEventType.EXECUTION_RESUMED

    def test_stop_emits_execution_stopped(self):
        inner = MagicMock()
        execution = _make_execution(status=ExecutionStatus.CANCELLED)
        inner.stop.return_value = execution

        outbox_repo = MagicMock()
        decorator = OutboxExecutionControllerDecorator(inner, outbox_repo)

        decorator.stop("exec-1")

        outbox_repo.add.assert_called_once()
        event = outbox_repo.add.call_args[0][0]
        assert event.event_type == OutboxEventType.EXECUTION_STOPPED

    def test_rollback_emits_rollback_performed(self):
        inner = MagicMock()
        execution = _make_execution(status=ExecutionStatus.PAUSED)
        inner.rollback.return_value = execution

        outbox_repo = MagicMock()
        decorator = OutboxExecutionControllerDecorator(inner, outbox_repo)

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
        decorator = OutboxExecutionControllerDecorator(inner, outbox_repo)

        workflow = _make_workflow()
        decorator.create_execution(workflow)

        outbox_repo.add.assert_called_once()
        event = outbox_repo.add.call_args[0][0]
        assert event.event_type == OutboxEventType.EXECUTION_CREATED

    def test_no_outbox_repo_does_not_raise(self):
        inner = MagicMock()
        execution = _make_execution(status=ExecutionStatus.COMPLETED)
        inner.run.return_value = execution

        decorator = OutboxExecutionControllerDecorator(inner, outbox_repo=None)
        result = decorator.run("exec-1")

        assert result.status == ExecutionStatus.COMPLETED

    def test_outbox_error_is_non_fatal(self):
        inner = MagicMock()
        execution = _make_execution(status=ExecutionStatus.COMPLETED)
        inner.run.return_value = execution

        outbox_repo = MagicMock()
        outbox_repo.add.side_effect = RuntimeError("DB error")

        decorator = OutboxExecutionControllerDecorator(inner, outbox_repo)
        result = decorator.run("exec-1")

        assert result.status == ExecutionStatus.COMPLETED

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
