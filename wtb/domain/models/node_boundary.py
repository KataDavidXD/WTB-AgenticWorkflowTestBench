"""
Node Boundary Domain Model.

Represents a marker pointing to checkpoints at node boundaries.
NOT a separate checkpoint - just references to entry/exit checkpoints.

Design Philosophy (Updated 2026-01-15 - DDD Compliant):
- Checkpoint = Atomic state snapshot at node level (LangGraph super-step)
- Node Boundary = Marker pointing to checkpoints (NOT a duplicate checkpoint)
- Rollback is unified: "node rollback" = rollback to exit_checkpoint_id
- Uses CheckpointId value object (string-based, LangGraph compatible)

DDD Compliance:
- Removed internal_session_id (AgentGit-specific)
- Removed tool_count, checkpoint_count (LangGraph doesn't track tools)
- Changed checkpoint IDs from int to Optional[str] (CheckpointId compatible)
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar, Optional

if TYPE_CHECKING:
    from .checkpoint import CheckpointId


class NodeStatus(str, Enum):
    """Lifecycle status of a node boundary (WTB execution)."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class NodeBoundary:
    """
    Entity - Marker for node execution boundaries.
    
    Points to the first and last checkpoints within a node's execution.
    Used for node-level rollback (which is just rollback to exit_checkpoint).
    
    Stored in WTB's own database (wtb_node_boundaries).
    
    DDD Changes (2026-01-15):
    - entry_checkpoint_id: Now Optional[str] (LangGraph UUID format)
    - exit_checkpoint_id: Now Optional[str] (LangGraph UUID format)
    - Removed: internal_session_id, tool_count, checkpoint_count
    """
    # Identity
    id: int | None = None  # Auto-assigned by DB
    
    # WTB Execution Context
    execution_id: str = ""  # WTB execution UUID
    
    # Node Identification
    node_id: str = ""  # Workflow node name
    
    # Checkpoint Pointers (string-based, LangGraph compatible)
    entry_checkpoint_id: str | None = None  # Checkpoint ID when entering node
    exit_checkpoint_id: str | None = None   # Checkpoint ID when exiting (rollback target)
    
    # Node Execution Status
    node_status: NodeStatus = field(default=NodeStatus.PENDING)
    
    # Timing
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    
    # Error Info
    error_message: str | None = None
    error_details: dict[str, Any] | None = None
    
    _VALID_TRANSITIONS: ClassVar[dict[NodeStatus, set[NodeStatus]]] = {
        NodeStatus.PENDING: {NodeStatus.RUNNING, NodeStatus.SKIPPED},
        NodeStatus.RUNNING: {NodeStatus.COMPLETED, NodeStatus.FAILED, NodeStatus.SKIPPED},
        NodeStatus.COMPLETED: set(),
        NodeStatus.FAILED: set(),
        NodeStatus.SKIPPED: set(),
    }

    def __post_init__(self) -> None:
        if type(self.node_status) is str:
            raw = self.node_status
            if raw == "started":
                self.node_status = NodeStatus.RUNNING
            else:
                self.node_status = NodeStatus(raw)

    def _validate_transition(self, target: NodeStatus) -> None:
        allowed = self._VALID_TRANSITIONS.get(self.node_status, set())
        if target not in allowed:
            raise ValueError(
                f"Invalid node transition: {self.node_status} -> {target}"
            )

    def start(self, entry_checkpoint_id: str) -> None:
        """Mark node as started (running)."""
        self._validate_transition(NodeStatus.RUNNING)
        self.node_status = NodeStatus.RUNNING
        self.entry_checkpoint_id = entry_checkpoint_id
        self.started_at = datetime.now()
    
    def complete(self, exit_checkpoint_id: str) -> None:
        """Mark node as completed."""
        self._validate_transition(NodeStatus.COMPLETED)
        self.node_status = NodeStatus.COMPLETED
        self.exit_checkpoint_id = exit_checkpoint_id
        self.completed_at = datetime.now()
        if self.started_at:
            self.duration_ms = int((self.completed_at - self.started_at).total_seconds() * 1000)
    
    def fail(self, error_message: str, error_details: dict[str, Any] | None = None) -> None:
        """Mark node as failed."""
        self._validate_transition(NodeStatus.FAILED)
        self.node_status = NodeStatus.FAILED
        self.completed_at = datetime.now()
        self.error_message = error_message
        self.error_details = error_details
        if self.started_at:
            self.duration_ms = int((self.completed_at - self.started_at).total_seconds() * 1000)
    
    def skip(self, reason: str = "") -> None:
        """Mark node as skipped."""
        self._validate_transition(NodeStatus.SKIPPED)
        self.node_status = NodeStatus.SKIPPED
        self.completed_at = datetime.now()
        if reason:
            self.error_message = f"Skipped: {reason}"
    
    def is_pending(self) -> bool:
        """Check if node has not started."""
        return self.node_status == NodeStatus.PENDING

    def is_running(self) -> bool:
        """Check if node is currently executing."""
        return self.node_status == NodeStatus.RUNNING

    def is_completed(self) -> bool:
        """Check if node completed successfully."""
        return self.node_status == NodeStatus.COMPLETED

    def is_failed(self) -> bool:
        """Check if node failed."""
        return self.node_status == NodeStatus.FAILED

    def is_rollback_target(self) -> bool:
        """Check if this boundary can be used as a rollback target."""
        return self.node_status == NodeStatus.COMPLETED and self.exit_checkpoint_id is not None
    
    def get_entry_checkpoint_id_value(self) -> Optional["CheckpointId"]:
        """Get entry checkpoint as CheckpointId value object."""
        from .checkpoint import CheckpointId
        if self.entry_checkpoint_id:
            return CheckpointId(self.entry_checkpoint_id)
        return None
    
    def get_exit_checkpoint_id_value(self) -> Optional["CheckpointId"]:
        """Get exit checkpoint as CheckpointId value object."""
        from .checkpoint import CheckpointId
        if self.exit_checkpoint_id:
            return CheckpointId(self.exit_checkpoint_id)
        return None
    
    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "id": self.id,
            "execution_id": self.execution_id,
            "node_id": self.node_id,
            "entry_checkpoint_id": self.entry_checkpoint_id,
            "exit_checkpoint_id": self.exit_checkpoint_id,
            "node_status": self.node_status.value,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_ms": self.duration_ms,
            "error_message": self.error_message,
            "error_details": self.error_details,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NodeBoundary":
        """Deserialize from dictionary."""
        started_at = None
        completed_at = None
        
        if data.get("started_at"):
            started_at = datetime.fromisoformat(data["started_at"])
        if data.get("completed_at"):
            completed_at = datetime.fromisoformat(data["completed_at"])
        
        return cls(
            id=data.get("id"),
            execution_id=data.get("execution_id", ""),
            node_id=data.get("node_id", ""),
            entry_checkpoint_id=data.get("entry_checkpoint_id"),
            exit_checkpoint_id=data.get("exit_checkpoint_id"),
            node_status=data.get("node_status", NodeStatus.PENDING.value),
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=data.get("duration_ms"),
            error_message=data.get("error_message"),
            error_details=data.get("error_details"),
        )
    
    @classmethod
    def create_for_node(cls, execution_id: str, node_id: str) -> "NodeBoundary":
        """Factory method to create a new pending node boundary."""
        return cls(
            execution_id=execution_id,
            node_id=node_id,
            node_status=NodeStatus.PENDING,
        )

