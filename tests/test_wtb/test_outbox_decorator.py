"""Tests for OutboxExecutionControllerDecorator (C2 fix: deferred commit)."""

from unittest.mock import MagicMock

import pytest

from wtb.application.services.outbox_controller_decorator import (
    OutboxExecutionControllerDecorator,
)
from wtb.application.factories import WTBTestBenchFactory
from wtb.domain.models import Execution, ExecutionState, ExecutionStatus


class TestOutboxDecoratorDeferredCommit:
    def test_sets_deferred_commit_on_inner(self):
        inner = MagicMock()
        inner.set_deferred_commit = MagicMock()
        OutboxExecutionControllerDecorator(
            inner=inner,
            outbox_repo=MagicMock(),
            commit_fn=MagicMock(),
            rollback_fn=MagicMock(),
        )
        inner.set_deferred_commit.assert_called_once_with(True)

    @pytest.mark.parametrize(
        ("commit_fn", "rollback_fn"),
        [
            (None, lambda: None),
            (lambda: None, None),
            ("not-callable", lambda: None),
            (lambda: None, "not-callable"),
        ],
    )
    def test_atomic_mode_rejects_incomplete_callbacks_without_mutating_inner(
        self,
        commit_fn,
        rollback_fn,
    ):
        inner = MagicMock()
        inner.set_deferred_commit = MagicMock()

        with pytest.raises(ValueError, match="atomic outbox"):
            OutboxExecutionControllerDecorator(
                inner=inner,
                outbox_repo=MagicMock(),
                commit_fn=commit_fn,
                rollback_fn=rollback_fn,
            )

        inner.set_deferred_commit.assert_not_called()

    def test_atomic_mode_rejects_inner_without_deferred_commit_support(self):
        class UnsupportedController:
            pass

        with pytest.raises(ValueError, match="set_deferred_commit"):
            OutboxExecutionControllerDecorator(
                inner=UnsupportedController(),
                outbox_repo=MagicMock(),
                commit_fn=MagicMock(),
                rollback_fn=MagicMock(),
            )

    def test_commit_fn_called_after_emit(self):
        inner = MagicMock()
        inner.set_deferred_commit = MagicMock()
        outbox = MagicMock()
        commit_fn = MagicMock()
        rollback_fn = MagicMock()

        state = ExecutionState(
            current_node_id="start",
            workflow_variables={},
            execution_path=[],
            node_results={},
        )
        execution = Execution(workflow_id="w1", status=ExecutionStatus.PENDING, state=state)
        inner.create_execution.return_value = execution

        decorator = OutboxExecutionControllerDecorator(
            inner=inner,
            outbox_repo=outbox,
            commit_fn=commit_fn,
            rollback_fn=rollback_fn,
        )
        decorator.create_execution(MagicMock())

        assert outbox.add.called
        assert commit_fn.called

    def test_testing_factory_wires_rollback_from_shared_uow(self):
        bench = WTBTestBenchFactory.create_for_testing()
        try:
            decorator = bench._exec_ctrl
            uow = decorator._inner._uow

            assert decorator._rollback_fn.__self__ is uow
            assert decorator._rollback_fn.__func__ is type(uow).rollback
        finally:
            bench.close()

    def test_no_outbox_still_delegates(self):
        inner = MagicMock()
        inner.set_deferred_commit = MagicMock()
        commit_fn = MagicMock()
        rollback_fn = MagicMock()
        state = ExecutionState(
            current_node_id="start",
            workflow_variables={},
            execution_path=[],
            node_results={},
        )
        execution = Execution(workflow_id="w1", status=ExecutionStatus.COMPLETED, state=state)
        inner.run.return_value = execution

        decorator = OutboxExecutionControllerDecorator(
            inner=inner,
            outbox_repo=None,
            commit_fn=commit_fn,
            rollback_fn=rollback_fn,
        )
        result = decorator.run("exec1")

        assert result is execution
        assert inner.run.called
        inner.set_deferred_commit.assert_not_called()
        commit_fn.assert_not_called()
        rollback_fn.assert_not_called()
