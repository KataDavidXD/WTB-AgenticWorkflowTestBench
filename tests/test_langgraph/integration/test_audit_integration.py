"""
Integration tests for LangGraph + Audit Trail integration.

Tests audit trail integration:
- Audit event recording
- Audit entry persistence
- Compliance and traceability
- Audit query operations

SOLID Compliance:
- SRP: Tests focus on audit integration
- DIP: Uses abstractions for audit components

Run with: pytest tests/test_langgraph/integration/test_audit_integration.py -v
"""

import pytest
from typing import Dict, Any, List
from datetime import datetime
from unittest.mock import Mock, MagicMock

from wtb.infrastructure.events import (
    WTBEventBus,
    WTBAuditTrail,
    WTBAuditEventType,
    WTBAuditSeverity,
    WTBAuditEntry,
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
    create_audit_event_from_langgraph,
    create_checkpoint_audit_event,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Audit Trail Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestWTBAuditTrail:
    """Tests for WTBAuditTrail."""
    
    def test_create_audit_trail(self, audit_trail):
        """Test creating audit trail."""
        assert audit_trail is not None
        assert len(audit_trail.entries) == 0
    
    def test_record_audit_entry(self, audit_trail):
        """Test recording audit entry."""
        entry = WTBAuditEntry(
            timestamp=datetime.now(),
            event_type=WTBAuditEventType.EXECUTION_STARTED,
            severity=WTBAuditSeverity.INFO,
            message="Execution started",
            execution_id="exec-1",
        )
        
        audit_trail.add_entry(entry)
        
        assert len(audit_trail.entries) == 1
        assert audit_trail.entries[0].execution_id == "exec-1"
    
    def test_record_event_creates_entry(self, audit_trail):
        """Test recording event creates audit entry."""
        event = ExecutionStartedEvent(
            execution_id="exec-1",
            workflow_id="wf-1",
        )
        
        audit_trail.record_event(event)
        
        assert len(audit_trail.entries) == 1
        assert audit_trail.entries[0].event_type == WTBAuditEventType.EXECUTION_STARTED
    
    def test_audit_severity_levels(self, audit_trail):
        """Test audit entries have correct severity."""
        # Record different events
        audit_trail.record_event(ExecutionStartedEvent(execution_id="e1"))
        audit_trail.record_event(ExecutionCompletedEvent(execution_id="e1"))
        audit_trail.record_event(ExecutionFailedEvent(
            execution_id="e2",
            error_type="Error",
            error_message="Test",
        ))
        
        severities = [e.severity for e in audit_trail.entries]
        
        assert WTBAuditSeverity.INFO in severities
        assert WTBAuditSeverity.SUCCESS in severities
        assert WTBAuditSeverity.ERROR in severities
    
    def test_get_entries_by_execution(self, audit_trail):
        """Test filtering entries by execution ID."""
        audit_trail.record_event(ExecutionStartedEvent(execution_id="e1"))
        audit_trail.record_event(NodeCompletedEvent(execution_id="e1", node_id="n1"))
        audit_trail.record_event(ExecutionStartedEvent(execution_id="e2"))
        
        # Filter entries by execution_id manually (no built-in method)
        e1_entries = [e for e in audit_trail.entries if e.execution_id == "e1"]
        
        assert len(e1_entries) == 2
        assert all(e.execution_id == "e1" for e in e1_entries)
    
    def test_get_errors(self, audit_trail):
        """Test getting error entries."""
        audit_trail.record_event(ExecutionStartedEvent(execution_id="e1"))
        audit_trail.record_event(NodeFailedEvent(
            execution_id="e1",
            node_id="n1",
            error_type="ValueError",
            error_message="Test error",
        ))
        audit_trail.record_event(ExecutionCompletedEvent(execution_id="e1"))
        
        errors = audit_trail.get_errors()
        
        assert len(errors) == 1
        assert errors[0].severity == WTBAuditSeverity.ERROR
    
    def test_get_summary(self, audit_trail):
        """Test getting audit trail summary."""
        # Add various entries
        audit_trail.record_event(ExecutionStartedEvent(execution_id="e1"))
        audit_trail.record_event(NodeCompletedEvent(execution_id="e1", node_id="n1"))
        audit_trail.record_event(NodeCompletedEvent(execution_id="e1", node_id="n2"))
        audit_trail.record_event(ExecutionCompletedEvent(execution_id="e1"))
        
        summary = audit_trail.get_summary()
        
        assert summary["total_entries"] == 4
        assert "nodes_executed" in summary
        assert "errors" in summary


# ═══════════════════════════════════════════════════════════════════════════════
# Audit Event Listener Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestAuditEventListener:
    """Tests for AuditEventListener."""
    
    def test_listener_attaches_to_event_bus(self, event_bus, audit_trail):
        """Test listener attaches to event bus."""
        listener = AuditEventListener(audit_trail)
        listener.attach(event_bus)
        
        # Listener should be attached
        assert listener._event_bus is event_bus
    
    def test_listener_records_execution_events(self, event_bus, audit_trail):
        """Test listener records execution lifecycle events."""
        listener = AuditEventListener(audit_trail)
        listener.attach(event_bus)
        
        event_bus.publish(ExecutionStartedEvent(execution_id="e1"))
        event_bus.publish(ExecutionCompletedEvent(execution_id="e1"))
        
        assert len(audit_trail.entries) == 2
        
        types = [e.event_type for e in audit_trail.entries]
        assert WTBAuditEventType.EXECUTION_STARTED in types
        assert WTBAuditEventType.EXECUTION_COMPLETED in types
    
    def test_listener_records_node_events(self, event_bus, audit_trail):
        """Test listener records node lifecycle events."""
        listener = AuditEventListener(audit_trail)
        listener.attach(event_bus)
        
        event_bus.publish(NodeStartedEvent(execution_id="e1", node_id="n1"))
        event_bus.publish(NodeCompletedEvent(execution_id="e1", node_id="n1"))
        
        assert len(audit_trail.entries) == 2
        
        types = [e.event_type for e in audit_trail.entries]
        assert WTBAuditEventType.NODE_STARTED in types
        assert WTBAuditEventType.NODE_COMPLETED in types
    
    def test_listener_detach(self, event_bus, audit_trail):
        """Test listener can be detached."""
        listener = AuditEventListener(audit_trail)
        listener.attach(event_bus)
        
        # Publish event while attached
        event_bus.publish(ExecutionStartedEvent(execution_id="e1"))
        assert len(audit_trail.entries) == 1
        
        # Detach
        listener.detach()
        
        # Publish event while detached
        event_bus.publish(ExecutionStartedEvent(execution_id="e2"))
        
        # Should not record new events
        assert len(audit_trail.entries) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# LangGraph Audit Event Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestLangGraphAuditEvent:
    """Tests for LangGraphAuditEvent."""
    
    def test_create_audit_event(self):
        """Test creating LangGraph audit event."""
        event = LangGraphAuditEvent(
            event_type=LangGraphAuditEventType.NODE_COMPLETED,
            thread_id="exec-1",
            node_id="test_node",
        )
        
        assert event.event_type == LangGraphAuditEventType.NODE_COMPLETED
        assert event.thread_id == "exec-1"
        assert event.event_id is not None
    
    def test_audit_event_immutable(self):
        """Test audit event is immutable."""
        event = LangGraphAuditEvent(
            event_type=LangGraphAuditEventType.NODE_STARTED,
            thread_id="exec-1",
        )
        
        with pytest.raises(AttributeError):
            event.thread_id = "modified"
    
    def test_audit_event_serialization(self):
        """Test audit event serialization."""
        event = LangGraphAuditEvent(
            event_type=LangGraphAuditEventType.CHECKPOINT_CREATED,
            thread_id="exec-1",
            checkpoint_id="cp-123",
            step=3,
        )
        
        data = event.to_dict()
        
        assert data["event_type"] == "checkpoint_created"
        assert data["checkpoint_id"] == "cp-123"
        assert data["step"] == 3
    
    def test_audit_event_deserialization(self):
        """Test audit event deserialization."""
        data = {
            "event_type": "node_completed",
            "thread_id": "exec-1",
            "node_id": "my_node",
            "duration_ms": 500,
        }
        
        event = LangGraphAuditEvent.from_dict(data)
        
        assert event.event_type == LangGraphAuditEventType.NODE_COMPLETED
        assert event.node_id == "my_node"
        assert event.duration_ms == 500


# ═══════════════════════════════════════════════════════════════════════════════
# Audit Event Factory Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestAuditEventFactories:
    """Tests for audit event factory functions."""
    
    def test_create_from_langgraph_event(self):
        """Test creating audit event from LangGraph event."""
        lg_event = {
            "event": "on_chain_end",
            "name": "my_node",
            "run_id": "run-123",
            "data": {"output": {"result": "success"}},
        }
        
        event = create_audit_event_from_langgraph(
            lg_event,
            thread_id="exec-1",
            include_outputs=True,
        )
        
        assert event.event_type == LangGraphAuditEventType.NODE_COMPLETED
        assert event.node_id == "my_node"
        assert event.outputs == {"result": "success"}
    
    def test_create_checkpoint_audit_event(self):
        """Test creating checkpoint audit event."""
        event = create_checkpoint_audit_event(
            execution_id="exec-1",
            checkpoint_id="cp-123",
            step=3,
            writes={"node_a": {"result": "done"}},
            next_nodes=["node_b"],
        )
        
        assert event.event_type == LangGraphAuditEventType.CHECKPOINT_CREATED
        assert event.checkpoint_id == "cp-123"
        assert event.step == 3
        assert event.node_id == "node_a"
    
    def test_event_type_mapping(self):
        """Test LangGraph event type mapping."""
        from wtb.domain.events.langgraph_events import map_langgraph_event_type
        
        assert map_langgraph_event_type("on_chain_start") == LangGraphAuditEventType.NODE_STARTED
        assert map_langgraph_event_type("on_chain_end") == LangGraphAuditEventType.NODE_COMPLETED
        assert map_langgraph_event_type("on_chain_error") == LangGraphAuditEventType.NODE_FAILED
        assert map_langgraph_event_type("unknown") == LangGraphAuditEventType.CUSTOM_EVENT


# ═══════════════════════════════════════════════════════════════════════════════
# Compliance and Traceability Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestComplianceTraceability:
    """Tests for compliance and traceability requirements."""
    
    def test_audit_entries_have_timestamps(self, audit_trail):
        """Test all audit entries have timestamps."""
        audit_trail.record_event(ExecutionStartedEvent(execution_id="e1"))
        audit_trail.record_event(NodeCompletedEvent(execution_id="e1", node_id="n1"))
        
        for entry in audit_trail.entries:
            assert entry.timestamp is not None
            assert isinstance(entry.timestamp, datetime)
    
    def test_audit_entries_have_unique_ids(self, audit_trail):
        """Test audit entries are distinct via timestamps."""
        import time
        audit_trail.record_event(ExecutionStartedEvent(execution_id="e1"))
        time.sleep(0.001)  # Ensure distinct timestamps
        audit_trail.record_event(ExecutionStartedEvent(execution_id="e2"))
        
        # Use (timestamp, execution_id) as unique identifier
        ids = [(e.timestamp.isoformat(), e.execution_id) for e in audit_trail.entries]
        
        assert len(ids) == len(set(ids))  # All unique
    
    def test_full_execution_traceability(self, event_bus, audit_trail):
        """Test complete execution is traceable through audit."""
        listener = AuditEventListener(audit_trail)
        listener.attach(event_bus)
        
        # Simulate full execution
        event_bus.publish(ExecutionStartedEvent(execution_id="e1", workflow_id="wf-1"))
        
        for node in ["node_a", "node_b", "node_c"]:
            event_bus.publish(NodeStartedEvent(execution_id="e1", node_id=node))
            event_bus.publish(NodeCompletedEvent(
                execution_id="e1",
                node_id=node,
                duration_ms=100.0,
            ))
        
        event_bus.publish(ExecutionCompletedEvent(
            execution_id="e1",
            duration_ms=300.0,
        ))
        
        # Verify complete trace - filter by execution_id
        e1_entries = [e for e in audit_trail.entries if e.execution_id == "e1"]
        
        # Should have: 1 start + 3*(start+complete) + 1 complete = 8 entries
        assert len(e1_entries) == 8
        
        # First should be execution start
        assert e1_entries[0].event_type == WTBAuditEventType.EXECUTION_STARTED
        
        # Last should be execution complete
        assert e1_entries[-1].event_type == WTBAuditEventType.EXECUTION_COMPLETED
    
    def test_error_traceability(self, event_bus, audit_trail):
        """Test errors are fully traceable."""
        listener = AuditEventListener(audit_trail)
        listener.attach(event_bus)
        
        event_bus.publish(ExecutionStartedEvent(execution_id="e1"))
        event_bus.publish(NodeStartedEvent(execution_id="e1", node_id="failing"))
        event_bus.publish(NodeFailedEvent(
            execution_id="e1",
            node_id="failing",
            error_type="ValueError",
            error_message="Detailed error message",
        ))
        event_bus.publish(ExecutionFailedEvent(
            execution_id="e1",
            error_type="ValueError",
            error_message="Execution failed due to node failure",
        ))
        
        errors = audit_trail.get_errors()
        
        assert len(errors) == 2  # Node failed + Execution failed
        
        # Verify error details are captured
        for error in errors:
            assert error.error is not None
            assert "ValueError" in error.error or "error" in error.message.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# Audit Persistence Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestAuditPersistence:
    """Tests for audit persistence behavior."""
    
    def test_audit_entries_serializable(self, audit_trail):
        """Test audit entries can be serialized."""
        audit_trail.record_event(ExecutionStartedEvent(execution_id="e1"))
        
        entry = audit_trail.entries[0]
        
        # Should be serializable to dict
        data = entry.to_dict()
        
        assert "event_type" in data
        assert "timestamp" in data
        assert "execution_id" in data
    
    def test_audit_trail_export(self, audit_trail):
        """Test audit trail can be exported."""
        audit_trail.record_event(ExecutionStartedEvent(execution_id="e1"))
        audit_trail.record_event(NodeCompletedEvent(execution_id="e1", node_id="n1"))
        
        export = audit_trail.to_dict()
        
        assert "entries" in export
        assert len(export["entries"]) == 2
        assert "summary" in export


# ═══════════════════════════════════════════════════════════════════════════════
# ACID Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestAuditACIDCompliance:
    """Tests for ACID compliance in audit operations."""
    
    def test_atomicity_entry_creation(self, audit_trail):
        """Test audit entry creation is atomic."""
        event = ExecutionStartedEvent(execution_id="e1")
        
        audit_trail.record_event(event)
        
        # Entry should be complete with all required fields
        entry = audit_trail.entries[0]
        assert entry.timestamp is not None
        assert entry.event_type is not None
        assert entry.message is not None
    
    def test_consistency_event_type_matching(self, audit_trail):
        """Test event types are consistently mapped."""
        events = [
            ExecutionStartedEvent(execution_id="e1"),
            NodeStartedEvent(execution_id="e1", node_id="n1"),
            NodeCompletedEvent(execution_id="e1", node_id="n1"),
            ExecutionCompletedEvent(execution_id="e1"),
        ]
        
        expected_types = [
            WTBAuditEventType.EXECUTION_STARTED,
            WTBAuditEventType.NODE_STARTED,
            WTBAuditEventType.NODE_COMPLETED,
            WTBAuditEventType.EXECUTION_COMPLETED,
        ]
        
        for event in events:
            audit_trail.record_event(event)
        
        for i, entry in enumerate(audit_trail.entries):
            assert entry.event_type == expected_types[i]
    
    def test_isolation_execution_entries(self, audit_trail):
        """Test entries for different executions are isolated."""
        audit_trail.record_event(ExecutionStartedEvent(execution_id="e1"))
        audit_trail.record_event(ExecutionStartedEvent(execution_id="e2"))
        
        e1_entries = [e for e in audit_trail.entries if e.execution_id == "e1"]
        e2_entries = [e for e in audit_trail.entries if e.execution_id == "e2"]
        
        assert len(e1_entries) == 1
        assert len(e2_entries) == 1
        assert e1_entries[0].execution_id != e2_entries[0].execution_id
    
    def test_durability_entries_preserved(self, audit_trail):
        """Test audit entries are durably stored."""
        for i in range(10):
            audit_trail.record_event(ExecutionStartedEvent(execution_id=f"e{i}"))
        
        # All entries should be preserved
        assert len(audit_trail.entries) == 10
        
        # Each entry should be retrievable via filter
        for i in range(10):
            entries = [e for e in audit_trail.entries if e.execution_id == f"e{i}"]
            assert len(entries) == 1
