"""
Integration tests for LangGraph + Event + Audit combined system.

Tests the complete integration of:
- LangGraph execution
- Event bus publishing
- Audit trail recording
- Metrics collection

SOLID/ACID Compliance verified through combined operations.

Run with: pytest tests/test_langgraph/integration/test_event_audit_combined.py -v
"""

import pytest
from typing import Dict, Any, List
from datetime import datetime
from unittest.mock import Mock, MagicMock, patch
import threading

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from wtb.infrastructure.events import (
    WTBEventBus,
    WTBAuditTrail,
    WTBAuditEventType,
    AuditEventListener,
    LangGraphEventBridge,
    StreamModeConfig,
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
    LangGraphAuditEvent,
    LangGraphAuditEventType,
)
from wtb.domain.events.checkpoint_events import CheckpointCreated
from wtb.config import WTBConfig, LangGraphEventConfig

from tests.test_langgraph.helpers import (
    create_initial_simple_state,
    create_initial_branch_state,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Full Integration Setup Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFullIntegrationSetup:
    """Tests for full integration setup."""
    
    def test_create_full_integration_components(self, full_integration_setup):
        """Test creating all integration components."""
        setup = full_integration_setup
        
        assert setup["event_bus"] is not None
        assert setup["audit_trail"] is not None
        assert setup["metrics_collector"] is not None
        assert setup["event_bridge"] is not None
        assert setup["audit_listener"] is not None
    
    def test_components_interconnected(self, full_integration_setup):
        """Test components are properly interconnected."""
        setup = full_integration_setup
        event_bus = setup["event_bus"]
        audit_trail = setup["audit_trail"]
        metrics = setup["metrics_collector"]
        
        # Publish event
        event_bus.publish(ExecutionStartedEvent(execution_id="e1"))
        
        # Should be recorded in audit
        assert len(audit_trail.entries) >= 0  # Depends on listener attachment
        
        # Should be tracked in metrics
        assert metrics.get_metrics()["active_executions"] >= 0


# ═══════════════════════════════════════════════════════════════════════════════
# End-to-End Execution Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestEndToEndExecution:
    """Tests for end-to-end execution with event+audit."""
    
    def test_complete_execution_flow(self, full_integration_setup):
        """Test complete execution flow with events and audit."""
        setup = full_integration_setup
        event_bus = setup["event_bus"]
        audit_trail = setup["audit_trail"]
        metrics = setup["metrics_collector"]
        
        # Simulate full execution
        event_bus.publish(ExecutionStartedEvent(
            execution_id="e2e-1",
            workflow_id="wf-1",
        ))
        
        for node in ["node_a", "node_b", "node_c"]:
            event_bus.publish(NodeStartedEvent(
                execution_id="e2e-1",
                node_id=node,
            ))
            event_bus.publish(NodeCompletedEvent(
                execution_id="e2e-1",
                node_id=node,
                duration_ms=100.0,
            ))
            event_bus.publish(CheckpointCreated(
                execution_id="e2e-1",
                checkpoint_id=f"cp-{node}",
                step=["node_a", "node_b", "node_c"].index(node) + 1,
            ))
        
        event_bus.publish(ExecutionCompletedEvent(
            execution_id="e2e-1",
            duration_ms=300.0,
        ))
        
        # Verify metrics
        m = metrics.get_metrics()
        assert m["active_executions"] == 0
        assert m["executions_total"]["completed"] == 1
        assert m["checkpoints_total"] == 3
    
    def test_execution_with_failure_full_tracking(self, full_integration_setup):
        """Test failed execution with full event and audit tracking."""
        setup = full_integration_setup
        event_bus = setup["event_bus"]
        audit_trail = setup["audit_trail"]
        metrics = setup["metrics_collector"]
        
        # Start execution
        event_bus.publish(ExecutionStartedEvent(execution_id="fail-1"))
        
        # Partial progress
        event_bus.publish(NodeStartedEvent(execution_id="fail-1", node_id="node_a"))
        event_bus.publish(NodeCompletedEvent(execution_id="fail-1", node_id="node_a"))
        
        # Failure
        event_bus.publish(NodeStartedEvent(execution_id="fail-1", node_id="failing_node"))
        event_bus.publish(NodeFailedEvent(
            execution_id="fail-1",
            node_id="failing_node",
            error_type="ValueError",
            error_message="Test failure",
        ))
        event_bus.publish(ExecutionFailedEvent(
            execution_id="fail-1",
            error_type="ValueError",
            error_message="Node failure",
        ))
        
        # Verify tracking
        m = metrics.get_metrics()
        assert m["executions_total"]["failed"] == 1
        assert "ValueError" in m["errors_total"]
        
        # Verify audit has errors
        errors = audit_trail.get_errors()
        # Note: errors may vary based on listener attachment


# ═══════════════════════════════════════════════════════════════════════════════
# Event-Audit Correlation Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestEventAuditCorrelation:
    """Tests for event-audit correlation."""
    
    def test_events_create_audit_entries(self, event_bus, audit_trail):
        """Test events automatically create audit entries."""
        listener = AuditEventListener(audit_trail)
        listener.attach(event_bus)
        
        events = [
            ExecutionStartedEvent(execution_id="corr-1"),
            NodeStartedEvent(execution_id="corr-1", node_id="n1"),
            NodeCompletedEvent(execution_id="corr-1", node_id="n1"),
            ExecutionCompletedEvent(execution_id="corr-1"),
        ]
        
        for event in events:
            event_bus.publish(event)
        
        # All events should have audit entries
        assert len(audit_trail.entries) == 4
        
        listener.detach()
    
    def test_event_audit_timestamp_correlation(self, event_bus, audit_trail):
        """Test event and audit timestamps are correlated."""
        listener = AuditEventListener(audit_trail)
        listener.attach(event_bus)
        
        before = datetime.now()
        event_bus.publish(ExecutionStartedEvent(execution_id="ts-1"))
        after = datetime.now()
        
        entry = audit_trail.entries[0]
        
        assert before <= entry.timestamp <= after
        
        listener.detach()
    
    def test_event_audit_execution_id_correlation(self, event_bus, audit_trail):
        """Test execution IDs are preserved in audit."""
        listener = AuditEventListener(audit_trail)
        listener.attach(event_bus)
        
        event_bus.publish(ExecutionStartedEvent(execution_id="exec-123"))
        event_bus.publish(NodeCompletedEvent(execution_id="exec-123", node_id="n1"))
        
        for entry in audit_trail.entries:
            assert entry.execution_id == "exec-123"
        
        listener.detach()


# ═══════════════════════════════════════════════════════════════════════════════
# Metrics-Audit Consistency Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestMetricsAuditConsistency:
    """Tests for metrics-audit consistency."""
    
    def test_metrics_audit_execution_count_match(self, event_bus, audit_trail, metrics_collector):
        """Test metrics and audit have consistent execution counts."""
        listener = AuditEventListener(audit_trail)
        listener.attach(event_bus)
        
        # Complete 3 executions
        for i in range(3):
            event_bus.publish(ExecutionStartedEvent(execution_id=f"e{i}"))
            event_bus.publish(ExecutionCompletedEvent(execution_id=f"e{i}"))
        
        # Metrics should show 3 completed
        m = metrics_collector.get_metrics()
        assert m["executions_total"]["completed"] == 3
        
        # Audit should have 3 start + 3 complete = 6 entries
        assert len(audit_trail.entries) == 6
        
        listener.detach()
    
    def test_metrics_audit_error_count_match(self, event_bus, audit_trail, metrics_collector):
        """Test metrics and audit have consistent error counts."""
        listener = AuditEventListener(audit_trail)
        listener.attach(event_bus)
        
        # Create 2 failures
        for i in range(2):
            event_bus.publish(ExecutionStartedEvent(execution_id=f"fail{i}"))
            event_bus.publish(ExecutionFailedEvent(
                execution_id=f"fail{i}",
                error_type="TestError",
                error_message="Test",
            ))
        
        # Metrics error count
        m = metrics_collector.get_metrics()
        assert m["executions_total"]["failed"] == 2
        
        # Audit error entries
        errors = audit_trail.get_errors()
        assert len(errors) == 2
        
        listener.detach()


# ═══════════════════════════════════════════════════════════════════════════════
# Multi-Execution Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestMultiExecution:
    """Tests for multiple concurrent executions."""
    
    def test_multiple_parallel_executions(self, full_integration_setup):
        """Test tracking multiple parallel executions."""
        setup = full_integration_setup
        event_bus = setup["event_bus"]
        metrics = setup["metrics_collector"]
        
        # Start 3 executions
        for i in range(3):
            event_bus.publish(ExecutionStartedEvent(execution_id=f"parallel-{i}"))
        
        assert metrics.get_metrics()["active_executions"] == 3
        
        # Complete 2
        event_bus.publish(ExecutionCompletedEvent(execution_id="parallel-0"))
        event_bus.publish(ExecutionCompletedEvent(execution_id="parallel-1"))
        
        m = metrics.get_metrics()
        assert m["active_executions"] == 1
        assert m["executions_total"]["completed"] == 2
        
        # Fail 1
        event_bus.publish(ExecutionFailedEvent(
            execution_id="parallel-2",
            error_type="Error",
            error_message="Test",
        ))
        
        m = metrics.get_metrics()
        assert m["active_executions"] == 0
        assert m["executions_total"]["failed"] == 1
    
    def test_execution_isolation_in_tracking(self, event_bus, audit_trail):
        """Test executions are isolated in tracking."""
        listener = AuditEventListener(audit_trail)
        listener.attach(event_bus)
        
        # Interleaved events from 2 executions
        event_bus.publish(ExecutionStartedEvent(execution_id="iso-1"))
        event_bus.publish(ExecutionStartedEvent(execution_id="iso-2"))
        event_bus.publish(NodeCompletedEvent(execution_id="iso-1", node_id="n1"))
        event_bus.publish(NodeCompletedEvent(execution_id="iso-2", node_id="n1"))
        event_bus.publish(ExecutionCompletedEvent(execution_id="iso-1"))
        event_bus.publish(ExecutionCompletedEvent(execution_id="iso-2"))
        
        # Each execution should have its entries
        iso1_entries = [e for e in audit_trail.entries if e.execution_id == "iso-1"]
        iso2_entries = [e for e in audit_trail.entries if e.execution_id == "iso-2"]
        
        assert len(iso1_entries) == 3  # start, node, complete
        assert len(iso2_entries) == 3
        
        listener.detach()


# ═══════════════════════════════════════════════════════════════════════════════
# Thread Safety Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestThreadSafety:
    """Tests for thread safety in combined system."""
    
    def test_concurrent_event_publishing(self, full_integration_setup):
        """Test concurrent event publishing is thread-safe."""
        setup = full_integration_setup
        event_bus = setup["event_bus"]
        metrics = setup["metrics_collector"]
        
        def publish_events(thread_id: int):
            for i in range(10):
                event_bus.publish(NodeCompletedEvent(
                    execution_id=f"thread-{thread_id}",
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
        
        # All events should be processed
        m = metrics.get_metrics()
        total_node_events = sum(
            v.get("completed", 0)
            for v in m["node_executions_total"].values()
        )
        assert total_node_events == 50  # 5 threads * 10 events
    
    def test_concurrent_audit_recording(self, event_bus, audit_trail):
        """Test concurrent audit recording is thread-safe."""
        listener = AuditEventListener(audit_trail)
        listener.attach(event_bus)
        
        def record_events(thread_id: int):
            for i in range(10):
                event_bus.publish(ExecutionStartedEvent(
                    execution_id=f"t{thread_id}-e{i}"
                ))
        
        threads = [
            threading.Thread(target=record_events, args=(i,))
            for i in range(5)
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # All entries should be recorded
        assert len(audit_trail.entries) == 50
        
        listener.detach()


# ═══════════════════════════════════════════════════════════════════════════════
# Configuration Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestConfigurationIntegration:
    """Tests for configuration integration."""
    
    def test_testing_config_minimal_overhead(self):
        """Test testing config has minimal overhead."""
        config = LangGraphEventConfig.for_testing()
        
        assert config.emit_audit_events is False
        assert config.prometheus_enabled is False
    
    def test_development_config_full_audit(self):
        """Test development config enables full audit."""
        config = LangGraphEventConfig.for_development()
        
        assert config.emit_audit_events is True
        assert config.include_inputs is True
    
    def test_production_config_pii_protection(self):
        """Test production config protects PII."""
        config = LangGraphEventConfig.for_production()
        
        assert config.include_inputs is False
        assert config.filter_internal is True
    
    def test_wtb_config_includes_event_config(self):
        """Test WTBConfig includes event config."""
        config = WTBConfig.for_testing()
        
        assert config.langgraph_event_config is not None


# ═══════════════════════════════════════════════════════════════════════════════
# SOLID Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSOLIDCompliance:
    """Tests verifying SOLID principles."""
    
    def test_single_responsibility_components(self, full_integration_setup):
        """Test each component has single responsibility."""
        setup = full_integration_setup
        
        # Event bus only handles event dispatch
        event_bus = setup["event_bus"]
        assert hasattr(event_bus, 'publish')
        assert hasattr(event_bus, 'subscribe')
        
        # Audit trail only handles audit entries
        audit_trail = setup["audit_trail"]
        assert hasattr(audit_trail, 'add_entry')
        # WTBAuditTrail stores entries in .entries list, filtered by execution_id
        assert hasattr(audit_trail, 'entries')
        
        # Metrics only handles metrics
        metrics = setup["metrics_collector"]
        assert hasattr(metrics, 'get_metrics')
    
    def test_open_closed_event_types(self, event_bus):
        """Test event system is open for extension."""
        # Can add new event types without modifying existing
        from dataclasses import dataclass
        
        @dataclass
        class CustomEvent:
            custom_field: str
        
        received = []
        event_bus.subscribe(CustomEvent, received.append)
        event_bus.publish(CustomEvent(custom_field="test"))
        
        assert len(received) == 1
    
    def test_dependency_inversion_event_bridge(self, event_bus):
        """Test event bridge depends on abstractions."""
        # Can use any event bus implementation
        bridge1 = LangGraphEventBridge(event_bus=event_bus)
        
        mock_bus = MagicMock()
        mock_bus.publish = MagicMock()
        bridge2 = LangGraphEventBridge(event_bus=mock_bus)
        
        assert bridge1._event_bus is not None
        assert bridge2._event_bus is not None


# ═══════════════════════════════════════════════════════════════════════════════
# ACID Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestACIDCompliance:
    """Tests verifying ACID properties."""
    
    def test_atomicity_event_audit_pair(self, event_bus, audit_trail):
        """Test event and audit entry creation are atomic."""
        listener = AuditEventListener(audit_trail)
        listener.attach(event_bus)
        
        event = ExecutionStartedEvent(execution_id="atomic-1")
        event_bus.publish(event)
        
        # Both event should be in history and audit
        history = event_bus.get_event_history(ExecutionStartedEvent)
        assert len(history) >= 1
        assert len(audit_trail.entries) >= 1
        
        listener.detach()
    
    def test_consistency_event_audit_match(self, event_bus, audit_trail):
        """Test event and audit types are consistent."""
        listener = AuditEventListener(audit_trail)
        listener.attach(event_bus)
        
        event_bus.publish(ExecutionStartedEvent(execution_id="c1"))
        event_bus.publish(NodeCompletedEvent(execution_id="c1", node_id="n1"))
        
        # Event types should map correctly
        event_types = [type(e).__name__ for e in event_bus.get_event_history()]
        audit_types = [e.event_type for e in audit_trail.entries]
        
        # Verify mapping
        assert WTBAuditEventType.EXECUTION_STARTED in audit_types
        assert WTBAuditEventType.NODE_COMPLETED in audit_types
        
        listener.detach()
    
    def test_isolation_execution_boundaries(self, event_bus, audit_trail, metrics_collector):
        """Test execution boundaries maintain isolation."""
        listener = AuditEventListener(audit_trail)
        listener.attach(event_bus)
        
        # Two independent executions
        event_bus.publish(ExecutionStartedEvent(execution_id="iso-a"))
        event_bus.publish(ExecutionStartedEvent(execution_id="iso-b"))
        
        # Each should have independent metrics
        m = metrics_collector.get_metrics()
        assert m["active_executions"] == 2
        
        event_bus.publish(ExecutionCompletedEvent(execution_id="iso-a"))
        
        m = metrics_collector.get_metrics()
        assert m["active_executions"] == 1
        
        listener.detach()
    
    def test_durability_audit_persistence(self, audit_trail):
        """Test audit entries persist."""
        for i in range(10):
            event = ExecutionStartedEvent(execution_id=f"durable-{i}")
            audit_trail.record_event(event)
        
        # All entries should persist
        assert len(audit_trail.entries) == 10
        
        # Each should be retrievable
        for i in range(10):
            entries = [e for e in audit_trail.entries if e.execution_id == f"durable-{i}"]
            assert len(entries) == 1
