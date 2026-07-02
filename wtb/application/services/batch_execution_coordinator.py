"""
Batch Execution Coordinator Implementation.

v1.8 (2026-02-05): Coordinates rollback/fork operations across batch test results.

Design Principles:
==================
- SRP: Coordinator orchestrates, delegates to ExecutionController
- OCP: Extensible via OperationType enum
- DIP: All dependencies via interfaces
- ACID: Each operation in single UoW transaction + post-commit file restore

Transaction Architecture:
========================
Why separate file restore from UoW transaction?
- FileTracker may use different database (PostgreSQL vs SQLite)
- Cannot guarantee atomic commit across heterogeneous databases
- Outbox pattern handles retry if file restore fails

Operation Flow:
  Phase 1: UoW Transaction (State + Metadata)
    1. controller.rollback() or controller.fork() - state change
    2. outbox.add(event) - queue audit event
    3. uow.commit() - ACID durability

  Phase 2: Post-Commit File Restore (Best-Effort)
    4. file_tracking.restore_commit() - restore files
    5. If fails: logged, retryable via outbox processor

Failure Handling:
- Phase 1 fails -> entire operation rolled back, no side effects
- Phase 2 fails -> state is correct, files retryable via outbox

Usage:
    coordinator = BatchExecutionCoordinator(
        uow_factory=uow_factory,
        controller_factory=controller_factory,
        state_adapter=shared_state_adapter,
        file_tracking=file_tracking_service,
    )

    # Single rollback
    result = coordinator.rollback(exec_id, checkpoint_id)

    # Batch fork
    results = coordinator.batch_operate([
        BatchOperationRequest(exec1, cp1, OperationType.FORK),
        BatchOperationRequest(exec2, cp2, OperationType.FORK),
    ])
"""

import logging
import os
from contextlib import contextmanager
from collections.abc import Mapping
from typing import List, Optional, Dict, Any, Callable, TYPE_CHECKING

from wtb.domain.interfaces.batch_coordinator import (
    IBatchExecutionCoordinator,
    IExecutionControllerFactory,
    OperationType,
    BatchOperationRequest,
    BatchOperationResult,
)
from wtb.domain.models.outbox import OutboxEvent, OutboxEventType
from wtb.application.services.external_storage import resolve_execution_storage_paths

if TYPE_CHECKING:
    from wtb.domain.interfaces.execution_controller import IExecutionController
    from wtb.domain.interfaces.unit_of_work import IUnitOfWork
    from wtb.domain.interfaces.state_adapter import IStateAdapter
    from wtb.domain.interfaces.file_tracking import IFileTrackingService
    from wtb.domain.models.workflow import Execution
    from wtb.config import WTBConfig

logger = logging.getLogger(__name__)


class DefaultExecutionControllerFactory(IExecutionControllerFactory):
    """
    Default factory for creating ExecutionController instances.

    Creates ExecutionController with provided dependencies.
    Each call creates a fresh controller for ACID isolation.
    """

    def create(
        self,
        uow: "IUnitOfWork",
        state_adapter: "IStateAdapter",
        file_tracking_service: Optional["IFileTrackingService"] = None,
    ) -> "IExecutionController":
        """Create ExecutionController with injected dependencies."""
        from wtb.application.services.execution_controller import (
            ExecutionController,
            DefaultNodeExecutor,
        )
        import os

        output_dir = None
        workspace = getattr(file_tracking_service, "_workspace", None)
        if workspace is not None:
            output_dir = os.path.join(str(workspace), "outputs")

        return ExecutionController(
            execution_repository=uow.executions,
            workflow_repository=uow.workflows,
            state_adapter=state_adapter,
            node_executor=DefaultNodeExecutor(),
            unit_of_work=uow,
            file_tracking_service=file_tracking_service,
            output_dir=output_dir,
        )


class BatchExecutionCoordinator(IBatchExecutionCoordinator):
    """
    Coordinates batch rollback/fork operations.

    Transaction Architecture:
    - Phase 1: State changes + outbox event in single UoW transaction
    - Phase 2: File restore post-commit (best-effort, retryable)

    SOLID Compliance:
    - SRP: Only coordinates operations, delegates to ExecutionController
    - OCP: New operations via OperationType enum
    - LSP: Implements IBatchExecutionCoordinator fully
    - ISP: Interface methods are focused and necessary
    - DIP: Depends on abstractions (IUnitOfWork, IStateAdapter, etc.)

    ACID Compliance:
    - Atomicity: Each operation in single UoW transaction
    - Consistency: State validated before commit
    - Isolation: Each operation gets fresh UoW
    - Durability: Changes persisted via commit()

    Usage:
        coordinator = BatchExecutionCoordinator(
            uow_factory=lambda: UnitOfWorkFactory.create("sqlalchemy", db_url),
            controller_factory=DefaultExecutionControllerFactory(),
            state_adapter=LangGraphStateAdapter(config),
            file_tracking=FileTrackerService(ft_config),
        )

        # Rollback a variant
        execution = coordinator.rollback(exec_id, checkpoint_id)

        # Fork for exploration
        forked = coordinator.fork(exec_id, checkpoint_id, {"temperature": 0.7})
    """

    def __init__(
        self,
        uow_factory: Callable[[], "IUnitOfWork"],
        controller_factory: Optional[IExecutionControllerFactory] = None,
        state_adapter: Optional["IStateAdapter"] = None,
        file_tracking: Optional["IFileTrackingService"] = None,
        config: Optional["WTBConfig"] = None,
        environment_provider: Optional[Any] = None,
    ):
        """
        Initialize coordinator with dependencies.

        Args:
            uow_factory: Factory function creating IUnitOfWork instances.
                         Each call should return a NEW UoW for ACID isolation.
            controller_factory: Factory for creating ExecutionController.
                               Defaults to DefaultExecutionControllerFactory.
            state_adapter: Shared StateAdapter (reused across operations for efficiency).
                          Must be thread-safe if used concurrently.
            file_tracking: Optional FileTrackingService for file restore operations.
            config: Optional WTBConfig for rollback cleanup options (v1.9).
            environment_provider: Optional IEnvironmentProvider for venv
                compatibility checks on rollback.

        Design Decision:
            StateAdapter is REUSED across operations because:
            1. It's expensive to create (may involve DB connections)
            2. State operations are idempotent
            3. Different from UoW which manages transaction boundaries

        v1.9 Design Decision:
            config is optional for backward compatibility.
            When rollback_cleanup_enabled is True, cleanup info is added to
            ROLLBACK_FILE_RESTORE outbox event payload. The actual cleanup
            is performed by OutboxProcessor (which has IFileCleanupService).
        """
        self._uow_factory = uow_factory
        self._controller_factory = controller_factory or DefaultExecutionControllerFactory()
        self._state_adapter = state_adapter
        self._file_tracking = file_tracking
        self._config = config
        self._environment_provider = environment_provider

    # ═══════════════════════════════════════════════════════════════════════════
    # Checkpoint Retrieval (execution-aware storage)
    # ═══════════════════════════════════════════════════════════════════════════

    def get_checkpoints(
        self,
        execution_id: str,
        graph: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve checkpoint history using execution-specific storage.

        Opens a UoW, resolves the execution's storage metadata, builds an
        adapter pointing at the correct checkpoint DB, and delegates to
        ``ExecutionController.get_checkpoint_history``.
        """
        uow = self._uow_factory()
        try:
            uow.__enter__()
            with self._controller_for_execution(uow, execution_id, graph=graph) as controller:
                return controller.get_checkpoint_history(execution_id)
        except Exception as e:
            logger.warning(
                f"get_checkpoints via coordinator failed for {execution_id}: {e}",
                exc_info=True,
            )
            return []
        finally:
            try:
                uow.__exit__(None, None, None)
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════════════════════════
    # Single Operations
    # ═══════════════════════════════════════════════════════════════════════════

    def rollback(
        self,
        execution_id: str,
        checkpoint_id: str,
        graph: Optional[Any] = None,
    ) -> "Execution":
        """
        Rollback execution to checkpoint (destructive).

        Transaction Flow:
        1. [UoW] controller.rollback() - restore state
        2. [UoW] outbox.add(ROLLBACK_PERFORMED) - queue audit event
        3. [UoW] commit() - ACID durability
        4. [Post] file_tracking.restore_commit() - best-effort file restore

        Performance: ~10-20ms (StateAdapter reused)

        Args:
            execution_id: Execution to rollback
            checkpoint_id: Target checkpoint (UUID string)
            graph: Optional LangGraph graph for state adapter (v1.8)
                   Required if using LangGraphStateAdapter for rollback.
                   Can be created via graph_factory().

        Returns:
            Execution in PAUSED state with restored checkpoint state

        Raises:
            ValueError: If execution or checkpoint not found
            RuntimeError: If rollback fails
        """
        file_commit_id: Optional[str] = None
        execution: Optional["Execution"] = None
        uow = self._uow_factory()
        try:
            uow.__enter__()
            with self._controller_for_execution(uow, execution_id, graph=graph) as controller:
                controller.set_deferred_commit(True)

                execution = controller.rollback(execution_id, checkpoint_id)

                # Check venv compatibility post-rollback and emit warning if drifted
                self._check_venv_compat_after_rollback(execution, checkpoint_id)

                # checkpoint_id is the canonical handle; file_commit_id is
                # resolved internally from the checkpoint->CAS link for audit.
                file_commit_id = self._get_file_commit_for_checkpoint(checkpoint_id)

                # 1. Emit audit event via outbox (for tracking)
                audit_event = OutboxEvent.create(
                    event_type=OutboxEventType.ROLLBACK_PERFORMED,
                    aggregate_id=execution_id,
                    aggregate_type="Execution",
                    payload={
                        "execution_id": execution_id,
                        "checkpoint_id": checkpoint_id,
                        "file_commit_id": file_commit_id,
                        "operation": "rollback",
                    },
                )
                uow.outbox.add(audit_event)

                # 2. Emit file restore event via outbox (for ACID file restoration)
                # This ensures file restoration is coordinated via outbox processor
                if file_commit_id and self._file_tracking:
                    # Build payload with base restore info
                    file_restore_payload = {
                        "source_checkpoint_id": checkpoint_id,
                        "target_checkpoint_id": checkpoint_id,
                        "source_commit_id": file_commit_id,
                        "execution_id": execution_id,
                    }

                    # v1.9: Add cleanup configuration if enabled
                    if self._config and self._config.rollback_cleanup_enabled:
                        file_restore_payload.update({
                            "cleanup_orphaned_files": True,
                            "cleanup_dry_run": self._config.rollback_cleanup_dry_run,
                            "cleanup_backup": self._config.rollback_cleanup_backup,
                            "cleanup_max_files": self._config.rollback_cleanup_max_files,
                        })

                        # Add workspace path and patterns from file_tracking_config
                        if self._config.file_tracking_config:
                            ft_cfg = self._config.file_tracking_config
                            file_restore_payload.update({
                                "workspace_path": str(ft_cfg.workspace_path) if ft_cfg.workspace_path else ".",
                                "track_patterns": ft_cfg.auto_track_patterns or [],
                                "exclude_patterns": ft_cfg.exclude_patterns or [],
                            })

                        logger.debug(
                            f"Rollback cleanup enabled: dry_run={self._config.rollback_cleanup_dry_run}, "
                            f"backup={self._config.rollback_cleanup_backup}, "
                            f"max_files={self._config.rollback_cleanup_max_files}"
                        )

                    file_restore_event = OutboxEvent.create(
                        event_type=OutboxEventType.ROLLBACK_FILE_RESTORE,
                        aggregate_id=execution_id,
                        aggregate_type="Execution",
                        payload=file_restore_payload,
                    )
                    uow.outbox.add(file_restore_event)
                    logger.debug(f"Queued file restore for commit {file_commit_id}")

                uow.commit()

        except Exception as e:
            uow.rollback()
            logger.error(f"Rollback failed for {execution_id}: {e}")
            raise
        finally:
            uow.__exit__(None, None, None)

        # No more post-commit file restore - all handled via outbox pattern
        # OutboxProcessor will process ROLLBACK_FILE_RESTORE event

        return execution

    def fork(
        self,
        execution_id: str,
        checkpoint_id: str,
        new_state: Optional[Dict[str, Any]] = None,
        graph: Optional[Any] = None,
    ) -> "Execution":
        """
        Fork execution from checkpoint (non-destructive).

        Creates new execution with PENDING status.
        Original execution is unchanged.

        Performance: ~10-20ms

        Args:
            execution_id: Source execution to fork from
            checkpoint_id: Checkpoint to fork from
            new_state: Optional state to merge into checkpoint state
            graph: Optional LangGraph graph for state adapter (v1.8)
                   Required if using LangGraphStateAdapter for fork.
                   Can be created via graph_factory().

        Returns:
            NEW Execution in PAUSED state

        Raises:
            ValueError: If execution or checkpoint not found
            RuntimeError: If fork fails
        """
        forked: Optional["Execution"] = None

        uow = self._uow_factory()
        try:
            uow.__enter__()
            with self._controller_for_execution(uow, execution_id, graph=graph) as controller:
                controller.set_deferred_commit(True)

                forked = controller.fork(execution_id, checkpoint_id, new_state)

                # Emit audit event
                outbox_event = OutboxEvent.create(
                    event_type=OutboxEventType.EXECUTION_FORKED,
                    aggregate_id=forked.id,
                    aggregate_type="Execution",
                    payload={
                        "source_execution_id": execution_id,
                        "fork_execution_id": forked.id,
                        "source_checkpoint_id": checkpoint_id,
                        "new_state_keys": list(new_state.keys()) if new_state else [],
                    },
                )
                uow.outbox.add(outbox_event)
                uow.commit()

        except Exception as e:
            uow.rollback()
            logger.error(f"Fork failed for {execution_id}: {e}")
            raise
        finally:
            uow.__exit__(None, None, None)

        logger.info(
            f"Forked execution {forked.id} from {execution_id} "
            f"at checkpoint {checkpoint_id[:8]}..."
        )

        return forked

    def rollback_and_run(
        self,
        execution_id: str,
        checkpoint_id: str,
        graph: Any,
    ) -> "Execution":
        """
        Rollback and continue execution (atomic).

        Both operations in same UoW transaction for atomicity.
        Graph is required for execution to continue.

        Args:
            execution_id: Execution to rollback
            checkpoint_id: Target checkpoint
            graph: Compiled LangGraph for execution (required)

        Returns:
            Execution with updated state after continued run

        Raises:
            ValueError: If graph is None or execution/checkpoint not found
        """
        if graph is None:
            raise ValueError("Graph is required for rollback_and_run operation")

        file_commit_id: Optional[str] = None
        execution: Optional["Execution"] = None

        uow = self._uow_factory()
        try:
            uow.__enter__()
            with self._controller_for_execution(uow, execution_id, graph=graph) as controller:
                controller.set_deferred_commit(True)

                # Rollback state
                execution = controller.rollback(execution_id, checkpoint_id)
                file_commit_id = self._get_file_commit_for_checkpoint(checkpoint_id)

                # Continue execution with graph
                execution = controller.run(execution_id, graph=graph)

                # Single audit event for compound operation
                outbox_event = OutboxEvent.create(
                    event_type=OutboxEventType.ROLLBACK_PERFORMED,
                    aggregate_id=execution_id,
                    aggregate_type="Execution",
                    payload={
                        "execution_id": execution_id,
                        "checkpoint_id": checkpoint_id,
                        "continued": True,
                        "final_status": execution.status.value,
                        "file_commit_id": file_commit_id,
                    },
                )
                uow.outbox.add(outbox_event)
                uow.commit()

        except Exception as e:
            uow.rollback()
            logger.error(f"Rollback and run failed for {execution_id}: {e}")
            raise
        finally:
            uow.__exit__(None, None, None)

        # Post-commit file restore
        self._restore_files_post_commit(
            file_commit_id=file_commit_id,
            execution_id=execution_id,
            checkpoint_id=checkpoint_id,
            operation="rollback_and_run",
        )

        return execution

    def fork_and_run(
        self,
        execution_id: str,
        checkpoint_id: str,
        graph: Any,
        new_state: Optional[Dict[str, Any]] = None,
    ) -> "Execution":
        """
        Fork and run new execution (atomic).

        Creates a fork then immediately runs it with the provided graph.
        Both operations in same UoW transaction for atomicity.

        Args:
            execution_id: Source execution to fork from
            checkpoint_id: Checkpoint to fork from
            graph: Compiled LangGraph for execution (required)
            new_state: Optional state to merge before running

        Returns:
            NEW Execution after run (may be COMPLETED or PAUSED)

        Raises:
            ValueError: If graph is None or execution/checkpoint not found
        """
        if graph is None:
            raise ValueError("Graph is required for fork_and_run operation")

        forked: Optional["Execution"] = None
        result: Optional["Execution"] = None

        uow = self._uow_factory()
        try:
            uow.__enter__()
            with self._controller_for_execution(uow, execution_id, graph=graph) as controller:
                controller.set_deferred_commit(True)

                # Fork from checkpoint
                forked = controller.fork(execution_id, checkpoint_id, new_state)

                # Run the forked execution
                result = controller.run(forked.id, graph=graph)

                # Audit event
                outbox_event = OutboxEvent.create(
                    event_type=OutboxEventType.EXECUTION_FORKED,
                    aggregate_id=forked.id,
                    aggregate_type="Execution",
                    payload={
                        "source_execution_id": execution_id,
                        "fork_execution_id": forked.id,
                        "source_checkpoint_id": checkpoint_id,
                        "ran_immediately": True,
                        "final_status": result.status.value,
                    },
                )
                uow.outbox.add(outbox_event)
                uow.commit()

        except Exception as e:
            uow.rollback()
            logger.error(f"Fork and run failed for {execution_id}: {e}")
            raise
        finally:
            uow.__exit__(None, None, None)

        logger.info(
            f"Forked and ran execution {forked.id} from {execution_id}, "
            f"final status: {result.status.value}"
        )

        return result

    # ═══════════════════════════════════════════════════════════════════════════
    # Batch Operations
    # ═══════════════════════════════════════════════════════════════════════════

    def batch_operate(
        self,
        requests: List[BatchOperationRequest],
        stop_on_error: bool = False,
        graph: Optional[Any] = None,
    ) -> List[BatchOperationResult]:
        """
        Execute batch operations.

        Each request is processed in its own transaction for ACID isolation.
        StateAdapter is reused across all operations for efficiency.

        Args:
            requests: List of operation requests
            stop_on_error: If True, stop on first error
            graph: Optional LangGraph graph for state adapter (v1.8)
                   Required if using LangGraphStateAdapter.

        Returns:
            List of results (same order as requests)
        """
        results: List[BatchOperationResult] = []

        for req in requests:
            try:
                if req.operation == OperationType.ROLLBACK:
                    execution = self.rollback(
                        req.execution_id, req.checkpoint_id, graph=graph
                    )
                    results.append(BatchOperationResult(
                        execution_id=req.execution_id,
                        checkpoint_id=req.checkpoint_id,
                        operation=req.operation,
                        success=True,
                        result_execution=execution,
                    ))

                elif req.operation == OperationType.FORK:
                    execution = self.fork(
                        req.execution_id, req.checkpoint_id, req.new_state, graph=graph
                    )
                    results.append(BatchOperationResult(
                        execution_id=req.execution_id,
                        checkpoint_id=req.checkpoint_id,
                        operation=req.operation,
                        success=True,
                        result_execution=execution,
                        new_execution_id=execution.id,
                    ))

                elif req.operation == OperationType.ROLLBACK_AND_RUN:
                    if not req.graph:
                        raise ValueError("Graph required for ROLLBACK_AND_RUN")
                    execution = self.rollback_and_run(
                        req.execution_id, req.checkpoint_id, req.graph
                    )
                    results.append(BatchOperationResult(
                        execution_id=req.execution_id,
                        checkpoint_id=req.checkpoint_id,
                        operation=req.operation,
                        success=True,
                        result_execution=execution,
                    ))

                elif req.operation == OperationType.FORK_AND_RUN:
                    if not req.graph:
                        raise ValueError("Graph required for FORK_AND_RUN")
                    execution = self.fork_and_run(
                        req.execution_id, req.checkpoint_id, req.graph, req.new_state
                    )
                    results.append(BatchOperationResult(
                        execution_id=req.execution_id,
                        checkpoint_id=req.checkpoint_id,
                        operation=req.operation,
                        success=True,
                        result_execution=execution,
                        new_execution_id=execution.id,
                    ))

            except Exception as e:
                logger.error(f"Batch operation failed for {req.execution_id}: {e}")
                results.append(BatchOperationResult(
                    execution_id=req.execution_id,
                    checkpoint_id=req.checkpoint_id,
                    operation=req.operation,
                    success=False,
                    error=str(e),
                ))
                if stop_on_error:
                    break

        return results

    # ═══════════════════════════════════════════════════════════════════════════
    # Convenience Methods
    # ═══════════════════════════════════════════════════════════════════════════

    def batch_rollback(
        self,
        items: List[tuple],  # [(exec_id, checkpoint_id), ...]
    ) -> List[BatchOperationResult]:
        """
        Convenience: batch rollback multiple executions.

        Args:
            items: List of (execution_id, checkpoint_id) tuples

        Returns:
            List of BatchOperationResult
        """
        requests = [
            BatchOperationRequest(
                execution_id=exec_id,
                checkpoint_id=cp_id,
                operation=OperationType.ROLLBACK,
            )
            for exec_id, cp_id in items
        ]
        return self.batch_operate(requests)

    def batch_fork(
        self,
        items: List[tuple],  # [(exec_id, checkpoint_id, new_state?), ...]
    ) -> List[BatchOperationResult]:
        """
        Convenience: batch fork multiple executions.

        Args:
            items: List of (execution_id, checkpoint_id) or
                   (execution_id, checkpoint_id, new_state) tuples

        Returns:
            List of BatchOperationResult
        """
        requests = []
        for item in items:
            exec_id, cp_id = item[0], item[1]
            new_state = item[2] if len(item) > 2 else None
            requests.append(BatchOperationRequest(
                execution_id=exec_id,
                checkpoint_id=cp_id,
                operation=OperationType.FORK,
                new_state=new_state,
            ))
        return self.batch_operate(requests)

    # ═══════════════════════════════════════════════════════════════════════════
    # Resource Management
    # ═══════════════════════════════════════════════════════════════════════════

    def close(self) -> None:
        """Release resources held by the coordinator (e.g., state adapter connections)."""
        if self._state_adapter:
            try:
                self._state_adapter.close()
            except Exception:
                pass

    def __enter__(self) -> "BatchExecutionCoordinator":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # ═══════════════════════════════════════════════════════════════════════════
    # Private Helpers
    # ═══════════════════════════════════════════════════════════════════════════

    def _get_execution_from_uow(
        self,
        uow: "IUnitOfWork",
        execution_id: str,
    ) -> Optional["Execution"]:
        """Return the execution from the current UoW if the repo is available."""
        executions_repo = getattr(uow, "executions", None)
        get_fn = getattr(executions_repo, "get", None)
        if callable(get_fn):
            return get_fn(execution_id)
        return None

    @contextmanager
    def _execution_env_context(self, execution: Optional["Execution"]):
        """Temporarily project execution storage metadata into process env."""
        metadata = getattr(execution, "metadata", {}) or {}
        if not metadata or not isinstance(metadata, Mapping):
            yield
            return
        if not any(
            metadata.get(key)
            for key in (
                "checkpoint_db_path",
                "llm_cache_path",
                "actor_id",
                "cache_storage_scope",
            )
        ) and not any(
            os.getenv(env_name)
            for env_name in (
                "WTB_CHECKPOINT_DB_PATH",
                "WTB_LLM_CACHE_PATH",
                "WTB_CACHE_ACTOR_ID",
                "WTB_CACHE_STORAGE_SCOPE",
            )
        ):
            yield
            return

        paths = resolve_execution_storage_paths(
            metadata,
            fallback_actor_id=metadata.get("actor_id"),
        )
        env_updates = paths.to_env_vars()
        env_updates["WTB_CACHE_ACTOR_ID"] = paths.actor_id

        previous_values = {key: os.environ.get(key) for key in env_updates}
        try:
            for key, value in env_updates.items():
                os.environ[key] = value
            yield
        finally:
            for key, previous in previous_values.items():
                if previous is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous

    def _build_state_adapter_for_execution(
        self,
        execution: Optional["Execution"],
    ) -> "IStateAdapter":
        """Create an execution-specific LangGraph adapter when metadata exists."""
        if execution is None:
            return self._state_adapter

        metadata = getattr(execution, "metadata", {}) or {}
        if not isinstance(metadata, Mapping):
            return self._state_adapter
        if not any(
            metadata.get(key)
            for key in (
                "checkpoint_db_path",
                "llm_cache_path",
                "actor_id",
                "cache_storage_scope",
            )
        ) and not any(
            os.getenv(env_name)
            for env_name in (
                "WTB_CHECKPOINT_DB_PATH",
                "WTB_LLM_CACHE_PATH",
                "WTB_CACHE_ACTOR_ID",
                "WTB_CACHE_STORAGE_SCOPE",
            )
        ):
            return self._state_adapter
        paths = resolve_execution_storage_paths(
            metadata,
            fallback_actor_id=metadata.get("actor_id"),
        )
        checkpoint_db_path = str(paths.checkpoint_db_path)

        try:
            from wtb.infrastructure.adapters.langgraph_state_adapter import (
                LANGGRAPH_AVAILABLE,
                LangGraphConfig,
                LangGraphStateAdapter,
            )
        except Exception:
            return self._state_adapter

        if not LANGGRAPH_AVAILABLE:
            return self._state_adapter

        existing_connection = getattr(
            getattr(self._state_adapter, "_config", None),
            "connection_string",
            None,
        )
        if existing_connection and str(existing_connection) == checkpoint_db_path:
            return self._state_adapter

        try:
            return LangGraphStateAdapter(LangGraphConfig.for_development(checkpoint_db_path))
        except Exception as e:
            logger.debug(
                f"Could not create execution-specific state adapter for {execution.id}: {e}"
            )
            return self._state_adapter

    @contextmanager
    def _controller_for_execution(
        self,
        uow: "IUnitOfWork",
        execution_id: str,
        graph: Optional[Any] = None,
    ):
        """Yield a controller wired to execution-specific storage when available."""
        execution = self._get_execution_from_uow(uow, execution_id)
        state_adapter = self._state_adapter
        try:
            with self._execution_env_context(execution):
                state_adapter = self._build_state_adapter_for_execution(execution)
                if graph and hasattr(state_adapter, "set_workflow_graph"):
                    state_adapter.set_workflow_graph(graph, force_recompile=True)
                    logger.debug(
                        "Set graph on state adapter for execution-specific rollback/fork"
                    )

                controller = self._controller_factory.create(
                    uow=uow,
                    state_adapter=state_adapter,
                    file_tracking_service=self._file_tracking,
                )
                yield controller
        finally:
            if state_adapter is not self._state_adapter:
                close_fn = getattr(state_adapter, "close", None)
                if callable(close_fn):
                    try:
                        close_fn()
                    except Exception:
                        pass

    def _check_venv_compat_after_rollback(
        self,
        execution: "Execution",
        checkpoint_id: str,
    ) -> None:
        """Emit a warning event if the venv spec has drifted since the checkpoint."""
        if not self._environment_provider:
            return
        check_fn = getattr(self._environment_provider, "check_venv_compatibility", None)
        if not callable(check_fn):
            return

        workflow_vars = getattr(
            getattr(execution, "state", None), "workflow_variables", {}
        ) or {}
        expected_hash = workflow_vars.get("_venv_spec_hash")
        workspace_id = workflow_vars.get("_workspace_id", execution.id if execution else "")
        if not expected_hash:
            return

        compatible = check_fn(workspace_id, expected_hash)
        if not compatible:
            logger.warning(
                f"Venv spec drifted after rollback to checkpoint {checkpoint_id}. "
                f"Expected spec hash {expected_hash}."
            )
            try:
                from wtb.domain.events.workspace_events import VenvMismatchWarningEvent
                if hasattr(self, "_config") and self._config:
                    event_bus = getattr(self._config, "event_bus", None)
                    if event_bus:
                        event_bus.publish(VenvMismatchWarningEvent(
                            node_id=workspace_id,
                            expected_spec_hash=expected_hash,
                            actual_spec_hash="",
                            checkpoint_id=checkpoint_id,
                        ))
            except Exception as e:
                logger.debug(f"Could not emit VenvMismatchWarningEvent: {e}")

    def _extract_file_commit_id(self, execution: "Execution") -> Optional[str]:
        """Extract file_commit_id from execution state if available."""
        if not execution or not execution.state:
            return None

        # Try multiple possible locations
        workflow_vars = getattr(execution.state, 'workflow_variables', {})

        # Location 1: _file_tracking_result.commit_id
        ft_result = workflow_vars.get("_file_tracking_result", {})
        if isinstance(ft_result, dict) and ft_result.get("commit_id"):
            return ft_result["commit_id"]

        # Location 2: Direct file_commit_id
        if workflow_vars.get("file_commit_id"):
            return workflow_vars["file_commit_id"]

        return None

    def _get_file_commit_for_checkpoint(self, checkpoint_id: str) -> Optional[str]:
        """Resolve checkpoint_id -> file_commit_id through file tracking."""
        if not checkpoint_id or not self._file_tracking:
            return None
        get_commit = getattr(self._file_tracking, "get_commit_for_checkpoint", None)
        if not callable(get_commit):
            return None
        return get_commit(checkpoint_id)

    def _restore_files_post_commit(
        self,
        file_commit_id: Optional[str],
        execution_id: str,
        checkpoint_id: str,
        operation: str,
    ) -> None:
        """
        Restore files post-commit (best-effort).

        If file restore fails, it's logged but not raised.
        Outbox processor will handle retry.
        """
        if not file_commit_id or not self._file_tracking:
            return

        if not self._file_tracking.is_available():
            logger.debug(f"File tracking not available for {operation}")
            return

        try:
            result = self._file_tracking.restore_commit(file_commit_id)
            logger.info(
                f"Restored {result.files_restored} files for "
                f"{operation} {execution_id} -> {checkpoint_id[:8]}..."
            )
        except Exception as e:
            # Log but don't fail - outbox processor will retry
            logger.warning(
                f"File restore failed for {operation} (will retry via outbox): {e}"
            )
