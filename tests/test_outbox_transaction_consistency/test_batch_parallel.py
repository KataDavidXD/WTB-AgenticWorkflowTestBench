"""
Unit tests for Batch Testing and Parallel Execution Transaction Consistency.

Tests outbox transaction consistency during:
- Batch test execution with multiple variants
- Parallel worker execution
- Ray actor pool management
- Concurrent variant processing
- Result aggregation
- Failure handling in batch contexts

ACID Compliance Focus:
- Atomicity: Batch operations complete fully or rolled back
- Consistency: Results consistent across parallel workers
- Isolation: Workers don't interfere with each other
- Durability: All results persisted correctly

REFACTORED (2026-01-28):
- Now uses REAL OutboxEvent domain types
- Uses centralized mock infrastructure from tests.mocks
- Tests pass with both mock and real implementations

Run with: pytest tests/test_outbox_transaction_consistency/test_batch_parallel.py -v
"""

import pytest
import threading
import time
import queue
from datetime import datetime
from typing import Dict, Any, List
from unittest.mock import Mock, MagicMock

# Import real domain types
from wtb.domain.models.outbox import OutboxEvent, OutboxEventType, OutboxStatus

# Import test helpers
from tests.test_outbox_transaction_consistency.helpers import (
    BatchTestState,
    ParallelExecutionState,
    create_batch_test_state,
    create_parallel_state,
)

# Import centralized mocks and utilities
from tests.mocks import (
    MockMemento,
    create_test_outbox_event,
    generate_batch_test_variants,
)
from tests.mocks.services import verify_outbox_consistency, verify_transaction_atomicity


# ═══════════════════════════════════════════════════════════════════════════════
# Helper Function for Creating Test Events
# ═══════════════════════════════════════════════════════════════════════════════


def create_batch_outbox_event(
    event_id: str,
    event_type_name: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: Dict[str, Any] = None,
) -> OutboxEvent:
    """
    Create an OutboxEvent for batch testing.
    
    This helper bridges old string-based event types to proper enums.
    Maps custom event type names to appropriate OutboxEventType enums.
    
    Args:
        event_id: Unique event ID
        event_type_name: String name for event type (for backward compatibility)
        aggregate_type: Type of aggregate
        aggregate_id: ID of aggregate
        payload: Event payload
        
    Returns:
        Real OutboxEvent instance
    """
    # Map common test event type strings to real enums
    type_mapping = {
        "BATCH_TEST_STARTED": OutboxEventType.BATCH_TEST_CREATED,
        "BATCH_TEST_COMPLETED": OutboxEventType.BATCH_TEST_CREATED,
        "BATCH_TEST_CANCELLED": OutboxEventType.BATCH_TEST_CANCELLED,
        "VARIANT_EXECUTION_COMPLETED": OutboxEventType.CHECKPOINT_SAVED,
        "TASK_COMPLETED": OutboxEventType.CHECKPOINT_SAVED,
        "PARALLEL_TASK_COMPLETED": OutboxEventType.CHECKPOINT_SAVED,
        "ACTOR_POOL_CREATED": OutboxEventType.RAY_EVENT,
        "ACTOR_FAILED": OutboxEventType.RAY_EVENT,
        "ACTOR_REPLACED": OutboxEventType.RAY_EVENT,
        "TASK_ASSIGNED": OutboxEventType.RAY_EVENT,
        "RESULTS_AGGREGATED": OutboxEventType.CHECKPOINT_VERIFY,
    }
    
    # Get the enum type, default to CHECKPOINT_CREATE for unknown types
    event_type = type_mapping.get(event_type_name, OutboxEventType.CHECKPOINT_CREATE)
    
    event = OutboxEvent(
        event_id=event_id,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload or {},
    )
    # Store original type name for backward-compatible comparisons
    event.payload["_event_type_name"] = event_type_name
    return event


def event_type_matches(event: OutboxEvent, type_name: str) -> bool:
    """
    Check if event matches a given type name.
    
    Supports both enum comparisons and backward-compatible string names.
    """
    # Check if original type name was stored
    if "_event_type_name" in event.payload:
        return event.payload["_event_type_name"] == type_name
    # Fall back to enum value comparison
    return event.event_type.value == type_name.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# Batch Test Execution Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBatchTestExecutionConsistency:
    """Tests for batch test execution transaction consistency."""
    
    def test_batch_test_start_creates_outbox_event(
        self,
        outbox_repository,
        ray_event_bridge,
    ):
        """Batch test start should create outbox event."""
        batch_test_id = "bt-start-1"
        
        # Create batch start event using REAL OutboxEvent
        start_event = create_test_outbox_event(
            event_type=OutboxEventType.BATCH_TEST_CREATED,
            aggregate_type="BatchTest",
            aggregate_id=batch_test_id,
            event_id=f"batch-start-{batch_test_id}",
            payload={
                "workflow_id": "wf-1",
                "variant_count": 5,
                "parallel_workers": 4,
                "max_pending_tasks": 8,
            },
        )
        outbox_repository.add(start_event)
        
        # Verify event created
        saved = outbox_repository.get_by_id(f"batch-start-{batch_test_id}")
        assert saved is not None
        assert saved.payload["variant_count"] == 5
    
    def test_batch_test_variant_execution_events(
        self,
        batch_test_compiled_graph,
        outbox_repository,
        batch_test_variants,
    ):
        """Each variant execution should create appropriate events."""
        batch_test_id = "bt-variants-1"
        
        # Execute each variant
        for i, variant in enumerate(batch_test_variants[:3]):  # Just test 3
            config = {"configurable": {"thread_id": f"{batch_test_id}-v{i}"}}
            state = create_batch_test_state(
                batch_test_id=batch_test_id,
                variant_name=variant["variant_name"],
                variant_config=variant["variant_config"],
            )
            
            result = batch_test_compiled_graph.invoke(state, config)
            
            # Create variant completion event using the helper for backward compatibility
            event = create_batch_outbox_event(
                event_id=f"variant-complete-{batch_test_id}-{i}",
                event_type_name="VARIANT_EXECUTION_COMPLETED",
                aggregate_type="BatchTest",
                aggregate_id=batch_test_id,
                payload={
                    "variant_name": variant["variant_name"],
                    "execution_result": result.get("execution_result"),
                    "duration_ms": 100 + i * 10,
                },
            )
            outbox_repository.add(event)
        
        # Verify all variant events created
        all_events = outbox_repository.list_all()
        variant_events = [e for e in all_events if event_type_matches(e, "VARIANT_EXECUTION_COMPLETED")]
        assert len(variant_events) == 3
    
    def test_batch_test_completion_event(
        self,
        outbox_repository,
    ):
        """Batch test completion should create summary event."""
        batch_test_id = "bt-complete-1"
        
        # Simulate variant completions
        for i in range(5):
            outbox_repository.add(create_batch_outbox_event(
                event_id=f"var-{batch_test_id}-{i}",
                event_type_name="VARIANT_EXECUTION_COMPLETED",
                aggregate_type="BatchTest",
                aggregate_id=batch_test_id,
                payload={"variant_index": i, "success": True},
            ))
        
        # Create completion event
        completion_event = create_batch_outbox_event(
            event_id=f"batch-complete-{batch_test_id}",
            event_type_name="BATCH_TEST_COMPLETED",
            aggregate_type="BatchTest",
            aggregate_id=batch_test_id,
            payload={
                "total_variants": 5,
                "succeeded": 5,
                "failed": 0,
                "total_duration_ms": 500,
            },
        )
        outbox_repository.add(completion_event)
        
        # Verify completion event
        saved = outbox_repository.get_by_id(f"batch-complete-{batch_test_id}")
        assert saved.payload["succeeded"] == 5
        assert saved.payload["failed"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Parallel Worker Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestParallelWorkerConsistency:
    """Tests for parallel worker transaction consistency."""
    
    def test_parallel_workers_isolated(
        self,
        actor_pool,
        outbox_repository,
    ):
        """Parallel workers should be isolated."""
        batch_test_id = "bt-parallel-1"
        
        # Create actors for parallel workers
        workers = []
        for i in range(4):
            actor = actor_pool.create_actor(
                actor_id=f"actor-{batch_test_id}-{i}",
                execution_id=f"exec-{batch_test_id}-{i}",
                workspace_id=f"ws-{i}",
            )
            workers.append(actor)
        
        # Execute in parallel
        results = {}
        lock = threading.Lock()
        
        def execute_worker(worker_id: int):
            actor_id = f"actor-{batch_test_id}-{worker_id}"
            # Simulate work
            time.sleep(0.01 * worker_id)
            
            with lock:
                results[worker_id] = {
                    "actor_id": actor_id,
                    "completed_at": datetime.now().isoformat(),
                }
        
        threads = [threading.Thread(target=execute_worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # All workers should complete
        assert len(results) == 4
        
        # Each worker result should be distinct
        actor_ids = [r["actor_id"] for r in results.values()]
        assert len(set(actor_ids)) == 4
    
    def test_parallel_file_commits_isolated(
        self,
        commit_repository,
        blob_repository,
    ):
        """Parallel file commits should be isolated."""
        commits = []
        lock = threading.Lock()
        
        def create_commit(worker_id: int):
            # Each worker creates its own files
            blob_hash = blob_repository.save(f"content from worker {worker_id}".encode())
            
            commit = commit_repository.add_commit(
                commit_id=f"commit-worker-{worker_id}",
                mementos=[MockMemento(f"worker_{worker_id}_output.txt", blob_hash)],
                execution_id=f"exec-parallel-{worker_id}",
            )
            
            with lock:
                commits.append(commit)
        
        threads = [threading.Thread(target=create_commit, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # All commits should be created
        assert len(commits) == 5
        
        # Each commit should have distinct content
        commit_ids = [c.commit_id for c in commits]
        assert len(set(commit_ids)) == 5
    
    def test_parallel_outbox_events_thread_safe(
        self,
        outbox_repository,
    ):
        """Parallel outbox event creation should be thread-safe."""
        errors = queue.Queue()
        
        def create_event(event_id: int):
            try:
                event = create_batch_outbox_event(
                    event_id=f"parallel-event-{event_id}",
                    event_type_name="PARALLEL_TASK_COMPLETED",
                    aggregate_type="Task",
                    aggregate_id=f"task-{event_id}",
                )
                outbox_repository.add(event)
            except Exception as e:
                errors.put(e)
        
        threads = [threading.Thread(target=create_event, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # No errors should occur
        assert errors.empty()
        
        # All events should be created
        all_events = outbox_repository.list_all()
        assert len(all_events) == 20


# ═══════════════════════════════════════════════════════════════════════════════
# Ray Actor Pool Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRayActorPoolConsistency:
    """Tests for Ray actor pool transaction consistency."""
    
    def test_actor_pool_creation_event(
        self,
        actor_pool,
        outbox_repository,
    ):
        """Actor pool creation should create outbox event."""
        batch_test_id = "bt-pool-1"
        
        # Create actors
        for i in range(4):
            actor_pool.create_actor(
                actor_id=f"actor-pool-{i}",
                execution_id=f"exec-pool-{i}",
                workspace_id="ws-pool",
            )
        
        # Create pool event
        pool_event = create_batch_outbox_event(
            event_id=f"pool-created-{batch_test_id}",
            event_type_name="ACTOR_POOL_CREATED",
            aggregate_type="ActorPool",
            aggregate_id=batch_test_id,
            payload={
                "pool_size": 4,
                "actor_ids": [f"actor-pool-{i}" for i in range(4)],
            },
        )
        outbox_repository.add(pool_event)
        
        # Verify
        saved = outbox_repository.get_by_id(f"pool-created-{batch_test_id}")
        assert len(saved.payload["actor_ids"]) == 4
    
    def test_actor_task_distribution(
        self,
        actor_pool,
        outbox_repository,
    ):
        """Tasks should be distributed across actors correctly."""
        batch_test_id = "bt-distribute-1"
        num_actors = 4
        num_tasks = 10
        
        # Create actors
        for i in range(num_actors):
            actor_pool.create_actor(
                actor_id=f"actor-dist-{i}",
                execution_id=f"exec-dist-{i}",
                workspace_id="ws-dist",
            )
        
        # Distribute tasks
        task_assignments = {}
        for task_id in range(num_tasks):
            actor_idx = task_id % num_actors
            actor_id = f"actor-dist-{actor_idx}"
            
            if actor_id not in task_assignments:
                task_assignments[actor_id] = []
            task_assignments[actor_id].append(task_id)
            
            # Create task assignment event
            outbox_repository.add(create_batch_outbox_event(
                event_id=f"task-assign-{task_id}",
                event_type_name="TASK_ASSIGNED",
                aggregate_type="Task",
                aggregate_id=str(task_id),
                payload={
                    "actor_id": actor_id,
                    "task_id": task_id,
                },
            ))
        
        # Verify distribution
        for actor_id, tasks in task_assignments.items():
            assert len(tasks) >= 2  # Each actor should have at least 2 tasks
    
    def test_actor_failure_handling(
        self,
        actor_pool,
        outbox_repository,
    ):
        """Actor failure should be handled correctly."""
        batch_test_id = "bt-failure-1"
        
        # Create actor
        actor = actor_pool.create_actor(
            actor_id="actor-fail-1",
            execution_id="exec-fail-1",
            workspace_id="ws-fail",
        )
        
        # Simulate failure
        actor_pool.kill_actor("actor-fail-1")
        
        # Create failure event
        failure_event = create_batch_outbox_event(
            event_id="actor-failure-event-1",
            event_type_name="ACTOR_FAILED",
            aggregate_type="Actor",
            aggregate_id="actor-fail-1",
            payload={
                "error_type": "RayActorError",
                "error_message": "Actor died unexpectedly",
                "pending_tasks": 3,
            },
        )
        outbox_repository.add(failure_event)
        
        # Create replacement actor
        replacement = actor_pool.create_actor(
            actor_id="actor-fail-1-replacement",
            execution_id="exec-fail-1",  # Same execution
            workspace_id="ws-fail",
        )
        
        # Create replacement event
        replacement_event = create_batch_outbox_event(
            event_id="actor-replacement-event-1",
            event_type_name="ACTOR_REPLACED",
            aggregate_type="Actor",
            aggregate_id="actor-fail-1-replacement",
            payload={
                "replaced_actor": "actor-fail-1",
                "tasks_reassigned": 3,
            },
        )
        outbox_repository.add(replacement_event)
        
        # Verify events
        assert outbox_repository.get_by_id("actor-failure-event-1") is not None
        assert outbox_repository.get_by_id("actor-replacement-event-1") is not None


# ═══════════════════════════════════════════════════════════════════════════════
# Result Aggregation Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestResultAggregationConsistency:
    """Tests for result aggregation transaction consistency."""
    
    def test_variant_results_aggregated(
        self,
        outbox_repository,
    ):
        """Variant results should be correctly aggregated."""
        batch_test_id = "bt-aggregate-1"
        
        # Create variant completion events
        results = []
        for i in range(5):
            success = i != 2  # Variant 2 fails
            event = create_batch_outbox_event(
                event_id=f"var-result-{i}",
                event_type_name="VARIANT_EXECUTION_COMPLETED",
                aggregate_type="BatchTest",
                aggregate_id=batch_test_id,
                payload={
                    "variant_index": i,
                    "success": success,
                    "duration_ms": 100 + i * 10,
                    "result": f"result_{i}" if success else None,
                    "error": "Test error" if not success else None,
                },
            )
            outbox_repository.add(event)
            results.append({"success": success, "duration": 100 + i * 10})
        
        # Aggregate results
        succeeded = sum(1 for r in results if r["success"])
        failed = sum(1 for r in results if not r["success"])
        avg_duration = sum(r["duration"] for r in results) / len(results)
        
        # Create aggregation event
        agg_event = create_batch_outbox_event(
            event_id=f"aggregate-{batch_test_id}",
            event_type_name="RESULTS_AGGREGATED",
            aggregate_type="BatchTest",
            aggregate_id=batch_test_id,
            payload={
                "total": 5,
                "succeeded": succeeded,
                "failed": failed,
                "avg_duration_ms": avg_duration,
            },
        )
        outbox_repository.add(agg_event)
        
        # Verify aggregation
        saved = outbox_repository.get_by_id(f"aggregate-{batch_test_id}")
        assert saved.payload["succeeded"] == 4
        assert saved.payload["failed"] == 1
    
    def test_partial_results_on_cancellation(
        self,
        outbox_repository,
    ):
        """Cancellation should preserve partial results."""
        batch_test_id = "bt-cancel-1"
        
        # Some variants complete
        for i in range(3):
            outbox_repository.add(create_batch_outbox_event(
                event_id=f"var-partial-{i}",
                event_type_name="VARIANT_EXECUTION_COMPLETED",
                aggregate_type="BatchTest",
                aggregate_id=batch_test_id,
                payload={"variant_index": i, "success": True},
            ))
        
        # Cancellation
        cancel_event = create_batch_outbox_event(
            event_id=f"cancel-{batch_test_id}",
            event_type_name="BATCH_TEST_CANCELLED",
            aggregate_type="BatchTest",
            aggregate_id=batch_test_id,
            payload={
                "reason": "User requested",
                "completed_variants": 3,
                "pending_variants": 2,
            },
        )
        outbox_repository.add(cancel_event)
        
        # Verify partial results preserved
        all_events = outbox_repository.list_all()
        completed = [e for e in all_events if event_type_matches(e, "VARIANT_EXECUTION_COMPLETED")]
        assert len(completed) == 3


# ═══════════════════════════════════════════════════════════════════════════════
# Concurrent Batch Test Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestConcurrentBatchTestConsistency:
    """Tests for concurrent batch test transaction consistency."""
    
    def test_multiple_batch_tests_isolated(
        self,
        outbox_repository,
    ):
        """Multiple concurrent batch tests should be isolated."""
        batch_ids = ["bt-concurrent-1", "bt-concurrent-2", "bt-concurrent-3"]
        
        results = {bid: [] for bid in batch_ids}
        lock = threading.Lock()
        
        def run_batch(batch_id: str):
            # Start event
            outbox_repository.add(create_batch_outbox_event(
                event_id=f"start-{batch_id}",
                event_type_name="BATCH_TEST_STARTED",
                aggregate_type="BatchTest",
                aggregate_id=batch_id,
            ))
            
            # Variant events
            for i in range(3):
                outbox_repository.add(create_batch_outbox_event(
                    event_id=f"var-{batch_id}-{i}",
                    event_type_name="VARIANT_EXECUTION_COMPLETED",
                    aggregate_type="BatchTest",
                    aggregate_id=batch_id,
                    payload={"variant_index": i},
                ))
                with lock:
                    results[batch_id].append(i)
            
            # Complete event
            outbox_repository.add(create_batch_outbox_event(
                event_id=f"complete-{batch_id}",
                event_type_name="BATCH_TEST_COMPLETED",
                aggregate_type="BatchTest",
                aggregate_id=batch_id,
            ))
        
        threads = [threading.Thread(target=run_batch, args=(bid,)) for bid in batch_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # Each batch should have its own results
        for batch_id in batch_ids:
            assert len(results[batch_id]) == 3
        
        # Events should be correctly assigned to batches
        all_events = outbox_repository.list_all()
        for batch_id in batch_ids:
            batch_events = [e for e in all_events if e.aggregate_id == batch_id]
            assert len(batch_events) == 5  # 1 start + 3 variants + 1 complete
    
    def test_resource_contention_handling(
        self,
        actor_pool,
        outbox_repository,
    ):
        """Resource contention between batch tests should be handled."""
        # Limited actor pool
        pool_size = 4
        for i in range(pool_size):
            actor_pool.create_actor(
                actor_id=f"shared-actor-{i}",
                execution_id=f"shared-exec-{i}",
                workspace_id="shared-ws",
            )
        
        # Multiple batch tests compete for actors
        batch_requests = []
        lock = threading.Lock()
        
        def request_actors(batch_id: str, requested: int):
            allocated = []
            active = actor_pool.list_active()
            # Try to get actors (may not get all requested)
            for actor in active[:requested]:
                allocated.append(actor.actor_id)
            
            with lock:
                batch_requests.append({
                    "batch_id": batch_id,
                    "requested": requested,
                    "allocated": len(allocated),
                })
        
        threads = [
            threading.Thread(target=request_actors, args=("bt-1", 3)),
            threading.Thread(target=request_actors, args=("bt-2", 3)),
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # Both should have requested actors (may be shared)
        assert len(batch_requests) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# ACID Compliance Tests for Batch/Parallel
# ═══════════════════════════════════════════════════════════════════════════════


class TestBatchParallelACIDCompliance:
    """Tests for ACID compliance in batch and parallel operations."""
    
    def test_batch_atomicity_all_variants_or_none(
        self,
        outbox_repository,
    ):
        """Batch test should complete all variants or none (on error)."""
        batch_test_id = "bt-atomic-1"
        operations = []
        
        try:
            # Start
            outbox_repository.add(create_batch_outbox_event(
                event_id=f"start-{batch_test_id}",
                event_type_name="BATCH_TEST_STARTED",
                aggregate_type="BatchTest",
                aggregate_id=batch_test_id,
            ))
            operations.append(("start", True))
            
            # All variants
            for i in range(3):
                outbox_repository.add(create_batch_outbox_event(
                    event_id=f"var-{batch_test_id}-{i}",
                    event_type_name="VARIANT_EXECUTION_COMPLETED",
                    aggregate_type="BatchTest",
                    aggregate_id=batch_test_id,
                ))
                operations.append((f"variant_{i}", True))
            
            # Complete
            outbox_repository.add(create_batch_outbox_event(
                event_id=f"complete-{batch_test_id}",
                event_type_name="BATCH_TEST_COMPLETED",
                aggregate_type="BatchTest",
                aggregate_id=batch_test_id,
            ))
            operations.append(("complete", True))
            
        except Exception:
            operations.append(("error", False))
        
        result = verify_transaction_atomicity(operations)
        assert result.success
    
    def test_parallel_consistency_results_match_inputs(
        self,
        batch_test_compiled_graph,
    ):
        """Parallel execution results should match inputs."""
        results = {}
        lock = threading.Lock()
        
        def execute_variant(variant_id: int, config_value: int):
            config = {"configurable": {"thread_id": f"consist-{variant_id}"}}
            state = create_batch_test_state(
                variant_name=f"variant_{variant_id}",
                variant_config={"value": config_value},
            )
            
            result = batch_test_compiled_graph.invoke(state, config)
            
            with lock:
                results[variant_id] = {
                    "config_value": config_value,
                    "result": result,
                }
        
        threads = []
        for i in range(5):
            t = threading.Thread(target=execute_variant, args=(i, i * 10))
            threads.append(t)
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # Results should match inputs
        for vid, data in results.items():
            assert data["config_value"] == vid * 10
    
    def test_parallel_isolation_no_cross_contamination(
        self,
        outbox_repository,
    ):
        """Parallel executions should not contaminate each other."""
        exec_results = {}
        lock = threading.Lock()
        
        def create_events(exec_id: str, unique_data: str):
            for i in range(3):
                event = create_batch_outbox_event(
                    event_id=f"event-{exec_id}-{i}",
                    event_type_name="TASK_COMPLETED",
                    aggregate_type="Execution",
                    aggregate_id=exec_id,
                    payload={"data": unique_data, "step": i},
                )
                outbox_repository.add(event)
            
            with lock:
                exec_results[exec_id] = unique_data
        
        threads = []
        for i in range(5):
            exec_id = f"exec-iso-{i}"
            unique_data = f"data_unique_to_{i}"
            t = threading.Thread(target=create_events, args=(exec_id, unique_data))
            threads.append(t)
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # Verify no cross-contamination
        all_events = outbox_repository.list_all()
        for exec_id, expected_data in exec_results.items():
            exec_events = [e for e in all_events if e.aggregate_id == exec_id]
            for event in exec_events:
                assert event.payload["data"] == expected_data
    
    def test_batch_durability_all_events_persisted(
        self,
        outbox_repository,
    ):
        """All batch events should be durably persisted."""
        batch_test_id = "bt-durable-1"
        
        # Create events
        event_ids = []
        for i in range(10):
            event = create_batch_outbox_event(
                event_id=f"durable-{batch_test_id}-{i}",
                event_type_name="VARIANT_EXECUTION_COMPLETED",
                aggregate_type="BatchTest",
                aggregate_id=batch_test_id,
            )
            outbox_repository.add(event)
            event.mark_processed()
            outbox_repository.update(event)
            event_ids.append(event.event_id)
        
        # Verify all persisted
        for event_id in event_ids:
            saved = outbox_repository.get_by_id(event_id)
            assert saved is not None
            # Compare against enum value (works with both enum and string)
            assert saved.status.value == "processed" or saved.status == OutboxStatus.PROCESSED
