"""
LangGraph-related domain events.

Events for bridging LangGraph streaming events to WTB audit system.
Follows SOLID principles and maintains clear event schema.

Usage:
    from wtb.domain.events import (
        LangGraphAuditEvent,
        LangGraphAuditEventType,
        create_audit_event_from_langgraph,
    )
    
    # Create audit event from LangGraph streaming event
    audit_event = create_audit_event_from_langgraph(lg_event, execution_id)
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class LangGraphAuditEventType(Enum):
    """
    LangGraph-aligned audit event types.
    
    Maps to LangGraph's astream_events() event types.
    """
    # Execution lifecycle
    EXECUTION_STARTED = "execution_started"
    EXECUTION_COMPLETED = "execution_completed"
    EXECUTION_FAILED = "execution_failed"
    EXECUTION_PAUSED = "execution_paused"
    EXECUTION_RESUMED = "execution_resumed"
    
    # Node lifecycle (maps to on_chain_*)
    NODE_STARTED = "node_started"
    NODE_COMPLETED = "node_completed"
    NODE_FAILED = "node_failed"
    NODE_SKIPPED = "node_skipped"
    
    # Checkpoint lifecycle
    CHECKPOINT_CREATED = "checkpoint_created"
    CHECKPOINT_RESTORED = "checkpoint_restored"
    
    # Tool lifecycle (maps to on_tool_*)
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    
    # LLM lifecycle (maps to on_chat_model_*)
    LLM_STARTED = "llm_started"
    LLM_COMPLETED = "llm_completed"
    LLM_STREAMED = "llm_streamed"
    
    # Retriever lifecycle
    RETRIEVER_STARTED = "retriever_started"
    RETRIEVER_COMPLETED = "retriever_completed"
    
    # Custom events
    CUSTOM_EVENT = "custom_event"
    PROGRESS_UPDATE = "progress_update"
    
    # Super-step events
    SUPERSTEP_STARTED = "superstep_started"
    SUPERSTEP_COMPLETED = "superstep_completed"


@dataclass(frozen=True)
class LangGraphAuditEvent:
    """
    Standardized audit event structure for LangGraph integration.
    
    Maps to both LangGraph streaming events and WTB audit requirements.
    Immutable (frozen=True) to ensure event integrity.
    
    SOLID Compliance:
    - SRP: Single responsibility for representing an audit event
    - OCP: Can be extended via composition for specific event types
    
    ACID Compliance:
    - Immutable events ensure consistency
    - Event ID provides uniqueness for idempotent processing
    """
    # Identity
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_type: LangGraphAuditEventType = LangGraphAuditEventType.CUSTOM_EVENT
    
    # Context
    thread_id: str = ""          # LangGraph thread_id (= WTB execution_id)
    run_id: str = ""             # LangGraph run_id
    parent_run_id: str | None = None  # Parent run for nested calls
    
    # Timing
    timestamp: datetime = field(default_factory=datetime.utcnow)
    duration_ms: int | None = None
    
    # Location
    node_id: str | None = None        # Node name
    checkpoint_id: str | None = None  # Checkpoint ID
    step: int | None = None           # Super-step number
    
    # Content (Note: inputs may contain PII, handle carefully)
    inputs: dict[str, Any] | None = None
    outputs: dict[str, Any] | None = None
    error: str | None = None
    
    # Metadata
    metadata: dict[str, Any] | None = None
    tags: list[str] | None = None
    
    # Metrics
    metrics: dict[str, float] | None = None  # tokens, cost, latency
    
    def to_dict(self) -> dict[str, Any]:
        """
        Convert to dictionary for storage.
        
        Returns:
            Dictionary representation suitable for JSON serialization.
        """
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "parent_run_id": self.parent_run_id,
            "timestamp": self.timestamp.isoformat(),
            "duration_ms": self.duration_ms,
            "node_id": self.node_id,
            "checkpoint_id": self.checkpoint_id,
            "step": self.step,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "error": self.error,
            "metadata": self.metadata,
            "tags": self.tags,
            "metrics": self.metrics,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LangGraphAuditEvent":
        """
        Create from dictionary.
        
        Args:
            data: Dictionary with event data
            
        Returns:
            LangGraphAuditEvent instance
        """
        return cls(
            event_id=data.get("event_id", str(uuid.uuid4())),
            event_type=LangGraphAuditEventType(data.get("event_type", "custom_event")),
            thread_id=data.get("thread_id", ""),
            run_id=data.get("run_id", ""),
            parent_run_id=data.get("parent_run_id"),
            timestamp=datetime.fromisoformat(data["timestamp"]) if "timestamp" in data else datetime.now(timezone.utc),
            duration_ms=data.get("duration_ms"),
            node_id=data.get("node_id"),
            checkpoint_id=data.get("checkpoint_id"),
            step=data.get("step"),
            inputs=data.get("inputs"),
            outputs=data.get("outputs"),
            error=data.get("error"),
            metadata=data.get("metadata"),
            tags=data.get("tags"),
            metrics=data.get("metrics"),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# LangGraph Event Type Mapping
# ═══════════════════════════════════════════════════════════════════════════════

LANGGRAPH_EVENT_TYPE_MAP: dict[str, LangGraphAuditEventType] = {
    # Chain events
    "on_chain_start": LangGraphAuditEventType.NODE_STARTED,
    "on_chain_end": LangGraphAuditEventType.NODE_COMPLETED,
    "on_chain_error": LangGraphAuditEventType.NODE_FAILED,
    
    # Tool events
    "on_tool_start": LangGraphAuditEventType.TOOL_STARTED,
    "on_tool_end": LangGraphAuditEventType.TOOL_COMPLETED,
    "on_tool_error": LangGraphAuditEventType.TOOL_FAILED,
    
    # Chat model events
    "on_chat_model_start": LangGraphAuditEventType.LLM_STARTED,
    "on_chat_model_end": LangGraphAuditEventType.LLM_COMPLETED,
    "on_chat_model_stream": LangGraphAuditEventType.LLM_STREAMED,
    
    # Retriever events
    "on_retriever_start": LangGraphAuditEventType.RETRIEVER_STARTED,
    "on_retriever_end": LangGraphAuditEventType.RETRIEVER_COMPLETED,
}


def map_langgraph_event_type(lg_event_type: str) -> LangGraphAuditEventType:
    """
    Map LangGraph event type to WTB audit event type.
    
    Args:
        lg_event_type: LangGraph event type string
        
    Returns:
        Corresponding LangGraphAuditEventType
    """
    return LANGGRAPH_EVENT_TYPE_MAP.get(
        lg_event_type,
        LangGraphAuditEventType.CUSTOM_EVENT
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Factory Functions
# ═══════════════════════════════════════════════════════════════════════════════

def create_audit_event_from_langgraph(
    lg_event: dict[str, Any],
    thread_id: str,
    include_inputs: bool = False,
    include_outputs: bool = True,
) -> LangGraphAuditEvent:
    """
    Create LangGraphAuditEvent from LangGraph streaming event.
    
    Args:
        lg_event: LangGraph event from astream_events()
        thread_id: WTB execution ID (= LangGraph thread_id)
        include_inputs: Whether to include inputs (may contain PII)
        include_outputs: Whether to include outputs
        
    Returns:
        LangGraphAuditEvent instance
    """
    lg_event_type = lg_event.get("event", "")
    event_type = map_langgraph_event_type(lg_event_type)
    
    # Extract data
    data = lg_event.get("data", {})
    inputs = data.get("input") if include_inputs else None
    outputs = data.get("output") if include_outputs else None
    
    # Extract metadata
    metadata = lg_event.get("metadata", {})
    
    return LangGraphAuditEvent(
        event_type=event_type,
        thread_id=thread_id,
        run_id=lg_event.get("run_id", ""),
        parent_run_id=lg_event.get("parent_ids", [None])[0] if lg_event.get("parent_ids") else None,
        node_id=lg_event.get("name"),
        inputs=inputs,
        outputs=outputs,
        metadata=metadata,
        tags=lg_event.get("tags", []),
    )


def create_checkpoint_audit_event(
    execution_id: str,
    checkpoint_id: str,
    step: int,
    writes: dict[str, Any],
    next_nodes: list[str],
) -> LangGraphAuditEvent:
    """
    Create audit event for checkpoint creation.
    
    Args:
        execution_id: WTB execution ID
        checkpoint_id: LangGraph checkpoint ID
        step: Super-step number
        writes: Node writes from checkpoint
        next_nodes: Next nodes to execute
        
    Returns:
        LangGraphAuditEvent for checkpoint
    """
    # Determine completed node from writes
    completed_node = None
    if writes:
        nodes = [n for n in writes.keys() if not n.startswith("__")]
        completed_node = nodes[0] if nodes else None
    
    return LangGraphAuditEvent(
        event_type=LangGraphAuditEventType.CHECKPOINT_CREATED,
        thread_id=execution_id,
        checkpoint_id=checkpoint_id,
        step=step,
        node_id=completed_node,
        outputs=writes,
        metadata={
            "next_nodes": next_nodes,
            "is_terminal": len(next_nodes) == 0,
        },
    )


def create_superstep_audit_event(
    execution_id: str,
    step: int,
    completed: bool,
    nodes_executed: list[str],
    duration_ms: int | None = None,
) -> LangGraphAuditEvent:
    """
    Create audit event for super-step boundary.
    
    Args:
        execution_id: WTB execution ID
        step: Super-step number
        completed: Whether super-step completed successfully
        nodes_executed: Nodes executed in this super-step
        duration_ms: Super-step duration
        
    Returns:
        LangGraphAuditEvent for super-step
    """
    event_type = (
        LangGraphAuditEventType.SUPERSTEP_COMPLETED if completed
        else LangGraphAuditEventType.SUPERSTEP_STARTED
    )
    
    return LangGraphAuditEvent(
        event_type=event_type,
        thread_id=execution_id,
        step=step,
        duration_ms=duration_ms,
        metadata={
            "nodes_executed": nodes_executed,
        },
    )
