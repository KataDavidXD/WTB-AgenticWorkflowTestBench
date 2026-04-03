"""
Integration tests for LangGraph Audit and Event System.

Tests the full integration of LangGraph event bridging with WTB EventBus,
audit persistence, and metrics collection.

Run with: pytest tests/test_wtb/test_langgraph_audit_integration.py -v
"""

import pytest
from datetime import datetime
from typing import Dict, Any, List
from unittest.mock import MagicMock, patch
import threading

from wtb.infrastructure.events import (
    WTBEventBus,
    StreamModeConfig,
    LangGraphEventBridge,
    create_event_bridge_for_testing,
    InMemoryMetricsCollector,
    WTBAuditTrail,
    AuditEventListener,
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
)
from wtb.domain.events.checkpoint_events import CheckpointCreated
from wtb.config import WTBConfig, LangGraphEventConfig


# ═══════════════════════════════════════════════════════════════════════════════
# Test Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def event_bus():
    """Create fresh event bus."""
    return WTBEventBus(max_history=1000)


@pytest.fixture
def audit_trail():
    """Create audit trail."""
    return WTBAuditTrail()


@pytest.fixture
def metrics_collector(event_bus):
    """Create metrics collector."""
    return InMemoryMetricsCollector(event_bus)


@pytest.fixture
def event_bridge(event_bus):
    """Create event bridge with event bus."""
    return LangGraphEventBridge(
        event_bus=event_bus,
        stream_config=StreamModeConfig.for_testing(),
        emit_audit_events=True,
    )


@pytest.fixture
def full_integration_setup(event_bus):
    """Set up full integration with all components."""
    # Create components
    audit_trail = WTBAuditTrail()
    metrics_collector = InMemoryMetricsCollector(event_bus)
    event_bridge = LangGraphEventBridge(
        event_bus=event_bus,
        stream_config=StreamModeConfig.for_development(),
        emit_audit_events=True,
    )
    
    # Create audit listener (subscribes to events)
    audit_listener = AuditEventListener(audit_trail)
    audit_listener.attach(event_bus)
    
    return {
        "event_bus": event_bus,
        "audit_trail": audit_trail,
        "metrics_collector": metrics_collector,
        "event_bridge": event_bridge,
        "audit_listener": audit_listener,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Config Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestLangGraphEventConfigIntegration:
    """Tests for LangGraphEventConfig integration with WTBConfig."""
    
    def test_langgraph_event_config_for_testing(self):
        """Testing config should have minimal audit overhead."""
        config = LangGraphEventConfig.for_testing()
        
        assert config.enabled is True
        assert config.emit_audit_events is False
        assert config.prometheus_enabled is False
        assert "updates" in config.stream_modes
    
    def test_langgraph_event_config_for_development(self):
        """Development config should include debug modes."""
        config = LangGraphEventConfig.for_development()
        
        assert config.enabled is True
        assert config.emit_audit_events is True
        assert config.include_inputs is True
        assert "debug" in config.stream_modes
    
    def test_langgraph_event_config_for_production(self):
        """Production config should protect PII and enable metrics."""
        config = LangGraphEventConfig.for_production()
        
        assert config.enabled is True
        assert config.prometheus_enabled is True
        assert config.include_inputs is False  # PII protection
        assert config.filter_internal is True
    
    def test_wtb_config_includes_event_config(self):
        """WTBConfig should include LangGraph event config."""
        config = WTBConfig.for_testing()
        
        assert config.langgraph_event_config is not None
        assert config.langgraph_event_config.emit_audit_events is False
    
    def test_event_config_serialization(self):
        """Event config should serialize to dict."""
        config = LangGraphEventConfig.for_testing()
        
        data = config.to_dict()
        
        assert "enabled" in data
        assert "stream_modes" in data
        assert "emit_audit_events" in data
        assert data["emit_audit_events"] is False
    
    def test_event_config_deserialization(self):
        """Event config should deserialize from dict."""
        data = {
            "enabled": True,
            "stream_modes": ["values", "updates"],
            "include_inputs": True,
            "include_outputs": False,
            "emit_audit_events": True,
            "prometheus_enabled": False,
            "filter_internal": True,
        }
        
        config = LangGraphEventConfig.from_dict(data)
        
        assert config.enabled is True
        assert config.stream_modes == ["values", "updates"]
        assert config.include_inputs is True
        assert config.include_outputs is False


# ═══════════════════════════════════════════════════════════════════════════════
# Event Bus Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestEventBusIntegration:
    """Tests for event bus integration with LangGraph events."""
    
    def test_event_bus_receives_execution_events(self, event_bus):
        """Event bus should receive execution lifecycle events."""
        received = []
        event_bus.subscribe(ExecutionStartedEvent, received.append)
        event_bus.subscribe(ExecutionCompletedEvent, received.append)
        
        # Publish events
        event_bus.publish(ExecutionStartedEvent(
            execution_id="exec-1",
            workflow_id="wf-1",
        ))
        event_bus.publish(ExecutionCompletedEvent(
            execution_id="exec-1",
            workflow_id="wf-1",
            duration_ms=1000.0,
        ))
        
        assert len(received) == 2
        assert isinstance(received[0], ExecutionStartedEvent)
        assert isinstance(received[1], ExecutionCompletedEvent)
    
    def test_event_bus_receives_node_events(self, event_bus):
        """Event bus should receive node lifecycle events."""
        received = []
        event_bus.subscribe(NodeStartedEvent, received.append)
        event_bus.subscribe(NodeCompletedEvent, received.append)
        
        # Publish events
        event_bus.publish(NodeStartedEvent(
            execution_id="exec-1",
            node_id="node_a",
        ))
        event_bus.publish(NodeCompletedEvent(
            execution_id="exec-1",
            node_id="node_a",
            duration_ms=100.0,
        ))
        
        assert len(received) == 2
        assert received[0].node_id == "node_a"
        assert received[1].node_id == "node_a"
    
    def test_event_history_preserved(self, event_bus):
        """Event bus should preserve event history."""
        # Publish multiple events
        for i in range(5):
            event_bus.publish(NodeCompletedEvent(
                execution_id=f"exec-{i}",
                node_id=f"node_{i}",
            ))
        
        # Get history
        history = event_bus.get_event_history(NodeCompletedEvent)
        
        assert len(history) == 5
    
    def test_multiple_subscribers(self, event_bus):
        """Multiple subscribers should all receive events."""
        received_1 = []
        received_2 = []
        received_3 = []
        
        event_bus.subscribe(ExecutionStartedEvent, received_1.append)
        event_bus.subscribe(ExecutionStartedEvent, received_2.append)
        event_bus.subscribe(ExecutionStartedEvent, received_3.append)
        
        event_bus.publish(ExecutionStartedEvent(execution_id="exec-1"))
        
        assert len(received_1) == 1
        assert len(received_2) == 1
        assert len(received_3) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Metrics Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestMetricsIntegration:
    """Tests for metrics collection integration."""
    
    def test_metrics_track_execution_lifecycle(self, event_bus, metrics_collector):
        """Metrics should track execution lifecycle."""
        # Simulate execution lifecycle
        event_bus.publish(ExecutionStartedEvent(
            execution_id="exec-1",
            workflow_id="wf-1",
        ))
        
        assert metrics_collector.get_metrics()["active_executions"] == 1
        
        event_bus.publish(ExecutionCompletedEvent(
            execution_id="exec-1",
            duration_ms=500.0,
        ))
        
        assert metrics_collector.get_metrics()["active_executions"] == 0
        assert metrics_collector.get_metrics()["executions_total"]["completed"] == 1
    
    def test_metrics_track_node_execution(self, event_bus, metrics_collector):
        """Metrics should track node execution."""
        # Simulate node executions
        for i in range(3):
            event_bus.publish(NodeCompletedEvent(
                execution_id="exec-1",
                node_id="node_a",
                duration_ms=100.0 * (i + 1),
            ))
        
        node_metrics = metrics_collector.get_metrics()["node_executions_total"]
        
        assert "node_a" in node_metrics
        assert node_metrics["node_a"]["completed"] == 3
    
    def test_metrics_track_errors(self, event_bus, metrics_collector):
        """Metrics should track errors."""
        event_bus.publish(ExecutionStartedEvent(execution_id="exec-1"))
        event_bus.publish(ExecutionFailedEvent(
            execution_id="exec-1",
            error_type="ValueError",
            error_message="Test error",
        ))
        
        assert metrics_collector.get_metrics()["errors_total"]["ValueError"] == 1
    
    def test_metrics_track_checkpoints(self, event_bus, metrics_collector):
        """Metrics should track checkpoint creation."""
        for i in range(5):
            event_bus.publish(CheckpointCreated(
                execution_id="exec-1",
                checkpoint_id=f"cp-{i}",
                step=i,
            ))
        
        assert metrics_collector.get_metrics()["checkpoints_total"] == 5
    
    def test_metrics_reset(self, event_bus, metrics_collector):
        """Metrics should be resettable."""
        event_bus.publish(ExecutionStartedEvent(execution_id="exec-1"))
        event_bus.publish(ExecutionCompletedEvent(execution_id="exec-1"))
        
        metrics_collector.reset()
        
        assert metrics_collector.get_metrics()["executions_total"]["completed"] == 0
        assert metrics_collector.get_metrics()["active_executions"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Event Bridge Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestEventBridgeIntegration:
    """Tests for event bridge integration."""
    
    def test_event_bridge_publishes_to_bus(self, event_bus, event_bridge):
        """Event bridge should publish transformed events to bus."""
        received = []
        event_bus.subscribe(NodeCompletedEvent, received.append)
        
        # Simulate LangGraph event transformation
        lg_event = {
            "event": "on_chain_end",
            "name": "my_node",
            "run_id": "run-1",
            "data": {"output": {"result": "success"}},
        }
        
        # First start the node to track timing
        start_event = {"event": "on_chain_start", "name": "my_node", "run_id": "run-1"}
        event_bridge._transform_event(start_event, "exec-1", "thread-1")
        
        # Transform and publish
        wtb_event = event_bridge._transform_event(lg_event, "exec-1", "thread-1")
        event_bridge._publish_event(wtb_event)
        
        assert len(received) == 1
        assert received[0].node_id == "my_node"
    
    def test_event_bridge_stats_tracking(self, event_bridge):
        """Event bridge should track statistics."""
        # Simulate some processing
        event_bridge._events_processed = 10
        event_bridge._events_published = 8
        event_bridge._errors = 2
        
        stats = event_bridge.get_stats()
        
        assert stats["events_processed"] == 10
        assert stats["events_published"] == 8
        assert stats["errors"] == 2
    
    def test_event_bridge_stats_reset(self, event_bridge):
        """Event bridge stats should be resettable."""
        event_bridge._events_processed = 10
        event_bridge._events_published = 8
        
        event_bridge.reset_stats()
        
        stats = event_bridge.get_stats()
        assert stats["events_processed"] == 0
        assert stats["events_published"] == 0
    
    def test_event_bridge_filters_internal_events(self, event_bridge):
        """Event bridge should filter internal LangGraph events."""
        # Internal events start with __
        internal_event = {
            "event": "on_chain_start",
            "name": "__internal_node",
            "run_id": "run-1",
        }
        
        # Should not include internal events (depending on config)
        config = StreamModeConfig.for_production()
        assert config.filter_internal is True
        assert config.should_include_event(internal_event) is False
    
    def test_event_bridge_includes_user_events(self, event_bridge):
        """Event bridge should include user-defined events."""
        user_event = {
            "event": "on_chain_start",
            "name": "my_custom_node",
            "run_id": "run-1",
        }
        
        config = StreamModeConfig.for_production()
        assert config.should_include_event(user_event) is True


# ═══════════════════════════════════════════════════════════════════════════════
# Stream Mode Config Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestStreamModeConfig:
    """Tests for StreamModeConfig."""
    
    def test_for_testing_minimal(self):
        """Testing config should be minimal."""
        config = StreamModeConfig.for_testing()
        
        assert config.modes == ["updates"]
        assert config.include_inputs is False
        assert config.emit_timing_events is False
    
    def test_for_development_detailed(self):
        """Development config should be detailed."""
        config = StreamModeConfig.for_development()
        
        assert "debug" in config.modes
        assert config.include_inputs is True
        assert config.filter_internal is False
    
    def test_for_production_balanced(self):
        """Production config should be balanced."""
        config = StreamModeConfig.for_production()
        
        assert "updates" in config.modes
        assert config.include_inputs is False  # PII protection
        assert config.filter_internal is True
    
    def test_for_full_audit_complete(self):
        """Full audit config should capture everything."""
        config = StreamModeConfig.for_full_audit()
        
        assert len(config.modes) >= 5
        assert "values" in config.modes
        assert "messages" in config.modes
        assert "debug" in config.modes
    
    def test_custom_config(self):
        """Custom config should accept custom modes."""
        config = StreamModeConfig.custom(
            modes=["values", "custom"],
            include_inputs=True,
        )
        
        assert config.modes == ["values", "custom"]
        assert config.include_inputs is True
    
    def test_serialization(self):
        """StreamModeConfig should serialize/deserialize."""
        original = StreamModeConfig.for_development()
        
        data = original.to_dict()
        restored = StreamModeConfig.from_dict(data)
        
        assert restored.modes == original.modes
        assert restored.include_inputs == original.include_inputs
        assert restored.filter_internal == original.filter_internal


# ═══════════════════════════════════════════════════════════════════════════════
# Checkpoint Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckpointIntegration:
    """Tests for checkpoint event integration."""
    
    def test_checkpoint_events_published(self, event_bus):
        """Checkpoint events should be published."""
        received = []
        event_bus.subscribe(CheckpointCreated, received.append)
        
        # Publish checkpoint event
        event_bus.publish(CheckpointCreated(
            execution_id="exec-1",
            checkpoint_id="cp-123",
            step=3,
            node_writes={"node_a": {"result": "done"}},
            completed_node="node_a",
        ))
        
        assert len(received) == 1
        assert received[0].checkpoint_id == "cp-123"
        assert received[0].step == 3
        assert received[0].completed_node == "node_a"
    
    def test_checkpoint_terminal_detection(self, event_bus):
        """Checkpoint should detect terminal state."""
        received = []
        event_bus.subscribe(CheckpointCreated, received.append)
        
        # Terminal checkpoint has no next nodes
        event_bus.publish(CheckpointCreated(
            execution_id="exec-1",
            checkpoint_id="cp-final",
            step=5,
            next_nodes=[],
            is_terminal=True,
        ))
        
        assert received[0].is_terminal is True


# ═══════════════════════════════════════════════════════════════════════════════
# LangGraph Audit Event Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestLangGraphAuditEvent:
    """Tests for LangGraphAuditEvent."""
    
    def test_audit_event_creation(self):
        """LangGraphAuditEvent should be creatable with defaults."""
        event = LangGraphAuditEvent(
            event_type=LangGraphAuditEventType.NODE_COMPLETED,
            thread_id="exec-1",
        )
        
        assert event.event_type == LangGraphAuditEventType.NODE_COMPLETED
        assert event.thread_id == "exec-1"
        assert event.event_id is not None
        assert event.timestamp is not None
    
    def test_audit_event_immutable(self):
        """LangGraphAuditEvent should be immutable."""
        event = LangGraphAuditEvent(
            event_type=LangGraphAuditEventType.NODE_STARTED,
            thread_id="exec-1",
        )
        
        with pytest.raises(Exception):  # FrozenInstanceError
            event.thread_id = "exec-2"
    
    def test_audit_event_serialization(self):
        """LangGraphAuditEvent should serialize to dict."""
        event = LangGraphAuditEvent(
            event_type=LangGraphAuditEventType.CHECKPOINT_CREATED,
            thread_id="exec-1",
            checkpoint_id="cp-123",
            step=3,
        )
        
        data = event.to_dict()
        
        assert data["event_type"] == "checkpoint_created"
        assert data["thread_id"] == "exec-1"
        assert data["checkpoint_id"] == "cp-123"
        assert data["step"] == 3
    
    def test_audit_event_deserialization(self):
        """LangGraphAuditEvent should deserialize from dict."""
        data = {
            "event_type": "node_completed",
            "thread_id": "exec-1",
            "node_id": "my_node",
            "duration_ms": 500,
        }
        
        event = LangGraphAuditEvent.from_dict(data)
        
        assert event.event_type == LangGraphAuditEventType.NODE_COMPLETED
        assert event.thread_id == "exec-1"
        assert event.node_id == "my_node"
        assert event.duration_ms == 500
    
    def test_event_type_mapping(self):
        """LangGraph event types should map correctly."""
        from wtb.domain.events.langgraph_events import map_langgraph_event_type
        
        assert map_langgraph_event_type("on_chain_start") == LangGraphAuditEventType.NODE_STARTED
        assert map_langgraph_event_type("on_chain_end") == LangGraphAuditEventType.NODE_COMPLETED
        assert map_langgraph_event_type("on_chain_error") == LangGraphAuditEventType.NODE_FAILED
        assert map_langgraph_event_type("on_tool_start") == LangGraphAuditEventType.TOOL_STARTED
        assert map_langgraph_event_type("on_tool_end") == LangGraphAuditEventType.TOOL_COMPLETED
        assert map_langgraph_event_type("unknown") == LangGraphAuditEventType.CUSTOM_EVENT


# ═══════════════════════════════════════════════════════════════════════════════
# SOLID Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestSOLIDCompliance:
    """Tests verifying SOLID principle compliance."""
    
    def test_single_responsibility_event_bridge(self, event_bridge):
        """Event bridge should only handle event transformation and bridging."""
        # Event bridge should not handle persistence
        assert not hasattr(event_bridge, 'save')
        assert not hasattr(event_bridge, 'load')
        
        # Event bridge should not handle business logic
        assert not hasattr(event_bridge, 'execute_workflow')
    
    def test_open_closed_stream_config(self):
        """Stream config should be extensible without modification."""
        # Can create custom config without modifying class
        custom = StreamModeConfig.custom(
            modes=["values", "custom"],
            include_inputs=True,
        )
        
        assert custom.modes == ["values", "custom"]
        assert custom.include_inputs is True
    
    def test_dependency_inversion_event_bridge(self, event_bus):
        """Event bridge should depend on abstractions."""
        # Event bridge accepts any event bus, not specific implementation
        bridge1 = LangGraphEventBridge(event_bus=event_bus)
        
        mock_bus = MagicMock()
        mock_bus.publish = MagicMock()
        bridge2 = LangGraphEventBridge(event_bus=mock_bus)
        
        # Both should work
        assert bridge1._event_bus is not None
        assert bridge2._event_bus is not None
    
    def test_single_responsibility_metrics_collector(self, event_bus, metrics_collector):
        """Metrics collector should only collect metrics."""
        # Should not publish events
        assert not hasattr(metrics_collector, 'publish')
        
        # Should only track metrics
        assert hasattr(metrics_collector, 'get_metrics')
        assert hasattr(metrics_collector, 'reset')


# ═══════════════════════════════════════════════════════════════════════════════
# ACID Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestACIDCompliance:
    """Tests verifying ACID property compliance."""
    
    def test_atomicity_event_publishing(self, event_bus):
        """Each event should be published atomically."""
        received = []
        event_bus.subscribe(NodeCompletedEvent, received.append)
        
        # Publish event - should be all or nothing
        event_bus.publish(NodeCompletedEvent(
            execution_id="exec-1",
            node_id="node_a",
        ))
        
        assert len(received) == 1
        # No partial state - event is fully received
        assert received[0].execution_id == "exec-1"
        assert received[0].node_id == "node_a"
    
    def test_consistency_event_validation(self):
        """Events should maintain consistent state."""
        # Events with required fields should be valid
        event = LangGraphAuditEvent(
            event_type=LangGraphAuditEventType.NODE_COMPLETED,
            thread_id="exec-1",
        )
        
        assert event.event_type is not None
        assert event.thread_id == "exec-1"
        assert event.timestamp is not None
    
    def test_isolation_execution_tracking(self, event_bus, metrics_collector):
        """Different executions should be isolated."""
        # Start two executions
        event_bus.publish(ExecutionStartedEvent(execution_id="exec-1", workflow_id="wf-1"))
        event_bus.publish(ExecutionStartedEvent(execution_id="exec-2", workflow_id="wf-2"))
        
        # Complete only one
        event_bus.publish(ExecutionCompletedEvent(execution_id="exec-1"))
        
        # Verify isolation
        assert metrics_collector.get_metrics()["active_executions"] == 1
    
    def test_durability_event_history(self, event_bus):
        """Events should be durably stored in history."""
        # Publish events
        for i in range(10):
            event_bus.publish(NodeCompletedEvent(
                execution_id=f"exec-{i}",
                node_id=f"node_{i}",
            ))
        
        # Events should persist in history
        history = event_bus.get_event_history()
        
        assert len(history) >= 10


# ═══════════════════════════════════════════════════════════════════════════════
# Thread Safety Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestThreadSafety:
    """Tests for thread safety of event components."""
    
    def test_concurrent_event_publishing(self, event_bus):
        """Event bus should handle concurrent publishing."""
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
        
        # Create threads
        threads = [
            threading.Thread(target=publish_events, args=(i,))
            for i in range(5)
        ]
        
        # Start all threads
        for t in threads:
            t.start()
        
        # Wait for completion
        for t in threads:
            t.join()
        
        # All events should be received
        assert len(received) == 50
    
    def test_concurrent_subscription(self, event_bus):
        """Event bus should handle concurrent subscription."""
        handlers = []
        lock = threading.Lock()
        
        def add_handler(handler_id: int):
            def handler(event):
                with lock:
                    handlers.append(handler_id)
            event_bus.subscribe(NodeCompletedEvent, handler)
        
        # Create subscription threads
        threads = [
            threading.Thread(target=add_handler, args=(i,))
            for i in range(10)
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # Publish event
        event_bus.publish(NodeCompletedEvent(execution_id="exec-1"))
        
        # All handlers should have been called
        assert len(handlers) == 10


# ═══════════════════════════════════════════════════════════════════════════════
# Factory Function Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestFactoryFunctions:
    """Tests for factory functions."""
    
    def test_create_event_bridge_for_testing(self):
        """Factory should create testing-configured bridge."""
        bridge = create_event_bridge_for_testing()
        
        assert bridge is not None
        assert bridge._emit_audit_events is False
        assert bridge._stream_config.emit_timing_events is False
    
    def test_wtb_config_factory_methods(self):
        """WTBConfig factory methods should work correctly."""
        # Testing config
        test_config = WTBConfig.for_testing()
        assert test_config.wtb_storage_mode == "inmemory"
        assert test_config.langgraph_event_config is not None
        
        # Development config
        dev_config = WTBConfig.for_development()
        assert dev_config.wtb_storage_mode == "sqlalchemy"
        assert dev_config.langgraph_event_config is not None
        assert dev_config.langgraph_event_config.emit_audit_events is True


# ═══════════════════════════════════════════════════════════════════════════════
# End-to-End Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestEndToEndIntegration:
    """End-to-end integration tests."""
    
    def test_full_execution_flow(self, full_integration_setup):
        """Test full execution flow from start to completion."""
        setup = full_integration_setup
        event_bus = setup["event_bus"]
        metrics = setup["metrics_collector"]
        
        # Start execution
        event_bus.publish(ExecutionStartedEvent(
            execution_id="exec-1",
            workflow_id="wf-1",
        ))
        
        # Execute nodes
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
            # Create checkpoint after each node
            event_bus.publish(CheckpointCreated(
                execution_id="exec-1",
                checkpoint_id=f"cp-{node}",
                step=["node_a", "node_b", "node_c"].index(node),
                completed_node=node,
            ))
        
        # Complete execution
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
    
    def test_execution_with_failure(self, full_integration_setup):
        """Test execution flow with failure."""
        setup = full_integration_setup
        event_bus = setup["event_bus"]
        metrics = setup["metrics_collector"]
        
        # Start execution
        event_bus.publish(ExecutionStartedEvent(
            execution_id="exec-fail",
            workflow_id="wf-1",
        ))
        
        # Node starts but fails
        event_bus.publish(NodeStartedEvent(
            execution_id="exec-fail",
            node_id="failing_node",
        ))
        event_bus.publish(NodeFailedEvent(
            execution_id="exec-fail",
            node_id="failing_node",
            error_type="ValueError",
            error_message="Something went wrong",
        ))
        
        # Execution fails
        event_bus.publish(ExecutionFailedEvent(
            execution_id="exec-fail",
            workflow_id="wf-1",
            error_type="ValueError",
            error_message="Node failed",
        ))
        
        # Verify metrics
        m = metrics.get_metrics()
        assert m["active_executions"] == 0
        assert m["executions_total"]["failed"] == 1
        assert "ValueError" in m["errors_total"]
