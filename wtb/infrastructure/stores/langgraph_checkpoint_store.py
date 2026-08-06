"""
LangGraph Checkpoint Store Implementation.

Production-ready checkpoint store using LangGraph checkpointers.
Maps domain Checkpoint model to/from LangGraph StateSnapshot.

Design Philosophy:
- ONLY handles persistence - maps domain ↔ LangGraph
- NO BUSINESS LOGIC (that's in ExecutionHistory)
- Supports multiple backends: InMemorySaver, SqliteSaver, PostgresSaver

Usage:
    # For testing
    store = LangGraphCheckpointStore.create_for_testing()
    
    # For development (SQLite)
    store = LangGraphCheckpointStore.create_for_development("./data/checkpoints.db")
    
    # For production (PostgreSQL)
    store = LangGraphCheckpointStore.create_for_production("postgresql://...")
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver as InMemorySaver
from langgraph.graph.state import CompiledStateGraph

from wtb.domain.interfaces.checkpoint_store import (
    ICheckpointStore,
    ICheckpointStoreFactory,
)
from wtb.domain.models.checkpoint import (
    Checkpoint,
    CheckpointId,
    ExecutionHistory,
)

logger = logging.getLogger(__name__)


@dataclass
class LangGraphCheckpointConfig:
    """Configuration for LangGraph checkpoint store."""
    checkpointer_type: str = "memory"  # "memory", "sqlite", "postgres"
    connection_string: str | None = None
    
    @classmethod
    def for_testing(cls) -> "LangGraphCheckpointConfig":
        """InMemorySaver for unit tests."""
        return cls(checkpointer_type="memory")
    
    @classmethod
    def for_development(cls, db_path: str = "data/wtb_checkpoints.db") -> "LangGraphCheckpointConfig":
        """SqliteSaver for local development."""
        return cls(checkpointer_type="sqlite", connection_string=db_path)
    
    @classmethod
    def for_production(cls, connection_string: str) -> "LangGraphCheckpointConfig":
        """PostgresSaver for production."""
        return cls(checkpointer_type="postgres", connection_string=connection_string)


class LangGraphCheckpointStore(ICheckpointStore):
    """
    LangGraph implementation of checkpoint store.
    
    ONLY handles persistence - maps domain Checkpoint ↔ LangGraph StateSnapshot.
    NO BUSINESS LOGIC (that's in ExecutionHistory).
    
    Key LangGraph Concepts:
    - thread_id: Isolates checkpoint streams (maps to execution_id)
    - checkpoint_id: Unique identifier for each checkpoint
    - StateSnapshot: LangGraph's checkpoint representation
    
    Thread Naming Convention:
    - Thread ID format: "wtb-{execution_id}"
    - This ensures WTB executions don't conflict with other LangGraph users
    """
    
    def __init__(
        self, 
        checkpointer: BaseCheckpointSaver,
        graph: CompiledStateGraph | None = None
    ):
        """
        Initialize with checkpointer and optional compiled graph.
        
        Args:
            checkpointer: LangGraph checkpointer (InMemorySaver, SqliteSaver, etc.)
            graph: Optional compiled graph for state operations
        """
        self._checkpointer = checkpointer
        self._graph = graph
    
    @classmethod
    def create_for_testing(cls) -> "LangGraphCheckpointStore":
        """Create store with InMemorySaver for testing."""
        return cls(checkpointer=InMemorySaver())
    
    @classmethod
    def create_for_development(cls, db_path: str = "data/wtb_checkpoints.db") -> "LangGraphCheckpointStore":
        """Create store with SqliteSaver for development."""
        try:
            import sqlite3

            from langgraph.checkpoint.sqlite import SqliteSaver
            
            conn = sqlite3.connect(db_path, check_same_thread=False)
            checkpointer = SqliteSaver(conn)
            return cls(checkpointer=checkpointer)
        except ImportError:
            logger.warning("langgraph-checkpoint-sqlite not installed, falling back to memory")
            return cls.create_for_testing()
    
    @classmethod
    def create_for_production(cls, connection_string: str) -> "LangGraphCheckpointStore":
        """Create store with PostgresSaver for production."""
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
            
            checkpointer = PostgresSaver.from_conn_string(connection_string)
            checkpointer.setup()  # Create tables if needed
            return cls(checkpointer=checkpointer)
        except ImportError:
            logger.error("langgraph-checkpoint-postgres not installed")
            raise ImportError(
                "PostgresSaver requires langgraph-checkpoint-postgres. "
                "Install with: pip install langgraph-checkpoint-postgres"
            )
    
    @classmethod
    def from_config(cls, config: LangGraphCheckpointConfig) -> "LangGraphCheckpointStore":
        """Create store from configuration."""
        if config.checkpointer_type == "memory":
            return cls.create_for_testing()
        elif config.checkpointer_type == "sqlite":
            return cls.create_for_development(config.connection_string or "data/wtb_checkpoints.db")
        elif config.checkpointer_type == "postgres":
            if not config.connection_string:
                raise ValueError("PostgresSaver requires connection_string")
            return cls.create_for_production(config.connection_string)
        else:
            raise ValueError(f"Unknown checkpointer type: {config.checkpointer_type}")
    
    def set_graph(self, graph: CompiledStateGraph) -> None:
        """Set the compiled graph for state operations."""
        self._graph = graph
    
    def get_checkpointer(self) -> BaseCheckpointSaver:
        """Get the underlying checkpointer."""
        return self._checkpointer
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Thread ID Management
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _to_thread_id(self, execution_id: str) -> str:
        """Convert execution_id to LangGraph thread_id."""
        return f"wtb-{execution_id}"
    
    def _from_thread_id(self, thread_id: str) -> str:
        """Convert LangGraph thread_id to execution_id."""
        if thread_id.startswith("wtb-"):
            return thread_id[4:]
        return thread_id
    
    def _get_config(self, execution_id: str, checkpoint_id: str | None = None) -> dict[str, Any]:
        """Build LangGraph config."""
        config: dict[str, Any] = {
            "configurable": {
                "thread_id": self._to_thread_id(execution_id)
            }
        }
        if checkpoint_id:
            config["configurable"]["checkpoint_id"] = checkpoint_id
        return config
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Domain ↔ LangGraph Mapping
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _to_domain_checkpoint(self, snapshot: Any, execution_id: str) -> Checkpoint:
        """
        Map LangGraph StateSnapshot to domain Checkpoint.
        
        This mapping is the ONLY place LangGraph-specific knowledge exists.
        """
        config = snapshot.config.get("configurable", {})
        metadata = snapshot.metadata or {}
        
        # Extract checkpoint_id
        cp_id = config.get("checkpoint_id", "")
        
        # Extract node_writes (which node completed at this checkpoint)
        node_writes = metadata.get("writes", {})
        if node_writes is None:
            node_writes = {}
        
        # Extract next nodes
        next_nodes = list(snapshot.next) if snapshot.next else []
        
        # Extract step
        step = metadata.get("step", 0)
        
        # Extract created_at
        created_at = getattr(snapshot, "created_at", None)
        if created_at is None:
            created_at = datetime.now()
        elif isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        
        return Checkpoint(
            id=CheckpointId(cp_id),
            execution_id=execution_id,
            step=step,
            node_writes=node_writes,
            next_nodes=next_nodes,
            state_values=snapshot.values or {},
            created_at=created_at,
            metadata={
                "source": metadata.get("source"),
                "thread_id": config.get("thread_id"),
            },
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # ICheckpointStore Implementation
    # ═══════════════════════════════════════════════════════════════════════════
    
    def save(self, checkpoint: Checkpoint) -> None:
        """
        Save is implicit in LangGraph - happens during graph.invoke().
        
        For explicit saves (like update_state), we need a graph.
        This method uses update_state if graph is available.
        """
        if not self._graph:
            logger.warning(
                "Cannot explicitly save checkpoint without graph. "
                "LangGraph saves checkpoints automatically during invoke()."
            )
            return
        
        config = self._get_config(checkpoint.execution_id)
        try:
            self._graph.update_state(config, checkpoint.state_values)
        except Exception as e:
            logger.error(f"Failed to save checkpoint: {e}")
            raise
    
    def load(self, checkpoint_id: CheckpointId) -> Checkpoint | None:
        """
        Load checkpoint by ID.
        
        LangGraph doesn't have direct ID lookup, so we scan history.
        """
        if not self._graph:
            logger.warning("Cannot load checkpoint without graph")
            return None
        
        # We need to know the execution_id, which we can extract from stored checkpoints
        # For now, we'll need to search across known executions
        # This is a limitation - in practice, callers should use load_history()
        logger.warning(
            "Direct checkpoint lookup requires execution_id. "
            "Consider using load_history() instead."
        )
        return None
    
    def load_by_execution(self, execution_id: str) -> list[Checkpoint]:
        """Load all checkpoints for an execution."""
        if not self._graph:
            logger.warning("Cannot load checkpoints without graph")
            return []
        
        config = self._get_config(execution_id)
        checkpoints = []
        
        try:
            for snapshot in self._graph.get_state_history(config):
                checkpoint = self._to_domain_checkpoint(snapshot, execution_id)
                checkpoints.append(checkpoint)
        except Exception as e:
            logger.error(f"Failed to load checkpoints for {execution_id}: {e}")
        
        return checkpoints
    
    def load_history(self, execution_id: str) -> ExecutionHistory:
        """
        Load all checkpoints for an execution as ExecutionHistory aggregate.
        
        This is the preferred method - returns domain aggregate with all
        business logic available.
        """
        checkpoints = self.load_by_execution(execution_id)
        return ExecutionHistory(
            execution_id=execution_id,
            checkpoints=checkpoints,
        )
    
    def load_latest(self, execution_id: str) -> Checkpoint | None:
        """Load the most recent checkpoint for an execution."""
        if not self._graph:
            logger.warning("Cannot load checkpoint without graph")
            return None
        
        config = self._get_config(execution_id)
        
        try:
            snapshot = self._graph.get_state(config)
            if snapshot:
                return self._to_domain_checkpoint(snapshot, execution_id)
        except Exception as e:
            logger.error(f"Failed to load latest checkpoint for {execution_id}: {e}")
        
        return None
    
    def delete(self, checkpoint_id: CheckpointId) -> bool:
        """
        Delete a checkpoint.
        
        LangGraph doesn't support checkpoint deletion.
        """
        logger.warning("LangGraph does not support checkpoint deletion")
        return False
    
    def delete_by_execution(self, execution_id: str) -> int:
        """
        Delete all checkpoints for an execution.
        
        LangGraph doesn't support checkpoint deletion.
        """
        logger.warning("LangGraph does not support checkpoint deletion")
        return 0
    
    def exists(self, checkpoint_id: CheckpointId) -> bool:
        """Check if a checkpoint exists."""
        # Without execution_id, we can't efficiently check
        logger.warning(
            "Checkpoint existence check requires execution_id. "
            "Consider using load_history() instead."
        )
        return False
    
    def count(self, execution_id: str) -> int:
        """Count checkpoints for an execution."""
        checkpoints = self.load_by_execution(execution_id)
        return len(checkpoints)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # LangGraph-Specific Operations
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_state_at_checkpoint(
        self, 
        execution_id: str, 
        checkpoint_id: str
    ) -> dict[str, Any] | None:
        """
        Get state values at a specific checkpoint.
        
        Useful for time travel / rollback.
        """
        if not self._graph:
            return None
        
        config = self._get_config(execution_id, checkpoint_id)
        
        try:
            snapshot = self._graph.get_state(config)
            return snapshot.values if snapshot else None
        except Exception as e:
            logger.error(f"Failed to get state at checkpoint {checkpoint_id}: {e}")
            return None
    
    def fork_from_checkpoint(
        self, 
        execution_id: str, 
        checkpoint_id: str,
        new_execution_id: str
    ) -> bool:
        """
        Fork execution from a checkpoint.
        
        Creates a new thread starting from the specified checkpoint.
        """
        if not self._graph:
            logger.warning("Cannot fork without graph")
            return False
        
        # Get state at checkpoint
        state = self.get_state_at_checkpoint(execution_id, checkpoint_id)
        if state is None:
            return False
        
        # Create new thread with that state
        new_config = self._get_config(new_execution_id)
        
        try:
            self._graph.update_state(new_config, state)
            return True
        except Exception as e:
            logger.error(f"Failed to fork from checkpoint: {e}")
            return False


class LangGraphCheckpointStoreFactory(ICheckpointStoreFactory):
    """Factory for creating LangGraphCheckpointStore instances."""
    
    def __init__(self, config: LangGraphCheckpointConfig | None = None):
        """
        Initialize factory with optional config.
        
        Args:
            config: Checkpoint store configuration. If None, uses testing config.
        """
        self._config = config or LangGraphCheckpointConfig.for_testing()
    
    def create(self) -> ICheckpointStore:
        """Create a checkpoint store instance based on config."""
        return LangGraphCheckpointStore.from_config(self._config)
    
    def create_for_testing(self) -> ICheckpointStore:
        """Create an in-memory store for testing."""
        return LangGraphCheckpointStore.create_for_testing()
    
    def create_with_graph(self, graph: CompiledStateGraph) -> LangGraphCheckpointStore:
        """Create store with a compiled graph attached."""
        store = LangGraphCheckpointStore.from_config(self._config)
        store.set_graph(graph)
        return store
