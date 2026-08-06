"""
In-Memory State Adapter Implementation.

Refactored (v1.6): Uses string IDs throughout, matches clean IStateAdapter interface.
"""

from typing import Optional, Dict, Any, List
from datetime import datetime
from copy import deepcopy
import uuid

from wtb.domain.interfaces.state_adapter import (
    IStateAdapter,
    CheckpointTrigger,
    CheckpointInfo,
    NodeBoundaryInfo,
)
from wtb.domain.models import ExecutionState


class InMemoryCheckpoint:
    """Internal checkpoint representation (v1.6: string IDs)."""
    def __init__(
        self,
        id: str,
        session_id: str,
        state: ExecutionState,
        node_id: str,
        trigger: CheckpointTrigger,
        name: Optional[str],
        metadata: Dict[str, Any],
        step: int,
    ):
        self.id = id
        self.session_id = session_id
        self.state = deepcopy(state)
        self.node_id = node_id
        self.trigger = trigger
        self.name = name
        self.metadata = metadata
        self.step = step
        self.created_at = datetime.now().isoformat()
        self.is_auto = trigger != CheckpointTrigger.USER_REQUEST


class InMemoryNodeBoundary:
    """Internal node boundary representation (v1.6: string IDs)."""
    def __init__(
        self,
        id: str,
        execution_id: str,
        session_id: str,
        node_id: str,
        entry_checkpoint_id: str,
    ):
        self.id = id
        self.execution_id = execution_id
        self.session_id = session_id
        self.node_id = node_id
        self.entry_checkpoint_id = entry_checkpoint_id
        self.exit_checkpoint_id: Optional[str] = None
        self.node_status = "started"
        self.started_at = datetime.now().isoformat()
        self.completed_at: Optional[str] = None
        self.error_message: Optional[str] = None


class InMemoryStateAdapter(IStateAdapter):
    """
    In-memory implementation of IStateAdapter.
    
    Refactored (v1.6):
    - All IDs are strings (UUIDs)
    - Matches clean IStateAdapter interface
    - No AgentGit-specific methods
    
    Useful for:
    - Unit testing
    - Development without database setup
    - Prototyping
    
    Limitations:
    - No persistence between runs
    - No cross-process sharing
    - Memory grows with checkpoints
    """
    
    def __init__(self):
        # Storage (v1.6: keyed by string IDs)
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._checkpoints: Dict[str, InMemoryCheckpoint] = {}
        self._boundaries: Dict[str, InMemoryNodeBoundary] = {}
        
        # Current session tracking
        self._current_session_id: Optional[str] = None
        self._current_execution_id: Optional[str] = None
        self._step_counter = 0
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Session Management
    # ═══════════════════════════════════════════════════════════════════════════
    
    def initialize_session(
        self, 
        execution_id: str,
        initial_state: ExecutionState
    ) -> Optional[str]:
        """Initialize a new session (thread context)."""
        # Session ID = thread_id format (matches LangGraph)
        session_id = f"wtb-{execution_id}"
        
        self._sessions[session_id] = {
            "id": session_id,
            "execution_id": execution_id,
            "initial_state": deepcopy(initial_state),
            "created_at": datetime.now().isoformat(),
        }
        
        self._current_session_id = session_id
        self._current_execution_id = execution_id
        self._step_counter = 0
        
        return session_id
    
    def get_current_session_id(self) -> Optional[str]:
        """Get current session ID (thread_id)."""
        return self._current_session_id
    
    def set_current_session(
        self, 
        session_id: str,
        execution_id: Optional[str] = None,
    ) -> bool:
        """Set current session."""
        if session_id in self._sessions:
            self._current_session_id = session_id
            if execution_id:
                self._current_execution_id = execution_id
            return True
        
        # Support reconstruction from execution_id (ACID: Durability)
        if execution_id:
            reconstructed_id = f"wtb-{execution_id}"
            if reconstructed_id in self._sessions:
                self._current_session_id = reconstructed_id
                self._current_execution_id = execution_id
                return True
        
        return False
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Checkpoint Operations
    # ═══════════════════════════════════════════════════════════════════════════
    
    def save_checkpoint(
        self,
        state: ExecutionState,
        node_id: str,
        trigger: CheckpointTrigger,
        name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """Save a state checkpoint. Returns checkpoint_id (UUID string)."""
        if self._current_session_id is None:
            raise RuntimeError("No active session")
        
        self._step_counter += 1
        
        checkpoint_id = str(uuid.uuid4())
        
        checkpoint = InMemoryCheckpoint(
            id=checkpoint_id,
            session_id=self._current_session_id,
            state=state,
            node_id=node_id,
            trigger=trigger,
            name=name,
            metadata=metadata or {},
            step=self._step_counter,
        )
        
        self._checkpoints[checkpoint_id] = checkpoint
        return checkpoint_id

    def _require_owned_checkpoint(self, checkpoint_id: str) -> InMemoryCheckpoint:
        """Return a checkpoint only when it belongs to the active session."""
        checkpoint = self._checkpoints.get(checkpoint_id)
        if checkpoint is None:
            raise ValueError(f"Checkpoint {checkpoint_id} not found")
        if self._current_session_id is None:
            raise RuntimeError("No active session")
        if checkpoint.session_id != self._current_session_id:
            raise ValueError(
                f"Checkpoint {checkpoint_id} does not belong to active session"
            )
        return checkpoint
    
    def load_checkpoint(self, checkpoint_id: str) -> ExecutionState:
        """Load a checkpoint's state."""
        checkpoint = self._require_owned_checkpoint(checkpoint_id)
        return deepcopy(checkpoint.state)
    
    def rollback(self, to_checkpoint_id: str) -> ExecutionState:
        """Rollback to a specific checkpoint."""
        checkpoint = self._require_owned_checkpoint(to_checkpoint_id)
        
        # Reset step counter to checkpoint's step
        self._step_counter = checkpoint.step
        
        return deepcopy(checkpoint.state)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Node Boundary Operations
    # ═══════════════════════════════════════════════════════════════════════════
    
    def mark_node_started(self, node_id: str, entry_checkpoint_id: str) -> str:
        """Mark node as started. Returns boundary ID."""
        if self._current_session_id is None:
            raise RuntimeError("No active session")
        
        boundary_id = str(uuid.uuid4())
        
        boundary = InMemoryNodeBoundary(
            id=boundary_id,
            execution_id=self._current_execution_id or "",
            session_id=self._current_session_id,
            node_id=node_id,
            entry_checkpoint_id=entry_checkpoint_id,
        )
        
        self._boundaries[boundary_id] = boundary
        return boundary_id
    
    def mark_node_completed(
        self, 
        node_id: str, 
        exit_checkpoint_id: str,
    ) -> bool:
        """Mark node as completed."""
        boundary = self._find_boundary(node_id)
        if boundary:
            boundary.exit_checkpoint_id = exit_checkpoint_id
            boundary.node_status = "completed"
            boundary.completed_at = datetime.now().isoformat()
            return True
        return False
    
    def mark_node_failed(self, node_id: str, error_message: str) -> bool:
        """Mark node as failed."""
        boundary = self._find_boundary(node_id)
        if boundary:
            boundary.node_status = "failed"
            boundary.completed_at = datetime.now().isoformat()
            boundary.error_message = error_message
            return True
        return False
    
    def _find_boundary(self, node_id: str) -> Optional[InMemoryNodeBoundary]:
        """Find boundary by node ID in current session."""
        for boundary in self._boundaries.values():
            if (boundary.session_id == self._current_session_id and 
                boundary.node_id == node_id):
                return boundary
        return None
    
    def get_node_boundaries(self, session_id: str) -> List[NodeBoundaryInfo]:
        """Get all node boundaries for a session."""
        return [
            NodeBoundaryInfo(
                id=b.id,
                node_id=b.node_id,
                entry_checkpoint_id=b.entry_checkpoint_id,
                exit_checkpoint_id=b.exit_checkpoint_id,
                node_status=b.node_status,
                started_at=b.started_at,
                completed_at=b.completed_at,
            )
            for b in self._boundaries.values()
            if b.session_id == session_id
        ]
    
    def get_node_boundary(self, session_id: str, node_id: str) -> Optional[NodeBoundaryInfo]:
        """Get a specific node boundary."""
        for b in self._boundaries.values():
            if b.session_id == session_id and b.node_id == node_id:
                return NodeBoundaryInfo(
                    id=b.id,
                    node_id=b.node_id,
                    entry_checkpoint_id=b.entry_checkpoint_id,
                    exit_checkpoint_id=b.exit_checkpoint_id,
                    node_status=b.node_status,
                    started_at=b.started_at,
                    completed_at=b.completed_at,
                )
        return None
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Query Operations
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_checkpoints(
        self, 
        session_id: str,
        node_id: Optional[str] = None
    ) -> List[CheckpointInfo]:
        """Get checkpoints for a session."""
        result = []
        for cp in self._checkpoints.values():
            if cp.session_id != session_id:
                continue
            if node_id and cp.node_id != node_id:
                continue
            
            result.append(CheckpointInfo(
                id=cp.id,
                name=cp.name,
                node_id=cp.node_id,
                step=cp.step,
                trigger_type=cp.trigger,
                created_at=cp.created_at,
                is_auto=cp.is_auto,
            ))
        
        return sorted(result, key=lambda c: c.step)
    
    def get_node_rollback_targets(self, session_id: str) -> List[CheckpointInfo]:
        """Get valid node-level rollback targets."""
        result = []
        for b in self._boundaries.values():
            if b.session_id == session_id and b.node_status == "completed" and b.exit_checkpoint_id:
                cp = self._checkpoints.get(b.exit_checkpoint_id)
                if cp:
                    result.append(CheckpointInfo(
                        id=cp.id,
                        name=cp.name or f"Node: {b.node_id}",
                        node_id=b.node_id,
                        step=cp.step,
                        trigger_type=cp.trigger,
                        created_at=cp.created_at,
                        is_auto=cp.is_auto,
                    ))
        return result
    
    def cleanup(self, session_id: str, keep_latest: int = 5) -> int:
        """Cleanup old checkpoints."""
        session_cps = [
            cp for cp in self._checkpoints.values()
            if cp.session_id == session_id and cp.is_auto
        ]
        
        # Sort by creation time (newest first)
        session_cps.sort(key=lambda c: c.created_at, reverse=True)
        
        # Remove old ones
        to_delete = session_cps[keep_latest:]
        for cp in to_delete:
            del self._checkpoints[cp.id]
        
        return len(to_delete)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Extended Capabilities
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_checkpoint_history(self) -> List[Dict[str, Any]]:
        """Get checkpoint history for current session."""
        if not self._current_session_id:
            return []
        
        history = []
        for cp in sorted(
            [c for c in self._checkpoints.values() if c.session_id == self._current_session_id],
            key=lambda x: x.step,
            reverse=True
        ):
            history.append({
                "checkpoint_id": cp.id,
                "step": cp.step,
                "source": cp.node_id,
                "writes": {cp.node_id: {}},
                "values": cp.state.to_dict() if cp.state else {},
                "next": [],
                "created_at": cp.created_at,
            })
        return history
    
    def supports_time_travel(self) -> bool:
        """In-memory adapter supports time-travel."""
        return True
    
    def supports_streaming(self) -> bool:
        """In-memory adapter does not support streaming."""
        return False
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Testing Utilities
    # ═══════════════════════════════════════════════════════════════════════════
    
    def reset(self):
        """Reset all state (for testing)."""
        self._sessions.clear()
        self._checkpoints.clear()
        self._boundaries.clear()
        self._current_session_id = None
        self._current_execution_id = None
        self._step_counter = 0
    
    def get_checkpoint_count(self) -> int:
        """Get total checkpoint count (for testing)."""
        return len(self._checkpoints)
    
    def get_session_count(self) -> int:
        """Get total session count (for testing)."""
        return len(self._sessions)
