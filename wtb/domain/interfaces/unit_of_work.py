"""
Unit of Work Interface.

Manages transaction boundaries across multiple repositories.
Use when operations span multiple aggregates and require atomicity.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .repositories import (
        IWorkflowRepository,
        IExecutionRepository,
        INodeVariantRepository,
        IBatchTestRepository,
        IEvaluationResultRepository,
        INodeBoundaryRepository,
        ICheckpointFileRepository,
        IOutboxRepository,
    )


class IUnitOfWork(ABC):
    """
    Unit of Work pattern interface.
    
    Manages transaction boundaries and provides access to repositories.
    Use when operations span multiple aggregates.
    
    Usage:
        with uow:
            workflow = uow.workflows.get(workflow_id)
            execution = Execution(workflow_id=workflow.id)
            uow.executions.add(execution)
            uow.commit()  # Both saved atomically
    
    Design Decisions:
    - Provides repository access through properties
    - Context manager handles transaction lifecycle
    - Automatic rollback on exception
    - Explicit commit required
    """
    
    # WTB Core Repositories
    workflows: "IWorkflowRepository"
    executions: "IExecutionRepository"
    variants: "INodeVariantRepository"
    batch_tests: "IBatchTestRepository"
    evaluation_results: "IEvaluationResultRepository"
    
    # WTB-Specific Repositories (Anti-Corruption Layer)
    node_boundaries: "INodeBoundaryRepository"
    checkpoint_files: "ICheckpointFileRepository"
    
    # Outbox Pattern (for cross-database consistency)
    outbox: "IOutboxRepository"
    
    @abstractmethod
    def __enter__(self) -> "IUnitOfWork":
        """
        Begin transaction.
        
        Returns:
            Self for context manager usage
        """
        pass
    
    @abstractmethod
    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        End transaction.
        
        Automatically rolls back on exception.
        
        Args:
            exc_type: Exception type if raised
            exc_val: Exception value if raised
            exc_tb: Exception traceback if raised
        """
        pass
    
    @abstractmethod
    def commit(self):
        """
        Commit the transaction.
        
        Persists all changes made through repositories.
        
        Raises:
            Exception: If commit fails (implementation-specific)
        """
        pass
    
    @abstractmethod
    def rollback(self):
        """
        Rollback the transaction.
        
        Discards all changes made through repositories.
        """
        pass

