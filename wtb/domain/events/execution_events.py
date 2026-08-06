"""
Execution-related domain events.

Published during workflow execution lifecycle.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class WTBEvent:
    """Base class for all WTB domain events."""
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=datetime.now)
    
    @property
    def event_type(self) -> str:
        return self.__class__.__name__


@dataclass
class ExecutionStartedEvent(WTBEvent):
    """Published when a workflow execution starts."""
    execution_id: str = ""
    workflow_id: str = ""
    workflow_name: str = ""
    initial_state: dict[str, Any] = field(default_factory=dict)
    breakpoints: list = field(default_factory=list)


@dataclass
class ExecutionPausedEvent(WTBEvent):
    """Published when execution is paused (breakpoint or manual)."""
    execution_id: str = ""
    paused_at_node: str = ""
    reason: str = ""  # "breakpoint", "manual", "error"
    checkpoint_id: str | None = None
    elapsed_time_ms: float = 0.0


@dataclass
class ExecutionResumedEvent(WTBEvent):
    """Published when execution is resumed."""
    execution_id: str = ""
    resume_from_node: str = ""
    modified_state: dict[str, Any] | None = None


@dataclass
class ExecutionCompletedEvent(WTBEvent):
    """Published when execution completes successfully."""
    execution_id: str = ""
    workflow_id: str = ""
    final_state: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    nodes_executed: int = 0
    checkpoints_created: int = 0


@dataclass
class ExecutionFailedEvent(WTBEvent):
    """Published when execution fails."""
    execution_id: str = ""
    workflow_id: str = ""
    failed_at_node: str = ""
    error_message: str = ""
    error_type: str = ""
    checkpoint_id: str | None = None
    duration_ms: float = 0.0


@dataclass
class ExecutionCancelledEvent(WTBEvent):
    """Published when execution is cancelled."""
    execution_id: str = ""
    cancelled_at_node: str | None = None
    reason: str = ""
    duration_ms: float = 0.0

