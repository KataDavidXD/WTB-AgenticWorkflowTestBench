"""
State Adapter Interface.

Refactored (2026-01-27): Cleaned interface removing AgentGit-specific methods.
Session is preserved as a DOMAIN CONCEPT - maps to LangGraph thread_id.

Design Philosophy:
==================
- Session = Scope/Context for workflow execution (Domain Concept)
- Session maps 1:1 to LangGraph thread_id
- All IDs are strings (UUIDs) - no more int/str mapping
- Checkpoint = StateSnapshot at super-step (node) level

Implementations:
- InMemoryStateAdapter: For unit testing
- LangGraphStateAdapter: Production (maps to LangGraph checkpointers)

REMOVED (v1.6):
- link_file_commit() - AgentGit-specific file commit linking
- create_branch() - Use fork() in ExecutionController instead
- Tool-level checkpoint concepts (tool_track_position, etc.)
- Numeric ID mapping (_checkpoint_id_map, _numeric_to_lg_id)
"""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from enum import Enum
from dataclasses import dataclass

from ..models.workflow import ExecutionState


class CheckpointTrigger(Enum):
    """What triggered the checkpoint creation."""
    NODE_START = "node_start"       # Before node execution
    NODE_END = "node_end"           # After node execution
    USER_REQUEST = "user_request"   # Manual checkpoint
    AUTO = "auto"                   # System auto-checkpoint (super-step)


@dataclass
class CheckpointInfo:
    """
    Lightweight checkpoint information for listings.
    
    Refactored (v1.6): Uses string IDs throughout.
    """
    id: str                         # Checkpoint ID (UUID string)
    name: Optional[str]             # Human-readable name
    node_id: Optional[str]          # Which node this checkpoint belongs to
    step: int                       # LangGraph super-step number
    trigger_type: CheckpointTrigger
    created_at: str
    is_auto: bool
    

@dataclass
class NodeBoundaryInfo:
    """
    Node boundary information - tracks node execution boundaries.
    
    Refactored (v1.6): Uses string IDs throughout.
    """
    id: str                         # Boundary ID
    node_id: str                    # The workflow node
    entry_checkpoint_id: str        # First checkpoint when entering node
    exit_checkpoint_id: Optional[str]  # Last checkpoint when exiting (rollback target)
    node_status: str                # "started", "completed", "failed"
    started_at: str
    completed_at: Optional[str]


class IStateAdapter(ABC):
    """
    State Adapter Interface - Clean, LangGraph-aligned.
    
    Session is a DOMAIN CONCEPT (scope/context), not just an implementation detail.
    
    Key Changes (v1.6):
    - All IDs are strings (UUIDs)
    - initialize_session() returns str (thread_id)
    - save_checkpoint() returns str (checkpoint_id)
    - Removed AgentGit-specific methods
    
    Session Lifecycle:
    1. initialize_session(execution_id) → session_id (thread_id)
    2. save_checkpoint(state) → checkpoint_id (str)
    3. load_checkpoint(checkpoint_id) → ExecutionState
    4. rollback(checkpoint_id) → ExecutionState
    
    Implementations:
    - InMemoryStateAdapter: For testing
    - LangGraphStateAdapter: For production (maps to checkpointers)
    """
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Session Management
    # ═══════════════════════════════════════════════════════════════════════════
    
    @abstractmethod
    def initialize_session(
        self, 
        execution_id: str,
        initial_state: ExecutionState
    ) -> Optional[str]:
        """
        Initialize a new session (thread context) for execution.
        
        Creates an isolated context for workflow execution. In LangGraph,
        this maps to a thread_id.
        
        Args:
            execution_id: WTB execution ID for correlation
            initial_state: Initial workflow execution state
            
        Returns:
            Session ID (thread_id string), or None if creation failed
        """
        pass
    
    @abstractmethod
    def get_current_session_id(self) -> Optional[str]:
        """Get the current active session ID (thread_id)."""
        pass
    
    @abstractmethod
    def set_current_session(
        self, 
        session_id: str,
        execution_id: Optional[str] = None,
    ) -> bool:
        """
        Set the current active session.
        
        Args:
            session_id: Session ID to activate
            execution_id: Optional execution ID for thread reconstruction
            
        Returns:
            True if session was set successfully
        """
        pass
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Checkpoint Operations
    # ═══════════════════════════════════════════════════════════════════════════
    
    @abstractmethod
    def save_checkpoint(
        self,
        state: ExecutionState,
        node_id: str,
        trigger: CheckpointTrigger,
        name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Save a state checkpoint.
        
        In LangGraph, checkpoints are saved automatically at super-steps.
        This method creates an explicit checkpoint via update_state().
        
        Args:
            state: Current execution state to save
            node_id: Which workflow node this checkpoint belongs to
            trigger: What triggered this checkpoint
            name: Optional human-readable name
            metadata: Additional metadata
            
        Returns:
            Checkpoint ID (UUID string)
            
        Raises:
            RuntimeError: If no active session
        """
        pass
    
    @abstractmethod
    def load_checkpoint(self, checkpoint_id: str) -> ExecutionState:
        """
        Load a checkpoint's state by ID.
        
        Args:
            checkpoint_id: Checkpoint ID (UUID string)
            
        Returns:
            ExecutionState restored from the checkpoint
            
        Raises:
            ValueError: If checkpoint not found
        """
        pass
    
    @abstractmethod
    def rollback(self, to_checkpoint_id: str) -> ExecutionState:
        """
        Rollback to a specific checkpoint.
        
        Restores state to the specified checkpoint. In LangGraph,
        this uses get_state(config + checkpoint_id).
        
        Args:
            to_checkpoint_id: Checkpoint ID to rollback to
            
        Returns:
            Restored execution state
            
        Raises:
            ValueError: If checkpoint not found
        """
        pass
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Node Boundary Operations
    # ═══════════════════════════════════════════════════════════════════════════
    
    @abstractmethod
    def mark_node_started(self, node_id: str, entry_checkpoint_id: str) -> str:
        """
        Mark that a node has started executing.
        
        Args:
            node_id: The workflow node ID
            entry_checkpoint_id: First checkpoint created in this node
            
        Returns:
            Node boundary ID
        """
        pass
    
    @abstractmethod
    def mark_node_completed(
        self, 
        node_id: str, 
        exit_checkpoint_id: str,
    ) -> bool:
        """
        Mark that a node has completed.
        
        Args:
            node_id: The workflow node ID
            exit_checkpoint_id: Last checkpoint in this node (rollback target)
            
        Returns:
            True if successful
        """
        pass
    
    @abstractmethod
    def mark_node_failed(self, node_id: str, error_message: str) -> bool:
        """
        Mark that a node has failed.
        
        Args:
            node_id: The workflow node ID
            error_message: Error description
            
        Returns:
            True if successful
        """
        pass
    
    @abstractmethod
    def get_node_boundaries(self, session_id: str) -> List[NodeBoundaryInfo]:
        """
        Get all node boundaries for a session.
        
        Args:
            session_id: Session to query
            
        Returns:
            List of NodeBoundaryInfo objects
        """
        pass
    
    @abstractmethod
    def get_node_boundary(self, session_id: str, node_id: str) -> Optional[NodeBoundaryInfo]:
        """
        Get a specific node boundary.
        
        Args:
            session_id: Session to query
            node_id: Node to look up
            
        Returns:
            NodeBoundaryInfo or None if not found
        """
        pass
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Query Operations
    # ═══════════════════════════════════════════════════════════════════════════
    
    @abstractmethod
    def get_checkpoints(
        self, 
        session_id: str,
        node_id: Optional[str] = None
    ) -> List[CheckpointInfo]:
        """
        Get checkpoints for a session.
        
        Args:
            session_id: Session to query
            node_id: Optional filter by node
            
        Returns:
            List of CheckpointInfo objects
        """
        pass
    
    @abstractmethod
    def get_node_rollback_targets(self, session_id: str) -> List[CheckpointInfo]:
        """
        Get valid node-level rollback targets.
        
        Returns the exit_checkpoint for each completed node.
        
        Args:
            session_id: Session to query
            
        Returns:
            List of CheckpointInfo (one per completed node)
        """
        pass
    
    @abstractmethod
    def cleanup(self, session_id: str, keep_latest: int = 5) -> int:
        """
        Cleanup old checkpoints, keeping only the most recent.
        
        Args:
            session_id: Session to cleanup
            keep_latest: Number of recent checkpoints to keep
            
        Returns:
            Number of checkpoints removed
        """
        pass
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Extended Capabilities (Optional)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_checkpoint_history(self) -> List[Dict[str, Any]]:
        """
        Get full checkpoint history with detailed metadata (time-travel support).
        
        Returns:
            List of checkpoint dicts with full state and metadata.
            Returns empty list if not supported.
        """
        return []
    
    def update_state(
        self, 
        values: Dict[str, Any], 
        as_node: Optional[str] = None
    ) -> bool:
        """
        Update state mid-execution (human-in-the-loop support).
        
        Args:
            values: Values to update in state
            as_node: Attribute the update to this node
            
        Returns:
            True if update successful, False if not supported
        """
        return False
    
    def get_current_state(self) -> Dict[str, Any]:
        """
        Get current state as dictionary.
        
        Returns:
            Current state dict, or empty dict if not supported
        """
        return {}
    
    def get_next_nodes(self) -> List[str]:
        """
        Get next available nodes from current state.
        
        Returns:
            List of next node IDs, empty if at end or not supported
        """
        return []
    
    def supports_graph_execution(self) -> bool:
        """
        Check if this adapter supports LangGraph-style graph execution.
        
        Adapters that support graph execution must also implement:
        - execute(initial_state) -> Dict
        - set_workflow_graph(graph, force_recompile) -> None
        - has_graph() -> bool
        - get_checkpointer() -> BaseCheckpointSaver
        
        Returns:
            True if graph execution is supported
        """
        return False
    
    def supports_streaming(self) -> bool:
        """
        Check if this adapter supports event streaming.
        
        Returns:
            True if streaming is supported
        """
        return False
    
    def supports_time_travel(self) -> bool:
        """
        Check if this adapter supports time-travel (rich history).
        
        Returns:
            True if time-travel is supported
        """
        return False

    def close(self) -> None:
        """Release any resources held by the adapter (e.g., DB connections)."""
        pass
