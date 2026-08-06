"""
Outbox Execution Controller Decorator.

OCP-compliant decorator that wraps IExecutionController and emits outbox events
for all lifecycle operations. Keeps the controller SRP-clean (orchestration only).

ACID Guarantee:
    The decorator uses deferred-commit mode on the inner controller so that
    outbox events are written to the same UoW session *before* the single
    commit happens.  This ensures outbox rows and business data are atomically
    committed in one transaction (core Outbox Pattern requirement).

Architecture:
    WTBTestBench / BatchRunner
        |
        v
    OutboxExecutionControllerDecorator (IExecutionController)
        |--- sets inner.set_deferred_commit(True)
        |--- delegates to inner (no commit inside inner)
        |--- emits OutboxEvent into shared UoW outbox repo
        |--- calls commit_fn() once  (ACID: single commit)
        v
    ExecutionController (real orchestration, commit deferred)
"""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional

from wtb.domain.interfaces.execution_controller import IExecutionController
from wtb.domain.models.outbox import OutboxEvent, OutboxEventType
from wtb.domain.models.workflow import (
    Execution,
    ExecutionState,
    ExecutionStatus,
    TestWorkflow,
)

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

    ACID: The inner controller's commit is deferred. Outbox events are added
    to the same UoW, then a single commit_fn() persists both business data
    and outbox rows atomically.
    """

    def __init__(
        self,
        inner: IExecutionController,
        outbox_repo: Optional["IOutboxRepository"] = None,
        commit_fn: Callable[[], None] | None = None,
        rollback_fn: Callable[[], None] | None = None,
    ):
        self._inner = inner
        self._outbox = outbox_repo
        self._commit_fn: Callable[[], None] | None = None
        self._rollback_fn: Callable[[], None] | None = None

        if outbox_repo is None:
            return

        if not callable(commit_fn) or not callable(rollback_fn):
            raise ValueError(
                "atomic outbox mode requires callable commit_fn and rollback_fn"
            )

        set_deferred_commit = getattr(inner, "set_deferred_commit", None)
        if not callable(set_deferred_commit):
            raise ValueError(
                "atomic outbox mode requires callable inner.set_deferred_commit"
            )

        self._commit_fn = commit_fn
        self._rollback_fn = rollback_fn
        set_deferred_commit(True)

    def _emit(
        self,
        event_type: OutboxEventType,
        aggregate_id: str,
        payload: dict[str, Any],
    ) -> None:
        if self._outbox is None:
            return
        try:
            event = OutboxEvent.create(
                event_type=event_type,
                aggregate_type="Execution",
                aggregate_id=aggregate_id,
                payload=payload,
            )
            self._outbox.add(event)
        except Exception as error:
            logger.error(
                f"Outbox event emission failed for {event_type.value}: {error}",
                exc_info=True,
            )
            self._rollback_after_failure()
            raise

    def _rollback_after_failure(self) -> None:
        """Best-effort rollback that never masks the active primary error."""
        if not self._rollback_fn:
            return
        try:
            self._rollback_fn()
        except Exception as rollback_error:
            logger.error(
                f"Outbox rollback failed while preserving primary error: {rollback_error}",
                exc_info=True,
            )

    def _commit_outbox(self) -> None:
        """Single atomic commit for business data + outbox events."""
        if self._commit_fn:
            try:
                self._commit_fn()
            except Exception as error:
                logger.error(f"Outbox commit failed: {error}", exc_info=True)
                self._rollback_after_failure()
                raise

    def _invoke_inner_mutation(
        self,
        mutation: Callable[..., Execution],
        *args: Any,
        **kwargs: Any,
    ) -> Execution:
        """Run one deferred inner mutation and rollback if it raises."""
        try:
            return mutation(*args, **kwargs)
        except Exception:
            self._rollback_after_failure()
            raise

    # -- Delegated IExecutionController methods with outbox events --

    def create_execution(
        self,
        workflow: TestWorkflow,
        initial_state: dict[str, Any] | None = None,
        breakpoints: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        execution_id: str | None = None,
    ) -> Execution:
        create_args = (workflow, initial_state, breakpoints)
        create_kwargs: dict[str, Any] = {}
        if metadata is not None:
            create_kwargs["metadata"] = metadata
        if execution_id is not None:
            create_kwargs["execution_id"] = execution_id
        result = self._invoke_inner_mutation(
            self._inner.create_execution,
            *create_args,
            **create_kwargs,
        )
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
        result = self._invoke_inner_mutation(self._inner.run, execution_id, graph)
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
        result = self._invoke_inner_mutation(self._inner.pause, execution_id)
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
        modified_state: dict[str, Any] | None = None,
    ) -> Execution:
        result = self._invoke_inner_mutation(
            self._inner.resume,
            execution_id,
            modified_state,
        )
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
        result = self._invoke_inner_mutation(self._inner.stop, execution_id)
        self._emit(
            OutboxEventType.EXECUTION_STOPPED,
            execution_id,
            {"execution_id": execution_id},
        )
        self._commit_outbox()
        return result

    def rollback(self, execution_id: str, checkpoint_id: str) -> Execution:
        result = self._invoke_inner_mutation(
            self._inner.rollback,
            execution_id,
            checkpoint_id,
        )
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

    def fork(
        self,
        execution_id: str,
        checkpoint_id: str,
        new_initial_state: dict[str, Any] | None = None,
    ) -> Execution:
        result = self._invoke_inner_mutation(
            self._inner.fork,
            execution_id,
            checkpoint_id,
            new_initial_state,
        )
        self._emit(
            OutboxEventType.EXECUTION_FORKED,
            result.id,
            {
                "source_execution_id": execution_id,
                "fork_execution_id": result.id,
                "checkpoint_id": checkpoint_id,
            },
        )
        self._commit_outbox()
        return result

    def rollback_to_node(self, execution_id: str, node_id: str) -> Execution:
        result = self._invoke_inner_mutation(
            self._inner.rollback_to_node,
            execution_id,
            node_id,
        )
        self._emit(
            OutboxEventType.ROLLBACK_PERFORMED,
            execution_id,
            {
                "execution_id": execution_id,
                "node_id": node_id,
            },
        )
        self._commit_outbox()
        return result

    def update_execution_state(
        self,
        execution_id: str,
        values: dict[str, Any],
    ) -> bool:
        result = self._invoke_inner_mutation(
            self._inner.update_execution_state,
            execution_id,
            values,
        )
        if result:
            self._emit(
                OutboxEventType.STATE_MODIFIED,
                execution_id,
                {
                    "execution_id": execution_id,
                    "updated_fields": list(values),
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
