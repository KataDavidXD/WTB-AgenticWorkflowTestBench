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

import logging
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from threading import RLock
from typing import Any

from wtb.domain.interfaces.state_adapter import (
    CheckpointInfo,
    CheckpointTrigger,
    IStateAdapter,
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
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph import StateGraph
    from langgraph.graph.state import CompiledStateGraph
    
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
    connection_string: str | None = None
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
    exit_checkpoint_id: str | None = None
    status: str = "started"
    started_at: datetime = field(default_factory=datetime.now)
    completed_at: datetime | None = None
    error_message: str | None = None


class _SyncCheckpointerResource:
    """Reference-counted lifetime for a saver shared by adapter forks."""

    def __init__(self, checkpointer: Any, context: Any | None):
        self.checkpointer = checkpointer
        self.context = context
        self._references = 1
        self._closed = False
        self._lock = RLock()

    def acquire(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Checkpointer resource is closed")
            self._references += 1

    def release(self):
        with self._lock:
            if self._references <= 0:
                return None
            self._references -= 1
            if self._references > 0 or self._closed:
                return None
            self._closed = True
            payload = (self.checkpointer, self.context)
            self.checkpointer = None
            self.context = None
            return payload



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
        self._checkpointer_context = None
        self._checkpointer_resource = None
        self._checkpointer = None
        self._owns_checkpointer = True
        self._closed = False
        self._checkpointer = self._create_checkpointer()
        self._checkpointer_resource = _SyncCheckpointerResource(
            self._checkpointer,
            self._checkpointer_context,
        )
        self._compiled_graph: CompiledStateGraph | None = None
        self._graph_builder: StateGraph | None = None
        
        # Session tracking (v1.6: string IDs)
        self._current_thread_id: str | None = None
        self._current_execution_id: str | None = None
        self._resume_checkpoint_id: str | None = None
        
        # Node boundary tracking (per thread_id)
        self._node_boundaries: dict[str, dict[str, _NodeBoundaryTracker]] = {}
        
        logger.info(f"LangGraphStateAdapter initialized with {config.checkpointer_type.value} checkpointer")

    @property
    def state_adapter_backend(self) -> str | None:
        """Advertise the durable SDK rehydration protocol when using SQLite."""
        if self._config.checkpointer_type == CheckpointerType.SQLITE:
            return "langgraph_sqlite"
        return None

    @property
    def storage_path(self) -> str | None:
        """Return the SQLite path used for exact adapter reuse checks."""
        if self._config.checkpointer_type == CheckpointerType.SQLITE:
            return self._config.connection_string
        return None
    
    def _create_checkpointer(self) -> "BaseCheckpointSaver":
        """Create checkpointer based on configuration."""
        if self._config.checkpointer_type == CheckpointerType.MEMORY:
            from langgraph.checkpoint.memory import MemorySaver
            return MemorySaver()
        
        elif self._config.checkpointer_type == CheckpointerType.SQLITE:
            try:
                import os
                import sqlite3

                from langgraph.checkpoint.sqlite import SqliteSaver
                
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
            except ImportError:
                raise ImportError(
                    "langgraph-checkpoint-postgres not installed. "
                    "Install with: pip install langgraph-checkpoint-postgres"
                )

            context = PostgresSaver.from_conn_string(
                self._config.connection_string
            )
            enter_context = getattr(context, "__enter__", None)
            if not callable(enter_context):
                raise TypeError(
                    "PostgresSaver.from_conn_string() must return a context manager"
                )

            entered = False
            try:
                saver = enter_context()
                entered = True
                saver.setup()
            except BaseException:
                if entered:
                    context.__exit__(*sys.exc_info())
                raise

            self._checkpointer_context = context
            return saver
        
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
            graph_builder = getattr(graph, 'builder', None)

            if force_recompile and graph_builder is not None:
                compiled_graph = graph_builder.compile(checkpointer=self._checkpointer)
                self._graph_builder = graph_builder
                self._compiled_graph = compiled_graph
                logger.info("Graph recompiled with adapter's checkpointer")
            elif self._has_valid_checkpointer(graph):
                self._graph_builder = graph_builder
                self._compiled_graph = graph
                logger.info("Using pre-compiled graph with existing checkpointer")
            else:
                raise ValueError(
                    "Compiled workflow graph is not bound to a valid checkpointer "
                    "and cannot be safely rebound to this adapter."
                )
        else:
            # Uncompiled StateGraph - compile with our checkpointer
            self._graph_builder = graph
            self._compiled_graph = graph.compile(checkpointer=self._checkpointer)
            logger.info("Graph compiled with checkpointer (recommended path)")
    
    def _has_valid_checkpointer(self, compiled_graph: Any) -> bool:
        """Check that a compiled graph uses this adapter's concrete saver."""
        try:
            saver = None
            if hasattr(compiled_graph, 'checkpointer'):
                saver = compiled_graph.checkpointer
            elif hasattr(compiled_graph, '_checkpointer'):
                saver = compiled_graph._checkpointer
            return (
                self._checkpointer is not None
                and saver is self._checkpointer
            )
        except Exception:
            return False
    
    def get_compiled_graph(self) -> "CompiledStateGraph":
        """Get compiled graph."""
        self._ensure_open()
        if not self._compiled_graph:
            raise RuntimeError("Graph not set. Call set_workflow_graph() first.")
        return self._compiled_graph

    def _ensure_open(self) -> None:
        """Reject state operations after this adapter releases its lease."""
        if getattr(self, "_closed", False):
            raise RuntimeError("State adapter is closed")
    
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
        """Release this adapter's checkpointer lease."""
        if getattr(self, "_closed", False):
            return
        if not getattr(self, "_owns_checkpointer", True):
            return

        self._closed = True
        resource = getattr(self, "_checkpointer_resource", None)
        checkpointer = getattr(self, "_checkpointer", None)
        context = getattr(self, "_checkpointer_context", None)
        self._checkpointer_resource = None
        self._checkpointer = None
        self._checkpointer_context = None

        if resource is not None:
            payload = resource.release()
            if payload is None:
                return
            checkpointer, context = payload

        self._close_checkpointer(checkpointer, context)

    @staticmethod
    def _close_checkpointer(checkpointer: Any, context: Any | None) -> None:
        """Close the saver held by the final lease, surfacing close failures."""
        if context is not None:
            context.__exit__(None, None, None)
        elif checkpointer is None:
            return
        # Finalize WAL so the DB file is self-contained and no stale
        # .wal/.shm files are left behind for the next process.
        elif hasattr(checkpointer, "conn") and checkpointer.conn:
            try:
                checkpointer.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            checkpointer.conn.close()
        elif hasattr(checkpointer, "close"):
            checkpointer.close()
        elif callable(getattr(checkpointer, "__exit__", None)):
            checkpointer.__exit__(None, None, None)

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
    ) -> str | None:
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
    
    def get_current_session_id(self) -> str | None:
        """Get current session ID (thread_id)."""
        return self._current_thread_id
    
    def set_current_session(
        self, 
        session_id: str,
        execution_id: str | None = None,
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
    
    def get_config(self, checkpoint_id: str | None = None) -> dict[str, Any]:
        """Get LangGraph config for current thread."""
        self._ensure_open()
        if not self._current_thread_id:
            raise RuntimeError("No active session. Call initialize_session() first.")
        
        config: dict[str, Any] = {
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
        name: str | None = None,
        metadata: dict[str, Any] | None = None
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
    
    def get_node_boundaries(self, session_id: str) -> list[NodeBoundaryInfo]:
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
    
    def get_node_boundary(self, session_id: str, node_id: str) -> NodeBoundaryInfo | None:
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
        node_id: str | None = None
    ) -> list[CheckpointInfo]:
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
    
    def get_node_rollback_targets(self, session_id: str) -> list[CheckpointInfo]:
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
    
    def execute(self, initial_state: dict[str, Any] | None) -> dict[str, Any]:
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
    
    async def aexecute(self, initial_state: dict[str, Any]) -> dict[str, Any]:
        """Async execution."""
        if not self._compiled_graph:
            raise RuntimeError("Graph not set. Call set_workflow_graph() first.")
        
        config = self.get_config()
        return await self._compiled_graph.ainvoke(initial_state, config)
    
    def stream(self, initial_state: dict[str, Any], stream_mode: str = "updates"):
        """Stream execution events."""
        if not self._compiled_graph:
            raise RuntimeError("Graph not set. Call set_workflow_graph() first.")
        
        config = self.get_config()
        return self._compiled_graph.stream(initial_state, config, stream_mode=stream_mode)
    
    def get_current_state(self) -> dict[str, Any]:
        """Get current state values."""
        if not self._compiled_graph:
            return {}
        
        config = self.get_config()
        snapshot = self._compiled_graph.get_state(config)
        return snapshot.values if snapshot else {}
    
    def get_next_nodes(self) -> list[str]:
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
    
    def update_state(self, values: dict[str, Any], as_node: str | None = None) -> bool:
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
    
    def get_checkpoint_history(self) -> list[dict[str, Any]]:
        """Get full checkpoint history for time travel.
        
        v1.6: Falls back to direct checkpointer query when no compiled graph.
        This enables checkpoint retrieval after reconnection without running workflow.
        """
        if not self._current_thread_id:
            return []

        config = self.get_config()
        history = []
        graph_error: Exception | None = None

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
            except Exception as error:
                graph_error = error
                history.clear()
                logger.warning(
                    "Failed to get history from graph, trying checkpointer: "
                    f"{error}"
                )

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
            except Exception as error:
                if graph_error is not None:
                    error.add_note(
                        "Compiled graph history retrieval also failed: "
                        f"{graph_error}"
                    )
                logger.error(f"Failed to get history from checkpointer: {error}")
                raise
        elif graph_error is not None:
            raise graph_error

        return history
    
    def create_fork(
        self,
        fork_thread_id: str,
        from_checkpoint_id: str | None = None
    ) -> "LangGraphStateAdapter":
        """
        Create a fork adapter for variant execution.

        Uses ``object.__new__`` to avoid the ``__init__`` path which would
        open a **new** checkpointer connection that is immediately discarded.
        The fork shares the parent's checkpointer and graph through a lease.
        The final adapter to close releases the underlying saver.
        """
        if getattr(self, "_closed", False):
            raise RuntimeError("Cannot fork a closed state adapter")

        resource = getattr(self, "_checkpointer_resource", None)
        if resource is not None:
            resource.acquire()

        try:
            fork_adapter = object.__new__(LangGraphStateAdapter)
            fork_adapter._config = self._config
            fork_adapter._checkpointer_context = self._checkpointer_context
            fork_adapter._checkpointer_resource = resource
            fork_adapter._checkpointer = self._checkpointer
            fork_adapter._owns_checkpointer = resource is not None
            fork_adapter._closed = False
            fork_adapter._compiled_graph = self._compiled_graph
            fork_adapter._graph_builder = self._graph_builder
            fork_adapter._current_thread_id = fork_thread_id
            fork_adapter._current_execution_id = None
            fork_adapter._resume_checkpoint_id = None
            fork_adapter._node_boundaries = {fork_thread_id: {}}

            # If forking from specific checkpoint, copy state
            if from_checkpoint_id:
                source_config = self.get_config(checkpoint_id=from_checkpoint_id)
                source_state = self._compiled_graph.get_state(source_config)

                if source_state:
                    fork_config = {"configurable": {"thread_id": fork_thread_id}}
                    self._compiled_graph.update_state(
                        fork_config, source_state.values
                    )

            return fork_adapter
        except BaseException:
            if resource is not None:
                payload = resource.release()
                if payload is not None:
                    self._close_checkpointer(*payload)
            raise


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
