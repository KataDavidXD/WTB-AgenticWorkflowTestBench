"""
Checkpoint-related domain events.

Published during checkpoint and rollback operations.

Updated 2026-01-15 (DDD Compliance):
- Uses CheckpointId value object instead of int
- Uses execution_id instead of session_id
- Removed tool_track_position (LangGraph doesn't track tools)
- Added proper domain event structure

Updated 2026-01-27 (Event Unification - HIGH-001):
- CheckpointEvent now has same interface as WTBEvent (event_id, timestamp, event_type)
- Consistent event handling across the system
- Frozen for immutability (checkpoint events should not be mutated)
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class CheckpointEvent:
    """
    Base class for checkpoint events.
    
    Has the same interface as WTBEvent (event_id, timestamp, event_type)
    for consistent event handling across the system.
    
    All checkpoint events are immutable (frozen=True) to ensure
    event integrity in event-driven architectures.
    
    Note: Cannot inherit from WTBEvent because frozen dataclass
    cannot inherit from non-frozen. Instead, we implement the same interface.
    """
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=datetime.now)
    execution_id: str = ""
    
    @property
    def event_type(self) -> str:
        """Event type name (same interface as WTBEvent)."""
        return self.__class__.__name__


@dataclass(frozen=True)
class CheckpointCreated(CheckpointEvent):
    """
    Domain event: checkpoint was created.
    
    Published when a checkpoint is saved at a node boundary.
    In LangGraph, this corresponds to a super-step completion.
    """
    checkpoint_id: str = ""  # CheckpointId value (string)
    step: int = 0
    node_writes: dict[str, Any] = field(default_factory=dict)
    completed_node: str | None = None
    next_nodes: list[str] = field(default_factory=list)
    is_terminal: bool = False


@dataclass(frozen=True)
class RollbackRequested(CheckpointEvent):
    """
    Domain event: rollback was requested.
    
    Published when a user or system requests a rollback operation.
    """
    target_checkpoint_id: str = ""  # CheckpointId value (string)
    target_node_id: str | None = None  # Node to rollback to (if node-level)
    reason: str = ""
    requested_by: str = ""  # "user", "system", "error_handler"


@dataclass(frozen=True)
class RollbackCompleted(CheckpointEvent):
    """
    Domain event: rollback completed successfully.
    
    Published after a rollback operation completes.
    """
    from_checkpoint_id: str = ""  # Where we were
    to_checkpoint_id: str = ""    # Where we rolled back to
    rolled_back_nodes: list[str] = field(default_factory=list)
    state_restored: bool = True


@dataclass(frozen=True)
class RollbackFailed(CheckpointEvent):
    """
    Domain event: rollback failed.
    
    Published when a rollback operation fails.
    """
    target_checkpoint_id: str = ""
    error_message: str = ""
    error_type: str = ""  # "not_found", "invalid_target", "system_error"


@dataclass(frozen=True)
class NodeBoundaryRecorded(CheckpointEvent):
    """
    Domain event: node execution boundary was recorded.
    
    Published when a node's entry or exit boundary is established.
    """
    node_id: str = ""
    boundary_type: str = ""  # "entry" or "exit"
    checkpoint_id: str = ""  # CheckpointId value (string)
    node_status: str = ""    # "running", "completed", "failed"
    duration_ms: int | None = None


@dataclass(frozen=True)
class ExecutionHistoryLoaded(CheckpointEvent):
    """
    Domain event: execution history was loaded.
    
    Published when checkpoint history is retrieved for an execution.
    Useful for audit and debugging.
    """
    checkpoint_count: int = 0
    completed_nodes: list[str] = field(default_factory=list)
    latest_checkpoint_id: str | None = None


# ═══════════════════════════════════════════════════════════════════════════════
# Legacy Event Aliases (Deprecated)
# ═══════════════════════════════════════════════════════════════════════════════

# Keep old names for backward compatibility
CheckpointCreatedEvent = CheckpointCreated
RollbackPerformedEvent = RollbackCompleted


@dataclass(frozen=True)
class BranchCreatedEvent(CheckpointEvent):
    """
    Domain event: branch created from checkpoint.
    
    Published when execution forks from a checkpoint.
    Used in batch testing for variant isolation.
    """
    from_checkpoint_id: str = ""
    new_thread_id: str = ""      # LangGraph thread_id for the branch
    parent_execution_id: str = ""
    branch_reason: str = ""      # "variant_test", "rollback", "manual"


# ═══════════════════════════════════════════════════════════════════════════════
# Event Utilities
# ═══════════════════════════════════════════════════════════════════════════════

def create_checkpoint_created_event(
    execution_id: str,
    checkpoint_id: str,
    step: int,
    node_writes: dict[str, Any],
    next_nodes: list[str],
) -> CheckpointCreated:
    """Factory function to create CheckpointCreated event."""
    completed_node = None
    if node_writes:
        nodes = [n for n in node_writes.keys() if not n.startswith("__")]
        completed_node = nodes[0] if nodes else None
    
    return CheckpointCreated(
        execution_id=execution_id,
        checkpoint_id=checkpoint_id,
        step=step,
        node_writes=node_writes,
        completed_node=completed_node,
        next_nodes=next_nodes,
        is_terminal=len(next_nodes) == 0,
    )


def create_rollback_completed_event(
    execution_id: str,
    from_checkpoint_id: str,
    to_checkpoint_id: str,
    rolled_back_nodes: list[str],
) -> RollbackCompleted:
    """Factory function to create RollbackCompleted event."""
    return RollbackCompleted(
        execution_id=execution_id,
        from_checkpoint_id=from_checkpoint_id,
        to_checkpoint_id=to_checkpoint_id,
        rolled_back_nodes=rolled_back_nodes,
        state_restored=True,
    )
