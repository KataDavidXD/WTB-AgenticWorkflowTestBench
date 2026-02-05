"""
Batch Execution Coordinator Interfaces.

v1.8 (2026-02-05): New interface for coordinating rollback/fork operations
across batch test results.

Design Principles:
==================
- SRP: Coordinator only orchestrates; delegates to ExecutionController
- OCP: New operation types via OperationType enum
- DIP: All dependencies via interfaces (IUnitOfWork, etc.)
- ACID: Each operation in single UoW transaction

Rollback vs Fork Semantics:
===========================
- Rollback: Destructive - goes back in time, overwrites future checkpoints
- Fork: Non-destructive - creates new execution branch, original unchanged

Graph Requirement Matrix:
========================
| Operation           | Graph Required | Reason                               |
|---------------------|----------------|--------------------------------------|
| rollback()          | No             | Only restores state, PAUSED status   |
| fork()              | No             | Only creates new execution, PENDING  |
| rollback_and_run()  | Yes            | Needs graph to continue execution    |
| fork_and_run()      | Yes            | Needs graph to run new execution     |

Transaction Architecture:
========================
Phase 1: UoW Transaction (State + Metadata)
  - State changes via ExecutionController
  - Outbox event for audit trail
  - Single atomic commit

Phase 2: Post-Commit File Restore (Best-Effort)
  - File restoration from FileTracker
  - If fails: logged, retryable via outbox processor

Usage:
    coordinator = BatchExecutionCoordinator(
        uow_factory=uow_factory,
        controller_factory=controller_factory,
        state_adapter=state_adapter,
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

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Callable, TYPE_CHECKING
from enum import Enum

if TYPE_CHECKING:
    from wtb.domain.models.workflow import Execution
    from wtb.domain.interfaces.unit_of_work import IUnitOfWork
    from wtb.domain.interfaces.state_adapter import IStateAdapter
    from wtb.domain.interfaces.file_tracking import IFileTrackingService
    from wtb.domain.interfaces.execution_controller import IExecutionController


class OperationType(Enum):
    """
    Operation type for batch coordination.
    
    Follows OCP - new operation types can be added without modifying
    existing coordinator code.
    """
    ROLLBACK = "rollback"               # Destructive rollback to checkpoint
    FORK = "fork"                       # Non-destructive fork from checkpoint
    ROLLBACK_AND_RUN = "rollback_run"   # Rollback + continue execution
    FORK_AND_RUN = "fork_run"           # Fork + run new execution


@dataclass
class BatchOperationRequest:
    """
    Request for a batch operation.
    
    Value object containing all information needed to perform
    a rollback or fork operation.
    
    Attributes:
        execution_id: Target execution ID
        checkpoint_id: Target checkpoint ID (str for LangGraph compatibility)
        operation: Type of operation to perform
        new_state: For fork: optional state to merge into checkpoint state
        graph: Required for *_AND_RUN operations (compiled LangGraph)
    """
    execution_id: str
    checkpoint_id: str
    operation: OperationType = OperationType.ROLLBACK
    new_state: Optional[Dict[str, Any]] = None
    graph: Optional[Any] = None  # CompiledStateGraph for *_AND_RUN operations


@dataclass
class BatchOperationResult:
    """
    Result of a batch operation.
    
    Value object containing operation outcome and relevant IDs.
    
    Attributes:
        execution_id: Original execution ID
        checkpoint_id: Target checkpoint ID
        operation: Operation that was performed
        success: Whether operation succeeded
        result_execution: Updated/new Execution object
        new_execution_id: For fork: ID of the new forked execution
        files_restored: Number of files restored (for rollback)
        error: Error message if operation failed
    """
    execution_id: str
    checkpoint_id: str
    operation: OperationType
    success: bool
    result_execution: Optional["Execution"] = None
    new_execution_id: Optional[str] = None  # For fork: new execution ID
    files_restored: int = 0
    error: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "execution_id": self.execution_id,
            "checkpoint_id": self.checkpoint_id,
            "operation": self.operation.value,
            "success": self.success,
            "new_execution_id": self.new_execution_id,
            "files_restored": self.files_restored,
            "error": self.error,
        }


class IExecutionControllerFactory(ABC):
    """
    Factory for creating ExecutionController instances.
    
    Enables ACID-compliant isolated execution where each operation
    gets its own controller with fresh UoW.
    
    SOLID Compliance:
    - SRP: Only creates controllers
    - OCP: New controller types via subclasses
    - DIP: Returns IExecutionController abstraction
    """
    
    @abstractmethod
    def create(
        self,
        uow: "IUnitOfWork",
        state_adapter: "IStateAdapter",
        file_tracking_service: Optional["IFileTrackingService"] = None,
    ) -> "IExecutionController":
        """
        Create ExecutionController with injected dependencies.
        
        Args:
            uow: Unit of work for transaction management
            state_adapter: State adapter for checkpoint operations
            file_tracking_service: Optional file tracking service
            
        Returns:
            Configured IExecutionController instance
        """
        pass


class IBatchExecutionCoordinator(ABC):
    """
    Interface for batch execution coordination.
    
    Coordinates rollback/fork operations across batch test results,
    maintaining transaction consistency via UoW pattern.
    
    Responsibilities:
    - Coordinate rollback/fork operations
    - Manage transaction boundaries
    - Handle file restore with outbox pattern
    - Emit audit events for traceability
    
    Design Decisions:
    1. Each operation creates its own UoW for ACID isolation
    2. StateAdapter is reused across operations for efficiency
    3. File restore happens post-commit (best-effort, retryable)
    4. Outbox events ensure eventual consistency for audit trail
    
    Usage:
        # Create coordinator
        coordinator = BatchExecutionCoordinator(...)
        
        # After batch test completes, rollback specific variant
        execution = coordinator.rollback(
            execution_id=result.execution_id,
            checkpoint_id=result.last_checkpoint_id,
        )
        
        # Fork for A/B comparison
        forked = coordinator.fork(
            execution_id=result.execution_id,
            checkpoint_id=result.last_checkpoint_id,
            new_state={"temperature": 0.7},
        )
    """
    
    @abstractmethod
    def rollback(
        self,
        execution_id: str,
        checkpoint_id: str,
    ) -> "Execution":
        """
        Rollback execution to checkpoint (destructive).
        
        Restores execution state to the specified checkpoint,
        overwriting any subsequent state changes.
        
        Transaction Flow:
        1. [UoW] controller.rollback() - restore state
        2. [UoW] outbox.add(ROLLBACK_PERFORMED) - queue audit event
        3. [UoW] commit() - ACID durability
        4. [Post] file_tracking.restore_commit() - best-effort file restore
        
        Args:
            execution_id: Execution to rollback
            checkpoint_id: Target checkpoint to restore to
            
        Returns:
            Execution in PAUSED state with restored checkpoint state
            
        Raises:
            ValueError: If execution or checkpoint not found
            RuntimeError: If rollback operation fails
        """
        pass
    
    @abstractmethod
    def fork(
        self,
        execution_id: str,
        checkpoint_id: str,
        new_state: Optional[Dict[str, Any]] = None,
    ) -> "Execution":
        """
        Fork execution from checkpoint (non-destructive).
        
        Creates a NEW execution starting from the specified checkpoint.
        Original execution remains unchanged.
        
        Use Cases:
        - A/B testing with modified parameters
        - Exploring alternative paths from a checkpoint
        - Creating parallel branches for comparison
        
        Args:
            execution_id: Source execution to fork from
            checkpoint_id: Checkpoint to fork from
            new_state: Optional state to merge into checkpoint state
            
        Returns:
            NEW Execution in PENDING state
            
        Raises:
            ValueError: If execution or checkpoint not found
            RuntimeError: If fork operation fails
        """
        pass
    
    @abstractmethod
    def rollback_and_run(
        self,
        execution_id: str,
        checkpoint_id: str,
        graph: Any,
    ) -> "Execution":
        """
        Rollback and continue execution (atomic).
        
        Performs rollback then continues execution from the checkpoint.
        Both operations in same UoW transaction for atomicity.
        
        Args:
            execution_id: Execution to rollback
            checkpoint_id: Target checkpoint
            graph: Compiled LangGraph for execution (required)
            
        Returns:
            Execution with updated state after continued run
            
        Raises:
            ValueError: If graph is None or execution/checkpoint not found
            RuntimeError: If operation fails
        """
        pass
    
    @abstractmethod
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
            RuntimeError: If operation fails
        """
        pass
    
    @abstractmethod
    def batch_operate(
        self,
        requests: List[BatchOperationRequest],
        stop_on_error: bool = False,
    ) -> List[BatchOperationResult]:
        """
        Execute batch operations.
        
        Processes multiple rollback/fork operations sequentially.
        Each request is processed in its own transaction for ACID isolation.
        StateAdapter is reused across all operations for efficiency.
        
        Args:
            requests: List of operation requests to process
            stop_on_error: If True, stop on first error; 
                          if False (default), continue processing
            
        Returns:
            List of BatchOperationResult (same order as requests)
            
        Note:
            Results are returned for all processed requests.
            If stop_on_error=True and error occurs, remaining requests
            are not processed and not included in results.
        """
        pass
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Convenience Methods (Optional, may have default implementations)
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
