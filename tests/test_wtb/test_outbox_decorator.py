"""Tests for OutboxExecutionControllerDecorator (C2 fix: deferred commit)."""

from unittest.mock import MagicMock

from wtb.application.services.outbox_controller_decorator import (
    OutboxExecutionControllerDecorator,
)
from wtb.domain.models import Execution, ExecutionState, ExecutionStatus


class TestOutboxDecoratorDeferredCommit:
    def test_sets_deferred_commit_on_inner(self):
        inner = MagicMock()
        inner.set_deferred_commit = MagicMock()
        OutboxExecutionControllerDecorator(inner=inner)
        inner.set_deferred_commit.assert_called_once_with(True)

    def test_commit_fn_called_after_emit(self):
        inner = MagicMock()
        inner.set_deferred_commit = MagicMock()
        outbox = MagicMock()
        commit_fn = MagicMock()

        state = ExecutionState(
            current_node_id="start",
            workflow_variables={},
            execution_path=[],
            node_results={},
        )
        execution = Execution(workflow_id="w1", status=ExecutionStatus.PENDING, state=state)
        inner.create_execution.return_value = execution

        decorator = OutboxExecutionControllerDecorator(
            inner=inner, outbox_repo=outbox, commit_fn=commit_fn
        )
        decorator.create_execution(MagicMock())

        assert outbox.add.called
        assert commit_fn.called

    def test_no_outbox_still_delegates(self):
        inner = MagicMock()
        state = ExecutionState(
            current_node_id="start",
            workflow_variables={},
            execution_path=[],
            node_results={},
        )
        execution = Execution(workflow_id="w1", status=ExecutionStatus.COMPLETED, state=state)
        inner.run.return_value = execution

        decorator = OutboxExecutionControllerDecorator(inner=inner)
        decorator.run("exec1")
        assert inner.run.called
