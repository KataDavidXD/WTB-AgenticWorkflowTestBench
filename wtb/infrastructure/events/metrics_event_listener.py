"""
Metrics Event Listener for Prometheus Integration.

Captures LangGraph and WTB execution metrics from events for Prometheus export.
Follows SOLID principles for clean separation of concerns.

Usage:
    from wtb.infrastructure.events import MetricsEventListener, WTBEventBus
    
    event_bus = WTBEventBus()
    metrics_listener = MetricsEventListener(event_bus)
    
    # Metrics are now automatically captured from events
    # Export via prometheus_client
"""

import logging
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
from wtb.domain.events.checkpoint_events import CheckpointCreated

logger = logging.getLogger(__name__)

# Try to import prometheus_client, but don't fail if not available
try:
    from prometheus_client import Counter, Gauge, Histogram, Info
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    logger.warning("prometheus_client not installed. Metrics will not be exported.")


# ═══════════════════════════════════════════════════════════════════════════════
# Prometheus Metrics Definitions
# ═══════════════════════════════════════════════════════════════════════════════

if PROMETHEUS_AVAILABLE:
    # Execution metrics
    EXECUTIONS_TOTAL = Counter(
        "wtb_langgraph_executions_total",
        "Total LangGraph executions",
        ["workflow_id", "status"]
    )
    
    EXECUTION_DURATION_SECONDS = Histogram(
        "wtb_langgraph_execution_duration_seconds",
        "Execution duration in seconds",
        ["workflow_id"],
        buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0]
    )
    
    ACTIVE_EXECUTIONS = Gauge(
        "wtb_langgraph_active_executions",
        "Currently active executions"
    )
    
    # Node metrics
    NODE_EXECUTIONS_TOTAL = Counter(
        "wtb_langgraph_node_executions_total",
        "Total node executions",
        ["workflow_id", "node_id", "status"]
    )
    
    NODE_DURATION_SECONDS = Histogram(
        "wtb_langgraph_node_duration_seconds",
        "Node execution duration in seconds",
        ["workflow_id", "node_id"],
        buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0]
    )
    
    # Checkpoint metrics
    CHECKPOINTS_TOTAL = Counter(
        "wtb_langgraph_checkpoints_total",
        "Total checkpoints created",
        ["workflow_id"]
    )
    
    # Error metrics
    ERRORS_TOTAL = Counter(
        "wtb_langgraph_errors_total",
        "Total errors by type",
        ["workflow_id", "error_type"]
    )
    
    # Event bus metrics
    EVENTS_PUBLISHED_TOTAL = Counter(
        "wtb_event_bus_events_published_total",
        "Total events published to event bus",
        ["event_type"]
    )


class MetricsEventListener:
    """
    Listens to WTB events and exports Prometheus metrics.
    
    Automatically subscribes to relevant events and updates metrics.
    
    SOLID Compliance:
    - SRP: Single responsibility for metrics collection
    - OCP: New metrics via subclass or composition
    - DIP: Depends on WTBEventBus abstraction
    
    Attributes:
        _event_bus: WTB event bus instance
        _enabled: Whether metrics collection is enabled
        _workflow_context: Mapping of execution_id to workflow_id
    """
    
    def __init__(
        self,
        event_bus: Any,  # WTBEventBus
        enabled: bool = True,
        default_workflow_id: str = "unknown",
    ):
        """
        Initialize metrics event listener.
        
        Args:
            event_bus: WTB event bus to subscribe to
            enabled: Enable metrics collection
            default_workflow_id: Default workflow ID when not provided
        """
        self._event_bus = event_bus
        self._enabled = enabled and PROMETHEUS_AVAILABLE
        self._default_workflow_id = default_workflow_id
        
        # Track workflow context for execution_id → workflow_id mapping
        self._workflow_context: dict[str, str] = {}
        
        # Track active executions for timing
        self._execution_start_times: dict[str, datetime] = {}
        
        if self._enabled:
            self._subscribe_to_events()
    
    def _subscribe_to_events(self) -> None:
        """Subscribe to relevant events."""
        # Execution events
        self._event_bus.subscribe(ExecutionStartedEvent, self._on_execution_started)
        self._event_bus.subscribe(ExecutionCompletedEvent, self._on_execution_completed)
        self._event_bus.subscribe(ExecutionFailedEvent, self._on_execution_failed)
        
        # Node events
        self._event_bus.subscribe(NodeStartedEvent, self._on_node_started)
        self._event_bus.subscribe(NodeCompletedEvent, self._on_node_completed)
        self._event_bus.subscribe(NodeFailedEvent, self._on_node_failed)
        
        # Checkpoint events
        self._event_bus.subscribe(CheckpointCreated, self._on_checkpoint_created)
        
        logger.info("MetricsEventListener subscribed to events")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Execution Event Handlers
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _on_execution_started(self, event: ExecutionStartedEvent) -> None:
        """Handle execution started event."""
        if not self._enabled:
            return
        
        execution_id = event.execution_id
        workflow_id = event.workflow_id or self._default_workflow_id
        
        # Track workflow context
        self._workflow_context[execution_id] = workflow_id
        self._execution_start_times[execution_id] = datetime.now(timezone.utc)
        
        # Update metrics
        ACTIVE_EXECUTIONS.inc()
        EVENTS_PUBLISHED_TOTAL.labels(event_type="execution_started").inc()
        
        logger.debug(f"Execution started: {execution_id}")
    
    def _on_execution_completed(self, event: ExecutionCompletedEvent) -> None:
        """Handle execution completed event."""
        if not self._enabled:
            return
        
        execution_id = event.execution_id
        workflow_id = self._workflow_context.get(execution_id, self._default_workflow_id)
        
        # Calculate duration
        duration_seconds = event.duration_ms / 1000.0 if event.duration_ms else 0.0
        
        # Update metrics
        ACTIVE_EXECUTIONS.dec()
        EXECUTIONS_TOTAL.labels(workflow_id=workflow_id, status="completed").inc()
        EXECUTION_DURATION_SECONDS.labels(workflow_id=workflow_id).observe(duration_seconds)
        EVENTS_PUBLISHED_TOTAL.labels(event_type="execution_completed").inc()
        
        # Cleanup context
        self._cleanup_execution(execution_id)
        
        logger.debug(f"Execution completed: {execution_id} in {duration_seconds:.2f}s")
    
    def _on_execution_failed(self, event: ExecutionFailedEvent) -> None:
        """Handle execution failed event."""
        if not self._enabled:
            return
        
        execution_id = event.execution_id
        workflow_id = self._workflow_context.get(execution_id, self._default_workflow_id)
        
        # Calculate duration
        duration_seconds = event.duration_ms / 1000.0 if event.duration_ms else 0.0
        
        # Update metrics
        ACTIVE_EXECUTIONS.dec()
        EXECUTIONS_TOTAL.labels(workflow_id=workflow_id, status="failed").inc()
        EXECUTION_DURATION_SECONDS.labels(workflow_id=workflow_id).observe(duration_seconds)
        ERRORS_TOTAL.labels(
            workflow_id=workflow_id,
            error_type=event.error_type or "unknown"
        ).inc()
        EVENTS_PUBLISHED_TOTAL.labels(event_type="execution_failed").inc()
        
        # Cleanup context
        self._cleanup_execution(execution_id)
        
        logger.debug(f"Execution failed: {execution_id} - {event.error_message}")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Node Event Handlers
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _on_node_started(self, event: NodeStartedEvent) -> None:
        """Handle node started event."""
        if not self._enabled:
            return
        
        EVENTS_PUBLISHED_TOTAL.labels(event_type="node_started").inc()
    
    def _on_node_completed(self, event: NodeCompletedEvent) -> None:
        """Handle node completed event."""
        if not self._enabled:
            return
        
        workflow_id = self._workflow_context.get(
            event.execution_id,
            self._default_workflow_id
        )
        node_id = event.node_id or event.node_name or "unknown"
        
        # Update metrics
        NODE_EXECUTIONS_TOTAL.labels(
            workflow_id=workflow_id,
            node_id=node_id,
            status="completed"
        ).inc()
        
        if event.duration_ms:
            NODE_DURATION_SECONDS.labels(
                workflow_id=workflow_id,
                node_id=node_id
            ).observe(event.duration_ms / 1000.0)
        
        EVENTS_PUBLISHED_TOTAL.labels(event_type="node_completed").inc()
    
    def _on_node_failed(self, event: NodeFailedEvent) -> None:
        """Handle node failed event."""
        if not self._enabled:
            return
        
        workflow_id = self._workflow_context.get(
            event.execution_id,
            self._default_workflow_id
        )
        node_id = event.node_id or event.node_name or "unknown"
        
        # Update metrics
        NODE_EXECUTIONS_TOTAL.labels(
            workflow_id=workflow_id,
            node_id=node_id,
            status="failed"
        ).inc()
        
        ERRORS_TOTAL.labels(
            workflow_id=workflow_id,
            error_type=event.error_type or "node_error"
        ).inc()
        
        EVENTS_PUBLISHED_TOTAL.labels(event_type="node_failed").inc()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Checkpoint Event Handlers
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _on_checkpoint_created(self, event: CheckpointCreated) -> None:
        """Handle checkpoint created event."""
        if not self._enabled:
            return
        
        workflow_id = self._workflow_context.get(
            event.execution_id,
            self._default_workflow_id
        )
        
        CHECKPOINTS_TOTAL.labels(workflow_id=workflow_id).inc()
        EVENTS_PUBLISHED_TOTAL.labels(event_type="checkpoint_created").inc()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Cleanup
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _cleanup_execution(self, execution_id: str) -> None:
        """Cleanup execution context."""
        self._workflow_context.pop(execution_id, None)
        self._execution_start_times.pop(execution_id, None)
    
    def unsubscribe(self) -> None:
        """Unsubscribe from all events."""
        if not self._enabled:
            return
        
        self._event_bus.unsubscribe(ExecutionStartedEvent, self._on_execution_started)
        self._event_bus.unsubscribe(ExecutionCompletedEvent, self._on_execution_completed)
        self._event_bus.unsubscribe(ExecutionFailedEvent, self._on_execution_failed)
        self._event_bus.unsubscribe(NodeStartedEvent, self._on_node_started)
        self._event_bus.unsubscribe(NodeCompletedEvent, self._on_node_completed)
        self._event_bus.unsubscribe(NodeFailedEvent, self._on_node_failed)
        self._event_bus.unsubscribe(CheckpointCreated, self._on_checkpoint_created)
        
        logger.info("MetricsEventListener unsubscribed from events")


# ═══════════════════════════════════════════════════════════════════════════════
# In-Memory Metrics Collector (for testing without Prometheus)
# ═══════════════════════════════════════════════════════════════════════════════

class InMemoryMetricsCollector:
    """
    In-memory metrics collector for testing.
    
    Captures metrics in dictionaries instead of Prometheus.
    Useful for unit tests where Prometheus is not available.
    """
    
    def __init__(self, event_bus: Any):
        """Initialize collector."""
        self._event_bus = event_bus
        self._metrics: dict[str, Any] = {
            "executions_total": {"completed": 0, "failed": 0},
            "active_executions": 0,
            "node_executions_total": {},
            "checkpoints_total": 0,
            "errors_total": {},
            "execution_durations": [],
            "node_durations": {},
        }
        self._workflow_context: dict[str, str] = {}
        
        self._subscribe_to_events()
    
    def _subscribe_to_events(self) -> None:
        """Subscribe to events."""
        self._event_bus.subscribe(ExecutionStartedEvent, self._on_execution_started)
        self._event_bus.subscribe(ExecutionCompletedEvent, self._on_execution_completed)
        self._event_bus.subscribe(ExecutionFailedEvent, self._on_execution_failed)
        self._event_bus.subscribe(NodeCompletedEvent, self._on_node_completed)
        self._event_bus.subscribe(NodeFailedEvent, self._on_node_failed)
        self._event_bus.subscribe(CheckpointCreated, self._on_checkpoint_created)
    
    def _on_execution_started(self, event: ExecutionStartedEvent) -> None:
        self._workflow_context[event.execution_id] = event.workflow_id
        self._metrics["active_executions"] += 1
    
    def _on_execution_completed(self, event: ExecutionCompletedEvent) -> None:
        self._metrics["active_executions"] -= 1
        self._metrics["executions_total"]["completed"] += 1
        if event.duration_ms:
            self._metrics["execution_durations"].append(event.duration_ms)
        self._workflow_context.pop(event.execution_id, None)
    
    def _on_execution_failed(self, event: ExecutionFailedEvent) -> None:
        self._metrics["active_executions"] -= 1
        self._metrics["executions_total"]["failed"] += 1
        error_type = event.error_type or "unknown"
        self._metrics["errors_total"][error_type] = \
            self._metrics["errors_total"].get(error_type, 0) + 1
        self._workflow_context.pop(event.execution_id, None)
    
    def _on_node_completed(self, event: NodeCompletedEvent) -> None:
        node_id = event.node_id or event.node_name
        if node_id not in self._metrics["node_executions_total"]:
            self._metrics["node_executions_total"][node_id] = {"completed": 0, "failed": 0}
        self._metrics["node_executions_total"][node_id]["completed"] += 1
        
        if event.duration_ms:
            if node_id not in self._metrics["node_durations"]:
                self._metrics["node_durations"][node_id] = []
            self._metrics["node_durations"][node_id].append(event.duration_ms)
    
    def _on_node_failed(self, event: NodeFailedEvent) -> None:
        node_id = event.node_id or event.node_name
        if node_id not in self._metrics["node_executions_total"]:
            self._metrics["node_executions_total"][node_id] = {"completed": 0, "failed": 0}
        self._metrics["node_executions_total"][node_id]["failed"] += 1
    
    def _on_checkpoint_created(self, event: CheckpointCreated) -> None:
        self._metrics["checkpoints_total"] += 1
    
    def get_metrics(self) -> dict[str, Any]:
        """Get collected metrics."""
        return self._metrics.copy()
    
    def reset(self) -> None:
        """Reset all metrics."""
        self._metrics = {
            "executions_total": {"completed": 0, "failed": 0},
            "active_executions": 0,
            "node_executions_total": {},
            "checkpoints_total": 0,
            "errors_total": {},
            "execution_durations": [],
            "node_durations": {},
        }
