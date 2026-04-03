"""
Unit tests for Ray Event Integration.

Tests the integration of Ray batch testing with the WTB Event Bus and Audit system,
ensuring ACID compliance and transaction consistency.

Test Categories:
- Ray Event Domain Models: Event serialization and creation
- RayEventBridge: Event publishing with outbox pattern
- WTBAuditTrail: Ray event recording
- RayBatchTestRunner: Event integration during execution
- Transaction Consistency: ACID properties
"""

import pytest
from datetime import datetime
from typing import Dict, Any, List, Optional
from unittest.mock import Mock, MagicMock, patch
import threading
import time


# ═══════════════════════════════════════════════════════════════
# Ray Event Domain Models Tests
# ═══════════════════════════════════════════════════════════════


class TestRayEventModels:
    """Tests for Ray event domain models."""
    
    def test_ray_batch_test_started_event_creation(self):
        """Test RayBatchTestStartedEvent creation."""
        from wtb.domain.events import RayBatchTestStartedEvent
        
        event = RayBatchTestStartedEvent(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test Workflow",
            variant_count=10,
            parallel_workers=4,
            max_pending_tasks=8,
            file_tracking_enabled=True,
        )
        
        assert event.batch_test_id == "bt-1"
        assert event.workflow_id == "wf-1"
        assert event.variant_count == 10
        assert event.parallel_workers == 4
        assert event.file_tracking_enabled is True
        assert event.event_type == "ray_batch_test_started"
        assert event.timestamp is not None
    
    def test_ray_variant_execution_completed_event(self):
        """Test RayVariantExecutionCompletedEvent creation."""
        from wtb.domain.events import RayVariantExecutionCompletedEvent
        
        event = RayVariantExecutionCompletedEvent(
            execution_id="exec-1",
            batch_test_id="bt-1",
            actor_id="actor_0",
            combination_name="RandomForest",
            variants={"train": "rf"},
            duration_ms=1500,
            checkpoint_count=5,
            node_count=10,
            metrics={"accuracy": 0.95, "overall_score": 0.9},
            overall_score=0.9,
            files_tracked=3,
            file_commit_id="fc-123",
        )
        
        assert event.execution_id == "exec-1"
        assert event.combination_name == "RandomForest"
        assert event.duration_ms == 1500
        assert event.metrics["accuracy"] == 0.95
        assert event.files_tracked == 3
    
    def test_ray_event_type_enum_values(self):
        """Test RayEventType enum has all expected values."""
        from wtb.domain.events import RayEventType
        
        expected_types = [
            "ray_batch_test_started",
            "ray_batch_test_completed",
            "ray_batch_test_failed",
            "ray_variant_execution_started",
            "ray_variant_execution_completed",
            "ray_variant_execution_failed",
            "ray_actor_pool_created",
            "ray_actor_initialized",
            "ray_actor_failed",
            "ray_backpressure_applied",
        ]
        
        for expected in expected_types:
            assert hasattr(RayEventType, expected.upper().replace("RAY_", ""))
    
    def test_ray_event_inherits_from_wtb_event(self):
        """Test all Ray events inherit from WTBEvent."""
        from wtb.domain.events import (
            WTBEvent,
            RayBatchTestStartedEvent,
            RayBatchTestCompletedEvent,
            RayVariantExecutionCompletedEvent,
        )
        
        assert issubclass(RayBatchTestStartedEvent, WTBEvent)
        assert issubclass(RayBatchTestCompletedEvent, WTBEvent)
        assert issubclass(RayVariantExecutionCompletedEvent, WTBEvent)


class TestRayEventSerialization:
    """Tests for Ray event serialization/deserialization."""
    
    def test_serialize_ray_batch_test_started(self):
        """Test serialization of RayBatchTestStartedEvent."""
        from wtb.domain.events import RayBatchTestStartedEvent
        from wtb.infrastructure.events import serialize_ray_event, deserialize_ray_event
        
        event = RayBatchTestStartedEvent(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test",
            variant_count=5,
            parallel_workers=2,
            max_pending_tasks=4,
        )
        
        serialized = serialize_ray_event(event)
        
        assert serialized["__event_type__"] == "RayBatchTestStartedEvent"
        assert serialized["batch_test_id"] == "bt-1"
        assert serialized["variant_count"] == 5
        assert "timestamp" in serialized
    
    def test_deserialize_ray_batch_test_started(self):
        """Test deserialization of RayBatchTestStartedEvent."""
        from wtb.domain.events import RayBatchTestStartedEvent
        from wtb.infrastructure.events import serialize_ray_event, deserialize_ray_event
        
        original = RayBatchTestStartedEvent(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test",
            variant_count=5,
            parallel_workers=2,
            max_pending_tasks=4,
        )
        
        serialized = serialize_ray_event(original)
        deserialized = deserialize_ray_event(serialized)
        
        assert deserialized is not None
        assert isinstance(deserialized, RayBatchTestStartedEvent)
        assert deserialized.batch_test_id == original.batch_test_id
        assert deserialized.variant_count == original.variant_count
    
    def test_serialization_roundtrip_all_ray_events(self):
        """Test serialization roundtrip for all Ray event types."""
        from wtb.domain.events import (
            RayBatchTestStartedEvent,
            RayBatchTestCompletedEvent,
            RayBatchTestFailedEvent,
            RayVariantExecutionStartedEvent,
            RayVariantExecutionCompletedEvent,
            RayVariantExecutionFailedEvent,
            RayActorPoolCreatedEvent,
        )
        from wtb.infrastructure.events import serialize_ray_event, deserialize_ray_event
        
        events = [
            RayBatchTestStartedEvent(batch_test_id="bt-1", workflow_id="wf-1"),
            RayBatchTestCompletedEvent(batch_test_id="bt-1", workflow_id="wf-1"),
            RayBatchTestFailedEvent(batch_test_id="bt-1", workflow_id="wf-1", error_type="Error", error_message="Test"),
            RayVariantExecutionStartedEvent(execution_id="e-1", batch_test_id="bt-1", actor_id="a-1", combination_name="v1"),
            RayVariantExecutionCompletedEvent(execution_id="e-1", batch_test_id="bt-1", actor_id="a-1", combination_name="v1"),
            RayVariantExecutionFailedEvent(execution_id="e-1", batch_test_id="bt-1", actor_id="a-1", combination_name="v1", error_type="E", error_message="M"),
            RayActorPoolCreatedEvent(batch_test_id="bt-1", num_actors=4),
        ]
        
        for original in events:
            serialized = serialize_ray_event(original)
            deserialized = deserialize_ray_event(serialized)
            
            assert deserialized is not None, f"Failed to deserialize {type(original).__name__}"
            assert type(deserialized) == type(original)
    
    def test_deserialize_unknown_event_returns_none(self):
        """Test deserialization of unknown event type returns None."""
        from wtb.infrastructure.events import deserialize_ray_event
        
        unknown_data = {
            "__event_type__": "UnknownEventType",
            "some_field": "value",
        }
        
        result = deserialize_ray_event(unknown_data)
        assert result is None


# ═══════════════════════════════════════════════════════════════
# RayEventBridge Tests
# ═══════════════════════════════════════════════════════════════


class TestRayEventBridge:
    """Tests for RayEventBridge."""
    
    def test_bridge_creation_without_outbox(self):
        """Test bridge creation without outbox pattern."""
        from wtb.infrastructure.events import WTBEventBus, RayEventBridge
        
        event_bus = WTBEventBus()
        bridge = RayEventBridge(
            event_bus=event_bus,
            uow_factory=None,
            use_outbox=False,
        )
        
        assert bridge is not None
    
    def test_bridge_publishes_to_event_bus(self):
        """Test bridge publishes events to event bus."""
        from wtb.infrastructure.events import WTBEventBus, RayEventBridge
        from wtb.domain.events import RayBatchTestStartedEvent
        
        event_bus = WTBEventBus()
        bridge = RayEventBridge(
            event_bus=event_bus,
            use_outbox=False,
        )
        
        received_events = []
        event_bus.subscribe(RayBatchTestStartedEvent, lambda e: received_events.append(e))
        
        bridge.on_batch_test_started(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test",
            variant_count=5,
            parallel_workers=2,
            max_pending_tasks=4,
        )
        
        assert len(received_events) == 1
        assert received_events[0].batch_test_id == "bt-1"
    
    def test_bridge_tracks_batch_start_time(self):
        """Test bridge tracks batch test start times."""
        from wtb.infrastructure.events import WTBEventBus, RayEventBridge
        
        event_bus = WTBEventBus()
        bridge = RayEventBridge(event_bus=event_bus, use_outbox=False)
        
        bridge.on_batch_test_started(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test",
            variant_count=5,
            parallel_workers=2,
            max_pending_tasks=4,
        )
        
        assert "bt-1" in bridge._batch_start_times
    
    def test_bridge_calculates_duration_on_completion(self):
        """Test bridge calculates duration on batch completion."""
        from wtb.infrastructure.events import WTBEventBus, RayEventBridge
        from wtb.domain.events import RayBatchTestCompletedEvent
        
        event_bus = WTBEventBus()
        bridge = RayEventBridge(event_bus=event_bus, use_outbox=False)
        
        completed_events = []
        event_bus.subscribe(RayBatchTestCompletedEvent, lambda e: completed_events.append(e))
        
        bridge.on_batch_test_started(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test",
            variant_count=5,
            parallel_workers=2,
            max_pending_tasks=4,
        )
        
        # Small delay to have measurable duration
        time.sleep(0.01)
        
        bridge.on_batch_test_completed(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            variants_succeeded=4,
            variants_failed=1,
        )
        
        assert len(completed_events) == 1
        assert completed_events[0].duration_ms > 0
    
    def test_bridge_variant_execution_events(self):
        """Test bridge emits variant execution events."""
        from wtb.infrastructure.events import WTBEventBus, RayEventBridge
        from wtb.domain.events import RayVariantExecutionStartedEvent, RayVariantExecutionCompletedEvent
        
        event_bus = WTBEventBus()
        bridge = RayEventBridge(event_bus=event_bus, use_outbox=False)
        
        started_events = []
        completed_events = []
        event_bus.subscribe(RayVariantExecutionStartedEvent, lambda e: started_events.append(e))
        event_bus.subscribe(RayVariantExecutionCompletedEvent, lambda e: completed_events.append(e))
        
        bridge.on_variant_execution_started(
            execution_id="exec-1",
            batch_test_id="bt-1",
            actor_id="actor_0",
            combination_name="RandomForest",
            variants={"train": "rf"},
        )
        
        bridge.on_variant_execution_completed(
            execution_id="exec-1",
            batch_test_id="bt-1",
            actor_id="actor_0",
            combination_name="RandomForest",
            variants={"train": "rf"},
            duration_ms=1000,
            metrics={"accuracy": 0.9},
            overall_score=0.85,
        )
        
        assert len(started_events) == 1
        assert len(completed_events) == 1
        assert completed_events[0].overall_score == 0.85
    
    def test_bridge_cleanup_removes_tracking(self):
        """Test bridge cleanup removes batch tracking data."""
        from wtb.infrastructure.events import WTBEventBus, RayEventBridge
        
        event_bus = WTBEventBus()
        bridge = RayEventBridge(event_bus=event_bus, use_outbox=False)
        
        bridge.on_batch_test_started(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test",
            variant_count=5,
            parallel_workers=2,
            max_pending_tasks=4,
        )
        
        assert "bt-1" in bridge._batch_start_times
        
        bridge.cleanup_batch("bt-1")
        
        assert "bt-1" not in bridge._batch_start_times
    
    def test_bridge_thread_safety(self):
        """Test bridge is thread-safe for concurrent access."""
        from wtb.infrastructure.events import WTBEventBus, RayEventBridge
        from wtb.domain.events import RayVariantExecutionCompletedEvent
        
        event_bus = WTBEventBus()
        bridge = RayEventBridge(event_bus=event_bus, use_outbox=False)
        
        completed_events = []
        event_bus.subscribe(RayVariantExecutionCompletedEvent, lambda e: completed_events.append(e))
        
        def emit_events(thread_id: int):
            for i in range(10):
                bridge.on_variant_execution_completed(
                    execution_id=f"exec-{thread_id}-{i}",
                    batch_test_id="bt-1",
                    actor_id=f"actor_{thread_id}",
                    combination_name=f"variant_{i}",
                    variants={"v": str(i)},
                    duration_ms=100,
                )
        
        threads = [threading.Thread(target=emit_events, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # Should have 50 events total (5 threads * 10 events each)
        assert len(completed_events) == 50


class TestRayEventBridgeWithOutbox:
    """Tests for RayEventBridge with outbox pattern."""
    
    def test_bridge_with_outbox_creates_outbox_event(self):
        """Test bridge with outbox creates outbox events."""
        from wtb.infrastructure.events import WTBEventBus, RayEventBridge
        
        # Mock UoW and outbox
        mock_uow = MagicMock()
        mock_outbox = MagicMock()
        mock_uow.outbox = mock_outbox
        mock_uow.__enter__ = Mock(return_value=mock_uow)
        mock_uow.__exit__ = Mock(return_value=False)
        
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
        
        # Verify outbox.add was called
        assert mock_outbox.add.called
    
    def test_bridge_with_outbox_commits_transaction(self):
        """Test bridge with outbox commits transaction."""
        from wtb.infrastructure.events import WTBEventBus, RayEventBridge
        
        mock_uow = MagicMock()
        mock_uow.__enter__ = Mock(return_value=mock_uow)
        mock_uow.__exit__ = Mock(return_value=False)
        
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
        
        # Verify commit was called
        assert mock_uow.commit.called
    
    def test_bridge_batch_publish(self):
        """Test bridge can publish multiple events atomically."""
        from wtb.infrastructure.events import WTBEventBus, RayEventBridge
        from wtb.domain.events import RayVariantExecutionCompletedEvent
        
        mock_uow = MagicMock()
        mock_uow.__enter__ = Mock(return_value=mock_uow)
        mock_uow.__exit__ = Mock(return_value=False)
        
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
        # Verify commit was called (atomic transaction)
        assert mock_uow.commit.called


# ═══════════════════════════════════════════════════════════════
# WTBAuditTrail Ray Event Tests
# ═══════════════════════════════════════════════════════════════


class TestWTBAuditTrailRayEvents:
    """Tests for Ray event recording in WTBAuditTrail."""
    
    def test_audit_trail_records_ray_batch_test_started(self):
        """Test audit trail records RayBatchTestStartedEvent."""
        from wtb.infrastructure.events import WTBAuditTrail, WTBAuditEventType
        from wtb.domain.events import RayBatchTestStartedEvent
        
        trail = WTBAuditTrail()
        
        event = RayBatchTestStartedEvent(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test Workflow",
            variant_count=10,
            parallel_workers=4,
            max_pending_tasks=8,
        )
        
        trail.record_event(event)
        
        assert len(trail.entries) == 1
        assert trail.entries[0].event_type == WTBAuditEventType.RAY_BATCH_TEST_STARTED
        assert "bt-1" in trail.entries[0].message
    
    def test_audit_trail_records_ray_batch_test_completed(self):
        """Test audit trail records RayBatchTestCompletedEvent."""
        from wtb.infrastructure.events import WTBAuditTrail, WTBAuditEventType, WTBAuditSeverity
        from wtb.domain.events import RayBatchTestCompletedEvent
        
        trail = WTBAuditTrail()
        
        event = RayBatchTestCompletedEvent(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            duration_ms=5000,
            variants_succeeded=8,
            variants_failed=2,
            best_combination_name="RandomForest",
            best_overall_score=0.95,
        )
        
        trail.record_event(event)
        
        assert len(trail.entries) == 1
        assert trail.entries[0].event_type == WTBAuditEventType.RAY_BATCH_TEST_COMPLETED
        assert trail.entries[0].severity == WTBAuditSeverity.SUCCESS
        assert "8 succeeded" in trail.entries[0].message
    
    def test_audit_trail_records_ray_batch_test_failed(self):
        """Test audit trail records RayBatchTestFailedEvent."""
        from wtb.infrastructure.events import WTBAuditTrail, WTBAuditEventType, WTBAuditSeverity
        from wtb.domain.events import RayBatchTestFailedEvent
        
        trail = WTBAuditTrail()
        
        event = RayBatchTestFailedEvent(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            duration_ms=1000,
            error_type="OutOfMemory",
            error_message="Cluster ran out of memory",
        )
        
        trail.record_event(event)
        
        assert len(trail.entries) == 1
        assert trail.entries[0].event_type == WTBAuditEventType.RAY_BATCH_TEST_FAILED
        assert trail.entries[0].severity == WTBAuditSeverity.ERROR
        assert trail.entries[0].error is not None
    
    def test_audit_trail_records_ray_variant_events(self):
        """Test audit trail records variant execution events."""
        from wtb.infrastructure.events import WTBAuditTrail, WTBAuditEventType
        from wtb.domain.events import (
            RayVariantExecutionStartedEvent,
            RayVariantExecutionCompletedEvent,
            RayVariantExecutionFailedEvent,
        )
        
        trail = WTBAuditTrail()
        
        # Record events
        trail.record_event(RayVariantExecutionStartedEvent(
            execution_id="exec-1",
            batch_test_id="bt-1",
            actor_id="actor_0",
            combination_name="Variant1",
        ))
        
        trail.record_event(RayVariantExecutionCompletedEvent(
            execution_id="exec-1",
            batch_test_id="bt-1",
            actor_id="actor_0",
            combination_name="Variant1",
            duration_ms=1000,
            overall_score=0.85,
        ))
        
        trail.record_event(RayVariantExecutionFailedEvent(
            execution_id="exec-2",
            batch_test_id="bt-1",
            actor_id="actor_1",
            combination_name="Variant2",
            error_type="RuntimeError",
            error_message="Something went wrong",
        ))
        
        assert len(trail.entries) == 3
        
        types = [e.event_type for e in trail.entries]
        assert WTBAuditEventType.RAY_VARIANT_EXECUTION_STARTED in types
        assert WTBAuditEventType.RAY_VARIANT_EXECUTION_COMPLETED in types
        assert WTBAuditEventType.RAY_VARIANT_EXECUTION_FAILED in types
    
    def test_audit_trail_records_ray_actor_events(self):
        """Test audit trail records actor lifecycle events."""
        from wtb.infrastructure.events import WTBAuditTrail, WTBAuditEventType
        from wtb.domain.events import (
            RayActorPoolCreatedEvent,
            RayActorInitializedEvent,
            RayActorFailedEvent,
        )
        
        trail = WTBAuditTrail()
        
        trail.record_event(RayActorPoolCreatedEvent(
            batch_test_id="bt-1",
            num_actors=4,
            cpus_per_actor=1.0,
            memory_per_actor_gb=2.0,
        ))
        
        trail.record_event(RayActorInitializedEvent(
            actor_id="actor_0",
            batch_test_id="bt-1",
            initialization_time_ms=500,
            state_adapter_type="InMemory",
        ))
        
        trail.record_event(RayActorFailedEvent(
            actor_id="actor_1",
            batch_test_id="bt-1",
            error_type="RuntimeError",
            error_message="Actor crashed",
        ))
        
        assert len(trail.entries) == 3
        
        types = [e.event_type for e in trail.entries]
        assert WTBAuditEventType.RAY_ACTOR_POOL_CREATED in types
        assert WTBAuditEventType.RAY_ACTOR_INITIALIZED in types
        assert WTBAuditEventType.RAY_ACTOR_FAILED in types


class TestAuditEventListenerRayEvents:
    """Tests for AuditEventListener with Ray events."""
    
    def test_listener_auto_records_ray_events(self):
        """Test AuditEventListener automatically records Ray events."""
        from wtb.infrastructure.events import (
            WTBEventBus,
            WTBAuditTrail,
            AuditEventListener,
            WTBAuditEventType,
        )
        from wtb.domain.events import RayBatchTestStartedEvent, RayBatchTestCompletedEvent
        
        event_bus = WTBEventBus()
        trail = WTBAuditTrail()
        listener = AuditEventListener(trail)
        listener.attach(event_bus)
        
        # Publish events
        event_bus.publish(RayBatchTestStartedEvent(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test",
            variant_count=5,
            parallel_workers=2,
            max_pending_tasks=4,
        ))
        
        event_bus.publish(RayBatchTestCompletedEvent(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            variants_succeeded=4,
            variants_failed=1,
        ))
        
        # Verify events were recorded
        assert len(trail.entries) == 2
        
        types = [e.event_type for e in trail.entries]
        assert WTBAuditEventType.RAY_BATCH_TEST_STARTED in types
        assert WTBAuditEventType.RAY_BATCH_TEST_COMPLETED in types
        
        listener.detach()


# ═══════════════════════════════════════════════════════════════
# Static Event Factory Tests (for Ray actors)
# ═══════════════════════════════════════════════════════════════


class TestStaticEventFactories:
    """Tests for static event factory methods used in Ray actors."""
    
    def test_create_variant_completed_event_dict(self):
        """Test creating serializable variant completed event dict."""
        from wtb.infrastructure.events import RayEventBridge, deserialize_ray_event
        
        event_dict = RayEventBridge.create_variant_completed_event_dict(
            execution_id="exec-1",
            batch_test_id="bt-1",
            actor_id="actor_0",
            combination_name="RandomForest",
            variants={"train": "rf"},
            duration_ms=1500,
            checkpoint_count=5,
            node_count=10,
            metrics={"accuracy": 0.95},
            overall_score=0.9,
            files_tracked=3,
            file_commit_id="fc-123",
        )
        
        assert event_dict["__event_type__"] == "RayVariantExecutionCompletedEvent"
        assert event_dict["execution_id"] == "exec-1"
        assert event_dict["combination_name"] == "RandomForest"
        
        # Verify it can be deserialized
        event = deserialize_ray_event(event_dict)
        assert event is not None
        assert event.execution_id == "exec-1"
    
    def test_create_variant_failed_event_dict(self):
        """Test creating serializable variant failed event dict."""
        from wtb.infrastructure.events import RayEventBridge, deserialize_ray_event
        
        event_dict = RayEventBridge.create_variant_failed_event_dict(
            execution_id="exec-1",
            batch_test_id="bt-1",
            actor_id="actor_0",
            combination_name="XGBoost",
            variants={"train": "xgb"},
            error_type="RuntimeError",
            error_message="Model training failed",
            failed_at_node="train_node",
            duration_ms=500,
        )
        
        assert event_dict["__event_type__"] == "RayVariantExecutionFailedEvent"
        assert event_dict["error_type"] == "RuntimeError"
        
        # Verify it can be deserialized
        event = deserialize_ray_event(event_dict)
        assert event is not None
        assert event.error_type == "RuntimeError"


# ═══════════════════════════════════════════════════════════════
# Global Instance Management Tests
# ═══════════════════════════════════════════════════════════════


class TestGlobalRayEventBridge:
    """Tests for global RayEventBridge instance management."""
    
    def test_get_ray_event_bridge_returns_none_initially(self):
        """Test get_ray_event_bridge returns None when not set."""
        from wtb.infrastructure.events import (
            get_ray_event_bridge,
            reset_ray_event_bridge,
        )
        
        reset_ray_event_bridge()
        assert get_ray_event_bridge() is None
    
    def test_set_and_get_ray_event_bridge(self):
        """Test setting and getting global bridge."""
        from wtb.infrastructure.events import (
            WTBEventBus,
            RayEventBridge,
            get_ray_event_bridge,
            set_ray_event_bridge,
            reset_ray_event_bridge,
        )
        
        reset_ray_event_bridge()
        
        event_bus = WTBEventBus()
        bridge = RayEventBridge(event_bus=event_bus)
        
        set_ray_event_bridge(bridge)
        
        assert get_ray_event_bridge() is bridge
        
        reset_ray_event_bridge()


# ═══════════════════════════════════════════════════════════════
# Integration Tests (without Ray)
# ═══════════════════════════════════════════════════════════════


class TestRayEventIntegration:
    """Integration tests for Ray event system (without actual Ray)."""
    
    def test_full_batch_test_event_flow(self):
        """Test full event flow for a batch test simulation."""
        from wtb.infrastructure.events import (
            WTBEventBus,
            WTBAuditTrail,
            AuditEventListener,
            RayEventBridge,
            WTBAuditEventType,
        )
        
        # Setup
        event_bus = WTBEventBus()
        trail = WTBAuditTrail(execution_id="bt-1")
        listener = AuditEventListener(trail)
        listener.attach(event_bus)
        
        bridge = RayEventBridge(
            event_bus=event_bus,
            use_outbox=False,
            audit_trail_factory=lambda: trail,
        )
        
        # Simulate batch test flow
        bridge.on_batch_test_started(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="ML Pipeline Test",
            variant_count=3,
            parallel_workers=2,
            max_pending_tasks=4,
        )
        
        bridge.on_actor_pool_created(
            batch_test_id="bt-1",
            num_actors=2,
            cpus_per_actor=1.0,
            memory_per_actor_gb=2.0,
            actor_ids=["actor_0", "actor_1"],
        )
        
        # Simulate 3 variant executions
        for i in range(3):
            bridge.on_variant_execution_started(
                execution_id=f"exec-{i}",
                batch_test_id="bt-1",
                actor_id=f"actor_{i % 2}",
                combination_name=f"Variant{i}",
                variants={"model": f"model_{i}"},
            )
            
            bridge.on_variant_execution_completed(
                execution_id=f"exec-{i}",
                batch_test_id="bt-1",
                actor_id=f"actor_{i % 2}",
                combination_name=f"Variant{i}",
                variants={"model": f"model_{i}"},
                duration_ms=1000 + i * 100,
                metrics={"accuracy": 0.8 + i * 0.05},
                overall_score=0.8 + i * 0.05,
            )
        
        bridge.on_batch_test_completed(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            variants_succeeded=3,
            variants_failed=0,
            best_combination_name="Variant2",
            best_overall_score=0.9,
        )
        
        # Verify audit trail
        summary = trail.get_summary()
        
        # Should have: 1 batch started + 1 pool created + 3 started + 3 completed + 1 batch completed = 9
        assert len(trail.entries) == 9
        
        # Verify event types
        types = [e.event_type for e in trail.entries]
        assert types.count(WTBAuditEventType.RAY_BATCH_TEST_STARTED) == 1
        assert types.count(WTBAuditEventType.RAY_BATCH_TEST_COMPLETED) == 1
        assert types.count(WTBAuditEventType.RAY_ACTOR_POOL_CREATED) == 1
        assert types.count(WTBAuditEventType.RAY_VARIANT_EXECUTION_STARTED) == 3
        assert types.count(WTBAuditEventType.RAY_VARIANT_EXECUTION_COMPLETED) == 3
        
        listener.detach()
    
    def test_batch_test_with_failures(self):
        """Test event flow with variant failures."""
        from wtb.infrastructure.events import (
            WTBEventBus,
            WTBAuditTrail,
            AuditEventListener,
            RayEventBridge,
            WTBAuditSeverity,
        )
        
        event_bus = WTBEventBus()
        trail = WTBAuditTrail()
        listener = AuditEventListener(trail)
        listener.attach(event_bus)
        
        bridge = RayEventBridge(event_bus=event_bus, use_outbox=False)
        
        # Start batch
        bridge.on_batch_test_started(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test",
            variant_count=2,
            parallel_workers=2,
            max_pending_tasks=4,
        )
        
        # One success, one failure
        bridge.on_variant_execution_completed(
            execution_id="exec-1",
            batch_test_id="bt-1",
            actor_id="actor_0",
            combination_name="Success",
            variants={},
            duration_ms=1000,
            overall_score=0.9,
        )
        
        bridge.on_variant_execution_failed(
            execution_id="exec-2",
            batch_test_id="bt-1",
            actor_id="actor_1",
            combination_name="Failure",
            variants={},
            error_type="ModelError",
            error_message="Training diverged",
        )
        
        # Verify we have error entries
        errors = trail.get_errors()
        assert len(errors) == 1
        assert "Training diverged" in errors[0].error
        
        listener.detach()
