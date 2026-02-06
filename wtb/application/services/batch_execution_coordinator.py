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
from typing import List, Optional, Dict, Any, Callable, TYPE_CHECKING

from wtb.domain.interfaces.batch_coordinator import (
    IBatchExecutionCoordinator,
    IExecutionControllerFactory,
    OperationType,
    BatchOperationRequest,
    BatchOperationResult,
)
from wtb.domain.models.outbox import OutboxEvent, OutboxEventType

if TYPE_CHECKING:
    from wtb.domain.interfaces.unit_of_work import IUnitOfWork
    from wtb.domain.interfaces.state_adapter import IStateAdapter
    from wtb.domain.interfaces.file_tracking import IFileTrackingService
    from wtb.domain.models.workflow import Execution

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
        
        return ExecutionController(
            execution_repository=uow.executions,
            workflow_repository=uow.workflows,
            state_adapter=state_adapter,
            node_executor=DefaultNodeExecutor(),
            unit_of_work=uow,
            file_tracking_service=file_tracking_service,
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
        
        Design Decision:
            StateAdapter is REUSED across operations because:
            1. It's expensive to create (may involve DB connections)
            2. State operations are idempotent
            3. Different from UoW which manages transaction boundaries
        """
        self._uow_factory = uow_factory
        self._controller_factory = controller_factory or DefaultExecutionControllerFactory()
        self._state_adapter = state_adapter
        self._file_tracking = file_tracking
    
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
        
        # v1.8: Set graph on state adapter if provided (for LangGraph rollback)
        if graph and hasattr(self._state_adapter, 'set_workflow_graph'):
            self._state_adapter.set_workflow_graph(graph, force_recompile=True)
            logger.debug(f"Set graph on state adapter for rollback")
        
        # Single Phase: UoW Transaction (State + File Restore Outbox = ACID)
        # Both rollback state AND file restore intent are in same transaction
        uow = self._uow_factory()
        try:
            uow.__enter__()
            
            controller = self._controller_factory.create(
                uow=uow,
                state_adapter=self._state_adapter,
                file_tracking_service=self._file_tracking,
            )
            
            execution = controller.rollback(execution_id, checkpoint_id)
            
            # Extract file_commit_id from execution state if available
            file_commit_id = self._extract_file_commit_id(execution)
            
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
                file_restore_event = OutboxEvent.create(
                    event_type=OutboxEventType.ROLLBACK_FILE_RESTORE,
                    aggregate_id=execution_id,
                    aggregate_type="Execution",
                    payload={
                        "source_checkpoint_id": checkpoint_id,
                        "target_checkpoint_id": checkpoint_id,
                        "source_commit_id": file_commit_id,
                        "execution_id": execution_id,
                    },
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
            NEW Execution in PENDING state
            
        Raises:
            ValueError: If execution or checkpoint not found
            RuntimeError: If fork fails
        """
        # v1.8: Set graph on state adapter if provided (for LangGraph fork)
        if graph and hasattr(self._state_adapter, 'set_workflow_graph'):
            self._state_adapter.set_workflow_graph(graph, force_recompile=True)
            logger.debug(f"Set graph on state adapter for fork")
        
        forked: Optional["Execution"] = None
        
        uow = self._uow_factory()
        try:
            uow.__enter__()
            
            controller = self._controller_factory.create(
                uow=uow,
                state_adapter=self._state_adapter,
                file_tracking_service=self._file_tracking,
            )
            
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
            
            controller = self._controller_factory.create(
                uow=uow,
                state_adapter=self._state_adapter,
                file_tracking_service=self._file_tracking,
            )
            
            # Rollback state
            execution = controller.rollback(execution_id, checkpoint_id)
            file_commit_id = self._extract_file_commit_id(execution)
            
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
            
            controller = self._controller_factory.create(
                uow=uow,
                state_adapter=self._state_adapter,
                file_tracking_service=self._file_tracking,
            )
            
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
    # Private Helpers
    # ═══════════════════════════════════════════════════════════════════════════
    
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
