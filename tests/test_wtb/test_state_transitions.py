"""Tests for state transition guards (H13, H14, H15 fixes)."""

import pytest

from wtb.domain.models import (
    Execution,
    ExecutionState,
    ExecutionStatus,
    InvalidStateTransition,
)
from wtb.domain.models.batch_test import BatchTest, BatchTestStatus
from wtb.domain.models.node_boundary import NodeBoundary, NodeStatus


class TestExecutionTransitions:
    def _make_execution(self, status=ExecutionStatus.PENDING):
        state = ExecutionState(
            current_node_id="start",
            workflow_variables={},
            execution_path=[],
            node_results={},
        )
        return Execution(workflow_id="w1", status=status, state=state)

    def test_complete_from_running(self):
        ex = self._make_execution(ExecutionStatus.RUNNING)
        ex.complete()
        assert ex.status == ExecutionStatus.COMPLETED

    def test_complete_from_pending_raises(self):
        ex = self._make_execution(ExecutionStatus.PENDING)
        with pytest.raises(InvalidStateTransition):
            ex.complete()

    def test_fail_from_running(self):
        ex = self._make_execution(ExecutionStatus.RUNNING)
        ex.fail("test error")
        assert ex.status == ExecutionStatus.FAILED

    def test_cancel_from_completed_raises(self):
        ex = self._make_execution(ExecutionStatus.COMPLETED)
        with pytest.raises(InvalidStateTransition):
            ex.cancel()


class TestBatchTestTransitions:
    def test_complete_from_running(self):
        bt = BatchTest(workflow_id="w1")
        bt.status = BatchTestStatus.RUNNING
        bt.complete()
        assert bt.status == BatchTestStatus.COMPLETED

    def test_complete_from_pending_raises(self):
        bt = BatchTest(workflow_id="w1")
        with pytest.raises(ValueError):
            bt.complete()


class TestNodeBoundaryTransitions:
    def test_start_from_pending(self):
        nb = NodeBoundary(execution_id="e1", node_id="n1")
        nb.start("cp1")
        assert nb.node_status == NodeStatus.RUNNING

    def test_complete_from_running(self):
        nb = NodeBoundary(execution_id="e1", node_id="n1")
        nb.start("cp1")
        nb.complete("cp2")
        assert nb.node_status == NodeStatus.COMPLETED

    def test_complete_from_pending_raises(self):
        nb = NodeBoundary(execution_id="e1", node_id="n1")
        with pytest.raises(ValueError):
            nb.complete("cp2")
