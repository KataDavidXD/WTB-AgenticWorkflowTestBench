"""
In-Memory Checkpoint Store Implementation.

A simple in-memory implementation of ICheckpointStore for testing and development.
Does not persist data between runs.

Design Philosophy:
- NO BUSINESS LOGIC - only persistence operations
- Uses domain Checkpoint model directly
- Thread-safe with threading.Lock
"""

from copy import deepcopy
from threading import Lock

from wtb.domain.interfaces.checkpoint_store import (
    ICheckpointStore,
    ICheckpointStoreFactory,
)
from wtb.domain.models.checkpoint import (
    Checkpoint,
    CheckpointId,
    ExecutionHistory,
)


class InMemoryCheckpointStore(ICheckpointStore):
    """
    In-memory implementation of ICheckpointStore.
    
    Useful for:
    - Unit testing (fast, isolated)
    - Development without database setup
    - Prototyping
    
    Limitations:
    - No persistence between runs
    - No cross-process sharing
    - Memory grows with checkpoints
    
    Thread-safe via Lock.
    """
    
    def __init__(self):
        """Initialize in-memory storage."""
        self._lock = Lock()
        # Storage: execution_id -> list of checkpoints
        self._checkpoints: dict[str, list[Checkpoint]] = {}
        # Index: checkpoint_id -> (execution_id, index)
        self._index: dict[str, tuple[str, int]] = {}
    
    def save(self, checkpoint: Checkpoint) -> None:
        """
        Persist a checkpoint.
        
        Creates a deep copy to avoid reference issues.
        """
        with self._lock:
            execution_id = checkpoint.execution_id
            
            if execution_id not in self._checkpoints:
                self._checkpoints[execution_id] = []
            
            # Deep copy to avoid external mutations
            cp_copy = deepcopy(checkpoint)
            
            # Check if already exists (update case)
            cp_id_str = str(checkpoint.id)
            if cp_id_str in self._index:
                exec_id, idx = self._index[cp_id_str]
                self._checkpoints[exec_id][idx] = cp_copy
            else:
                # New checkpoint
                idx = len(self._checkpoints[execution_id])
                self._checkpoints[execution_id].append(cp_copy)
                self._index[cp_id_str] = (execution_id, idx)
    
    def load(self, checkpoint_id: CheckpointId) -> Checkpoint | None:
        """Load a checkpoint by ID."""
        with self._lock:
            cp_id_str = str(checkpoint_id)
            if cp_id_str not in self._index:
                return None
            
            exec_id, idx = self._index[cp_id_str]
            return deepcopy(self._checkpoints[exec_id][idx])
    
    def load_by_execution(self, execution_id: str) -> list[Checkpoint]:
        """Load all checkpoints for an execution."""
        with self._lock:
            checkpoints = self._checkpoints.get(execution_id, [])
            return [deepcopy(cp) for cp in checkpoints]
    
    def load_history(self, execution_id: str) -> ExecutionHistory:
        """
        Load all checkpoints for an execution as ExecutionHistory aggregate.
        
        Returns an ExecutionHistory with all domain logic available.
        """
        checkpoints = self.load_by_execution(execution_id)
        return ExecutionHistory(
            execution_id=execution_id,
            checkpoints=checkpoints,
        )
    
    def load_latest(self, execution_id: str) -> Checkpoint | None:
        """Load the most recent checkpoint for an execution."""
        with self._lock:
            checkpoints = self._checkpoints.get(execution_id, [])
            if not checkpoints:
                return None
            
            # Find checkpoint with highest step
            latest = max(checkpoints, key=lambda cp: cp.step)
            return deepcopy(latest)
    
    def delete(self, checkpoint_id: CheckpointId) -> bool:
        """Delete a checkpoint."""
        with self._lock:
            cp_id_str = str(checkpoint_id)
            if cp_id_str not in self._index:
                return False
            
            exec_id, idx = self._index[cp_id_str]
            
            # Remove from checkpoints list
            del self._checkpoints[exec_id][idx]
            
            # Remove from index
            del self._index[cp_id_str]
            
            # Update indices for remaining checkpoints in same execution
            for i, cp in enumerate(self._checkpoints[exec_id]):
                self._index[str(cp.id)] = (exec_id, i)
            
            return True
    
    def delete_by_execution(self, execution_id: str) -> int:
        """Delete all checkpoints for an execution."""
        with self._lock:
            if execution_id not in self._checkpoints:
                return 0
            
            count = len(self._checkpoints[execution_id])
            
            # Remove from index
            for cp in self._checkpoints[execution_id]:
                cp_id_str = str(cp.id)
                if cp_id_str in self._index:
                    del self._index[cp_id_str]
            
            # Remove checkpoints
            del self._checkpoints[execution_id]
            
            return count
    
    def exists(self, checkpoint_id: CheckpointId) -> bool:
        """Check if a checkpoint exists."""
        with self._lock:
            return str(checkpoint_id) in self._index
    
    def count(self, execution_id: str) -> int:
        """Count checkpoints for an execution."""
        with self._lock:
            return len(self._checkpoints.get(execution_id, []))
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Testing Utilities
    # ═══════════════════════════════════════════════════════════════════════════
    
    def reset(self) -> None:
        """Reset all state (for testing)."""
        with self._lock:
            self._checkpoints.clear()
            self._index.clear()
    
    def get_all_execution_ids(self) -> list[str]:
        """Get all execution IDs with checkpoints (for testing)."""
        with self._lock:
            return list(self._checkpoints.keys())
    
    def get_total_checkpoint_count(self) -> int:
        """Get total number of checkpoints across all executions (for testing)."""
        with self._lock:
            return sum(len(cps) for cps in self._checkpoints.values())


class InMemoryCheckpointStoreFactory(ICheckpointStoreFactory):
    """Factory for creating InMemoryCheckpointStore instances."""
    
    def __init__(self):
        """Initialize factory with shared store instance."""
        self._store: InMemoryCheckpointStore | None = None
    
    def create(self) -> ICheckpointStore:
        """
        Create a checkpoint store instance.
        
        Returns a shared instance for testing isolation control.
        """
        if self._store is None:
            self._store = InMemoryCheckpointStore()
        return self._store
    
    def create_for_testing(self) -> ICheckpointStore:
        """
        Create a fresh checkpoint store for testing.
        
        Always returns a new instance for test isolation.
        """
        return InMemoryCheckpointStore()
    
    def create_isolated(self) -> InMemoryCheckpointStore:
        """Create an isolated store instance (for parallel tests)."""
        return InMemoryCheckpointStore()
