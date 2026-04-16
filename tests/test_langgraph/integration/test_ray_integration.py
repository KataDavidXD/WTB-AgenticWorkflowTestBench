"""
Integration tests for LangGraph + Ray distributed execution.

Tests Ray integration:
- Distributed workflow execution
- Actor-based batch processing
- Event bridging across Ray tasks
- Transaction consistency in distributed environment

Note: These tests can run without Ray by using mocks for the Ray-specific
components, testing the integration patterns.

Run with: pytest tests/test_langgraph/integration/test_ray_integration.py -v
"""

import pytest
from typing import Dict, Any, List, Optional
from datetime import datetime
from unittest.mock import Mock, MagicMock, patch
import threading
import time

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from wtb.infrastructure.events import (
    WTBEventBus,
    WTBAuditTrail,
    AuditEventListener,
    InMemoryMetricsCollector,
)
from wtb.domain.events import (
    RayBatchTestStartedEvent,
    RayBatchTestCompletedEvent,
    RayBatchTestFailedEvent,
    RayVariantExecutionStartedEvent,
    RayVariantExecutionCompletedEvent,
    RayVariantExecutionFailedEvent,
    RayActorPoolCreatedEvent,
    RayActorInitializedEvent,
)

from tests.test_langgraph.helpers import (
    create_initial_simple_state,
    SimpleState,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Ray Event Model Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRayEventModels:
    """Tests for Ray event domain models."""
    
    def test_batch_test_started_event(self):
        """Test RayBatchTestStartedEvent creation."""
        event = RayBatchTestStartedEvent(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test Workflow",
            variant_count=10,
            parallel_workers=4,
            max_pending_tasks=8,
        )
        
        assert event.batch_test_id == "bt-1"
        assert event.variant_count == 10
        assert event.parallel_workers == 4
    
    def test_batch_test_completed_event(self):
        """Test RayBatchTestCompletedEvent creation."""
        event = RayBatchTestCompletedEvent(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            duration_ms=5000,
            variants_succeeded=8,
            variants_failed=2,
            best_combination_name="Variant1",
            best_overall_score=0.95,
        )
        
        assert event.variants_succeeded == 8
        assert event.variants_failed == 2
        assert event.best_overall_score == 0.95
    
    def test_batch_test_failed_event(self):
        """Test RayBatchTestFailedEvent creation."""
        event = RayBatchTestFailedEvent(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            error_type="OutOfMemory",
            error_message="Cluster exhausted",
        )
        
        assert event.error_type == "OutOfMemory"
    
    def test_variant_execution_events(self):
        """Test variant execution event types."""
        started = RayVariantExecutionStartedEvent(
            execution_id="exec-1",
            batch_test_id="bt-1",
            actor_id="actor_0",
            combination_name="Variant1",
        )
        
        completed = RayVariantExecutionCompletedEvent(
            execution_id="exec-1",
            batch_test_id="bt-1",
            actor_id="actor_0",
            combination_name="Variant1",
            duration_ms=1000,
            overall_score=0.85,
        )
        
        failed = RayVariantExecutionFailedEvent(
            execution_id="exec-1",
            batch_test_id="bt-1",
            actor_id="actor_0",
            combination_name="Variant1",
            error_type="RuntimeError",
            error_message="Test error",
        )
        
        assert started.combination_name == "Variant1"
        assert completed.overall_score == 0.85
        assert failed.error_type == "RuntimeError"
    
    def test_actor_lifecycle_events(self):
        """Test actor lifecycle events."""
        pool_created = RayActorPoolCreatedEvent(
            batch_test_id="bt-1",
            num_actors=4,
            cpus_per_actor=1.0,
            memory_per_actor_gb=2.0,
        )
        
        actor_init = RayActorInitializedEvent(
            actor_id="actor_0",
            batch_test_id="bt-1",
            initialization_time_ms=500,
        )
        
        assert pool_created.num_actors == 4
        assert actor_init.initialization_time_ms == 500


# ═══════════════════════════════════════════════════════════════════════════════
# Ray Event Serialization Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRayEventSerialization:
    """Tests for Ray event serialization."""
    
    def test_serialize_batch_test_event(self):
        """Test serializing batch test events."""
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
    
    def test_deserialize_batch_test_event(self):
        """Test deserializing batch test events."""
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
        assert deserialized.batch_test_id == "bt-1"
    
    def test_serialization_roundtrip(self):
        """Test serialization roundtrip for all Ray events."""
        from wtb.infrastructure.events import serialize_ray_event, deserialize_ray_event
        
        events = [
            RayBatchTestStartedEvent(batch_test_id="bt-1", workflow_id="wf-1"),
            RayBatchTestCompletedEvent(batch_test_id="bt-1", workflow_id="wf-1"),
            RayVariantExecutionCompletedEvent(
                execution_id="e-1", 
                batch_test_id="bt-1",
                actor_id="a-1",
                combination_name="v1"
            ),
        ]
        
        for original in events:
            serialized = serialize_ray_event(original)
            deserialized = deserialize_ray_event(serialized)
            
            assert deserialized is not None
            assert type(deserialized) == type(original)


# ═══════════════════════════════════════════════════════════════════════════════
# Ray Event Bridge Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRayEventBridge:
    """Tests for RayEventBridge."""
    
    def test_bridge_publishes_to_event_bus(self, event_bus):
        """Test bridge publishes events to event bus."""
        from wtb.infrastructure.events import RayEventBridge
        
        bridge = RayEventBridge(
            event_bus=event_bus,
            use_outbox=False,
        )
        
        received = []
        event_bus.subscribe(RayBatchTestStartedEvent, received.append)
        
        bridge.on_batch_test_started(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test",
            variant_count=5,
            parallel_workers=2,
            max_pending_tasks=4,
        )
        
        assert len(received) == 1
        assert received[0].batch_test_id == "bt-1"
    
    def test_bridge_tracks_batch_duration(self, event_bus):
        """Test bridge tracks batch test duration."""
        from wtb.infrastructure.events import RayEventBridge
        
        bridge = RayEventBridge(event_bus=event_bus, use_outbox=False)
        
        completed_events = []
        event_bus.subscribe(RayBatchTestCompletedEvent, completed_events.append)
        
        bridge.on_batch_test_started(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            workflow_name="Test",
            variant_count=5,
            parallel_workers=2,
            max_pending_tasks=4,
        )
        
        time.sleep(0.01)  # Small delay
        
        bridge.on_batch_test_completed(
            batch_test_id="bt-1",
            workflow_id="wf-1",
            variants_succeeded=4,
            variants_failed=1,
        )
        
        assert len(completed_events) == 1
        assert completed_events[0].duration_ms > 0
    
    def test_bridge_variant_execution_flow(self, event_bus):
        """Test bridge variant execution event flow."""
        from wtb.infrastructure.events import RayEventBridge
        
        bridge = RayEventBridge(event_bus=event_bus, use_outbox=False)
        
        started_events = []
        completed_events = []
        
        event_bus.subscribe(RayVariantExecutionStartedEvent, started_events.append)
        event_bus.subscribe(RayVariantExecutionCompletedEvent, completed_events.append)
        
        bridge.on_variant_execution_started(
            execution_id="exec-1",
            batch_test_id="bt-1",
            actor_id="actor_0",
            combination_name="Variant1",
            variants={"model": "rf"},
        )
        
        bridge.on_variant_execution_completed(
            execution_id="exec-1",
            batch_test_id="bt-1",
            actor_id="actor_0",
            combination_name="Variant1",
            variants={"model": "rf"},
            duration_ms=1000,
            metrics={"accuracy": 0.9},
            overall_score=0.85,
        )
        
        assert len(started_events) == 1
        assert len(completed_events) == 1
        assert completed_events[0].overall_score == 0.85


# ═══════════════════════════════════════════════════════════════════════════════
# Ray + LangGraph Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRayLangGraphIntegration:
    """Tests for Ray + LangGraph integration patterns."""
    
    def test_distributed_execution_pattern(self, simple_graph_def):
        """Test distributed execution pattern with LangGraph."""
        # This tests the pattern, not actual Ray distribution
        checkpointer = MemorySaver()
        graph = simple_graph_def.compile(checkpointer=checkpointer)
        
        results = {}
        
        # Simulate distributed execution on different "workers"
        for worker_id in range(3):
            config = {"configurable": {"thread_id": f"worker-{worker_id}"}}
            result = graph.invoke(create_initial_simple_state(), config)
            results[worker_id] = result
        
        # All workers should complete independently
        assert len(results) == 3
        for worker_id, result in results.items():
            assert result["messages"] == ["A", "B", "C"]
    
    def test_variant_execution_with_langgraph(self, simple_graph_def):
        """Test variant execution pattern."""
        checkpointer = MemorySaver()
        graph = simple_graph_def.compile(checkpointer=checkpointer)
        
        # Define variants (different initial states)
        variants = [
            {"messages": ["V1"], "count": 0},
            {"messages": ["V2"], "count": 10},
            {"messages": ["V3"], "count": 100},
        ]
        
        results = []
        for i, initial_state in enumerate(variants):
            config = {"configurable": {"thread_id": f"variant-{i}"}}
            result = graph.invoke(initial_state, config)
            results.append(result)
        
        # Each variant should have its prefix
        assert "V1" in results[0]["messages"]
        assert "V2" in results[1]["messages"]
        assert "V3" in results[2]["messages"]
    
    def test_checkpoint_persistence_across_workers(self, simple_graph_def):
        """Test checkpoint persistence works across simulated workers."""
        # Shared checkpointer (simulates shared storage)
        checkpointer = MemorySaver()
        graph = simple_graph_def.compile(checkpointer=checkpointer)
        
        # Worker 1 executes
        config = {"configurable": {"thread_id": "shared-exec-1"}}
        graph.invoke(create_initial_simple_state(), config)
        
        # Worker 2 can access the state
        state = graph.get_state(config)
        
        assert state is not None
        assert state.values["messages"] == ["A", "B", "C"]


# ═══════════════════════════════════════════════════════════════════════════════
# Batch Test Simulation Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBatchTestSimulation:
    """Tests simulating batch test execution."""
    
    def test_simulate_batch_test_flow(self, event_bus, audit_trail):
        """Test simulating complete batch test flow."""
        from wtb.infrastructure.events import RayEventBridge
        
        listener = AuditEventListener(audit_trail)
        listener.attach(event_bus)
        bridge = RayEventBridge(event_bus=event_bus, use_outbox=False)
        
        # Start batch test
        bridge.on_batch_test_started(
            batch_test_id="sim-bt-1",
            workflow_id="wf-1",
            workflow_name="Simulation Test",
            variant_count=3,
            parallel_workers=2,
            max_pending_tasks=4,
        )
        
        # Create actor pool
        bridge.on_actor_pool_created(
            batch_test_id="sim-bt-1",
            num_actors=2,
            cpus_per_actor=1.0,
            memory_per_actor_gb=2.0,
            actor_ids=["actor_0", "actor_1"],
        )
        
        # Execute variants
        for i in range(3):
            bridge.on_variant_execution_started(
                execution_id=f"exec-{i}",
                batch_test_id="sim-bt-1",
                actor_id=f"actor_{i % 2}",
                combination_name=f"Variant{i}",
                variants={"model": f"model_{i}"},
            )
            
            bridge.on_variant_execution_completed(
                execution_id=f"exec-{i}",
                batch_test_id="sim-bt-1",
                actor_id=f"actor_{i % 2}",
                combination_name=f"Variant{i}",
                variants={"model": f"model_{i}"},
                duration_ms=1000 + i * 100,
                metrics={"accuracy": 0.8 + i * 0.05},
                overall_score=0.8 + i * 0.05,
            )
        
        # Complete batch test
        bridge.on_batch_test_completed(
            batch_test_id="sim-bt-1",
            workflow_id="wf-1",
            variants_succeeded=3,
            variants_failed=0,
            best_combination_name="Variant2",
            best_overall_score=0.9,
        )
        
        # Verify event count
        # Events: 1 batch start + 1 pool + 3 starts + 3 completes + 1 batch complete = 9
        history = event_bus.get_event_history()
        assert len(history) >= 9
        
        listener.detach()
    
    def test_simulate_batch_test_with_failures(self, event_bus):
        """Test simulating batch test with variant failures."""
        from wtb.infrastructure.events import RayEventBridge
        
        bridge = RayEventBridge(event_bus=event_bus, use_outbox=False)
        
        failed_events = []
        event_bus.subscribe(RayVariantExecutionFailedEvent, failed_events.append)
        
        bridge.on_batch_test_started(
            batch_test_id="sim-fail-1",
            workflow_id="wf-1",
            workflow_name="Failure Test",
            variant_count=2,
            parallel_workers=2,
            max_pending_tasks=4,
        )
        
        # One success
        bridge.on_variant_execution_completed(
            execution_id="exec-success",
            batch_test_id="sim-fail-1",
            actor_id="actor_0",
            combination_name="Success",
            variants={},
            duration_ms=1000,
            overall_score=0.9,
        )
        
        # One failure
        bridge.on_variant_execution_failed(
            execution_id="exec-fail",
            batch_test_id="sim-fail-1",
            actor_id="actor_1",
            combination_name="Failure",
            variants={},
            error_type="ModelError",
            error_message="Training diverged",
        )
        
        assert len(failed_events) == 1
        assert failed_events[0].error_type == "ModelError"


# ═══════════════════════════════════════════════════════════════════════════════
# Thread Safety Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRayThreadSafety:
    """Tests for thread safety in Ray integration."""
    
    def test_concurrent_variant_events(self, event_bus):
        """Test concurrent variant event publishing."""
        from wtb.infrastructure.events import RayEventBridge
        
        bridge = RayEventBridge(event_bus=event_bus, use_outbox=False)
        
        completed = []
        event_bus.subscribe(RayVariantExecutionCompletedEvent, completed.append)
        
        def emit_events(thread_id: int):
            for i in range(10):
                bridge.on_variant_execution_completed(
                    execution_id=f"exec-{thread_id}-{i}",
                    batch_test_id="bt-1",
                    actor_id=f"actor_{thread_id}",
                    combination_name=f"Variant{i}",
                    variants={},
                    duration_ms=100,
                )
        
        threads = [
            threading.Thread(target=emit_events, args=(i,))
            for i in range(5)
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(completed) == 50


# ═══════════════════════════════════════════════════════════════════════════════
# ACID Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRayACIDCompliance:
    """Tests for ACID compliance in Ray integration."""
    
    def test_atomicity_variant_execution_events(self, event_bus):
        """Test variant execution events are atomic."""
        from wtb.infrastructure.events import RayEventBridge
        
        bridge = RayEventBridge(event_bus=event_bus, use_outbox=False)
        
        completed = []
        event_bus.subscribe(RayVariantExecutionCompletedEvent, completed.append)
        
        bridge.on_variant_execution_completed(
            execution_id="atomic-exec",
            batch_test_id="bt-1",
            actor_id="actor_0",
            combination_name="Atomic",
            variants={"key": "value"},
            duration_ms=1000,
            metrics={"acc": 0.9},
            overall_score=0.85,
            checkpoint_count=5,
            node_count=10,
        )
        
        event = completed[0]
        
        # All fields should be set atomically
        assert event.execution_id == "atomic-exec"
        assert event.metrics == {"acc": 0.9}
        assert event.checkpoint_count == 5
    
    def test_isolation_batch_tests(self, event_bus):
        """Test batch tests are isolated."""
        from wtb.infrastructure.events import RayEventBridge
        
        bridge = RayEventBridge(event_bus=event_bus, use_outbox=False)
        
        # Start two batch tests
        bridge.on_batch_test_started(
            batch_test_id="iso-bt-1",
            workflow_id="wf-1",
            workflow_name="Test 1",
            variant_count=5,
            parallel_workers=2,
            max_pending_tasks=4,
        )
        
        bridge.on_batch_test_started(
            batch_test_id="iso-bt-2",
            workflow_id="wf-2",
            workflow_name="Test 2",
            variant_count=10,
            parallel_workers=4,
            max_pending_tasks=8,
        )
        
        # Each should have independent tracking
        assert "iso-bt-1" in bridge._batch_start_times
        assert "iso-bt-2" in bridge._batch_start_times
    
    def test_consistency_event_timestamps(self, event_bus):
        """Test event timestamps are consistent."""
        from wtb.infrastructure.events import RayEventBridge
        
        bridge = RayEventBridge(event_bus=event_bus, use_outbox=False)
        
        before = datetime.now()
        
        bridge.on_batch_test_started(
            batch_test_id="ts-bt",
            workflow_id="wf-1",
            workflow_name="Timestamp Test",
            variant_count=1,
            parallel_workers=1,
            max_pending_tasks=1,
        )
        
        after = datetime.now()
        
        # Timestamp should be within range
        history = event_bus.get_event_history(RayBatchTestStartedEvent)
        event = history[-1]
        
        assert before <= event.timestamp <= after
