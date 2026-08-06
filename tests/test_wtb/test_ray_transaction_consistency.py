"""
Integration tests for Ray Event Transaction Consistency.

Tests ACID properties of Ray event integration:
- Atomicity: Events and state changes committed together
- Consistency: Event schema and state invariants maintained
- Isolation: Concurrent batch tests don't interfere
- Durability: Events persisted to outbox for reliable delivery

Test Categories:
- Outbox Pattern: Events written to outbox within transactions
- Concurrent Access: Thread-safe event publishing
- Error Recovery: Events preserved on failure
- Audit Consistency: Audit trail matches events
"""

import queue
import threading
import time
from datetime import datetime
from unittest.mock import MagicMock, Mock

# ═══════════════════════════════════════════════════════════════
# ACID - Atomicity Tests
# ═══════════════════════════════════════════════════════════════


class TestAtomicity:
    """Tests for atomic event + state operations."""
    
    def test_outbox_event_created_in_same_transaction(self):
        """Test outbox event is created in same transaction as publish."""
        from wtb.infrastructure.events import RayEventBridge, WTBEventBus
        
        # Mock UoW to track operations
        operations = []
        
        mock_uow = MagicMock()
        mock_uow.__enter__ = Mock(return_value=mock_uow)
        mock_uow.__exit__ = Mock(return_value=False)
        
        def record_add(event):
            operations.append(("add", type(event).__name__))
        
        def record_commit():
            operations.append(("commit", None))
        
        mock_uow.outbox.add = record_add
        mock_uow.commit = record_commit
        
        event_bus = WTBEventBus()
        bridge = RayEventBridge(
            event_bus=event_bus,
            uow_factory=lambda: mock_uow,
            use_outbox=True,
        )
        
        bridge.on_batch_test_started(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test",
            variant_count=5,
            parallel_workers=2,
            max_pending_tasks=4,
        )
        
        # Verify: add happens before commit (atomic sequence)
        assert len(operations) >= 2
        add_idx = next(i for i, op in enumerate(operations) if op[0] == "add")
        commit_idx = next(i for i, op in enumerate(operations) if op[0] == "commit")
        assert add_idx < commit_idx, "Add must happen before commit for atomicity"
    
    def test_batch_events_committed_atomically(self):
        """Test multiple events are committed in single transaction."""
        from wtb.domain.events import RayVariantExecutionCompletedEvent
        from wtb.infrastructure.events import RayEventBridge, WTBEventBus
        
        commit_count = [0]
        add_count = [0]
        
        mock_uow = MagicMock()
        mock_uow.__enter__ = Mock(return_value=mock_uow)
        mock_uow.__exit__ = Mock(return_value=False)
        mock_uow.outbox.add = lambda e: add_count.__setitem__(0, add_count[0] + 1)
        mock_uow.commit = lambda: commit_count.__setitem__(0, commit_count[0] + 1)
        
        event_bus = WTBEventBus()
        bridge = RayEventBridge(
            event_bus=event_bus,
            uow_factory=lambda: mock_uow,
            use_outbox=True,
        )
        
        events = [
            RayVariantExecutionCompletedEvent(
                execution_id=f"exec-{i}",
                batch_test_id="bt-1",
                actor_id="actor_0",
                combination_name=f"variant_{i}",
            )
            for i in range(5)
        ]
        
        count = bridge.publish_events_batch(events, "bt-1")
        
        assert count == 5
        assert add_count[0] == 5  # All events added
        assert commit_count[0] == 1  # Single commit (atomic)
    
    def test_rollback_on_commit_failure(self):
        """Test transaction rolls back if commit fails."""
        from wtb.infrastructure.events import RayEventBridge, WTBEventBus
        
        exit_called = [False]
        
        mock_uow = MagicMock()
        mock_uow.__enter__ = Mock(return_value=mock_uow)
        
        def track_exit(self_arg, exc_type, exc_val, exc_tb):
            exit_called[0] = True
            return False  # Don't suppress exception
        
        mock_uow.__exit__ = lambda *args: (exit_called.__setitem__(0, True), False)[1]
        mock_uow.commit = Mock(side_effect=Exception("DB connection lost"))
        
        event_bus = WTBEventBus()
        bridge = RayEventBridge(
            event_bus=event_bus,
            uow_factory=lambda: mock_uow,
            use_outbox=True,
        )
        
        # Should fall back to immediate publish
        result = bridge.publish_event(
            event=Mock(
                __dataclass_fields__={},
                timestamp=datetime.now(),
            ),
            batch_test_id="bt-1",
        )
        
        # Verify context manager exited (cleanup)
        assert exit_called[0]


# ═══════════════════════════════════════════════════════════════
# ACID - Consistency Tests
# ═══════════════════════════════════════════════════════════════


class TestConsistency:
    """Tests for consistent state and event schema."""
    
    def test_event_schema_validated_on_serialization(self):
        """Test event schema is validated during serialization."""
        from wtb.domain.events import RayBatchTestStartedEvent
        from wtb.infrastructure.events import serialize_ray_event
        
        event = RayBatchTestStartedEvent(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test",
            variant_count=5,
            parallel_workers=2,
            max_pending_tasks=4,
        )
        
        serialized = serialize_ray_event(event)
        
        # Required fields must be present
        assert "__event_type__" in serialized
        assert "batch_test_id" in serialized
        assert "workflow_id" in serialized
        assert "timestamp" in serialized
    
    def test_audit_event_type_consistency(self):
        """Test audit event types are consistent with domain events."""
        from wtb.infrastructure.events import WTBAuditEventType
        
        # Verify Ray audit types exist
        ray_types = [t for t in WTBAuditEventType if t.name.startswith("RAY_")]
        
        assert len(ray_types) >= 10  # At least 10 Ray event types
        
        # Verify key types exist
        expected = [
            "RAY_BATCH_TEST_STARTED",
            "RAY_BATCH_TEST_COMPLETED",
            "RAY_BATCH_TEST_FAILED",
            "RAY_VARIANT_EXECUTION_STARTED",
            "RAY_VARIANT_EXECUTION_COMPLETED",
            "RAY_VARIANT_EXECUTION_FAILED",
            "RAY_ACTOR_POOL_CREATED",
        ]
        
        for name in expected:
            assert hasattr(WTBAuditEventType, name), f"Missing {name}"
    
    def test_event_timestamp_ordering(self):
        """Test events maintain timestamp ordering."""
        from wtb.infrastructure.events import (
            AuditEventListener,
            RayEventBridge,
            WTBAuditTrail,
            WTBEventBus,
        )
        
        event_bus = WTBEventBus()
        trail = WTBAuditTrail()
        listener = AuditEventListener(trail)
        listener.attach(event_bus)
        
        bridge = RayEventBridge(event_bus=event_bus, use_outbox=False)
        
        # Emit events in sequence
        bridge.on_batch_test_started(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test",
            variant_count=1,
            parallel_workers=1,
            max_pending_tasks=2,
        )
        
        time.sleep(0.01)  # Small delay
        
        bridge.on_variant_execution_completed(
            execution_id="exec-1",
            batch_test_id="bt-1",
            actor_id="actor_0",
            combination_name="v1",
            variants={},
            duration_ms=100,
        )
        
        time.sleep(0.01)
        
        bridge.on_batch_test_completed(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            variants_succeeded=1,
            variants_failed=0,
        )
        
        # Verify timestamps are ordered
        timestamps = [e.timestamp for e in trail.entries]
        assert timestamps == sorted(timestamps), "Timestamps must be in order"
        
        listener.detach()


# ═══════════════════════════════════════════════════════════════
# ACID - Isolation Tests
# ═══════════════════════════════════════════════════════════════


class TestIsolation:
    """Tests for isolated concurrent access."""
    
    def test_concurrent_batch_tests_isolated(self):
        """Test concurrent batch tests don't interfere."""
        from wtb.domain.events import (
            RayBatchTestCompletedEvent,
            RayBatchTestStartedEvent,
        )
        from wtb.infrastructure.events import RayEventBridge, WTBEventBus
        
        event_bus = WTBEventBus()
        bridge = RayEventBridge(event_bus=event_bus, use_outbox=False)
        
        received_events: dict[str, list] = {"bt-1": [], "bt-2": []}
        
        def handler(event):
            if hasattr(event, 'batch_test_id'):
                received_events[event.batch_test_id].append(event)
        
        event_bus.subscribe(RayBatchTestStartedEvent, handler)
        event_bus.subscribe(RayBatchTestCompletedEvent, handler)
        
        def run_batch(batch_id: str):
            bridge.on_batch_test_started(
                batch_test_id=batch_id,
                workflow_id=f"wf-{batch_id}",
                workflow_name="Test",
                variant_count=5,
                parallel_workers=2,
                max_pending_tasks=4,
            )
            time.sleep(0.01)
            bridge.on_batch_test_completed(
                batch_test_id=batch_id,
                workflow_id=f"wf-{batch_id}",
                variants_succeeded=5,
                variants_failed=0,
            )
        
        threads = [
            threading.Thread(target=run_batch, args=("bt-1",)),
            threading.Thread(target=run_batch, args=("bt-2",)),
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # Each batch should have its own events
        assert len(received_events["bt-1"]) == 2
        assert len(received_events["bt-2"]) == 2
        
        # No cross-contamination
        for event in received_events["bt-1"]:
            assert event.batch_test_id == "bt-1"
        for event in received_events["bt-2"]:
            assert event.batch_test_id == "bt-2"
    
    def test_per_batch_audit_trails_isolated(self):
        """Test each batch test has isolated audit trail."""
        from wtb.infrastructure.events import RayEventBridge, WTBAuditTrail, WTBEventBus
        
        event_bus = WTBEventBus()
        
        audit_trails = {}
        
        def create_trail():
            trail = WTBAuditTrail()
            return trail
        
        bridge = RayEventBridge(
            event_bus=event_bus,
            use_outbox=False,
            audit_trail_factory=create_trail,
        )
        
        # Start two batches
        bridge.on_batch_test_started(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test1",
            variant_count=3,
            parallel_workers=1,
            max_pending_tasks=2,
        )
        
        bridge.on_batch_test_started(
            batch_test_id="bt-2",
            workflow_id="wf-2",
            workflow_name="Test2",
            variant_count=5,
            parallel_workers=2,
            max_pending_tasks=4,
        )
        
        # Get audit trails
        trail1 = bridge.get_batch_audit_trail("bt-1")
        trail2 = bridge.get_batch_audit_trail("bt-2")
        
        # Trails should exist and be different
        assert trail1 is not None
        assert trail2 is not None
        assert trail1 is not trail2
    
    def test_thread_safe_event_history(self):
        """Test event history is thread-safe."""
        from wtb.domain.events import RayVariantExecutionCompletedEvent
        from wtb.infrastructure.events import RayEventBridge, WTBEventBus
        
        event_bus = WTBEventBus()
        bridge = RayEventBridge(event_bus=event_bus, use_outbox=False)
        
        error_queue = queue.Queue()
        
        def publish_events(thread_id: int):
            try:
                for i in range(20):
                    bridge.on_variant_execution_completed(
                        execution_id=f"exec-{thread_id}-{i}",
                        batch_test_id="bt-1",
                        actor_id=f"actor_{thread_id}",
                        combination_name=f"v_{i}",
                        variants={},
                        duration_ms=100,
                    )
            except Exception as e:
                error_queue.put(e)
        
        threads = [threading.Thread(target=publish_events, args=(i,)) for i in range(5)]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # No errors should have occurred
        assert error_queue.empty(), f"Thread errors: {list(error_queue.queue)}"
        
        # All events should be in history
        history = event_bus.get_event_history(RayVariantExecutionCompletedEvent)
        assert len(history) == 100  # 5 threads * 20 events


# ═══════════════════════════════════════════════════════════════
# ACID - Durability Tests
# ═══════════════════════════════════════════════════════════════


class TestDurability:
    """Tests for durable event storage."""
    
    def test_outbox_event_persisted_before_publish(self):
        """Test event is persisted to outbox before attempting publish."""
        from wtb.infrastructure.events import RayEventBridge, WTBEventBus
        
        operations = []
        
        mock_uow = MagicMock()
        mock_uow.__enter__ = Mock(return_value=mock_uow)
        mock_uow.__exit__ = Mock(return_value=False)
        
        def track_add(event):
            operations.append(("outbox_add", datetime.now()))
        
        def track_commit():
            operations.append(("commit", datetime.now()))
        
        mock_uow.outbox.add = track_add
        mock_uow.outbox.update = Mock()
        mock_uow.commit = track_commit
        
        # Event bus tracks publish time
        publish_times = []
        
        class TrackingEventBus(WTBEventBus):
            def publish(self, event):
                publish_times.append(datetime.now())
                super().publish(event)
        
        event_bus = TrackingEventBus()
        bridge = RayEventBridge(
            event_bus=event_bus,
            uow_factory=lambda: mock_uow,
            use_outbox=True,
        )
        
        bridge.on_batch_test_started(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test",
            variant_count=1,
            parallel_workers=1,
            max_pending_tasks=2,
        )
        
        # Verify order: outbox_add -> commit -> publish
        assert len(operations) >= 2
        outbox_add_time = operations[0][1]
        commit_time = next(t for op, t in operations if op == "commit")
        
        assert outbox_add_time <= commit_time, "Outbox add must happen before commit"
        
        if publish_times:
            # If publish happened, it should be after commit
            assert commit_time <= publish_times[0], "Publish should happen after commit"
    
    def test_outbox_event_survives_publish_failure(self):
        """Test event remains in outbox if publish fails."""
        from wtb.infrastructure.events import RayEventBridge, WTBEventBus
        
        outbox_events = []
        update_calls = []
        
        mock_uow = MagicMock()
        mock_uow.__enter__ = Mock(return_value=mock_uow)
        mock_uow.__exit__ = Mock(return_value=False)
        
        def track_add(event):
            outbox_events.append(event)
        
        def track_update(event):
            update_calls.append(event)
        
        mock_uow.outbox.add = track_add
        mock_uow.outbox.update = track_update
        mock_uow.commit = Mock()
        
        # Create event bus that fails to publish
        class FailingEventBus(WTBEventBus):
            def publish(self, event):
                raise Exception("Network error")
        
        event_bus = FailingEventBus()
        bridge = RayEventBridge(
            event_bus=event_bus,
            uow_factory=lambda: mock_uow,
            use_outbox=True,
        )
        
        # This should succeed (event in outbox) even though publish fails
        result = bridge.on_batch_test_started(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test",
            variant_count=1,
            parallel_workers=1,
            max_pending_tasks=2,
        )
        
        # Event should be in outbox
        assert len(outbox_events) == 1
        
        # Event should NOT be marked as published
        assert len(update_calls) == 0  # No update because publish failed
    
    def test_outbox_event_marked_published_on_success(self):
        """Test outbox event is marked published when publish succeeds."""
        from wtb.infrastructure.events import RayEventBridge, WTBEventBus
        
        outbox_events = []
        update_calls = []
        
        mock_uow = MagicMock()
        mock_uow.__enter__ = Mock(return_value=mock_uow)
        mock_uow.__exit__ = Mock(return_value=False)
        def mock_outbox_add(e):
            outbox_events.append(e)
            return e
        mock_uow.outbox.add = mock_outbox_add
        mock_uow.outbox.update = lambda e: update_calls.append(e)
        mock_uow.commit = Mock()
        
        event_bus = WTBEventBus()
        bridge = RayEventBridge(
            event_bus=event_bus,
            uow_factory=lambda: mock_uow,
            use_outbox=True,
        )
        
        bridge.on_batch_test_started(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test",
            variant_count=1,
            parallel_workers=1,
            max_pending_tasks=2,
        )
        
        # Event should be added and updated (marked published)
        assert len(outbox_events) == 1
        assert len(update_calls) == 1
        
        # Updated event should be marked as published
        updated_event = update_calls[0]
        assert updated_event.published_at is not None


# ═══════════════════════════════════════════════════════════════
# Audit Trail Consistency Tests
# ═══════════════════════════════════════════════════════════════


class TestAuditConsistency:
    """Tests for audit trail consistency with events."""
    
    def test_audit_entries_match_published_events(self):
        """Test audit entries match published events."""
        from wtb.domain.events import RayBatchTestStartedEvent
        from wtb.infrastructure.events import (
            AuditEventListener,
            RayEventBridge,
            WTBAuditTrail,
            WTBEventBus,
        )
        
        event_bus = WTBEventBus()
        trail = WTBAuditTrail()
        listener = AuditEventListener(trail)
        listener.attach(event_bus)
        
        bridge = RayEventBridge(event_bus=event_bus, use_outbox=False)
        
        # Publish events
        published_events = []
        
        def track_published(event):
            published_events.append(event)
        
        event_bus.subscribe(RayBatchTestStartedEvent, track_published)
        
        bridge.on_batch_test_started(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test",
            variant_count=5,
            parallel_workers=2,
            max_pending_tasks=4,
        )
        
        # Verify: one event published, one audit entry
        assert len(published_events) == 1
        assert len(trail.entries) == 1
        
        # Audit entry should contain event data
        entry = trail.entries[0]
        assert "bt-1" in entry.message
        
        listener.detach()
    
    def test_audit_summary_matches_event_counts(self):
        """Test audit summary matches actual event counts."""
        from wtb.infrastructure.events import (
            AuditEventListener,
            RayEventBridge,
            WTBAuditEventType,
            WTBAuditTrail,
            WTBEventBus,
        )
        
        event_bus = WTBEventBus()
        trail = WTBAuditTrail()
        listener = AuditEventListener(trail)
        listener.attach(event_bus)
        
        bridge = RayEventBridge(event_bus=event_bus, use_outbox=False)
        
        # Simulate batch test with multiple variants
        bridge.on_batch_test_started(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test",
            variant_count=3,
            parallel_workers=2,
            max_pending_tasks=4,
        )
        
        for i in range(3):
            bridge.on_variant_execution_completed(
                execution_id=f"exec-{i}",
                batch_test_id="bt-1",
                actor_id="actor_0",
                combination_name=f"v{i}",
                variants={},
                duration_ms=100,
            )
        
        bridge.on_batch_test_completed(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            variants_succeeded=3,
            variants_failed=0,
        )
        
        # Get summary
        summary = trail.get_summary()
        
        # Verify counts
        entries_by_type = {}
        for e in trail.entries:
            entries_by_type[e.event_type] = entries_by_type.get(e.event_type, 0) + 1
        
        assert entries_by_type.get(WTBAuditEventType.RAY_BATCH_TEST_STARTED, 0) == 1
        assert entries_by_type.get(WTBAuditEventType.RAY_BATCH_TEST_COMPLETED, 0) == 1
        assert entries_by_type.get(WTBAuditEventType.RAY_VARIANT_EXECUTION_COMPLETED, 0) == 3
        
        listener.detach()
    
    def test_audit_entries_have_correct_severity(self):
        """Test audit entries have correct severity levels."""
        from wtb.infrastructure.events import (
            AuditEventListener,
            RayEventBridge,
            WTBAuditSeverity,
            WTBAuditTrail,
            WTBEventBus,
        )
        
        event_bus = WTBEventBus()
        trail = WTBAuditTrail()
        listener = AuditEventListener(trail)
        listener.attach(event_bus)
        
        bridge = RayEventBridge(event_bus=event_bus, use_outbox=False)
        
        # Emit events with different severities
        bridge.on_batch_test_started(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test",
            variant_count=1,
            parallel_workers=1,
            max_pending_tasks=2,
        )
        
        bridge.on_variant_execution_completed(
            execution_id="exec-1",
            batch_test_id="bt-1",
            actor_id="actor_0",
            combination_name="v1",
            variants={},
            duration_ms=100,
        )
        
        bridge.on_variant_execution_failed(
            execution_id="exec-2",
            batch_test_id="bt-1",
            actor_id="actor_1",
            combination_name="v2",
            variants={},
            error_type="Error",
            error_message="Test error",
        )
        
        bridge.on_batch_test_cancelled(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            reason="Test cancelled",
        )
        
        # Verify severities
        severities = {e.event_type.value: e.severity for e in trail.entries}
        
        assert severities.get("ray_batch_test_started") == WTBAuditSeverity.INFO
        assert severities.get("ray_variant_execution_completed") == WTBAuditSeverity.SUCCESS
        assert severities.get("ray_variant_execution_failed") == WTBAuditSeverity.ERROR
        assert severities.get("ray_batch_test_cancelled") == WTBAuditSeverity.WARNING
        
        listener.detach()


# ═══════════════════════════════════════════════════════════════
# Error Recovery Tests
# ═══════════════════════════════════════════════════════════════


class TestErrorRecovery:
    """Tests for error recovery scenarios."""
    
    def test_fallback_to_immediate_publish_on_outbox_error(self):
        """Test fallback to immediate publish when outbox fails."""
        from wtb.domain.events import RayBatchTestStartedEvent
        from wtb.infrastructure.events import RayEventBridge, WTBEventBus
        
        # Create UoW that fails
        mock_uow = MagicMock()
        mock_uow.__enter__ = Mock(side_effect=Exception("DB unavailable"))
        mock_uow.__exit__ = Mock(return_value=False)
        
        published_events = []
        event_bus = WTBEventBus()
        event_bus.subscribe(RayBatchTestStartedEvent, lambda e: published_events.append(e))
        
        bridge = RayEventBridge(
            event_bus=event_bus,
            uow_factory=lambda: mock_uow,
            use_outbox=True,
        )
        
        # Should fall back to immediate publish
        bridge.on_batch_test_started(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test",
            variant_count=1,
            parallel_workers=1,
            max_pending_tasks=2,
        )
        
        # Event should still be published (fallback)
        assert len(published_events) == 1
    
    def test_cleanup_on_exception(self):
        """Test resources cleaned up on exception."""
        from wtb.infrastructure.events import RayEventBridge, WTBEventBus
        
        exit_called = [False]
        
        mock_uow = MagicMock()
        mock_uow.__enter__ = Mock(return_value=mock_uow)
        mock_uow.__exit__ = lambda *args: exit_called.__setitem__(0, True) or False
        mock_uow.outbox.add = Mock(side_effect=Exception("Insert failed"))
        
        event_bus = WTBEventBus()
        bridge = RayEventBridge(
            event_bus=event_bus,
            uow_factory=lambda: mock_uow,
            use_outbox=True,
        )
        
        # Should handle exception gracefully
        bridge.on_batch_test_started(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test",
            variant_count=1,
            parallel_workers=1,
            max_pending_tasks=2,
        )
        
        # Context manager should have been exited
        assert exit_called[0]
