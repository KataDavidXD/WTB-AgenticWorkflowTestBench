"""
Domain Checkpoint Models.

Contains domain-centric checkpoint concepts following DDD principles.
These models belong in the domain layer - infrastructure adapters only handle persistence.

Design Philosophy:
==================
- Checkpoint = Domain representation of state snapshot (node-level in LangGraph)
- CheckpointId = Value object for checkpoint identifier (string-based)
- ExecutionHistory = Aggregate root for checkpoint management with business logic

Key Principle:
"The domain expresses business concepts. Checkpoint, Rollback, and ExecutionHistory
are domain concepts - adapters only handle persistence mechanics."
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class CheckpointId:
    """
    Value object for checkpoint identifier.
    
    Immutable, comparable, can be used as dict key.
    Uses string-based IDs (LangGraph uses UUIDs).
    
    DDD Pattern: Value Object
    - Identity based on value, not reference
    - Immutable (frozen=True)
    - Encapsulates validation
    """
    value: str
    
    def __post_init__(self):
        """Validate checkpoint ID."""
        if not self.value:
            raise ValueError("CheckpointId cannot be empty")
    
    def __str__(self) -> str:
        return self.value
    
    def __hash__(self) -> int:
        return hash(self.value)
    
    def __eq__(self, other: object) -> bool:
        if isinstance(other, CheckpointId):
            return self.value == other.value
        if isinstance(other, str):
            return self.value == other
        return False


@dataclass(frozen=True)
class Checkpoint:
    """
    Domain entity representing a point-in-time state snapshot.
    
    This is a DOMAIN concept - how checkpoints are stored (LangGraph,
    AgentGit, etc.) is an infrastructure concern.
    
    In LangGraph, checkpoints are created at each "super-step" (node completion).
    This domain model abstracts that concept.
    
    DDD Pattern: Entity (identified by CheckpointId)
    """
    id: CheckpointId
    execution_id: str
    step: int                              # Monotonic step number
    node_writes: dict[str, Any]            # Which node wrote to this checkpoint
    next_nodes: list[str]                  # Nodes to execute next
    state_values: dict[str, Any]           # State at this checkpoint
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)
    
    @property
    def completed_node(self) -> str | None:
        """
        Get the node that completed at this checkpoint (if any).
        
        In LangGraph, node_writes contains the node name as key.
        """
        if self.node_writes:
            # Filter out special nodes
            nodes = [n for n in self.node_writes.keys() 
                    if not n.startswith("__")]
            return nodes[0] if nodes else None
        return None
    
    @property
    def is_terminal(self) -> bool:
        """Is this a terminal checkpoint (no next nodes)?"""
        return len(self.next_nodes) == 0
    
    @property
    def is_node_completion(self) -> bool:
        """Does this checkpoint represent a node completion?"""
        return bool(self.node_writes) and self.completed_node is not None
    
    def has_node_in_next(self, node_id: str) -> bool:
        """Check if node is about to execute."""
        return node_id in self.next_nodes
    
    def has_node_in_writes(self, node_id: str) -> bool:
        """Check if node completed at this checkpoint."""
        return node_id in self.node_writes
    
    def get_node_output(self, node_id: str) -> Any | None:
        """Get the output written by a specific node."""
        return self.node_writes.get(node_id)
    
    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "id": str(self.id),
            "execution_id": self.execution_id,
            "step": self.step,
            "node_writes": self.node_writes,
            "next_nodes": self.next_nodes,
            "state_values": self.state_values,
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Checkpoint":
        """Deserialize from dictionary."""
        created_at = data.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        elif created_at is None:
            created_at = datetime.now()
            
        return cls(
            id=CheckpointId(data["id"]),
            execution_id=data["execution_id"],
            step=data.get("step", 0),
            node_writes=data.get("node_writes", {}),
            next_nodes=data.get("next_nodes", []),
            state_values=data.get("state_values", {}),
            created_at=created_at,
            metadata=data.get("metadata", {}),
        )


@dataclass
class ExecutionHistory:
    """
    Aggregate root for checkpoint management.
    
    Contains all DOMAIN LOGIC for:
    - Querying checkpoints
    - Computing node boundaries
    - Finding rollback targets
    - Linearizing execution path
    
    This is the "Checkpoint knowledge" that belongs in the domain.
    Infrastructure adapters (LangGraphCheckpointStore) only load/save data.
    
    DDD Pattern: Aggregate Root
    - Encapsulates checkpoint collection
    - Provides consistent query interface
    - Contains business logic for checkpoint operations
    """
    execution_id: str
    checkpoints: list[Checkpoint] = field(default_factory=list)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Query Operations (Domain Logic)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_checkpoint(self, checkpoint_id: CheckpointId) -> Checkpoint | None:
        """Get checkpoint by ID."""
        for cp in self.checkpoints:
            if cp.id == checkpoint_id:
                return cp
        return None
    
    def get_checkpoint_by_id(self, checkpoint_id: str) -> Checkpoint | None:
        """Get checkpoint by string ID (convenience method)."""
        return self.get_checkpoint(CheckpointId(checkpoint_id))
    
    def get_latest_checkpoint(self) -> Checkpoint | None:
        """Get most recent checkpoint."""
        if not self.checkpoints:
            return None
        return max(self.checkpoints, key=lambda cp: cp.step)
    
    def get_checkpoint_for_node(self, node_id: str) -> Checkpoint | None:
        """
        Get checkpoint where node completed (exit checkpoint).
        
        Domain Logic: Find the checkpoint where node_id appears in node_writes.
        """
        sorted_cps = sorted(self.checkpoints, key=lambda cp: cp.step, reverse=True)
        for cp in sorted_cps:  # Most recent first
            if cp.has_node_in_writes(node_id):
                return cp
        return None
    
    def get_checkpoints_by_step_range(
        self, 
        start_step: int, 
        end_step: int | None = None
    ) -> list[Checkpoint]:
        """Get checkpoints within a step range."""
        result = []
        for cp in self.checkpoints:
            if cp.step >= start_step:
                if end_step is None or cp.step <= end_step:
                    result.append(cp)
        return sorted(result, key=lambda cp: cp.step)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Node Boundary Operations (Domain Logic)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_node_entry_checkpoint(self, node_id: str) -> Checkpoint | None:
        """
        Get the checkpoint where a node was about to execute.
        
        Domain Logic: Find checkpoint where node_id appears in next_nodes.
        """
        sorted_cps = sorted(self.checkpoints, key=lambda cp: cp.step)
        for cp in sorted_cps:
            if cp.has_node_in_next(node_id):
                return cp
        return None
    
    def get_node_exit_checkpoint(self, node_id: str) -> Checkpoint | None:
        """
        Get the checkpoint where a node completed.
        
        Alias for get_checkpoint_for_node().
        """
        return self.get_checkpoint_for_node(node_id)
    
    def get_completed_nodes(self) -> list[str]:
        """
        Get list of completed node IDs in execution order.
        
        Domain Logic: Derive sequence from checkpoint history.
        """
        completed = []
        for cp in sorted(self.checkpoints, key=lambda cp: cp.step):
            if cp.completed_node and cp.completed_node not in completed:
                completed.append(cp.completed_node)
        return completed
    
    def get_unique_nodes(self) -> list[str]:
        """
        Get all unique nodes mentioned in checkpoint history.
        
        Includes both executed and pending nodes.
        """
        nodes = set()
        for cp in self.checkpoints:
            nodes.update(cp.next_nodes)
            nodes.update(cp.node_writes.keys())
        
        # Remove special nodes
        nodes.discard("__start__")
        nodes.discard("__end__")
        
        return list(nodes)
    
    def is_node_completed(self, node_id: str) -> bool:
        """Check if a node has completed execution."""
        return self.get_checkpoint_for_node(node_id) is not None
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Rollback Operations (Domain Logic)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_rollback_target(self, to_node_id: str) -> Checkpoint | None:
        """
        Get checkpoint to rollback to for resuming after a node.
        
        Domain Logic: Rollback target is the checkpoint where node completed.
        """
        return self.get_checkpoint_for_node(to_node_id)
    
    def get_rollback_targets(self) -> list[Checkpoint]:
        """
        Get all valid rollback targets (completed node exit checkpoints).
        
        Domain Logic: Only checkpoints with node_writes are rollback targets.
        """
        targets = []
        for cp in self.checkpoints:
            if cp.is_node_completion:
                targets.append(cp)
        return sorted(targets, key=lambda cp: cp.step, reverse=True)
    
    def can_rollback_to(self, checkpoint_id: CheckpointId) -> bool:
        """
        Check if rollback to checkpoint is valid.
        
        Domain Logic: Must be a node completion checkpoint.
        """
        cp = self.get_checkpoint(checkpoint_id)
        return cp is not None and cp.is_node_completion
    
    def get_nodes_to_revert(
        self, 
        from_checkpoint_id: CheckpointId, 
        to_checkpoint_id: CheckpointId
    ) -> list[str]:
        """
        Get list of nodes that would be reverted in a rollback.
        
        Domain Logic: Find all completed nodes between from and to checkpoints.
        """
        from_cp = self.get_checkpoint(from_checkpoint_id)
        to_cp = self.get_checkpoint(to_checkpoint_id)
        
        if not from_cp or not to_cp:
            return []
        
        if to_cp.step >= from_cp.step:
            return []  # Can't rollback forward
        
        reverted = []
        for cp in self.checkpoints:
            if to_cp.step < cp.step <= from_cp.step:
                if cp.completed_node:
                    reverted.append(cp.completed_node)
        
        return reverted
    
    # ═══════════════════════════════════════════════════════════════════════════
    # History Operations (Domain Logic)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_execution_path(self) -> list[str]:
        """
        Get linearized execution path (node sequence).
        
        Domain Logic: Derive execution sequence from checkpoint history.
        """
        path = []
        for cp in sorted(self.checkpoints, key=lambda cp: cp.step):
            if cp.completed_node:
                path.append(cp.completed_node)
        return path
    
    def get_state_at_step(self, step: int) -> dict[str, Any] | None:
        """Get state values at a specific step."""
        for cp in self.checkpoints:
            if cp.step == step:
                return cp.state_values
        return None
    
    def add_checkpoint(self, checkpoint: Checkpoint) -> None:
        """Add a checkpoint to history."""
        if checkpoint.execution_id != self.execution_id:
            raise ValueError(
                f"Checkpoint execution_id {checkpoint.execution_id} "
                f"does not match history {self.execution_id}"
            )
        self.checkpoints.append(checkpoint)
    
    def __iter__(self) -> Iterator[Checkpoint]:
        """Iterate checkpoints in step order."""
        return iter(sorted(self.checkpoints, key=lambda cp: cp.step))
    
    def __len__(self) -> int:
        return len(self.checkpoints)
    
    def __bool__(self) -> bool:
        """History is truthy if it has any checkpoints."""
        return len(self.checkpoints) > 0
    
    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "execution_id": self.execution_id,
            "checkpoints": [cp.to_dict() for cp in self.checkpoints],
            "completed_nodes": self.get_completed_nodes(),
            "execution_path": self.get_execution_path(),
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionHistory":
        """Deserialize from dictionary."""
        checkpoints = [
            Checkpoint.from_dict(cp_data) 
            for cp_data in data.get("checkpoints", [])
        ]
        return cls(
            execution_id=data["execution_id"],
            checkpoints=checkpoints,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Domain Exceptions
# ═══════════════════════════════════════════════════════════════════════════════

class CheckpointNotFoundError(Exception):
    """Raised when a checkpoint is not found."""
    def __init__(self, checkpoint_id: CheckpointId):
        self.checkpoint_id = checkpoint_id
        super().__init__(f"Checkpoint {checkpoint_id} not found")


class InvalidRollbackTargetError(Exception):
    """Raised when attempting to rollback to an invalid checkpoint."""
    def __init__(self, checkpoint_id: CheckpointId, reason: str):
        self.checkpoint_id = checkpoint_id
        self.reason = reason
        super().__init__(f"Cannot rollback to {checkpoint_id}: {reason}")


class ExecutionHistoryError(Exception):
    """Base exception for execution history operations."""
    pass
