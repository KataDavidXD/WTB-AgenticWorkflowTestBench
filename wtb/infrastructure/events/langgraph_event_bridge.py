"""
LangGraph Event Bridge.

Bridges LangGraph streaming events to WTB EventBus for audit and observability.
Follows SOLID and ACID principles for consistent event handling.

Architecture:
    LangGraph astream_events() → LangGraphEventBridge → WTB EventBus
                                          ↓
                            Transform to WTB Domain Events
                                          ↓
                            Publish to Subscribers (AuditEventListener, etc.)

Usage:
    from wtb.infrastructure.events import LangGraphEventBridge, WTBEventBus
    
    event_bus = WTBEventBus()
    bridge = LangGraphEventBridge(event_bus)
    
    # Execute with event bridging
    result = await bridge.bridge_execution(graph, initial_state, config, execution_id)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from wtb.domain.events import (
    ExecutionCompletedEvent,
    ExecutionFailedEvent,
    ExecutionStartedEvent,
    NodeCompletedEvent,
    NodeFailedEvent,
    NodeStartedEvent,
)
from wtb.domain.events.checkpoint_events import (
    create_checkpoint_created_event,
)
from wtb.domain.events.langgraph_events import (
    LangGraphAuditEvent,
    create_audit_event_from_langgraph,
    create_checkpoint_audit_event,
)
from wtb.infrastructure.events.stream_mode_config import StreamModeConfig
from wtb.infrastructure.events.wtb_event_bus import WTBEventBus

logger = logging.getLogger(__name__)


@dataclass
class NodeExecutionTracker:
    """
    Tracks node execution state for duration calculation.
    
    Maintains start times for active nodes to compute duration
    when node completes.
    """
    _active_nodes: dict[str, datetime] = field(default_factory=dict)
    
    def node_started(self, run_id: str) -> None:
        """Record node start time."""
        self._active_nodes[run_id] = datetime.now(timezone.utc)
    
    def node_completed(self, run_id: str) -> int | None:
        """
        Record node completion and return duration.
        
        Args:
            run_id: LangGraph run ID
            
        Returns:
            Duration in milliseconds, or None if not tracked
        """
        start_time = self._active_nodes.pop(run_id, None)
        if start_time:
            return int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000)
        return None
    
    def clear(self) -> None:
        """Clear all tracked nodes."""
        self._active_nodes.clear()


class LangGraphEventBridge:
    """
    Bridge LangGraph streaming events to WTB EventBus.
    
    Responsibilities:
    - Consume LangGraph astream_events()
    - Transform to WTB domain events
    - Publish to WTBEventBus
    - Handle event correlation (run_id → execution_id)
    - Track node execution timing
    
    SOLID Compliance:
    - SRP: Only handles event transformation and bridging
    - OCP: New event types via transformer registry
    - LSP: Works with any CompiledStateGraph
    - ISP: Minimal interface for event bridging
    - DIP: Depends on WTBEventBus abstraction
    
    ACID Compliance:
    - Events are immutable once created
    - Event IDs ensure idempotent processing
    - Atomic event publishing (all-or-nothing per event)
    """
    
    def __init__(
        self,
        event_bus: WTBEventBus,
        stream_config: StreamModeConfig | None = None,
        emit_audit_events: bool = True,
    ):
        """
        Initialize LangGraph Event Bridge.
        
        Args:
            event_bus: WTB event bus for publishing events
            stream_config: Stream mode configuration
            emit_audit_events: Whether to emit LangGraphAuditEvent instances
        """
        self._event_bus = event_bus
        self._stream_config = stream_config or StreamModeConfig.for_production()
        self._emit_audit_events = emit_audit_events
        self._node_tracker = NodeExecutionTracker()
        
        # Event counters for metrics
        self._events_processed = 0
        self._events_published = 0
        self._errors = 0
    
    @property
    def include_inputs(self) -> bool:
        """Whether to include inputs in events."""
        return self._stream_config.include_inputs
    
    @property
    def include_outputs(self) -> bool:
        """Whether to include outputs in events."""
        return self._stream_config.include_outputs
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Main Execution Bridge
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def bridge_execution(
        self,
        graph: Any,  # CompiledStateGraph
        initial_state: dict[str, Any],
        config: dict[str, Any],
        execution_id: str,
        workflow_id: str = "",
    ) -> dict[str, Any]:
        """
        Execute graph while bridging events to WTB EventBus.
        
        Args:
            graph: Compiled LangGraph StateGraph
            initial_state: Initial state for execution
            config: LangGraph config with thread_id
            execution_id: WTB execution ID
            workflow_id: WTB workflow ID (optional)
            
        Returns:
            Final execution result
            
        Raises:
            Exception: Re-raises any execution exception after publishing failure event
        """
        thread_id = config.get("configurable", {}).get("thread_id", execution_id)
        start_time = datetime.now(timezone.utc)
        
        # Reset tracking state
        self._node_tracker.clear()
        
        # Publish execution started
        self._publish_execution_started(execution_id, workflow_id, initial_state)
        
        final_result = None
        nodes_executed = 0
        
        try:
            # Stream events and transform
            async for event in graph.astream_events(
                initial_state,
                config,
                version="v2",
            ):
                self._events_processed += 1
                
                # Apply filter
                if not self._stream_config.should_include_event(event):
                    continue
                
                # Transform and publish
                wtb_event = self._transform_event(event, execution_id, thread_id)
                if wtb_event:
                    self._publish_event(wtb_event)
                    nodes_executed += 1
                
                # Emit audit event if enabled
                if self._emit_audit_events:
                    audit_event = create_audit_event_from_langgraph(
                        event,
                        thread_id,
                        include_inputs=self.include_inputs,
                        include_outputs=self.include_outputs,
                    )
                    self._publish_audit_event(audit_event)
                
                # Capture final result
                if event.get("event") == "on_chain_end":
                    name = event.get("name", "")
                    if name == "LangGraph" or not name.startswith("_"):
                        output = event.get("data", {}).get("output")
                        if output:
                            final_result = output
            
            # Calculate duration
            duration_ms = int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000)
            
            # Publish execution completed
            self._publish_execution_completed(
                execution_id,
                workflow_id,
                final_result or {},
                duration_ms,
                nodes_executed,
            )
            
            return final_result or {}
            
        except Exception as e:
            # Calculate duration
            duration_ms = int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000)
            
            # Publish execution failed
            self._publish_execution_failed(
                execution_id,
                workflow_id,
                str(e),
                type(e).__name__,
                duration_ms,
            )
            
            self._errors += 1
            raise
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Sync Execution Bridge (for non-async use)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def bridge_execution_sync(
        self,
        graph: Any,  # CompiledStateGraph
        initial_state: dict[str, Any],
        config: dict[str, Any],
        execution_id: str,
        workflow_id: str = "",
    ) -> dict[str, Any]:
        """
        Execute graph synchronously with event bridging.
        
        Uses graph.stream() instead of astream_events() for sync execution.
        
        Args:
            graph: Compiled LangGraph StateGraph
            initial_state: Initial state for execution
            config: LangGraph config with thread_id
            execution_id: WTB execution ID
            workflow_id: WTB workflow ID (optional)
            
        Returns:
            Final execution result
        """
        thread_id = config.get("configurable", {}).get("thread_id", execution_id)
        start_time = datetime.now(timezone.utc)
        
        # Reset tracking
        self._node_tracker.clear()
        
        # Publish execution started
        self._publish_execution_started(execution_id, workflow_id, initial_state)
        
        final_result = None
        nodes_executed = 0
        
        try:
            # Use stream with updates mode
            for event in graph.stream(
                initial_state,
                config,
                stream_mode="updates",
            ):
                self._events_processed += 1
                
                # Event is dict of {node_name: output}
                for node_name, output in event.items():
                    if node_name.startswith("__"):
                        continue
                    
                    nodes_executed += 1
                    final_result = output
                    
                    # Publish node completed
                    self._event_bus.publish(NodeCompletedEvent(
                        execution_id=execution_id,
                        node_id=node_name,
                        node_name=node_name,
                        result=output if self.include_outputs else None,
                    ))
                    self._events_published += 1
            
            # Calculate duration
            duration_ms = int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000)
            
            # Publish execution completed
            self._publish_execution_completed(
                execution_id,
                workflow_id,
                final_result or {},
                duration_ms,
                nodes_executed,
            )
            
            return final_result or {}
            
        except Exception as e:
            duration_ms = int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000)
            self._publish_execution_failed(
                execution_id,
                workflow_id,
                str(e),
                type(e).__name__,
                duration_ms,
            )
            self._errors += 1
            raise
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Checkpoint Event Bridging
    # ═══════════════════════════════════════════════════════════════════════════
    
    def bridge_checkpoint_events(
        self,
        graph: Any,  # CompiledStateGraph
        config: dict[str, Any],
        execution_id: str,
    ) -> int:
        """
        Bridge checkpoint history to WTB audit events.
        
        Call after execution to emit checkpoint events from history.
        
        Args:
            graph: Compiled LangGraph StateGraph
            config: LangGraph config with thread_id
            execution_id: WTB execution ID
            
        Returns:
            Number of checkpoint events emitted
        """
        checkpoints_emitted = 0
        
        try:
            for snapshot in graph.get_state_history(config):
                checkpoint_id = snapshot.config["configurable"]["checkpoint_id"]
                step = snapshot.metadata.get("step", 0)
                writes = snapshot.metadata.get("writes", {})
                next_nodes = list(snapshot.next) if snapshot.next else []
                
                # Create and publish checkpoint event
                event = create_checkpoint_created_event(
                    execution_id=execution_id,
                    checkpoint_id=checkpoint_id,
                    step=step,
                    node_writes=writes,
                    next_nodes=next_nodes,
                )
                self._event_bus.publish(event)
                
                # Emit audit event if enabled
                if self._emit_audit_events:
                    audit_event = create_checkpoint_audit_event(
                        execution_id=execution_id,
                        checkpoint_id=checkpoint_id,
                        step=step,
                        writes=writes,
                        next_nodes=next_nodes,
                    )
                    self._publish_audit_event(audit_event)
                
                checkpoints_emitted += 1
                
        except Exception as e:
            logger.error(f"Error bridging checkpoint events: {e}")
        
        return checkpoints_emitted
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Event Transformation
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _transform_event(
        self,
        lg_event: dict[str, Any],
        execution_id: str,
        thread_id: str,
    ) -> Any | None:
        """
        Transform LangGraph event to WTB domain event.
        
        Args:
            lg_event: LangGraph streaming event
            execution_id: WTB execution ID
            thread_id: LangGraph thread ID
            
        Returns:
            WTB domain event or None if not applicable
        """
        event_type = lg_event.get("event", "")
        name = lg_event.get("name", "unknown")
        run_id = lg_event.get("run_id", "")
        
        # Skip graph-level events
        if name == "LangGraph":
            return None
        
        # Node start
        if event_type == "on_chain_start":
            self._node_tracker.node_started(run_id)
            return NodeStartedEvent(
                execution_id=execution_id,
                node_id=name,
                node_name=name,
            )
        
        # Node end
        if event_type == "on_chain_end":
            duration_ms = self._node_tracker.node_completed(run_id)
            
            outputs = None
            if self.include_outputs:
                outputs = lg_event.get("data", {}).get("output")
            
            return NodeCompletedEvent(
                execution_id=execution_id,
                node_id=name,
                node_name=name,
                result=outputs,
                duration_ms=duration_ms or 0.0,
            )
        
        # Node error
        if event_type == "on_chain_error":
            error_data = lg_event.get("data", {})
            return NodeFailedEvent(
                execution_id=execution_id,
                node_id=name,
                node_name=name,
                error_message=str(error_data.get("error", "Unknown error")),
                error_type=error_data.get("error_type", "Exception"),
            )
        
        return None
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Publishing Helpers
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _publish_event(self, event: Any) -> None:
        """Publish event to event bus."""
        try:
            self._event_bus.publish(event)
            self._events_published += 1
        except Exception as e:
            logger.error(f"Failed to publish event: {e}")
            self._errors += 1
    
    def _publish_audit_event(self, event: LangGraphAuditEvent) -> None:
        """
        Publish audit event.
        
        Note: Audit events are stored separately from domain events
        for compliance and analysis purposes.
        """
        # For now, we log audit events. In production, these would
        # go to a dedicated audit store or observability platform.
        logger.debug(f"Audit event: {event.event_type.value} - {event.node_id}")
    
    def _publish_execution_started(
        self,
        execution_id: str,
        workflow_id: str,
        initial_state: dict[str, Any],
    ) -> None:
        """Publish execution started event."""
        self._event_bus.publish(ExecutionStartedEvent(
            execution_id=execution_id,
            workflow_id=workflow_id,
            initial_state=initial_state if self.include_inputs else {},
        ))
        self._events_published += 1
    
    def _publish_execution_completed(
        self,
        execution_id: str,
        workflow_id: str,
        final_state: dict[str, Any],
        duration_ms: int,
        nodes_executed: int,
    ) -> None:
        """Publish execution completed event."""
        self._event_bus.publish(ExecutionCompletedEvent(
            execution_id=execution_id,
            workflow_id=workflow_id,
            final_state=final_state if self.include_outputs else {},
            duration_ms=float(duration_ms),
            nodes_executed=nodes_executed,
        ))
        self._events_published += 1
    
    def _publish_execution_failed(
        self,
        execution_id: str,
        workflow_id: str,
        error_message: str,
        error_type: str,
        duration_ms: int,
    ) -> None:
        """Publish execution failed event."""
        self._event_bus.publish(ExecutionFailedEvent(
            execution_id=execution_id,
            workflow_id=workflow_id,
            error_message=error_message,
            error_type=error_type,
            duration_ms=float(duration_ms),
        ))
        self._events_published += 1
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Statistics
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_stats(self) -> dict[str, int]:
        """
        Get bridge statistics.
        
        Returns:
            Dictionary with event counts
        """
        return {
            "events_processed": self._events_processed,
            "events_published": self._events_published,
            "errors": self._errors,
        }
    
    def reset_stats(self) -> None:
        """Reset statistics counters."""
        self._events_processed = 0
        self._events_published = 0
        self._errors = 0


# ═══════════════════════════════════════════════════════════════════════════════
# Factory Functions
# ═══════════════════════════════════════════════════════════════════════════════

def create_event_bridge(
    event_bus: WTBEventBus | None = None,
    stream_config: StreamModeConfig | None = None,
    emit_audit_events: bool = True,
) -> LangGraphEventBridge:
    """
    Factory function to create LangGraphEventBridge.
    
    Args:
        event_bus: Event bus instance (uses global if None)
        stream_config: Stream mode configuration
        emit_audit_events: Whether to emit audit events
        
    Returns:
        Configured LangGraphEventBridge
    """
    from wtb.infrastructure.events import get_wtb_event_bus
    
    bus = event_bus or get_wtb_event_bus()
    config = stream_config or StreamModeConfig.for_production()
    
    return LangGraphEventBridge(
        event_bus=bus,
        stream_config=config,
        emit_audit_events=emit_audit_events,
    )


def create_event_bridge_for_testing() -> LangGraphEventBridge:
    """
    Create event bridge configured for testing.
    
    Returns:
        LangGraphEventBridge with testing configuration
    """
    return create_event_bridge(
        event_bus=WTBEventBus(max_history=100),
        stream_config=StreamModeConfig.for_testing(),
        emit_audit_events=False,
    )


def create_event_bridge_for_development() -> LangGraphEventBridge:
    """
    Create event bridge configured for development.
    
    Returns:
        LangGraphEventBridge with development configuration
    """
    return create_event_bridge(
        stream_config=StreamModeConfig.for_development(),
        emit_audit_events=True,
    )
