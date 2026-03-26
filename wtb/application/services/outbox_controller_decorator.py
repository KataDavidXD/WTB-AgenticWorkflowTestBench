"""
Outbox Execution Controller Decorator.

OCP-compliant decorator that wraps IExecutionController and emits outbox events
for all lifecycle operations. Keeps the controller SRP-clean (orchestration only).

Architecture:
    WTBTestBench / BatchRunner
        |
        v
    OutboxExecutionControllerDecorator (IExecutionController)
        |--- emits OutboxEvent for lifecycle ops
        v
    ExecutionController (real orchestration)
"""

import logging
from typing import Optional, Dict, Any, List, Callable, TYPE_CHECKING

from wtb.domain.interfaces.execution_controller import IExecutionController
from wtb.domain.models.workflow import (
    Execution,
    ExecutionState,
    ExecutionStatus,
    TestWorkflow,
)
from wtb.domain.models.outbox import OutboxEvent, OutboxEventType

if TYPE_CHECKING:
    from wtb.domain.interfaces.repositories import IOutboxRepository

logger = logging.getLogger(__name__)


class OutboxExecutionControllerDecorator(IExecutionController):
    """
    Decorator that wraps IExecutionController and writes outbox events.

    SOLID Compliance:
    - SRP: Only adds outbox event emission, delegates all logic
    - OCP: Adds behavior without modifying ExecutionController
    - LSP: Fully substitutable for IExecutionController
    - DIP: Depends on IExecutionController and IOutboxRepository abstractions

    ACID: Outbox events are committed via commit_fn so they persist
    in the same (or immediately following) transaction as business data.
    """

    def __init__(
        self,
        inner: IExecutionController,
        outbox_repo: Optional["IOutboxRepository"] = None,
        commit_fn: Optional[Callable[[], None]] = None,
    ):
        self._inner = inner
        self._outbox = outbox_repo
        self._commit_fn = commit_fn

    def _emit(
        self,
        event_type: OutboxEventType,
        aggregate_id: str,
        payload: Dict[str, Any],
    ) -> None:
        if not self._outbox:
            return
        try:
            event = OutboxEvent.create(
                event_type=event_type,
                aggregate_type="Execution",
                aggregate_id=aggregate_id,
                payload=payload,
            )
            self._outbox.add(event)
        except Exception as e:
            logger.warning(f"Outbox event emission failed (non-fatal): {e}")

    def _commit_outbox(self) -> None:
        """Commit outbox events so they are persisted (ACID guarantee)."""
        if self._commit_fn:
            try:
                self._commit_fn()
            except Exception as e:
                logger.warning(f"Outbox commit failed (non-fatal): {e}")

    # -- Delegated IExecutionController methods with outbox events --

    def create_execution(
        self,
        workflow: TestWorkflow,
        initial_state: Optional[Dict[str, Any]] = None,
        breakpoints: Optional[List[str]] = None,
    ) -> Execution:
        result = self._inner.create_execution(workflow, initial_state, breakpoints)
        self._emit(
            OutboxEventType.EXECUTION_CREATED,
            result.id,
            {
                "execution_id": result.id,
                "workflow_id": result.workflow_id,
                "status": result.status.value,
            },
        )
        self._commit_outbox()
        return result

    def run(self, execution_id: str, graph: Any = None) -> Execution:
        result = self._inner.run(execution_id, graph)
        if result.status == ExecutionStatus.COMPLETED:
            self._emit(
                OutboxEventType.EXECUTION_STARTED,
                execution_id,
                {"execution_id": execution_id, "workflow_id": result.workflow_id},
            )
            self._emit(
                OutboxEventType.EXECUTION_COMPLETED,
                execution_id,
                {"execution_id": execution_id, "workflow_id": result.workflow_id},
            )
        elif result.status == ExecutionStatus.FAILED:
            self._emit(
                OutboxEventType.EXECUTION_STARTED,
                execution_id,
                {"execution_id": execution_id, "workflow_id": result.workflow_id},
            )
            self._emit(
                OutboxEventType.EXECUTION_FAILED,
                execution_id,
                {"execution_id": execution_id, "error": result.error_message or ""},
            )
        self._commit_outbox()
        return result

    def pause(self, execution_id: str) -> Execution:
        result = self._inner.pause(execution_id)
        self._emit(
            OutboxEventType.EXECUTION_PAUSED,
            execution_id,
            {
                "execution_id": execution_id,
                "paused_at_node": result.state.current_node_id or "",
            },
        )
        self._commit_outbox()
        return result

    def resume(
        self,
        execution_id: str,
        modified_state: Optional[Dict[str, Any]] = None,
    ) -> Execution:
        result = self._inner.resume(execution_id, modified_state)
        self._emit(
            OutboxEventType.EXECUTION_RESUMED,
            execution_id,
            {
                "execution_id": execution_id,
                "state_modified": modified_state is not None,
            },
        )
        self._commit_outbox()
        return result

    def stop(self, execution_id: str) -> Execution:
        result = self._inner.stop(execution_id)
        self._emit(
            OutboxEventType.EXECUTION_STOPPED,
            execution_id,
            {"execution_id": execution_id},
        )
        self._commit_outbox()
        return result

    def rollback(self, execution_id: str, checkpoint_id: str) -> Execution:
        result = self._inner.rollback(execution_id, checkpoint_id)
        self._emit(
            OutboxEventType.ROLLBACK_PERFORMED,
            execution_id,
            {
                "execution_id": execution_id,
                "checkpoint_id": checkpoint_id,
            },
        )
        self._commit_outbox()
        return result

    def get_state(self, execution_id: str) -> ExecutionState:
        return self._inner.get_state(execution_id)

    def get_status(self, execution_id: str) -> Execution:
        return self._inner.get_status(execution_id)

    # -- Pass-through for extended capabilities --

    def __getattr__(self, name: str):
        """Delegate any non-overridden attribute to the inner controller."""
        return getattr(self._inner, name)
