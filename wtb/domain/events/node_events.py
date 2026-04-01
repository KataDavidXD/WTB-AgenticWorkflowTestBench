"""
Node-related domain events.

Published during node execution within a workflow.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any
import uuid


@dataclass
class NodeEvent:
    """Base class for node events."""
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=datetime.now)
    execution_id: str = ""
    node_id: str = ""
    node_name: str = ""
    
    @property
    def event_type(self) -> str:
        return self.__class__.__name__


@dataclass
class NodeStartedEvent(NodeEvent):
    """Published when a node starts execution."""
    node_type: str = ""
    context: Dict[str, Any] = field(default_factory=dict)
    entry_checkpoint_id: Optional[str] = None


@dataclass
class NodeCompletedEvent(NodeEvent):
    """Published when a node completes successfully."""
    result: Any = None
    duration_ms: float = 0.0
    tool_invocations: int = 0
    exit_checkpoint_id: Optional[str] = None
    output_variables: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NodeFailedEvent(NodeEvent):
    """Published when a node execution fails."""
    error_message: str = ""
    error_type: str = ""
    duration_ms: float = 0.0
    recoverable: bool = True


@dataclass
class NodeSkippedEvent(NodeEvent):
    """Published when a node is skipped (e.g., condition not met)."""
    reason: str = ""
    condition: Optional[str] = None

