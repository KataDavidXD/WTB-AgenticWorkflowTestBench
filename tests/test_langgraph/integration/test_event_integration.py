"""
Integration tests for LangGraph + Event Bus integration.

Tests event system integration:
- Event publishing during execution
- Event transformation from LangGraph events
- Metrics collection
- Event history and replay

SOLID Compliance:
- SRP: Tests focus on event integration
- OCP: Extensible for new event types
- DIP: Uses abstractions for event bus

Run with: pytest tests/test_langgraph/integration/test_event_integration.py -v
"""

import pytest
from typing import Dict, Any, List
from datetime import datetime
from unittest.mock import Mock, MagicMock, patch
import threading
import time

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from wtb.infrastructure.events import (
    WTBEventBus,
    StreamModeConfig,
    LangGraphEventBridge,
    InMemoryMetricsCollector,
    create_event_bridge_for_testing,
)
from wtb.domain.events import (
    ExecutionStartedEvent,
    ExecutionCompletedEvent,
    ExecutionFailedEvent,
    NodeStartedEvent,
    NodeCompletedEvent,
    NodeFailedEvent,
)
from wtb.domain.events.checkpoint_events import CheckpointCreated

from tests.test_langgraph.helpers import (
    create_initial_simple_state,
    create_initial_branch_state,
    SimpleState,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Event Bus Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestEventBusIntegration:
    """Tests for event bus integration with LangGraph."""
    
    def test_event_bus_receives_published_events(self, event_bus):
        """Test event bus receives events published by bridge."""
        received = []
        event_bus.subscribe(NodeCompletedEvent, received.append)
        
        event_bus.publish(NodeCompletedEvent(
            execution_id="exec-1",
            node_id="test_node",
            duration_ms=100.0,
        ))
        
        assert len(received) == 1
        assert received[0].node_id == "test_node"
    
    def test_multiple_event_types_subscription(self, event_bus):
        """Test subscribing to multiple event types."""
        node_events = []
        exec_events = []
        
        event_bus.subscribe(NodeCompletedEvent, node_events.append)
        event_bus.subscribe(ExecutionCompletedEvent, exec_events.append)
        
        event_bus.publish(NodeCompletedEvent(execution_id="e1", node_id="n1"))
        event_bus.publish(ExecutionCompletedEvent(execution_id="e1"))
        event_bus.publish(NodeCompletedEvent(execution_id="e1", node_id="n2"))
        
        assert len(node_events) == 2
        assert len(exec_events) == 1
    
    def test_event_history_tracking(self, event_bus):
        """Test event bus tracks event history."""
        for i in range(5):
            event_bus.publish(NodeCompletedEvent(
                execution_id=f"exec-{i}",
                node_id=f"node_{i}",
            ))
        
        history = event_bus.get_event_history(NodeCompletedEvent)
        
        assert len(history) == 5
    
    def test_event_bus_thread_safety(self, event_bus):
        """Test event bus handles concurrent operations."""
        received = []
        lock = threading.Lock()
        
        def safe_append(event):
            with lock:
                received.append(event)
        
        event_bus.subscribe(NodeCompletedEvent, safe_append)
        
        def publish_events(start_id: int):
            for i in range(10):
                event_bus.publish(NodeCompletedEvent(
                    execution_id=f"exec-{start_id}-{i}",
                    node_id=f"node_{i}",
                ))
        
        threads = [
            threading.Thread(target=publish_events, args=(i,))
            for i in range(5)
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(received) == 50


# ═══════════════════════════════════════════════════════════════════════════════
# Event Bridge Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestEventBridge:
    """Tests for LangGraph event bridge."""
    
    def test_bridge_transforms_chain_start(self, event_bus, event_bridge):
        """Test bridge transforms on_chain_start events."""
        received = []
        event_bus.subscribe(NodeStartedEvent, received.append)
        
        lg_event = {
            "event": "on_chain_start",
            "name": "test_node",
            "run_id": "run-1",
            "data": {},
        }
        
        wtb_event = event_bridge._transform_event(lg_event, "exec-1", "thread-1")
        
        assert isinstance(wtb_event, NodeStartedEvent)
        assert wtb_event.node_id == "test_node"
    
    def test_bridge_transforms_chain_end(self, event_bus, event_bridge):
        """Test bridge transforms on_chain_end events."""
        # First start the node
        start_event = {
            "event": "on_chain_start",
            "name": "test_node",
            "run_id": "run-1",
        }
        event_bridge._transform_event(start_event, "exec-1", "thread-1")
        
        # Then end it
        end_event = {
            "event": "on_chain_end",
            "name": "test_node",
            "run_id": "run-1",
            "data": {"output": {"result": "success"}},
        }
        
        wtb_event = event_bridge._transform_event(end_event, "exec-1", "thread-1")
        
        assert isinstance(wtb_event, NodeCompletedEvent)
        assert wtb_event.result == {"result": "success"}
    
    def test_bridge_transforms_chain_error(self, event_bus, event_bridge):
        """Test bridge transforms on_chain_error events."""
        lg_event = {
            "event": "on_chain_error",
            "name": "test_node",
            "run_id": "run-1",
            "data": {
                "error": "Test error message",
                "error_type": "ValueError",
            },
        }
        
        wtb_event = event_bridge._transform_event(lg_event, "exec-1", "thread-1")
        
        assert isinstance(wtb_event, NodeFailedEvent)
        assert wtb_event.error_message == "Test error message"
    
    def test_bridge_filters_internal_events(self, event_bridge):
        """Test bridge config filters internal LangGraph events via should_include_event."""
        internal_event = {
            "event": "on_chain_start",
            "name": "__start__",
            "run_id": "run-1",
        }
        
        # Filtering happens via stream_config.should_include_event, not _transform_event
        # With filter_internal=True (default for testing config), internal nodes are filtered
        config = StreamModeConfig.for_testing()
        assert config.filter_internal is True
        
        # Internal event should be excluded by the filter
        assert config.should_include_event(internal_event) is False
        
        # Regular node should pass the filter
        normal_event = {
            "event": "on_chain_start",
            "name": "node_a",
            "run_id": "run-1",
        }
        assert config.should_include_event(normal_event) is True
    
    def test_bridge_tracks_statistics(self, event_bridge):
        """Test bridge tracks processing statistics."""
        # Simulate some processing
        event_bridge._events_processed = 10
        event_bridge._events_published = 8
        event_bridge._errors = 2
        
        stats = event_bridge.get_stats()
        
        assert stats["events_processed"] == 10
        assert stats["events_published"] == 8
        assert stats["errors"] == 2
    
    def test_bridge_reset_stats(self, event_bridge):
        """Test bridge statistics reset."""
        event_bridge._events_processed = 10
        event_bridge._events_published = 5
        
        event_bridge.reset_stats()
        
        stats = event_bridge.get_stats()
        assert stats["events_processed"] == 0
        assert stats["events_published"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Stream Mode Config Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestStreamModeConfig:
    """Tests for stream mode configuration."""
    
    def test_testing_config_minimal(self):
        """Test testing config is minimal."""
        config = StreamModeConfig.for_testing()
        
        assert config.modes == ["updates"]
        assert config.include_inputs is False
    
    def test_development_config_detailed(self):
        """Test development config is detailed."""
        config = StreamModeConfig.for_development()
        
        assert "debug" in config.modes
        assert config.include_inputs is True
        assert config.filter_internal is False
    
    def test_production_config_balanced(self):
        """Test production config is balanced."""
        config = StreamModeConfig.for_production()
        
        assert config.include_inputs is False  # PII protection
        assert config.filter_internal is True
    
    def test_full_audit_config(self):
        """Test full audit config captures everything."""
        config = StreamModeConfig.for_full_audit()
        
        assert len(config.modes) >= 5
        assert "values" in config.modes
        assert "messages" in config.modes
    
    def test_should_include_event_filtering(self):
        """Test event inclusion filtering."""
        config = StreamModeConfig(filter_internal=True)
        
        assert config.should_include_event({"name": "__pregel_push"}) is False
        assert config.should_include_event({"name": "my_node"}) is True
    
    def test_config_serialization_roundtrip(self):
        """Test config serialization/deserialization."""
        original = StreamModeConfig.for_development()
        
        data = original.to_dict()
        restored = StreamModeConfig.from_dict(data)
        
        assert restored.modes == original.modes
        assert restored.include_inputs == original.include_inputs


# ═══════════════════════════════════════════════════════════════════════════════
# Metrics Collection Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestMetricsCollection:
    """Tests for metrics collection from events."""
    
    def test_metrics_track_active_executions(self, event_bus, metrics_collector):
        """Test metrics track active executions."""
        event_bus.publish(ExecutionStartedEvent(
            execution_id="exec-1",
            workflow_id="wf-1",
        ))
        
        metrics = metrics_collector.get_metrics()
        assert metrics["active_executions"] == 1
        
        event_bus.publish(ExecutionCompletedEvent(execution_id="exec-1"))
        
        metrics = metrics_collector.get_metrics()
        assert metrics["active_executions"] == 0
    
    def test_metrics_track_completed_executions(self, event_bus, metrics_collector):
        """Test metrics track completed executions."""
        event_bus.publish(ExecutionStartedEvent(execution_id="exec-1"))
        event_bus.publish(ExecutionCompletedEvent(execution_id="exec-1", duration_ms=1000))
        
        metrics = metrics_collector.get_metrics()
        assert metrics["executions_total"]["completed"] == 1
    
    def test_metrics_track_failed_executions(self, event_bus, metrics_collector):
        """Test metrics track failed executions."""
        event_bus.publish(ExecutionStartedEvent(execution_id="exec-1"))
        event_bus.publish(ExecutionFailedEvent(
            execution_id="exec-1",
            error_type="ValueError",
            error_message="Test error",
        ))
        
        metrics = metrics_collector.get_metrics()
        assert metrics["executions_total"]["failed"] == 1
        assert metrics["errors_total"]["ValueError"] == 1
    
    def test_metrics_track_node_executions(self, event_bus, metrics_collector):
        """Test metrics track node-level executions."""
        for i in range(3):
            event_bus.publish(NodeCompletedEvent(
                execution_id="exec-1",
                node_id="node_a",
                duration_ms=100.0 * (i + 1),
            ))
        
        metrics = metrics_collector.get_metrics()
        assert metrics["node_executions_total"]["node_a"]["completed"] == 3
    
    def test_metrics_track_checkpoint_creation(self, event_bus, metrics_collector):
        """Test metrics track checkpoint creation."""
        for i in range(5):
            event_bus.publish(CheckpointCreated(
                execution_id="exec-1",
                checkpoint_id=f"cp-{i}",
                step=i,
            ))
        
        metrics = metrics_collector.get_metrics()
        assert metrics["checkpoints_total"] == 5
    
    def test_metrics_reset(self, event_bus, metrics_collector):
        """Test metrics can be reset."""
        event_bus.publish(ExecutionStartedEvent(execution_id="exec-1"))
        event_bus.publish(ExecutionCompletedEvent(execution_id="exec-1"))
        
        metrics_collector.reset()
        
        metrics = metrics_collector.get_metrics()
        assert metrics["executions_total"]["completed"] == 0
        assert metrics["active_executions"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Sync Execution Bridge Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSyncExecutionBridge:
    """Tests for synchronous execution bridging."""
    
    def test_bridge_sync_execution_publishes_events(self, event_bus, collected_events):
        """Test bridging sync execution publishes lifecycle events."""
        bridge = create_event_bridge_for_testing()
        bridge._event_bus = event_bus
        
        mock_graph = MagicMock()
        mock_graph.stream.return_value = iter([
            {"node_a": {"result": "a"}},
            {"node_b": {"result": "b"}},
        ])
        
        bridge.bridge_execution_sync(
            graph=mock_graph,
            initial_state={},
            config={"configurable": {"thread_id": "thread-1"}},
            execution_id="exec-1",
            workflow_id="wf-1",
        )
        
        event_types = [type(e).__name__ for e in collected_events]
        assert "ExecutionStartedEvent" in event_types
        assert "NodeCompletedEvent" in event_types
        assert "ExecutionCompletedEvent" in event_types
    
    def test_bridge_sync_execution_handles_error(self, event_bus, collected_events):
        """Test bridging sync execution handles errors."""
        bridge = create_event_bridge_for_testing()
        bridge._event_bus = event_bus
        
        mock_graph = MagicMock()
        mock_graph.stream.side_effect = ValueError("Test error")
        
        with pytest.raises(ValueError):
            bridge.bridge_execution_sync(
                graph=mock_graph,
                initial_state={},
                config={"configurable": {"thread_id": "thread-1"}},
                execution_id="exec-1",
            )
        
        event_types = [type(e).__name__ for e in collected_events]
        assert "ExecutionFailedEvent" in event_types


# ═══════════════════════════════════════════════════════════════════════════════
# Event Flow Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestEventFlowIntegration:
    """Tests for complete event flow integration."""
    
    def test_full_execution_event_flow(self, full_integration_setup):
        """Test complete event flow during execution."""
        setup = full_integration_setup
        event_bus = setup["event_bus"]
        metrics = setup["metrics_collector"]
        
        # Simulate execution lifecycle
        event_bus.publish(ExecutionStartedEvent(
            execution_id="exec-1",
            workflow_id="wf-1",
        ))
        
        for node in ["node_a", "node_b", "node_c"]:
            event_bus.publish(NodeStartedEvent(
                execution_id="exec-1",
                node_id=node,
            ))
            event_bus.publish(NodeCompletedEvent(
                execution_id="exec-1",
                node_id=node,
                duration_ms=100.0,
            ))
            event_bus.publish(CheckpointCreated(
                execution_id="exec-1",
                checkpoint_id=f"cp-{node}",
                step=["node_a", "node_b", "node_c"].index(node) + 1,
                completed_node=node,
            ))
        
        event_bus.publish(ExecutionCompletedEvent(
            execution_id="exec-1",
            workflow_id="wf-1",
            duration_ms=300.0,
        ))
        
        # Verify metrics
        m = metrics.get_metrics()
        assert m["active_executions"] == 0
        assert m["executions_total"]["completed"] == 1
        assert m["checkpoints_total"] == 3
        assert len(m["node_executions_total"]) == 3
    
    def test_execution_with_failure_event_flow(self, full_integration_setup):
        """Test event flow when execution fails."""
        setup = full_integration_setup
        event_bus = setup["event_bus"]
        metrics = setup["metrics_collector"]
        
        event_bus.publish(ExecutionStartedEvent(execution_id="exec-fail"))
        
        event_bus.publish(NodeStartedEvent(
            execution_id="exec-fail",
            node_id="failing_node",
        ))
        event_bus.publish(NodeFailedEvent(
            execution_id="exec-fail",
            node_id="failing_node",
            error_type="ValueError",
            error_message="Test failure",
        ))
        
        event_bus.publish(ExecutionFailedEvent(
            execution_id="exec-fail",
            error_type="ValueError",
            error_message="Node failed",
        ))
        
        m = metrics.get_metrics()
        assert m["active_executions"] == 0
        assert m["executions_total"]["failed"] == 1
        assert "ValueError" in m["errors_total"]


# ═══════════════════════════════════════════════════════════════════════════════
# Event Ordering and Consistency Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestEventOrdering:
    """Tests for event ordering and consistency."""
    
    def test_events_have_timestamps(self, event_bus):
        """Test all events have timestamps."""
        events = [
            ExecutionStartedEvent(execution_id="e1"),
            NodeStartedEvent(execution_id="e1", node_id="n1"),
            NodeCompletedEvent(execution_id="e1", node_id="n1"),
            ExecutionCompletedEvent(execution_id="e1"),
        ]
        
        for event in events:
            assert hasattr(event, 'timestamp')
            assert event.timestamp is not None
    
    def test_event_sequence_preserved(self, event_bus):
        """Test event sequence is preserved in history."""
        received = []
        
        event_bus.subscribe(NodeCompletedEvent, received.append)
        
        for i in range(5):
            event_bus.publish(NodeCompletedEvent(
                execution_id="exec-1",
                node_id=f"node_{i}",
            ))
        
        # Order should be preserved
        node_ids = [e.node_id for e in received]
        assert node_ids == [f"node_{i}" for i in range(5)]
    
    def test_execution_events_causally_ordered(self, event_bus):
        """Test execution events follow causal order."""
        events = []
        
        event_bus.subscribe(ExecutionStartedEvent, events.append)
        event_bus.subscribe(NodeStartedEvent, events.append)
        event_bus.subscribe(NodeCompletedEvent, events.append)
        event_bus.subscribe(ExecutionCompletedEvent, events.append)
        
        # Publish in correct causal order
        event_bus.publish(ExecutionStartedEvent(execution_id="e1"))
        event_bus.publish(NodeStartedEvent(execution_id="e1", node_id="n1"))
        event_bus.publish(NodeCompletedEvent(execution_id="e1", node_id="n1"))
        event_bus.publish(ExecutionCompletedEvent(execution_id="e1"))
        
        # Verify order
        assert isinstance(events[0], ExecutionStartedEvent)
        assert isinstance(events[1], NodeStartedEvent)
        assert isinstance(events[2], NodeCompletedEvent)
        assert isinstance(events[3], ExecutionCompletedEvent)
