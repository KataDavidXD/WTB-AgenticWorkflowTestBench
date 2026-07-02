"""
LangGraph State Adapter - Primary state persistence via LangGraph checkpointers.

Refactored (v1.6): Uses string IDs natively, removed int-to-string mapping.

Design Principles:
- SOLID: SRP (state persistence only), DIP (depends on IStateAdapter abstraction)
- ACID: LangGraph checkpointers handle transactions atomically per super-step
- All IDs are strings (UUIDs) - no more int/str mapping

Architecture:
    WTB ExecutionController
           │
           ▼
    IStateAdapter (abstraction)
           │
           ▼
    LangGraphStateAdapter
           │
           ├──► StateGraph compilation with checkpointer
           ├──► Thread-based execution isolation
           └──► Time-travel via get_state_history()
           │
           ▼
    BaseCheckpointSaver (InMemory | SQLite | PostgreSQL)
"""

from typing import Any, Optional, List, Dict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import logging
import uuid

from wtb.domain.interfaces.state_adapter import (
    IStateAdapter,
    CheckpointTrigger,
    CheckpointInfo,
    NodeBoundaryInfo,
)
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
    
    _langgraph = _lg
    LANGGRAPH_AVAILABLE = True
except ImportError:
    pass


# ═══════════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════════


class CheckpointerType(Enum):
    """Supported checkpointer types."""
    MEMORY = "memory"
    SQLITE = "sqlite"
    POSTGRES = "postgres"


@dataclass
class LangGraphConfig:
    """
    Configuration for LangGraph adapter.
    
    Attributes:
        checkpointer_type: Type of checkpointer to use
        connection_string: Database connection string (for sqlite/postgres)
        pool_size: Connection pool size for postgres
    """
    checkpointer_type: CheckpointerType = CheckpointerType.MEMORY
    connection_string: Optional[str] = None
    pool_size: int = 5
    
    @classmethod
    def for_testing(cls) -> "LangGraphConfig":
        """InMemorySaver for unit tests (fastest, no persistence)."""
        return cls(checkpointer_type=CheckpointerType.MEMORY)
    
    @classmethod
    def for_development(cls, db_path: str = "data/wtb_checkpoints.db") -> "LangGraphConfig":
        """SqliteSaver for local development."""
        return cls(
            checkpointer_type=CheckpointerType.SQLITE,
            connection_string=db_path,
        )
    
    @classmethod
    def for_production(cls, connection_string: str, pool_size: int = 10) -> "LangGraphConfig":
        """PostgresSaver for production with connection pooling."""
        return cls(
            checkpointer_type=CheckpointerType.POSTGRES,
            connection_string=connection_string,
            pool_size=pool_size,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Internal Tracking
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class _NodeBoundaryTracker:
    """Internal tracker for node boundaries within a session."""
    node_id: str
    entry_checkpoint_id: str
    exit_checkpoint_id: Optional[str] = None
    status: str = "started"
    started_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════════
# LangGraph State Adapter
# ═══════════════════════════════════════════════════════════════════════════════


class LangGraphStateAdapter(IStateAdapter):
    """
    Primary state adapter using LangGraph checkpointers.
    
    Refactored (v1.6):
    - All IDs are strings (UUIDs) - no mapping required
    - Session ID = thread_id (string)
    - Removed _checkpoint_id_map and _numeric_to_lg_id
    
    WTB Operation          → LangGraph API
    ═══════════════════════════════════════════════════════════
    initialize_session()   → Set thread_id, compile graph
    save_checkpoint()      → update_state() creates checkpoint
    load_checkpoint()      → graph.get_state(config + checkpoint_id)
    rollback()             → graph.get_state(config + checkpoint_id)
    get_checkpoints()      → graph.get_state_history(config)
    
    Thread Safety:
    - Each execution has isolated thread_id
    - Thread IDs: "wtb-{execution_id}"
    """
    
    def __init__(self, config: LangGraphConfig):
        """
        Initialize LangGraph adapter.
        
        Args:
            config: LangGraph configuration
            
        Raises:
            ImportError: If langgraph package not installed
        """
        if not LANGGRAPH_AVAILABLE:
            raise ImportError(
                "langgraph package not installed. "
                "Install with: pip install langgraph langgraph-checkpoint"
            )
        
        self._config = config
        self._checkpointer = self._create_checkpointer()
        self._owns_checkpointer = True
        self._compiled_graph: Optional[CompiledStateGraph] = None
        self._graph_builder: Optional[StateGraph] = None
        
        # Session tracking (v1.6: string IDs)
        self._current_thread_id: Optional[str] = None
        self._current_execution_id: Optional[str] = None
        self._resume_checkpoint_id: Optional[str] = None
        
        # Node boundary tracking (per thread_id)
        self._node_boundaries: Dict[str, Dict[str, _NodeBoundaryTracker]] = {}
        
        logger.info(f"LangGraphStateAdapter initialized with {config.checkpointer_type.value} checkpointer")
    
    def _create_checkpointer(self) -> "BaseCheckpointSaver":
        """Create checkpointer based on configuration."""
        if self._config.checkpointer_type == CheckpointerType.MEMORY:
            from langgraph.checkpoint.memory import MemorySaver
            return MemorySaver()
        
        elif self._config.checkpointer_type == CheckpointerType.SQLITE:
            try:
                from langgraph.checkpoint.sqlite import SqliteSaver
                import sqlite3
                import os
                
                # Ensure parent directory exists
                db_path = self._config.connection_string
                if db_path:
                    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
                
                conn = sqlite3.connect(
                    db_path,
                    check_same_thread=False,
                )
                saver = SqliteSaver(conn)
                saver.setup()  # Create checkpoint tables
                logger.info(f"SQLite checkpointer initialized at: {db_path}")
                return saver
            except ImportError:
                raise ImportError(
                    "langgraph-checkpoint-sqlite not installed. "
                    "Install with: pip install langgraph-checkpoint-sqlite"
                )
        
        elif self._config.checkpointer_type == CheckpointerType.POSTGRES:
            try:
                from langgraph.checkpoint.postgres import PostgresSaver
                
                saver = PostgresSaver.from_conn_string(
                    self._config.connection_string
                )
                saver.setup()
                return saver
            except ImportError:
                raise ImportError(
                    "langgraph-checkpoint-postgres not installed. "
                    "Install with: pip install langgraph-checkpoint-postgres"
                )
        
        raise ValueError(f"Unknown checkpointer type: {self._config.checkpointer_type}")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Graph Management
    # ═══════════════════════════════════════════════════════════════════════════

    def _can_reuse_compiled_graph(self, graph: Any) -> bool:
        """Return True when the adapter already holds a compatible compiled graph."""
        if self._compiled_graph is None or not self._has_valid_checkpointer(self._compiled_graph):
            return False
        if graph is self._compiled_graph or graph is self._graph_builder:
            return True
        if hasattr(graph, 'invoke') and hasattr(graph, 'get_state'):
            return getattr(graph, 'builder', None) is self._graph_builder
        return False
    
    def set_workflow_graph(self, graph: "StateGraph", force_recompile: bool = True) -> None:
        """
        Set workflow graph for execution.
        
        IMPORTANT: Always recompiles with our checkpointer for ACID compliance.
        """
        if self._can_reuse_compiled_graph(graph):
            logger.info("Reusing compiled graph with adapter's checkpointer")
            return

        # Check if already compiled (CompiledStateGraph)
        if hasattr(graph, 'invoke') and hasattr(graph, 'get_state'):
            self._graph_builder = getattr(graph, 'builder', None)
            
            if force_recompile and self._graph_builder is not None:
                self._compiled_graph = self._graph_builder.compile(checkpointer=self._checkpointer)
                logger.info("Graph recompiled with adapter's checkpointer")
            elif self._has_valid_checkpointer(graph):
                self._compiled_graph = graph
                logger.info("Using pre-compiled graph with existing checkpointer")
            else:
                self._compiled_graph = graph
                logger.warning(
                    "Using pre-compiled graph without checkpointer. "
                    "Checkpoints will NOT be persisted!"
                )
        else:
            # Uncompiled StateGraph - compile with our checkpointer
            self._graph_builder = graph
            self._compiled_graph = graph.compile(checkpointer=self._checkpointer)
            logger.info("Graph compiled with checkpointer (recommended path)")
    
    def _has_valid_checkpointer(self, compiled_graph: Any) -> bool:
        """Check if a compiled graph has a valid checkpointer configured."""
        try:
            if hasattr(compiled_graph, 'checkpointer'):
                return compiled_graph.checkpointer is not None
            if hasattr(compiled_graph, '_checkpointer'):
                return compiled_graph._checkpointer is not None
            return False
        except Exception:
            return False
    
    def get_compiled_graph(self) -> "CompiledStateGraph":
        """Get compiled graph."""
        if not self._compiled_graph:
            raise RuntimeError("Graph not set. Call set_workflow_graph() first.")
        return self._compiled_graph
    
    def supports_graph_execution(self) -> bool:
        """LangGraphStateAdapter always supports graph execution."""
        return True
    
    def has_graph(self) -> bool:
        """Check if graph is set."""
        return self._compiled_graph is not None
    
    def get_checkpointer(self) -> "BaseCheckpointSaver":
        """Get the checkpointer instance."""
        return self._checkpointer

    def close(self) -> None:
        """Release checkpointer resources (only if this adapter owns them)."""
        if self._checkpointer is None or not self._owns_checkpointer:
            return
        try:
            # Finalize WAL so the DB file is self-contained and no stale
            # .wal/.shm files are left behind for the next process.
            if hasattr(self._checkpointer, "conn") and self._checkpointer.conn:
                try:
                    self._checkpointer.conn.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)"
                    )
                except Exception:
                    pass
                self._checkpointer.conn.close()
            elif hasattr(self._checkpointer, "close"):
                self._checkpointer.close()
            elif callable(getattr(self._checkpointer, "__exit__", None)):
                self._checkpointer.__exit__(None, None, None)
        except Exception:
            pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self) -> "LangGraphStateAdapter":
        return self

    def __exit__(self, *args) -> None:
        self.close()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # IStateAdapter: Session Management (v1.6: string IDs)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def initialize_session(
        self,
        execution_id: str,
        initial_state: ExecutionState
    ) -> Optional[str]:
        """
        Initialize session using LangGraph thread.
        
        Returns:
            Session ID (thread_id string)
        """
        self._current_execution_id = execution_id
        self._current_thread_id = f"wtb-{execution_id}"
        
        # Initialize node boundary tracking for this thread
        if self._current_thread_id not in self._node_boundaries:
            self._node_boundaries[self._current_thread_id] = {}
        
        logger.info(f"Session initialized: thread_id={self._current_thread_id}")
        return self._current_thread_id
    
    def get_current_session_id(self) -> Optional[str]:
        """Get current session ID (thread_id)."""
        return self._current_thread_id
    
    def set_current_session(
        self, 
        session_id: str,
        execution_id: Optional[str] = None,
    ) -> bool:
        """
        Set current session for checkpoint retrieval.
        """
        # If execution_id provided, reconstruct thread_id (ACID: Durability)
        if execution_id:
            thread_id = f"wtb-{execution_id}"
            self._current_thread_id = thread_id
            self._current_execution_id = execution_id
            if thread_id not in self._node_boundaries:
                self._node_boundaries[thread_id] = {}
            return True
        
        # Use session_id directly as thread_id
        self._current_thread_id = session_id
        if session_id not in self._node_boundaries:
            self._node_boundaries[session_id] = {}
        return True
    
    def get_config(self, checkpoint_id: Optional[str] = None) -> Dict[str, Any]:
        """Get LangGraph config for current thread."""
        if not self._current_thread_id:
            raise RuntimeError("No active session. Call initialize_session() first.")
        
        config: Dict[str, Any] = {
            "configurable": {
                "thread_id": self._current_thread_id
            }
        }
        
        if checkpoint_id:
            config["configurable"]["checkpoint_id"] = checkpoint_id
        
        return config
    
    # ═══════════════════════════════════════════════════════════════════════════
    # IStateAdapter: Checkpoint Operations (v1.6: string IDs)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def save_checkpoint(
        self,
        state: ExecutionState,
        node_id: str,
        trigger: CheckpointTrigger,
        name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Save checkpoint via LangGraph update_state.
        
        Returns:
            Checkpoint ID (UUID string)
        """
        if not self._compiled_graph:
            raise RuntimeError("Graph not set. Call set_workflow_graph() first.")
        
        if not self._current_thread_id:
            raise RuntimeError("No active session. Call initialize_session() first.")
        
        config = self.get_config()
        
        # Convert ExecutionState to dict for LangGraph
        state_dict = {
            "current_node_id": state.current_node_id or "",
            "workflow_variables": state.workflow_variables,
            "execution_path": list(state.execution_path),
            "node_results": state.node_results,
            "wtb_metadata": {
                "trigger": trigger.value,
                "checkpoint_name": name,
                "node_id": node_id,
                **(metadata or {}),
            },
        }
        
        # Update state in LangGraph (creates checkpoint)
        self._compiled_graph.update_state(config, state_dict, as_node=node_id)
        
        # Get the latest checkpoint ID (UUID string from LangGraph)
        snapshot = self._compiled_graph.get_state(config)
        checkpoint_id = snapshot.config["configurable"].get("checkpoint_id", str(uuid.uuid4()))
        
        logger.debug(f"Checkpoint saved: {checkpoint_id[:8]}...")
        return checkpoint_id
    
    def load_checkpoint(self, checkpoint_id: str) -> ExecutionState:
        """
        Load checkpoint state by ID.
        
        Args:
            checkpoint_id: Checkpoint ID (UUID string)
        """
        if not self._compiled_graph:
            raise RuntimeError("Graph not set.")
        
        config = self.get_config(checkpoint_id=checkpoint_id)
        snapshot = self._compiled_graph.get_state(config)
        
        if not snapshot:
            raise ValueError(f"Checkpoint {checkpoint_id} not found")
        
        values = snapshot.values
        
        if isinstance(values, dict):
            current_node = values.get("current_node_id")
            exec_path = values.get("execution_path", [])
            node_results = values.get("node_results", {})
            workflow_vars = values.get("workflow_variables", {})
            node_boundaries = values.get("node_boundaries", {})
            
            # LangGraph stores state as a flat dict (e.g. {query, answer, _output_files})
            # rather than nested under "workflow_variables". When the nested key is absent,
            # use the full values dict so rollback preserves all state fields.
            if not workflow_vars:
                workflow_vars = dict(values)
        else:
            current_node = None
            exec_path = []
            node_results = {}
            workflow_vars = {}
            node_boundaries = {}
        
        return ExecutionState(
            current_node_id=current_node,
            workflow_variables=workflow_vars,
            execution_path=exec_path,
            node_results=node_results,
            node_boundaries=node_boundaries,
        )
    
    def rollback(self, to_checkpoint_id: str) -> ExecutionState:
        """Rollback to a specific checkpoint."""
        if not self._compiled_graph:
            raise RuntimeError("Graph not set")
        
        # Load the checkpoint state
        state = self.load_checkpoint(to_checkpoint_id)
        self._resume_checkpoint_id = to_checkpoint_id
        
        # Update current state to match checkpoint
        config = self.get_config(checkpoint_id=to_checkpoint_id)
        snapshot = self._compiled_graph.get_state(config)
        if snapshot:
            current_config = self.get_config()
            self._compiled_graph.update_state(current_config, snapshot.values)
        
        # Reset node boundary tracking for this thread since we rolled
        # back to an earlier point; stale boundaries would be misleading.
        if self._current_thread_id and self._current_thread_id in self._node_boundaries:
            self._node_boundaries[self._current_thread_id] = {}
        
        logger.info(f"Rolled back to checkpoint {to_checkpoint_id[:8]}...")
        return state
    
    # ═══════════════════════════════════════════════════════════════════════════
    # IStateAdapter: Node Boundary Operations (v1.6: string IDs)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def mark_node_started(self, node_id: str, entry_checkpoint_id: str) -> str:
        """Mark node as started with entry checkpoint."""
        if not self._current_thread_id:
            raise RuntimeError("No active session")
        
        boundary_id = str(uuid.uuid4())
        
        tracker = _NodeBoundaryTracker(
            node_id=node_id,
            entry_checkpoint_id=entry_checkpoint_id,
            status="started",
        )
        
        self._node_boundaries[self._current_thread_id][node_id] = tracker
        return boundary_id
    
    def mark_node_completed(
        self,
        node_id: str,
        exit_checkpoint_id: str,
    ) -> bool:
        """Mark node as completed with exit checkpoint."""
        if not self._current_thread_id:
            return False
        
        tracker = self._node_boundaries.get(self._current_thread_id, {}).get(node_id)
        if not tracker:
            return False
        
        tracker.exit_checkpoint_id = exit_checkpoint_id
        tracker.status = "completed"
        tracker.completed_at = datetime.now()
        
        return True
    
    def mark_node_failed(self, node_id: str, error_message: str) -> bool:
        """Mark node as failed."""
        if not self._current_thread_id:
            return False
        
        tracker = self._node_boundaries.get(self._current_thread_id, {}).get(node_id)
        if not tracker:
            return False
        
        tracker.status = "failed"
        tracker.error_message = error_message
        tracker.completed_at = datetime.now()
        
        return True
    
    def get_node_boundaries(self, session_id: str) -> List[NodeBoundaryInfo]:
        """Get all node boundaries for session."""
        boundaries = []
        for node_id, tracker in self._node_boundaries.get(session_id, {}).items():
            boundaries.append(NodeBoundaryInfo(
                id=str(uuid.uuid4()),
                node_id=node_id,
                entry_checkpoint_id=tracker.entry_checkpoint_id,
                exit_checkpoint_id=tracker.exit_checkpoint_id,
                node_status=tracker.status,
                started_at=tracker.started_at.isoformat(),
                completed_at=tracker.completed_at.isoformat() if tracker.completed_at else None,
            ))
        
        return boundaries
    
    def get_node_boundary(self, session_id: str, node_id: str) -> Optional[NodeBoundaryInfo]:
        """Get specific node boundary."""
        tracker = self._node_boundaries.get(session_id, {}).get(node_id)
        if not tracker:
            return None
        
        return NodeBoundaryInfo(
            id=str(uuid.uuid4()),
            node_id=node_id,
            entry_checkpoint_id=tracker.entry_checkpoint_id,
            exit_checkpoint_id=tracker.exit_checkpoint_id,
            node_status=tracker.status,
            started_at=tracker.started_at.isoformat(),
            completed_at=tracker.completed_at.isoformat() if tracker.completed_at else None,
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # IStateAdapter: Query Operations (v1.6: string IDs)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_checkpoints(
        self,
        session_id: str,
        node_id: Optional[str] = None
    ) -> List[CheckpointInfo]:
        """Get checkpoints for session."""
        if not self._compiled_graph:
            return []
        
        # Use session_id as thread_id
        config = {"configurable": {"thread_id": session_id}}
        checkpoints = []
        
        for snapshot in self._compiled_graph.get_state_history(config):
            cp_id = snapshot.config["configurable"].get("checkpoint_id", "")
            step = snapshot.metadata.get("step", 0)
            
            metadata = snapshot.values.get("wtb_metadata", {}) if snapshot.values else {}
            cp_node_id = metadata.get("node_id")
            
            # Filter by node_id if specified
            if node_id and cp_node_id != node_id:
                continue
            
            trigger_str = metadata.get("trigger", "auto")
            try:
                trigger = CheckpointTrigger(trigger_str)
            except ValueError:
                trigger = CheckpointTrigger.AUTO
            
            checkpoints.append(CheckpointInfo(
                id=cp_id,
                name=metadata.get("checkpoint_name"),
                node_id=cp_node_id,
                step=step,
                trigger_type=trigger,
                created_at=snapshot.metadata.get("created_at") or datetime.now().isoformat(),
                is_auto=trigger == CheckpointTrigger.AUTO,
            ))
        
        return checkpoints
    
    def get_node_rollback_targets(self, session_id: str) -> List[CheckpointInfo]:
        """Get rollback targets (exit checkpoints of completed nodes)."""
        targets = []
        
        for boundary in self.get_node_boundaries(session_id):
            if boundary.node_status == "completed" and boundary.exit_checkpoint_id:
                targets.append(CheckpointInfo(
                    id=boundary.exit_checkpoint_id,
                    name=f"Exit: {boundary.node_id}",
                    node_id=boundary.node_id,
                    step=0,
                    trigger_type=CheckpointTrigger.AUTO,
                    created_at=boundary.completed_at or "",
                    is_auto=True,
                ))
        
        return targets
    
    def cleanup(self, session_id: str, keep_latest: int = 5) -> int:
        """Cleanup old checkpoints (no-op for LangGraph)."""
        logger.debug(f"Cleanup requested for session {session_id}, keep_latest={keep_latest}")
        return 0
    
    # ═══════════════════════════════════════════════════════════════════════════
    # LangGraph-Specific Operations
    # ═══════════════════════════════════════════════════════════════════════════
    
    def execute(self, initial_state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Execute workflow with automatic checkpointing.
        
        Args:
            initial_state: Initial state dict for fresh execution, or None to
                          resume from the last checkpoint on the current thread
                          (native LangGraph behavior via graph.invoke(None, config)).
        Args:
            initial_state: Initial state dict for fresh execution, or None to
                          resume from the last checkpoint on the current thread
                          (native LangGraph behavior via graph.invoke(None, config)).
        """
        if not self._compiled_graph:
            raise RuntimeError("Graph not set. Call set_workflow_graph() first.")
        
        resume_checkpoint_id = None
        if initial_state is None:
            resume_checkpoint_id = self._resume_checkpoint_id
            self._resume_checkpoint_id = None

        config = self.get_config(checkpoint_id=resume_checkpoint_id)
        result = self._compiled_graph.invoke(initial_state, config)
        
        return result
    
    async def aexecute(self, initial_state: Dict[str, Any]) -> Dict[str, Any]:
        """Async execution."""
        if not self._compiled_graph:
            raise RuntimeError("Graph not set. Call set_workflow_graph() first.")
        
        config = self.get_config()
        return await self._compiled_graph.ainvoke(initial_state, config)
    
    def stream(self, initial_state: Dict[str, Any], stream_mode: str = "updates"):
        """Stream execution events."""
        if not self._compiled_graph:
            raise RuntimeError("Graph not set. Call set_workflow_graph() first.")
        
        config = self.get_config()
        return self._compiled_graph.stream(initial_state, config, stream_mode=stream_mode)
    
    def get_current_state(self) -> Dict[str, Any]:
        """Get current state values."""
        if not self._compiled_graph:
            return {}
        
        config = self.get_config()
        snapshot = self._compiled_graph.get_state(config)
        return snapshot.values if snapshot else {}
    
    def get_next_nodes(self) -> List[str]:
        """Get next nodes to execute."""
        if not self._compiled_graph:
            return []
        
        config = self.get_config()
        snapshot = self._compiled_graph.get_state(config)
        return list(snapshot.next) if snapshot and snapshot.next else []
    
    def supports_streaming(self) -> bool:
        """LangGraph supports event streaming."""
        return True
    
    def supports_time_travel(self) -> bool:
        """LangGraph supports time-travel via get_state_history."""
        return True
    
    def update_state(self, values: Dict[str, Any], as_node: Optional[str] = None) -> bool:
        """Update state mid-execution (human-in-the-loop)."""
        if not self._compiled_graph:
            return False
        
        try:
            config = self.get_config()
            self._compiled_graph.update_state(config, values, as_node=as_node)
            return True
        except Exception as e:
            logger.warning(f"Failed to update state: {e}")
            return False
    
    def get_checkpoint_history(self) -> List[Dict[str, Any]]:
        """Get full checkpoint history for time travel.
        
        v1.6: Falls back to direct checkpointer query when no compiled graph.
        This enables checkpoint retrieval after reconnection without running workflow.
        """
        if not self._current_thread_id:
            return []
        
        config = self.get_config()
        history = []
        
        # Try compiled graph first (gives full state)
        if self._compiled_graph:
            try:
                for snapshot in self._compiled_graph.get_state_history(config):
                    history.append({
                        "checkpoint_id": snapshot.config["configurable"]["checkpoint_id"],
                        "step": snapshot.metadata.get("step"),
                        "source": snapshot.metadata.get("source"),
                        "writes": snapshot.metadata.get("writes", {}),
                        "next": list(snapshot.next) if snapshot.next else [],
                        "values": snapshot.values,
                        "created_at": snapshot.metadata.get("created_at"),
                    })
                return history
            except Exception as e:
                logger.warning(f"Failed to get history from graph, trying checkpointer: {e}")
        
        # Fall back to direct checkpointer query (v1.6: for reconnection scenarios)
        if self._checkpointer:
            try:
                # Use checkpointer's list method to get checkpoint tuples
                for checkpoint_tuple in self._checkpointer.list(config):
                    checkpoint_config = checkpoint_tuple.config
                    metadata = checkpoint_tuple.metadata or {}
                    
                    history.append({
                        "checkpoint_id": checkpoint_config.get("configurable", {}).get("checkpoint_id", ""),
                        "step": metadata.get("step", 0),
                        "source": metadata.get("source", ""),
                        "writes": metadata.get("writes", {}),
                        "next": [],  # Not available without graph
                        "values": {},  # Not available without graph - would need get()
                        "created_at": metadata.get("created_at"),
                    })
            except Exception as e:
                logger.error(f"Failed to get history from checkpointer: {e}")
        
        return history
    
    def create_fork(
        self,
        fork_thread_id: str,
        from_checkpoint_id: Optional[str] = None
    ) -> "LangGraphStateAdapter":
        """
        Create a fork adapter for variant execution.
        
        Uses ``object.__new__`` to avoid the ``__init__`` path which would
        open a **new** checkpointer connection that is immediately discarded.
        The fork shares the parent's checkpointer and graph; it does NOT
        own the checkpointer, so ``close()`` is a no-op on this instance.
        """
        fork_adapter = object.__new__(LangGraphStateAdapter)
        fork_adapter._config = self._config
        fork_adapter._checkpointer = self._checkpointer
        fork_adapter._owns_checkpointer = False
        fork_adapter._compiled_graph = self._compiled_graph
        fork_adapter._graph_builder = self._graph_builder
        fork_adapter._current_thread_id = fork_thread_id
        fork_adapter._current_execution_id = None
        fork_adapter._node_boundaries = {fork_thread_id: {}}
        
        # If forking from specific checkpoint, copy state
        if from_checkpoint_id:
            source_config = self.get_config(checkpoint_id=from_checkpoint_id)
            source_state = self._compiled_graph.get_state(source_config)
            
            if source_state:
                fork_config = {"configurable": {"thread_id": fork_thread_id}}
                self._compiled_graph.update_state(fork_config, source_state.values)
        
        return fork_adapter


# ═══════════════════════════════════════════════════════════════════════════════
# Factory
# ═══════════════════════════════════════════════════════════════════════════════


class LangGraphStateAdapterFactory:
    """Factory for creating LangGraphStateAdapter instances."""
    
    @staticmethod
    def create_for_testing() -> LangGraphStateAdapter:
        """Create adapter for unit tests."""
        return LangGraphStateAdapter(LangGraphConfig.for_testing())
    
    @staticmethod
    def create_for_development(
        db_path: str = "data/wtb_checkpoints.db"
    ) -> LangGraphStateAdapter:
        """Create adapter for development."""
        return LangGraphStateAdapter(LangGraphConfig.for_development(db_path))
    
    @staticmethod
    def create_for_production(connection_string: str) -> LangGraphStateAdapter:
        """Create adapter for production."""
        return LangGraphStateAdapter(LangGraphConfig.for_production(connection_string))
    
    @staticmethod
    def is_available() -> bool:
        """Check if LangGraph is available."""
        return LANGGRAPH_AVAILABLE
