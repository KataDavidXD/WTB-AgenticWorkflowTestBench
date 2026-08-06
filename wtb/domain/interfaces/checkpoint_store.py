"""
Checkpoint Store Interface.

Infrastructure interface for checkpoint persistence.
NO BUSINESS LOGIC HERE - only save/load/list operations.

All checkpoint reasoning is in the domain (ExecutionHistory).

Design Philosophy:
==================
This follows the DDD principle of separating domain logic from infrastructure:
- Domain: ExecutionHistory contains business logic (get_rollback_target, etc.)
- Infrastructure: ICheckpointStore handles only persistence mechanics

Implementations:
- InMemoryCheckpointStore: For unit testing
- LangGraphCheckpointStore: Maps domain ↔ LangGraph StateSnapshot
"""

from abc import ABC, abstractmethod

from ..models.checkpoint import (
    Checkpoint,
    CheckpointId,
    ExecutionHistory,
)


class ICheckpointStore(ABC):
    """
    Infrastructure interface for checkpoint persistence.
    
    NO BUSINESS LOGIC HERE - only save/load/list operations.
    All checkpoint reasoning is in the domain (ExecutionHistory).
    
    Implementations should:
    1. Map between domain Checkpoint and storage format
    2. Handle connection management
    3. Provide efficient queries
    
    Implementations must NOT:
    1. Compute node boundaries
    2. Determine rollback targets
    3. Validate business rules
    """
    
    @abstractmethod
    def save(self, checkpoint: Checkpoint) -> None:
        """
        Persist a checkpoint.
        
        Note: In LangGraph, checkpoints are saved automatically during invoke().
        This method is for explicit saves via update_state().
        
        Args:
            checkpoint: Domain checkpoint to persist
        """
        pass
    
    @abstractmethod
    def load(self, checkpoint_id: CheckpointId) -> Checkpoint | None:
        """
        Load a checkpoint by ID.
        
        Args:
            checkpoint_id: The checkpoint identifier
            
        Returns:
            Domain Checkpoint or None if not found
        """
        pass
    
    @abstractmethod
    def load_by_execution(self, execution_id: str) -> list[Checkpoint]:
        """
        Load all checkpoints for an execution.
        
        Args:
            execution_id: The execution identifier
            
        Returns:
            List of checkpoints (may be empty)
        """
        pass
    
    @abstractmethod
    def load_history(self, execution_id: str) -> ExecutionHistory:
        """
        Load all checkpoints for an execution as ExecutionHistory aggregate.
        
        This is the preferred method for loading checkpoints as it returns
        the domain aggregate with all business logic available.
        
        Args:
            execution_id: The execution identifier
            
        Returns:
            ExecutionHistory aggregate (may be empty)
        """
        pass
    
    @abstractmethod
    def load_latest(self, execution_id: str) -> Checkpoint | None:
        """
        Load the most recent checkpoint for an execution.
        
        Args:
            execution_id: The execution identifier
            
        Returns:
            Latest checkpoint or None if no checkpoints exist
        """
        pass
    
    @abstractmethod
    def delete(self, checkpoint_id: CheckpointId) -> bool:
        """
        Delete a checkpoint.
        
        Note: LangGraph doesn't support checkpoint deletion.
        Implementations may return False if deletion is not supported.
        
        Args:
            checkpoint_id: The checkpoint to delete
            
        Returns:
            True if deleted, False if not found or not supported
        """
        pass
    
    @abstractmethod
    def delete_by_execution(self, execution_id: str) -> int:
        """
        Delete all checkpoints for an execution.
        
        Args:
            execution_id: The execution identifier
            
        Returns:
            Number of checkpoints deleted
        """
        pass
    
    @abstractmethod
    def exists(self, checkpoint_id: CheckpointId) -> bool:
        """
        Check if a checkpoint exists.
        
        Args:
            checkpoint_id: The checkpoint identifier
            
        Returns:
            True if checkpoint exists
        """
        pass
    
    @abstractmethod
    def count(self, execution_id: str) -> int:
        """
        Count checkpoints for an execution.
        
        Args:
            execution_id: The execution identifier
            
        Returns:
            Number of checkpoints
        """
        pass


class ICheckpointStoreFactory(ABC):
    """
    Factory interface for creating checkpoint stores.
    
    Implementations handle the configuration and initialization
    of specific checkpoint store backends.
    """
    
    @abstractmethod
    def create(self) -> ICheckpointStore:
        """
        Create a checkpoint store instance.
        
        Returns:
            Configured ICheckpointStore implementation
        """
        pass
    
    @abstractmethod
    def create_for_testing(self) -> ICheckpointStore:
        """
        Create a checkpoint store for testing (typically in-memory).
        
        Returns:
            In-memory ICheckpointStore for testing
        """
        pass
