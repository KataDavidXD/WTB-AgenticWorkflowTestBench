"""
Async LangGraph State Adapter - Async state persistence via LangGraph checkpointers.

Created: 2026-01-28
Status: Active
Reference: ASYNC_ARCHITECTURE_PLAN.md §4.1.3, FILE_TRACKING_ARCHITECTURE_DECISION.md §7

Design Principles:
- SOLID: SRP (state persistence only), ISP (separate async interface), DIP (depends on abstraction)
- ACID: LangGraph checkpointers handle transactions atomically per super-step
- Async-First: Native async execution via LangGraph ainvoke/astream

Architecture:
    AsyncExecutionController
           │
           ▼
    IAsyncStateAdapter (abstraction)
           │
           ▼
    AsyncLangGraphStateAdapter
           │
           ├──► StateGraph compilation with checkpointer
           ├──► Thread-based execution isolation
           ├──► Native async execution (ainvoke, astream)
           └──► Time-travel via get_state_history()
           │
           ▼
    BaseCheckpointSaver (InMemory | SQLite | PostgreSQL)
"""

from typing import Any, Optional, List, Dict, AsyncIterator
from datetime import datetime
import logging
import uuid

from wtb.domain.interfaces.async_state_adapter import IAsyncStateAdapter
from wtb.domain.interfaces.state_adapter import CheckpointTrigger
from wtb.domain.models.workflow import ExecutionState

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# LangGraph Availability Check
# ═══════════════════════════════════════════════════════════════════════════════

LANGGRAPH_AVAILABLE = False
_langgraph = None

try:
    import langgraph as _lg
    from langgraph.graph import StateGraph
    from langgraph.graph.state import CompiledStateGraph
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.checkpoint.memory import MemorySaver
    
    _langgraph = _lg
    LANGGRAPH_AVAILABLE = True
except ImportError:
    pass


# ═══════════════════════════════════════════════════════════════════════════════
# Configuration (reuse from sync adapter)
# ═══════════════════════════════════════════════════════════════════════════════

from wtb.infrastructure.adapters.langgraph_state_adapter import (
    LangGraphConfig,
    CheckpointerType,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Async LangGraph State Adapter
# ═══════════════════════════════════════════════════════════════════════════════


class AsyncLangGraphStateAdapter(IAsyncStateAdapter):
    """
    Async State Adapter using LangGraph checkpointers.
    
    Implements IAsyncStateAdapter for non-blocking I/O throughout the execution.
    
    Key Features:
    - Native async execution via ainvoke/astream
    - Async checkpoint operations
    - Session isolation via thread_id
    - Time-travel support via get_state_history
    
    SOLID Compliance:
    - SRP: Only manages state persistence
    - ISP: Implements async-only interface (not dual sync/async)
    - DIP: Application depends on IAsyncStateAdapter abstraction
    
    ACID Compliance:
    - Atomicity: Each super-step is atomic
    - Consistency: LangGraph validates state schema
    - Isolation: Thread-based isolation per execution
    - Durability: Checkpointer persists to configured backend
    """
    
    def __init__(self, config: LangGraphConfig):
        """
        Initialize async state adapter.
        
        Args:
            config: LangGraph configuration with checkpointer settings
        """
        if not LANGGRAPH_AVAILABLE:
            raise ImportError(
                "LangGraph is not installed. "
                "Install with: pip install langgraph"
            )
        
        self._config = config
        self._checkpointer: Optional["BaseCheckpointSaver"] = None
        self._compiled_graph: Optional["CompiledStateGraph"] = None
        self._workflow_graph: Optional["StateGraph"] = None
        
        # Session state
        self._current_thread_id: Optional[str] = None
        self._current_execution_id: Optional[str] = None
        self._current_checkpoint_ns: str = ""
        
        # Initialize checkpointer
        self._initialize_checkpointer()
    
    def _initialize_checkpointer(self):
        """Initialize checkpointer based on configuration."""
        if self._config.checkpointer_type == CheckpointerType.MEMORY:
            self._checkpointer = MemorySaver()
        elif self._config.checkpointer_type == CheckpointerType.SQLITE:
            from langgraph.checkpoint.sqlite import SqliteSaver
            self._checkpointer = SqliteSaver.from_conn_string(
                self._config.connection_string or ":memory:"
            )
        elif self._config.checkpointer_type == CheckpointerType.POSTGRES:
            # PostgreSQL requires async setup (deferred)
            # Will be set up on first use
            self._checkpointer = None
        else:
            raise ValueError(f"Unknown checkpointer type: {self._config.checkpointer_type}")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Graph Management
    # ═══════════════════════════════════════════════════════════════════════════
    
    def set_workflow_graph(
        self, 
        graph: "StateGraph", 
        force_recompile: bool = False
    ) -> None:
        """
        Set workflow graph for execution.
        
        Compiles the graph with checkpointer for state persistence.
        
        Args:
            graph: LangGraph StateGraph to execute
            force_recompile: Force recompilation even if same graph
        """
        if self._workflow_graph is graph and not force_recompile:
            return  # Same graph, skip recompilation
        
        self._workflow_graph = graph
        self._compiled_graph = graph.compile(checkpointer=self._checkpointer)
        logger.debug("Compiled workflow graph with checkpointer")
    
    async def aset_workflow_graph(
        self, 
        graph: "StateGraph", 
        force_recompile: bool = False
    ) -> None:
        """
        Async version - compile is sync but kept for API consistency.
        
        Note: Graph compilation is CPU-bound and sync, but called rarely
        so wrapping in executor is not necessary for most use cases.
        """
        self.set_workflow_graph(graph, force_recompile)
    
    def get_config(self) -> Dict[str, Any]:
        """Get LangGraph config dict for current session."""
        config = {"configurable": {"thread_id": self._current_thread_id}}
        if self._current_checkpoint_ns:
            config["configurable"]["checkpoint_ns"] = self._current_checkpoint_ns
        return config
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Session Management (Async)
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def ainitialize_session(
        self, 
        execution_id: str,
        initial_state: ExecutionState
    ) -> Optional[str]:
        """
        Initialize async session for execution.
        
        Creates unique thread_id for execution isolation.
        
        Args:
            execution_id: WTB execution ID
            initial_state: Initial workflow state
            
        Returns:
            Session ID (thread_id) or None if initialization fails
        """
        # Generate unique thread_id for this execution
        self._current_thread_id = f"wtb-{execution_id}"
        self._current_execution_id = execution_id
        self._current_checkpoint_ns = ""
        
        logger.info(
            f"Initialized async session: thread_id={self._current_thread_id}, "
            f"execution_id={execution_id}"
        )
        
        return self._current_thread_id
    
    async def aset_current_session(
        self, 
        session_id: str,
        execution_id: Optional[str] = None,
    ) -> bool:
        """
        Set current session asynchronously.
        
        Args:
            session_id: Thread ID to switch to
            execution_id: Optional execution ID
            
        Returns:
            True if session was set successfully
        """
        self._current_thread_id = session_id
        if execution_id:
            self._current_execution_id = execution_id
        
        logger.debug(f"Set current session: {session_id}")
        return True
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Checkpoint Operations (Async)
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def asave_checkpoint(
        self,
        state: ExecutionState,
        node_id: str,
        trigger: CheckpointTrigger,
        name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Save checkpoint asynchronously.
        
        Uses LangGraph's update_state to create a checkpoint.
        
        Args:
            state: Current execution state
            node_id: Node that triggered checkpoint
            trigger: Reason for checkpoint
            name: Optional checkpoint name
            metadata: Optional metadata
            
        Returns:
            Checkpoint ID (UUID string)
        """
        if not self._compiled_graph:
            raise RuntimeError("Graph not set. Call set_workflow_graph() first.")
        
        config = self.get_config()
        
        # Prepare state values with checkpoint metadata
        checkpoint_metadata = metadata or {}
        checkpoint_metadata.update({
            "trigger": trigger.value if hasattr(trigger, 'value') else str(trigger),
            "node_id": node_id,
            "name": name or f"checkpoint-{node_id}",
            "timestamp": datetime.now().isoformat(),
        })
        
        # Get current state to preserve non-variable data
        current_values = state.workflow_variables.copy()
        current_values["_checkpoint_metadata"] = checkpoint_metadata
        
        # Update state creates a checkpoint
        await self._compiled_graph.aupdate_state(config, current_values, as_node=node_id)
        
        # Get the checkpoint ID from the latest state
        snapshot = await self._compiled_graph.aget_state(config)
        checkpoint_id = snapshot.config.get("configurable", {}).get("checkpoint_id", str(uuid.uuid4()))
        
        logger.info(
            f"Saved async checkpoint: id={checkpoint_id}, node={node_id}, "
            f"trigger={trigger}"
        )
        
        return checkpoint_id
    
    async def aload_checkpoint(self, checkpoint_id: str) -> ExecutionState:
        """
        Load checkpoint asynchronously.
        
        Args:
            checkpoint_id: Checkpoint to load
            
        Returns:
            ExecutionState from checkpoint
        """
        if not self._compiled_graph:
            raise RuntimeError("Graph not set. Call set_workflow_graph() first.")
        
        config = self.get_config()
        config["configurable"]["checkpoint_id"] = checkpoint_id
        
        snapshot = await self._compiled_graph.aget_state(config)
        
        if not snapshot or not snapshot.values:
            raise ValueError(f"Checkpoint not found: {checkpoint_id}")
        
        # Remove internal metadata from state
        values = dict(snapshot.values)
        values.pop("_checkpoint_metadata", None)
        
        return ExecutionState(workflow_variables=values)
    
    async def arollback(self, to_checkpoint_id: str) -> ExecutionState:
        """
        Rollback to checkpoint asynchronously.
        
        Loads state from checkpoint and updates current thread to continue
        from that point.
        
        Args:
            to_checkpoint_id: Checkpoint to rollback to
            
        Returns:
            ExecutionState after rollback
        """
        state = await self.aload_checkpoint(to_checkpoint_id)
        
        # Update current state to the checkpoint state
        await self.aupdate_state(state.workflow_variables)
        
        logger.info(f"Rolled back to checkpoint: {to_checkpoint_id}")
        
        return state
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Execution (Async)
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def aexecute(self, initial_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute workflow asynchronously.
        
        Non-blocking execution using LangGraph's ainvoke().
        
        Args:
            initial_state: Initial state variables
            
        Returns:
            Final state after execution
        """
        if not self._compiled_graph:
            raise RuntimeError("Graph not set. Call set_workflow_graph() first.")
        
        config = self.get_config()
        
        logger.debug(f"Starting async execution: thread_id={self._current_thread_id}")
        
        result = await self._compiled_graph.ainvoke(initial_state, config)
        
        logger.debug(f"Completed async execution: thread_id={self._current_thread_id}")
        
        return result
    
    async def astream(
        self, 
        initial_state: Dict[str, Any],
        stream_mode: str = "updates"
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Stream execution events asynchronously.
        
        Yields state updates as they occur using LangGraph's astream().
        
        Args:
            initial_state: Initial state variables
            stream_mode: LangGraph stream mode ("updates", "values", "debug")
            
        Yields:
            State updates as they occur
        """
        if not self._compiled_graph:
            raise RuntimeError("Graph not set. Call set_workflow_graph() first.")
        
        config = self.get_config()
        
        logger.debug(f"Starting async stream: thread_id={self._current_thread_id}")
        
        try:
            async for event in self._compiled_graph.astream(
                initial_state, 
                config, 
                stream_mode=stream_mode
            ):
                yield event
        except Exception as e:
            logger.error(f"Stream error: {e}")
            raise
        finally:
            logger.debug(f"Stream completed: thread_id={self._current_thread_id}")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # State Operations (Async)
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def aupdate_state(
        self, 
        values: Dict[str, Any], 
        as_node: Optional[str] = None
    ) -> bool:
        """
        Update state asynchronously.
        
        Used for human-in-the-loop scenarios or state injection.
        
        Args:
            values: Values to update
            as_node: Optional node to attribute update to
            
        Returns:
            True if update was successful
        """
        if not self._compiled_graph:
            raise RuntimeError("Graph not set. Call set_workflow_graph() first.")
        
        config = self.get_config()
        
        try:
            await self._compiled_graph.aupdate_state(config, values, as_node=as_node)
            logger.debug(f"Updated async state: as_node={as_node}")
            return True
        except Exception as e:
            logger.error(f"Failed to update state: {e}")
            return False
    
    async def aget_current_state(self) -> Dict[str, Any]:
        """
        Get current state asynchronously.
        
        Returns:
            Current state values
        """
        if not self._compiled_graph:
            return {}
        
        config = self.get_config()
        snapshot = await self._compiled_graph.aget_state(config)
        
        if not snapshot:
            return {}
        
        # Remove internal metadata
        values = dict(snapshot.values) if snapshot.values else {}
        values.pop("_checkpoint_metadata", None)
        
        return values
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Additional Async Operations
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def aget_checkpoints(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Get checkpoint history asynchronously.
        
        Args:
            limit: Maximum checkpoints to return
            
        Returns:
            List of checkpoint info dicts
        """
        if not self._compiled_graph:
            return []
        
        config = self.get_config()
        checkpoints = []
        
        async for snapshot in self._compiled_graph.aget_state_history(config):
            if len(checkpoints) >= limit:
                break
            
            checkpoint_info = {
                "checkpoint_id": snapshot.config.get("configurable", {}).get("checkpoint_id"),
                "thread_id": self._current_thread_id,
                "parent_checkpoint_id": snapshot.parent_config.get("configurable", {}).get("checkpoint_id") if snapshot.parent_config else None,
                "values": snapshot.values,
                "next": list(snapshot.next) if snapshot.next else [],
            }
            checkpoints.append(checkpoint_info)
        
        return checkpoints
    
    async def acreate_fork(
        self,
        fork_thread_id: str,
        from_checkpoint_id: Optional[str] = None
    ) -> "AsyncLangGraphStateAdapter":
        """
        Create a fork asynchronously from current or specified checkpoint.
        
        Args:
            fork_thread_id: Thread ID for the fork
            from_checkpoint_id: Optional checkpoint to fork from
            
        Returns:
            New AsyncLangGraphStateAdapter for the fork
        """
        if not self._compiled_graph:
            raise RuntimeError("Graph not set. Call set_workflow_graph() first.")
        
        # Get source state
        config = self.get_config()
        if from_checkpoint_id:
            config["configurable"]["checkpoint_id"] = from_checkpoint_id
        
        source_state = await self._compiled_graph.aget_state(config)
        
        if not source_state or not source_state.values:
            raise ValueError(
                f"Cannot fork: checkpoint state is empty for thread {self._current_thread_id}"
            )
        
        # Create fork adapter
        fork_adapter = AsyncLangGraphStateAdapter(self._config)
        fork_adapter._checkpointer = self._checkpointer  # Share checkpointer
        fork_adapter._workflow_graph = self._workflow_graph
        fork_adapter._compiled_graph = self._compiled_graph  # Share compiled graph
        fork_adapter._current_thread_id = fork_thread_id
        
        # Initialize fork with source state
        fork_config = {"configurable": {"thread_id": fork_thread_id}}
        await self._compiled_graph.aupdate_state(fork_config, source_state.values)
        
        logger.info(
            f"Created async fork: {self._current_thread_id} -> {fork_thread_id}"
        )
        
        return fork_adapter


# ═══════════════════════════════════════════════════════════════════════════════
# Factory
# ═══════════════════════════════════════════════════════════════════════════════


class AsyncLangGraphStateAdapterFactory:
    """Factory for creating AsyncLangGraphStateAdapter instances."""
    
    @staticmethod
    def create_for_testing() -> AsyncLangGraphStateAdapter:
        """Create adapter for unit tests with in-memory checkpointer."""
        return AsyncLangGraphStateAdapter(LangGraphConfig.for_testing())
    
    @staticmethod
    def create_for_development(
        db_path: str = "data/wtb_checkpoints.db"
    ) -> AsyncLangGraphStateAdapter:
        """Create adapter for development with SQLite checkpointer."""
        return AsyncLangGraphStateAdapter(LangGraphConfig.for_development(db_path))
    
    @staticmethod
    def create_for_production(connection_string: str) -> AsyncLangGraphStateAdapter:
        """Create adapter for production with PostgreSQL checkpointer."""
        return AsyncLangGraphStateAdapter(LangGraphConfig.for_production(connection_string))
    
    @staticmethod
    def is_available() -> bool:
        """Check if LangGraph is available."""
        return LANGGRAPH_AVAILABLE
