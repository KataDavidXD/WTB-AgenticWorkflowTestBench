"""
Unit tests for LangGraph Event Bridge.

Tests the bridging of LangGraph streaming events to WTB EventBus,
including event transformation, metrics collection, and configuration.

Run with: pytest tests/test_wtb/test_langgraph_event_bridge.py -v
"""

import pytest
from datetime import datetime
from typing import Dict, Any, List
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio

from wtb.infrastructure.events import (
    WTBEventBus,
    StreamModeConfig,
    LangGraphEventBridge,
    NodeExecutionTracker,
    create_event_bridge,
    create_event_bridge_for_testing,
    InMemoryMetricsCollector,
)
from wtb.domain.events import (
    ExecutionStartedEvent,
    ExecutionCompletedEvent,
    ExecutionFailedEvent,
    NodeStartedEvent,
    NodeCompletedEvent,
    NodeFailedEvent,
    LangGraphAuditEvent,
    LangGraphAuditEventType,
    create_audit_event_from_langgraph,
    create_checkpoint_audit_event,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def event_bus():
    """Create fresh event bus for each test."""
    return WTBEventBus(max_history=100)


@pytest.fixture
def stream_config():
    """Create test stream configuration."""
    return StreamModeConfig.for_testing()


@pytest.fixture
def event_bridge(event_bus, stream_config):
    """Create event bridge for testing."""
    return LangGraphEventBridge(
        event_bus=event_bus,
        stream_config=stream_config,
        emit_audit_events=False,
    )


@pytest.fixture
def collected_events(event_bus):
    """Collect events published to event bus."""
    events = []
    
    def collect(event):
        events.append(event)
    
    # Subscribe to all event types
    event_bus.subscribe(ExecutionStartedEvent, collect)
    event_bus.subscribe(ExecutionCompletedEvent, collect)
    event_bus.subscribe(ExecutionFailedEvent, collect)
    event_bus.subscribe(NodeStartedEvent, collect)
    event_bus.subscribe(NodeCompletedEvent, collect)
    event_bus.subscribe(NodeFailedEvent, collect)
    
    return events


# ═══════════════════════════════════════════════════════════════════════════════
# StreamModeConfig Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestStreamModeConfig:
    """Tests for StreamModeConfig."""
    
    def test_for_testing_minimal_modes(self):
        """Testing config should have minimal modes."""
        config = StreamModeConfig.for_testing()
        
        assert config.modes == ["updates"]
        assert config.include_inputs is False
        assert config.include_outputs is True
        assert config.filter_internal is True
    
    def test_for_development_detailed_modes(self):
        """Development config should have detailed modes."""
        config = StreamModeConfig.for_development()
        
        assert "values" in config.modes
        assert "updates" in config.modes
        assert "debug" in config.modes
        assert config.include_inputs is True
        assert config.filter_internal is False
    
    def test_for_production_balanced_modes(self):
        """Production config should have balanced modes."""
        config = StreamModeConfig.for_production()
        
        assert config.modes == ["updates", "custom"]
        assert config.include_inputs is False  # PII protection
        assert config.include_outputs is True
    
    def test_for_full_audit_all_modes(self):
        """Full audit config should have all modes."""
        config = StreamModeConfig.for_full_audit()
        
        assert len(config.modes) == 6
        assert "events" in config.modes
        assert "messages" in config.modes
    
    def test_should_include_event_filters_internal(self):
        """Should filter internal nodes when configured."""
        config = StreamModeConfig(filter_internal=True)
        
        # Internal nodes should be filtered
        assert config.should_include_event({"name": "__pregel_push"}) is False
        assert config.should_include_event({"name": "__start__"}) is False
        
        # Regular nodes should pass
        assert config.should_include_event({"name": "my_node"}) is True
    
    def test_should_include_event_node_filter(self):
        """Should filter by node name when configured."""
        config = StreamModeConfig(
            node_name_filter=["allowed_node", "another_node"]
        )
        
        assert config.should_include_event({"name": "allowed_node"}) is True
        assert config.should_include_event({"name": "blocked_node"}) is False
    
    def test_should_include_event_exclude_nodes(self):
        """Should exclude specific nodes when configured."""
        config = StreamModeConfig(
            exclude_node_names=["excluded_node"]
        )
        
        assert config.should_include_event({"name": "regular_node"}) is True
        assert config.should_include_event({"name": "excluded_node"}) is False
    
    def test_serialization_roundtrip(self):
        """Config should serialize and deserialize correctly."""
        config = StreamModeConfig.for_development()
        
        data = config.to_dict()
        restored = StreamModeConfig.from_dict(data)
        
        assert restored.modes == config.modes
        assert restored.include_inputs == config.include_inputs
        assert restored.include_outputs == config.include_outputs


# ═══════════════════════════════════════════════════════════════════════════════
# NodeExecutionTracker Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestNodeExecutionTracker:
    """Tests for NodeExecutionTracker."""
    
    def test_track_node_duration(self):
        """Should track node start and calculate duration."""
        tracker = NodeExecutionTracker()
        
        # Start tracking
        tracker.node_started("run-1")
        
        # Simulate some time passing (in practice, this would be actual execution)
        import time
        time.sleep(0.01)  # 10ms
        
        # Complete and get duration
        duration = tracker.node_completed("run-1")
        
        assert duration is not None
        assert duration >= 10  # At least 10ms
    
    def test_unknown_run_id_returns_none(self):
        """Should return None for unknown run IDs."""
        tracker = NodeExecutionTracker()
        
        duration = tracker.node_completed("unknown-run")
        
        assert duration is None
    
    def test_clear_removes_all_tracking(self):
        """Should clear all tracked nodes."""
        tracker = NodeExecutionTracker()
        
        tracker.node_started("run-1")
        tracker.node_started("run-2")
        tracker.clear()
        
        assert tracker.node_completed("run-1") is None
        assert tracker.node_completed("run-2") is None


# ═══════════════════════════════════════════════════════════════════════════════
# LangGraphAuditEvent Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestLangGraphAuditEvent:
    """Tests for LangGraphAuditEvent."""
    
    def test_create_audit_event(self):
        """Should create audit event with correct fields."""
        event = LangGraphAuditEvent(
            event_type=LangGraphAuditEventType.NODE_COMPLETED,
            thread_id="exec-1",
            run_id="run-1",
            node_id="my_node",
        )
        
        assert event.event_type == LangGraphAuditEventType.NODE_COMPLETED
        assert event.thread_id == "exec-1"
        assert event.node_id == "my_node"
        assert event.event_id  # Should have auto-generated ID
    
    def test_audit_event_immutable(self):
        """Audit events should be immutable."""
        event = LangGraphAuditEvent(
            event_type=LangGraphAuditEventType.NODE_COMPLETED,
            thread_id="exec-1",
        )
        
        # Should raise error when trying to modify
        with pytest.raises(AttributeError):
            event.thread_id = "modified"
    
    def test_to_dict_serialization(self):
        """Should serialize to dictionary correctly."""
        event = LangGraphAuditEvent(
            event_type=LangGraphAuditEventType.CHECKPOINT_CREATED,
            thread_id="exec-1",
            checkpoint_id="cp-1",
            step=5,
        )
        
        data = event.to_dict()
        
        assert data["event_type"] == "checkpoint_created"
        assert data["thread_id"] == "exec-1"
        assert data["checkpoint_id"] == "cp-1"
        assert data["step"] == 5
    
    def test_from_dict_deserialization(self):
        """Should deserialize from dictionary correctly."""
        data = {
            "event_type": "node_completed",
            "thread_id": "exec-1",
            "node_id": "my_node",
            "duration_ms": 100,
        }
        
        event = LangGraphAuditEvent.from_dict(data)
        
        assert event.event_type == LangGraphAuditEventType.NODE_COMPLETED
        assert event.thread_id == "exec-1"
        assert event.duration_ms == 100


# ═══════════════════════════════════════════════════════════════════════════════
# Factory Function Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestFactoryFunctions:
    """Tests for audit event factory functions."""
    
    def test_create_audit_event_from_langgraph(self):
        """Should create audit event from LangGraph event."""
        lg_event = {
            "event": "on_chain_end",
            "name": "my_node",
            "run_id": "run-123",
            "data": {"output": {"result": "success"}},
            "metadata": {"key": "value"},
            "tags": ["tag1"],
        }
        
        event = create_audit_event_from_langgraph(
            lg_event,
            thread_id="exec-1",
            include_outputs=True,
        )
        
        assert event.event_type == LangGraphAuditEventType.NODE_COMPLETED
        assert event.thread_id == "exec-1"
        assert event.run_id == "run-123"
        assert event.node_id == "my_node"
        assert event.outputs == {"result": "success"}
    
    def test_create_checkpoint_audit_event(self):
        """Should create checkpoint audit event."""
        event = create_checkpoint_audit_event(
            execution_id="exec-1",
            checkpoint_id="cp-123",
            step=3,
            writes={"node_a": {"foo": "bar"}},
            next_nodes=["node_b"],
        )
        
        assert event.event_type == LangGraphAuditEventType.CHECKPOINT_CREATED
        assert event.checkpoint_id == "cp-123"
        assert event.step == 3
        assert event.node_id == "node_a"
        assert event.outputs == {"node_a": {"foo": "bar"}}


# ═══════════════════════════════════════════════════════════════════════════════
# LangGraphEventBridge Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestLangGraphEventBridge:
    """Tests for LangGraphEventBridge."""
    
    def test_create_event_bridge(self, event_bus):
        """Should create event bridge with dependencies."""
        bridge = LangGraphEventBridge(
            event_bus=event_bus,
            stream_config=StreamModeConfig.for_testing(),
        )
        
        assert bridge._event_bus is event_bus
        assert bridge.include_inputs is False
        assert bridge.include_outputs is True
    
    def test_factory_create_for_testing(self):
        """Should create testing bridge with correct config."""
        bridge = create_event_bridge_for_testing()
        
        assert isinstance(bridge, LangGraphEventBridge)
        assert bridge._emit_audit_events is False
    
    def test_transform_node_start_event(self, event_bridge):
        """Should transform on_chain_start to NodeStartedEvent."""
        lg_event = {
            "event": "on_chain_start",
            "name": "my_node",
            "run_id": "run-1",
            "data": {},
        }
        
        wtb_event = event_bridge._transform_event(lg_event, "exec-1", "thread-1")
        
        assert isinstance(wtb_event, NodeStartedEvent)
        assert wtb_event.execution_id == "exec-1"
        assert wtb_event.node_id == "my_node"
    
    def test_transform_node_end_event(self, event_bridge):
        """Should transform on_chain_end to NodeCompletedEvent."""
        # First start the node
        start_event = {
            "event": "on_chain_start",
            "name": "my_node",
            "run_id": "run-1",
        }
        event_bridge._transform_event(start_event, "exec-1", "thread-1")
        
        # Then end it
        end_event = {
            "event": "on_chain_end",
            "name": "my_node",
            "run_id": "run-1",
            "data": {"output": {"result": "success"}},
        }
        
        wtb_event = event_bridge._transform_event(end_event, "exec-1", "thread-1")
        
        assert isinstance(wtb_event, NodeCompletedEvent)
        assert wtb_event.execution_id == "exec-1"
        assert wtb_event.result == {"result": "success"}
    
    def test_transform_node_error_event(self, event_bridge):
        """Should transform on_chain_error to NodeFailedEvent."""
        lg_event = {
            "event": "on_chain_error",
            "name": "my_node",
            "run_id": "run-1",
            "data": {"error": "Something went wrong", "error_type": "ValueError"},
        }
        
        wtb_event = event_bridge._transform_event(lg_event, "exec-1", "thread-1")
        
        assert isinstance(wtb_event, NodeFailedEvent)
        assert wtb_event.error_message == "Something went wrong"
    
    def test_transform_skips_graph_level_events(self, event_bridge):
        """Should skip LangGraph-level events."""
        lg_event = {
            "event": "on_chain_start",
            "name": "LangGraph",
            "run_id": "run-1",
        }
        
        wtb_event = event_bridge._transform_event(lg_event, "exec-1", "thread-1")
        
        assert wtb_event is None
    
    def test_get_stats(self, event_bridge):
        """Should track event statistics."""
        # Initially all zeros
        stats = event_bridge.get_stats()
        
        assert stats["events_processed"] == 0
        assert stats["events_published"] == 0
        assert stats["errors"] == 0
    
    def test_reset_stats(self, event_bridge):
        """Should reset statistics."""
        event_bridge._events_processed = 10
        event_bridge._events_published = 5
        
        event_bridge.reset_stats()
        
        stats = event_bridge.get_stats()
        assert stats["events_processed"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# InMemoryMetricsCollector Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestInMemoryMetricsCollector:
    """Tests for InMemoryMetricsCollector."""
    
    def test_tracks_execution_started(self, event_bus):
        """Should track execution started events."""
        collector = InMemoryMetricsCollector(event_bus)
        
        event_bus.publish(ExecutionStartedEvent(
            execution_id="exec-1",
            workflow_id="wf-1",
        ))
        
        metrics = collector.get_metrics()
        assert metrics["active_executions"] == 1
    
    def test_tracks_execution_completed(self, event_bus):
        """Should track execution completed events."""
        collector = InMemoryMetricsCollector(event_bus)
        
        # Start
        event_bus.publish(ExecutionStartedEvent(execution_id="exec-1"))
        
        # Complete
        event_bus.publish(ExecutionCompletedEvent(
            execution_id="exec-1",
            duration_ms=1000.0,
        ))
        
        metrics = collector.get_metrics()
        assert metrics["active_executions"] == 0
        assert metrics["executions_total"]["completed"] == 1
        assert 1000.0 in metrics["execution_durations"]
    
    def test_tracks_execution_failed(self, event_bus):
        """Should track execution failed events."""
        collector = InMemoryMetricsCollector(event_bus)
        
        # Start
        event_bus.publish(ExecutionStartedEvent(execution_id="exec-1"))
        
        # Fail
        event_bus.publish(ExecutionFailedEvent(
            execution_id="exec-1",
            error_type="ValueError",
        ))
        
        metrics = collector.get_metrics()
        assert metrics["executions_total"]["failed"] == 1
        assert metrics["errors_total"]["ValueError"] == 1
    
    def test_tracks_node_completed(self, event_bus):
        """Should track node completed events."""
        collector = InMemoryMetricsCollector(event_bus)
        
        event_bus.publish(NodeCompletedEvent(
            execution_id="exec-1",
            node_id="my_node",
            duration_ms=100.0,
        ))
        
        metrics = collector.get_metrics()
        assert metrics["node_executions_total"]["my_node"]["completed"] == 1
        assert 100.0 in metrics["node_durations"]["my_node"]
    
    def test_reset_clears_metrics(self, event_bus):
        """Should reset all metrics."""
        collector = InMemoryMetricsCollector(event_bus)
        
        event_bus.publish(ExecutionStartedEvent(execution_id="exec-1"))
        collector.reset()
        
        metrics = collector.get_metrics()
        assert metrics["active_executions"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Sync Execution Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestSyncExecution:
    """Tests for synchronous execution bridging."""
    
    def test_bridge_execution_sync_publishes_events(self, event_bus, collected_events):
        """Should publish events during sync execution."""
        bridge = create_event_bridge_for_testing()
        bridge._event_bus = event_bus
        
        # Create mock graph
        mock_graph = MagicMock()
        mock_graph.stream.return_value = iter([
            {"node_a": {"result": "a"}},
            {"node_b": {"result": "b"}},
        ])
        
        result = bridge.bridge_execution_sync(
            graph=mock_graph,
            initial_state={},
            config={"configurable": {"thread_id": "thread-1"}},
            execution_id="exec-1",
            workflow_id="wf-1",
        )
        
        # Should have published events
        assert len(collected_events) >= 3  # Start + 2 nodes + Complete
        
        # Check event types
        event_types = [type(e).__name__ for e in collected_events]
        assert "ExecutionStartedEvent" in event_types
        assert "NodeCompletedEvent" in event_types
        assert "ExecutionCompletedEvent" in event_types
    
    def test_bridge_execution_sync_handles_error(self, event_bus, collected_events):
        """Should handle errors during sync execution."""
        bridge = create_event_bridge_for_testing()
        bridge._event_bus = event_bus
        
        # Create mock graph that raises
        mock_graph = MagicMock()
        mock_graph.stream.side_effect = ValueError("Test error")
        
        with pytest.raises(ValueError):
            bridge.bridge_execution_sync(
                graph=mock_graph,
                initial_state={},
                config={"configurable": {"thread_id": "thread-1"}},
                execution_id="exec-1",
            )
        
        # Should have published failure event
        event_types = [type(e).__name__ for e in collected_events]
        assert "ExecutionFailedEvent" in event_types


# ═══════════════════════════════════════════════════════════════════════════════
# ACID Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestACIDCompliance:
    """Tests for ACID compliance."""
    
    def test_events_are_immutable(self):
        """Audit events should be immutable (Consistency)."""
        event = LangGraphAuditEvent(
            event_type=LangGraphAuditEventType.NODE_COMPLETED,
            thread_id="exec-1",
        )
        
        with pytest.raises(AttributeError):
            event.thread_id = "modified"
    
    def test_event_ids_are_unique(self):
        """Each event should have unique ID (Atomicity/Isolation)."""
        event1 = LangGraphAuditEvent(thread_id="exec-1")
        event2 = LangGraphAuditEvent(thread_id="exec-1")
        
        assert event1.event_id != event2.event_id
    
    def test_thread_isolation(self, event_bus):
        """Events should be isolated by thread_id."""
        collector = InMemoryMetricsCollector(event_bus)
        
        # Publish events for different executions
        event_bus.publish(ExecutionStartedEvent(execution_id="exec-1", workflow_id="wf-1"))
        event_bus.publish(ExecutionStartedEvent(execution_id="exec-2", workflow_id="wf-2"))
        event_bus.publish(ExecutionCompletedEvent(execution_id="exec-1"))
        
        # Should track separately
        metrics = collector.get_metrics()
        assert metrics["active_executions"] == 1  # Only exec-2 still active
