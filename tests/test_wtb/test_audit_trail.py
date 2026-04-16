"""
Tests for WTBAuditTrail and AuditEventListener.

Tests audit entry creation, event recording, and display formatting.
"""

import pytest
from datetime import datetime, timedelta

from wtb.infrastructure.events.wtb_audit_trail import (
    WTBAuditEventType,
    WTBAuditSeverity,
    WTBAuditEntry,
    WTBAuditTrail,
    AuditEventListener,
)
from wtb.infrastructure.events.wtb_event_bus import WTBEventBus
from wtb.domain.events import (
    ExecutionStartedEvent,
    ExecutionCompletedEvent,
    ExecutionFailedEvent,
    ExecutionPausedEvent,
    NodeStartedEvent,
    NodeCompletedEvent,
    NodeFailedEvent,
    CheckpointCreatedEvent,
    RollbackPerformedEvent,
)


class TestWTBAuditEntry:
    """Tests for WTBAuditEntry dataclass."""
    
    def test_create_entry(self):
        """Can create an audit entry."""
        entry = WTBAuditEntry(
            timestamp=datetime.now(),
            event_type=WTBAuditEventType.EXECUTION_STARTED,
            severity=WTBAuditSeverity.INFO,
            message="Execution started",
            execution_id="exec-1",
        )
        
        assert entry.event_type == WTBAuditEventType.EXECUTION_STARTED
        assert entry.severity == WTBAuditSeverity.INFO
        assert entry.execution_id == "exec-1"
    
    def test_entry_to_dict(self):
        """Entry can be serialized to dict."""
        entry = WTBAuditEntry(
            timestamp=datetime(2025, 1, 9, 12, 0, 0),
            event_type=WTBAuditEventType.NODE_COMPLETED,
            severity=WTBAuditSeverity.SUCCESS,
            message="Node completed",
            execution_id="exec-1",
            node_id="node-1",
            duration_ms=150.5,
        )
        
        d = entry.to_dict()
        
        assert d["event_type"] == "node_completed"
        assert d["severity"] == "success"
        assert d["execution_id"] == "exec-1"
        assert d["node_id"] == "node-1"
        assert d["duration_ms"] == 150.5
    
    def test_entry_from_dict(self):
        """Entry can be deserialized from dict."""
        d = {
            "timestamp": "2025-01-09T12:00:00",
            "event_type": "node_failed",
            "severity": "error",
            "message": "Node failed",
            "execution_id": "exec-1",
            "node_id": "node-1",
            "error": "ValueError: Invalid input",
            "duration_ms": 50.0,
            "details": {"recoverable": True},
        }
        
        entry = WTBAuditEntry.from_dict(d)
        
        assert entry.event_type == WTBAuditEventType.NODE_FAILED
        assert entry.severity == WTBAuditSeverity.ERROR
        assert entry.error == "ValueError: Invalid input"
        assert entry.details["recoverable"] is True
    
    def test_entry_format_display(self):
        """Entry can be formatted for display."""
        entry = WTBAuditEntry(
            timestamp=datetime(2025, 1, 9, 12, 30, 45, 123000),
            event_type=WTBAuditEventType.EXECUTION_COMPLETED,
            severity=WTBAuditSeverity.SUCCESS,
            message="Execution completed (5 nodes)",
            execution_id="exec-1",
            duration_ms=1500.0,
        )
        
        display = entry.format_display()
        
        assert "12:30:45.123" in display
        assert "[+]" in display  # Success icon
        assert "Execution completed (5 nodes)" in display
        assert "1500.0ms" in display


class TestWTBAuditTrail:
    """Tests for WTBAuditTrail."""
    
    def test_create_trail(self):
        """Can create an audit trail."""
        trail = WTBAuditTrail(
            execution_id="exec-1",
            workflow_id="wf-1",
        )
        
        assert trail.execution_id == "exec-1"
        assert trail.workflow_id == "wf-1"
        assert len(trail.entries) == 0
    
    def test_add_entry(self):
        """Can add entries to trail."""
        trail = WTBAuditTrail()
        
        entry = WTBAuditEntry(
            timestamp=datetime.now(),
            event_type=WTBAuditEventType.EXECUTION_STARTED,
            severity=WTBAuditSeverity.INFO,
            message="Started",
        )
        
        trail.add_entry(entry)
        
        assert len(trail.entries) == 1
    
    def test_record_execution_started_event(self):
        """ExecutionStartedEvent is recorded correctly."""
        trail = WTBAuditTrail()
        
        event = ExecutionStartedEvent(
            execution_id="exec-1",
            workflow_id="wf-1",
            workflow_name="Test Workflow",
        )
        
        trail.record_event(event)
        
        assert len(trail.entries) == 1
        entry = trail.entries[0]
        assert entry.event_type == WTBAuditEventType.EXECUTION_STARTED
        assert entry.severity == WTBAuditSeverity.INFO
        assert "Test Workflow" in entry.message
    
    def test_record_execution_completed_event(self):
        """ExecutionCompletedEvent is recorded correctly."""
        trail = WTBAuditTrail()
        
        event = ExecutionCompletedEvent(
            execution_id="exec-1",
            workflow_id="wf-1",
            nodes_executed=5,
            checkpoints_created=3,
            duration_ms=1000.0,
        )
        
        trail.record_event(event)
        
        entry = trail.entries[0]
        assert entry.event_type == WTBAuditEventType.EXECUTION_COMPLETED
        assert entry.severity == WTBAuditSeverity.SUCCESS
        assert entry.duration_ms == 1000.0
    
    def test_record_execution_failed_event(self):
        """ExecutionFailedEvent is recorded correctly."""
        trail = WTBAuditTrail()
        
        event = ExecutionFailedEvent(
            execution_id="exec-1",
            workflow_id="wf-1",
            failed_at_node="node-3",
            error_message="Connection timeout",
            error_type="TimeoutError",
        )
        
        trail.record_event(event)
        
        entry = trail.entries[0]
        assert entry.event_type == WTBAuditEventType.EXECUTION_FAILED
        assert entry.severity == WTBAuditSeverity.ERROR
        assert entry.node_id == "node-3"
        assert "TimeoutError" in entry.error
    
    def test_record_node_completed_event(self):
        """NodeCompletedEvent is recorded correctly."""
        trail = WTBAuditTrail()
        
        event = NodeCompletedEvent(
            execution_id="exec-1",
            node_id="node-1",
            node_name="Process Data",
            tool_invocations=5,
            duration_ms=500.0,
        )
        
        trail.record_event(event)
        
        entry = trail.entries[0]
        assert entry.event_type == WTBAuditEventType.NODE_COMPLETED
        assert entry.severity == WTBAuditSeverity.SUCCESS
        assert "5 tool calls" in entry.message
    
    def test_record_checkpoint_created_event(self):
        """CheckpointCreatedEvent is recorded correctly."""
        trail = WTBAuditTrail()
        
        # Use new DDD-compliant event format (2026-01-15)
        event = CheckpointCreatedEvent(
            execution_id="exec-1",
            checkpoint_id="cp-42",
            step=1,
            completed_node="node-1",
        )
        
        trail.record_event(event)
        
        entry = trail.entries[0]
        assert entry.event_type == WTBAuditEventType.CHECKPOINT_CREATED
        assert "cp-42" in entry.message
    
    def test_record_rollback_event(self):
        """RollbackPerformedEvent is recorded correctly."""
        trail = WTBAuditTrail()
        
        # Use new DDD-compliant event format (2026-01-15)
        event = RollbackPerformedEvent(
            execution_id="exec-1",
            from_checkpoint_id="cp-20",
            to_checkpoint_id="cp-10",
            rolled_back_nodes=["node-2", "node-3", "node-4"],
        )
        
        trail.record_event(event)
        
        entry = trail.entries[0]
        assert entry.event_type == WTBAuditEventType.ROLLBACK_PERFORMED
        assert entry.severity == WTBAuditSeverity.WARNING
        assert "3 nodes reverted" in entry.message
    
    def test_flush_entries(self):
        """Can flush entries for persistence."""
        trail = WTBAuditTrail()
        
        trail.record_event(ExecutionStartedEvent(execution_id="exec-1", workflow_id="wf-1"))
        trail.record_event(ExecutionCompletedEvent(execution_id="exec-1", workflow_id="wf-1"))
        
        assert len(trail.entries) == 2
        
        flushed = trail.flush()
        
        assert len(flushed) == 2
        assert len(trail.entries) == 0
    
    def test_get_errors(self):
        """Can filter for error entries."""
        trail = WTBAuditTrail()
        
        trail.record_event(ExecutionStartedEvent(execution_id="exec-1", workflow_id="wf-1"))
        trail.record_event(NodeFailedEvent(
            execution_id="exec-1",
            node_id="node-1",
            node_name="Failing Node",
            error_message="Error",
            error_type="TestError",
        ))
        trail.record_event(NodeCompletedEvent(
            execution_id="exec-1",
            node_id="node-2",
            node_name="Good Node",
        ))
        
        errors = trail.get_errors()
        
        assert len(errors) == 1
        assert errors[0].event_type == WTBAuditEventType.NODE_FAILED
    
    def test_get_by_node(self):
        """Can filter entries by node."""
        trail = WTBAuditTrail()
        
        trail.record_event(NodeStartedEvent(execution_id="exec-1", node_id="node-1", node_name="Node 1"))
        trail.record_event(NodeStartedEvent(execution_id="exec-1", node_id="node-2", node_name="Node 2"))
        trail.record_event(NodeCompletedEvent(execution_id="exec-1", node_id="node-1", node_name="Node 1"))
        
        node1_entries = trail.get_by_node("node-1")
        
        assert len(node1_entries) == 2
    
    def test_get_summary(self):
        """Can get summary statistics."""
        trail = WTBAuditTrail(execution_id="exec-1", workflow_id="wf-1")
        trail.started_at = datetime.now() - timedelta(seconds=5)
        
        trail.record_event(ExecutionStartedEvent(execution_id="exec-1", workflow_id="wf-1"))
        trail.record_event(NodeCompletedEvent(execution_id="exec-1", node_id="n1", node_name="N1"))
        trail.record_event(NodeCompletedEvent(execution_id="exec-1", node_id="n2", node_name="N2"))
        # Use new DDD-compliant event format (2026-01-15)
        trail.record_event(CheckpointCreatedEvent(execution_id="exec-1", checkpoint_id="cp-1", step=1))
        trail.record_event(NodeFailedEvent(execution_id="exec-1", node_id="n3", node_name="N3", error_message="E", error_type="T"))
        
        trail.ended_at = datetime.now()
        
        summary = trail.get_summary()
        
        assert summary["execution_id"] == "exec-1"
        assert summary["nodes_executed"] == 2
        assert summary["nodes_failed"] == 1
        assert summary["checkpoints_created"] == 1
        assert summary["errors"] == 1
    
    def test_to_dict_from_dict(self):
        """Trail can be serialized and deserialized."""
        trail = WTBAuditTrail(
            execution_id="exec-1",
            workflow_id="wf-1",
            started_at=datetime(2025, 1, 9, 12, 0, 0),
        )
        trail.record_event(ExecutionStartedEvent(execution_id="exec-1", workflow_id="wf-1"))
        
        d = trail.to_dict()
        restored = WTBAuditTrail.from_dict(d)
        
        assert restored.execution_id == "exec-1"
        assert restored.workflow_id == "wf-1"
        assert len(restored.entries) == 1
    
    def test_import_agentgit_audit(self):
        """Can import AgentGit audit for debugging."""
        trail = WTBAuditTrail()
        
        ag_audit = {
            "events": [
                {"type": "TOOL_START", "tool_name": "search"},
                {"type": "TOOL_SUCCESS", "tool_name": "search"},
            ]
        }
        
        trail.import_agentgit_audit(ag_audit, "checkpoint-42")
        
        retrieved = trail.get_agentgit_audit("checkpoint-42")
        assert retrieved == ag_audit
    
    def test_format_display(self):
        """Trail can be formatted for display."""
        trail = WTBAuditTrail(execution_id="exec-1", workflow_id="wf-1")
        trail.record_event(ExecutionStartedEvent(execution_id="exec-1", workflow_id="wf-1"))
        
        display = trail.format_display()
        
        assert "WTB AUDIT TRAIL" in display
        assert "exec-1" in display


class TestAuditEventListener:
    """Tests for AuditEventListener."""
    
    def test_attach_to_event_bus(self):
        """Listener can attach to event bus."""
        bus = WTBEventBus()
        trail = WTBAuditTrail()
        listener = AuditEventListener(trail)
        
        listener.attach(bus)
        
        # Should have subscriptions for all event types
        assert bus.get_subscriber_count() > 0
    
    def test_listener_records_events(self):
        """Listener automatically records events to trail."""
        bus = WTBEventBus()
        trail = WTBAuditTrail()
        listener = AuditEventListener(trail)
        
        listener.attach(bus)
        
        bus.publish(ExecutionStartedEvent(execution_id="exec-1", workflow_id="wf-1"))
        bus.publish(NodeCompletedEvent(execution_id="exec-1", node_id="n1", node_name="N1"))
        
        assert len(trail.entries) == 2
    
    def test_listener_detach(self):
        """Listener can detach from event bus."""
        bus = WTBEventBus()
        trail = WTBAuditTrail()
        listener = AuditEventListener(trail)
        
        listener.attach(bus)
        bus.publish(ExecutionStartedEvent(execution_id="exec-1", workflow_id="wf-1"))
        
        listener.detach()
        bus.publish(ExecutionCompletedEvent(execution_id="exec-1", workflow_id="wf-1"))
        
        # Should only have the first event
        assert len(trail.entries) == 1
    
    def test_listener_specific_event_types(self):
        """Can create listener for specific event types only."""
        bus = WTBEventBus()
        trail = WTBAuditTrail()
        listener = AuditEventListener(trail, event_types=[ExecutionStartedEvent])
        
        listener.attach(bus)
        
        bus.publish(ExecutionStartedEvent(execution_id="exec-1", workflow_id="wf-1"))
        bus.publish(ExecutionCompletedEvent(execution_id="exec-1", workflow_id="wf-1"))
        
        # Should only have the started event
        assert len(trail.entries) == 1
        assert trail.entries[0].event_type == WTBAuditEventType.EXECUTION_STARTED

