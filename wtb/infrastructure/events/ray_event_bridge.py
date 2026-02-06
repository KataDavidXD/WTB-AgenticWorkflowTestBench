"""
Ray Event Bridge - Integrates Ray batch testing with WTB Event Bus and Audit.

v1.8 (2026-02-06): Enhanced for rollback/fork operations with graph factory pattern.

Provides transaction-consistent event publishing for Ray distributed execution:
- Outbox pattern for ACID compliance
- Event aggregation from Ray actors
- Audit trail integration
- Rollback/Fork event coordination (v1.8)

Design Principles:
- SOLID: Single responsibility (bridge events), Open for extension
- ACID: Uses outbox pattern for event persistence before publishing
- DIP: Depends on abstractions (IOutboxRepository, WTBEventBus)

Architecture:
    Ray Actor ──► RayEventBridge ──► OutboxRepository (transactional)
                       │                    │
                       │                    ▼
                       │              OutboxProcessor (background)
                       │                    │
                       ▼                    ▼
                 WTBEventBus ◄────────────────
                       │
                       ▼
                 WTBAuditTrail

v1.8 Graph Factory Integration:
- Ray actors receive graph_factory_module/name (serializable)
- Actors import factory and create LangGraph locally
- Enables checkpoint creation during batch execution
- Coordinator receives graph for rollback/fork operations

Usage:
    # Create bridge with UoW factory
    bridge = RayEventBridge(
        event_bus=get_wtb_event_bus(),
        uow_factory=lambda: UnitOfWorkFactory.create("sqlalchemy", db_url),
    )
    
    # Use in RayBatchTestRunner
    bridge.on_batch_test_started(batch_test)
    bridge.on_variant_completed(execution_id, result)
    
    # For Ray actors (serializable events)
    event_dict = bridge.create_variant_completed_event_dict(...)
    # Send to main process via Ray object store
"""

from typing import Dict, Any, List, Optional, Callable, Type
from datetime import datetime
from dataclasses import dataclass
from threading import Lock
import logging
import uuid
import json

from wtb.domain.events import (
    WTBEvent,
    RayBatchTestStartedEvent,
    RayBatchTestCompletedEvent,
    RayBatchTestFailedEvent,
    RayBatchTestCancelledEvent,
    RayBatchTestProgressEvent,
    RayActorPoolCreatedEvent,
    RayActorInitializedEvent,
    RayActorFailedEvent,
    RayActorRestartedEvent,
    RayVariantExecutionStartedEvent,
    RayVariantExecutionCompletedEvent,
    RayVariantExecutionFailedEvent,
    RayVariantExecutionCancelledEvent,
    RayVariantFilesTrackedEvent,
    RayVariantFilesTrackingFailedEvent,
    RayBackpressureAppliedEvent,
    RayBackpressureReleasedEvent,
)
from wtb.domain.models.outbox import OutboxEvent, OutboxEventType, OutboxStatus
from wtb.infrastructure.events.wtb_event_bus import WTBEventBus

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Event Serialization Helper
# ═══════════════════════════════════════════════════════════════


def serialize_ray_event(event: WTBEvent) -> Dict[str, Any]:
    """
    Serialize a Ray event to a dictionary for outbox storage.
    
    Args:
        event: WTBEvent instance
        
    Returns:
        Serializable dictionary representation
    """
    # Get all dataclass fields
    data = {}
    for field_name in event.__dataclass_fields__:
        value = getattr(event, field_name)
        if isinstance(value, datetime):
            data[field_name] = value.isoformat()
        elif isinstance(value, dict):
            data[field_name] = value
        elif isinstance(value, list):
            data[field_name] = value
        else:
            data[field_name] = value
    
    data["__event_type__"] = type(event).__name__
    return data


def deserialize_ray_event(data: Dict[str, Any]) -> Optional[WTBEvent]:
    """
    Deserialize a Ray event from dictionary.
    
    Args:
        data: Dictionary from serialize_ray_event
        
    Returns:
        WTBEvent instance or None if unknown type
    """
    event_type_name = data.pop("__event_type__", None)
    if not event_type_name:
        return None
    
    # Map event type name to class
    event_classes = {
        "RayBatchTestStartedEvent": RayBatchTestStartedEvent,
        "RayBatchTestCompletedEvent": RayBatchTestCompletedEvent,
        "RayBatchTestFailedEvent": RayBatchTestFailedEvent,
        "RayBatchTestCancelledEvent": RayBatchTestCancelledEvent,
        "RayBatchTestProgressEvent": RayBatchTestProgressEvent,
        "RayActorPoolCreatedEvent": RayActorPoolCreatedEvent,
        "RayActorInitializedEvent": RayActorInitializedEvent,
        "RayActorFailedEvent": RayActorFailedEvent,
        "RayActorRestartedEvent": RayActorRestartedEvent,
        "RayVariantExecutionStartedEvent": RayVariantExecutionStartedEvent,
        "RayVariantExecutionCompletedEvent": RayVariantExecutionCompletedEvent,
        "RayVariantExecutionFailedEvent": RayVariantExecutionFailedEvent,
        "RayVariantExecutionCancelledEvent": RayVariantExecutionCancelledEvent,
        "RayVariantFilesTrackedEvent": RayVariantFilesTrackedEvent,
        "RayVariantFilesTrackingFailedEvent": RayVariantFilesTrackingFailedEvent,
        "RayBackpressureAppliedEvent": RayBackpressureAppliedEvent,
        "RayBackpressureReleasedEvent": RayBackpressureReleasedEvent,
    }
    
    event_class = event_classes.get(event_type_name)
    if not event_class:
        logger.warning(f"Unknown Ray event type: {event_type_name}")
        return None
    
    # Convert timestamp string back to datetime
    if "timestamp" in data and isinstance(data["timestamp"], str):
        data["timestamp"] = datetime.fromisoformat(data["timestamp"])
    
    # Remove event_type field as it's set by the dataclass
    data.pop("event_type", None)
    
    try:
        return event_class(**data)
    except Exception as e:
        logger.error(f"Failed to deserialize {event_type_name}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# Ray Event Bridge
# ═══════════════════════════════════════════════════════════════


class RayEventBridge:
    """
    Bridge between Ray batch testing and WTB Event Bus.
    
    Ensures transaction consistency using the outbox pattern:
    1. Events are first written to the outbox table within the same transaction
    2. A background processor reads pending events and publishes them
    3. If publishing fails, events remain in outbox for retry
    
    Thread Safety:
    - Uses Lock for concurrent access from multiple threads
    - Safe to use from RayBatchTestRunner which may process results concurrently
    
    ACID Compliance:
    - Atomic: Events written in same transaction as execution state
    - Consistent: Event schema validated before write
    - Isolated: Per-batch audit trails
    - Durable: Events persisted to database
    """
    
    def __init__(
        self,
        event_bus: WTBEventBus,
        uow_factory: Optional[Callable] = None,
        use_outbox: bool = True,
        audit_trail_factory: Optional[Callable] = None,
    ):
        """
        Initialize Ray Event Bridge.
        
        Args:
            event_bus: WTBEventBus instance for publishing
            uow_factory: Factory function to create UnitOfWork (for outbox pattern)
            use_outbox: Whether to use outbox pattern (True for ACID compliance)
            audit_trail_factory: Optional factory for creating batch-specific audit trails
        """
        self._event_bus = event_bus
        self._uow_factory = uow_factory
        self._use_outbox = use_outbox and uow_factory is not None
        self._audit_trail_factory = audit_trail_factory
        self._lock = Lock()
        
        # Per-batch tracking
        self._batch_audit_trails: Dict[str, Any] = {}  # batch_id -> WTBAuditTrail
        self._batch_start_times: Dict[str, datetime] = {}
        
        if self._use_outbox:
            logger.info("RayEventBridge initialized with outbox pattern (ACID compliant)")
        else:
            logger.info("RayEventBridge initialized without outbox (immediate publish)")
    
    @property
    def event_bus(self) -> WTBEventBus:
        """Get the underlying event bus (for WorkspaceManager integration)."""
        return self._event_bus
    
    # ═══════════════════════════════════════════════════════════════
    # Core Event Publishing
    # ═══════════════════════════════════════════════════════════════
    
    def publish_event(
        self,
        event: WTBEvent,
        batch_test_id: Optional[str] = None,
        use_outbox: Optional[bool] = None,
    ) -> bool:
        """
        Publish a Ray event with optional outbox pattern.
        
        Args:
            event: WTBEvent to publish
            batch_test_id: Optional batch test ID for outbox grouping
            use_outbox: Override default outbox behavior
            
        Returns:
            True if event was published/queued successfully
        """
        should_use_outbox = use_outbox if use_outbox is not None else self._use_outbox
        
        with self._lock:
            try:
                if should_use_outbox and self._uow_factory:
                    return self._publish_via_outbox(event, batch_test_id)
                else:
                    return self._publish_immediate(event)
            except Exception as e:
                logger.error(f"Failed to publish event {type(event).__name__}: {e}")
                return False
    
    def _publish_immediate(self, event: WTBEvent) -> bool:
        """Publish event immediately to event bus."""
        try:
            self._event_bus.publish(event)
            return True
        except Exception as e:
            logger.error(f"Immediate publish failed: {e}")
            return False
    
    def _publish_via_outbox(self, event: WTBEvent, batch_test_id: Optional[str]) -> bool:
        """
        Publish event via outbox pattern for ACID compliance.
        
        The event is written to the outbox table first, then published.
        If publish fails, the outbox processor will retry later.
        """
        if not self._uow_factory:
            return self._publish_immediate(event)
        
        try:
            uow = self._uow_factory()
            with uow:
                # Create outbox event
                outbox_event = OutboxEvent(
                    id=None,  # Auto-generated integer by database
                    event_id=str(uuid.uuid4()),  # UUID for idempotency
                    event_type=OutboxEventType.RAY_EVENT,
                    aggregate_id=batch_test_id or "ray-batch",
                    aggregate_type="RayBatchTest",
                    payload=serialize_ray_event(event),
                    created_at=datetime.now(),
                )
                
                # Write to outbox (within transaction) - capture returned event with ID
                outbox_event = uow.outbox.add(outbox_event)
                uow.commit()
                
                # Try immediate publish (best effort)
                try:
                    self._event_bus.publish(event)
                    
                    # Mark as published (outbox_event now has database-assigned ID)
                    outbox_event.mark_published()
                    uow.outbox.update(outbox_event)
                    uow.commit()
                except Exception as e:
                    # Event in outbox will be processed later by OutboxProcessor
                    logger.debug(f"Event queued for async processing: {e}")
                
                return True
                
        except Exception as e:
            logger.error(f"Outbox write failed: {e}")
            # Fall back to immediate publish
            return self._publish_immediate(event)
    
    def publish_events_batch(
        self,
        events: List[WTBEvent],
        batch_test_id: str,
    ) -> int:
        """
        Publish multiple events atomically.
        
        All events are written to outbox in a single transaction.
        
        Args:
            events: List of events to publish
            batch_test_id: Batch test ID for grouping
            
        Returns:
            Number of events successfully published/queued
        """
        if not events:
            return 0
        
        with self._lock:
            if self._use_outbox and self._uow_factory:
                try:
                    uow = self._uow_factory()
                    with uow:
                        for event in events:
                            outbox_event = OutboxEvent(
                                id=None,  # Auto-generated integer by database
                                event_id=str(uuid.uuid4()),  # UUID for idempotency
                                event_type=OutboxEventType.RAY_EVENT,
                                aggregate_id=batch_test_id,
                                aggregate_type="RayBatchTest",
                                payload=serialize_ray_event(event),
                                created_at=datetime.now(),
                            )
                            uow.outbox.add(outbox_event)
                        
                        uow.commit()
                        
                        # Publish all immediately (best effort)
                        self._event_bus.publish_all(events)
                        
                        return len(events)
                except Exception as e:
                    logger.error(f"Batch publish failed: {e}")
                    return 0
            else:
                # Direct publish
                count = 0
                for event in events:
                    if self._publish_immediate(event):
                        count += 1
                return count
    
    # ═══════════════════════════════════════════════════════════════
    # Batch Test Lifecycle Events
    # ═══════════════════════════════════════════════════════════════
    
    def on_batch_test_started(
        self,
        batch_test_id: str,
        workflow_id: str,
        workflow_name: str,
        variant_count: int,
        parallel_workers: int,
        max_pending_tasks: int,
        file_tracking_enabled: bool = False,
        config_snapshot: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Emit event when batch test starts."""
        with self._lock:
            self._batch_start_times[batch_test_id] = datetime.now()
            
            # Create audit trail for this batch
            if self._audit_trail_factory:
                from wtb.infrastructure.events.wtb_audit_trail import WTBAuditTrail
                trail = self._audit_trail_factory()
                trail.execution_id = batch_test_id
                trail.workflow_id = workflow_id
                trail.started_at = datetime.now()
                self._batch_audit_trails[batch_test_id] = trail
        
        event = RayBatchTestStartedEvent(
            batch_test_id=batch_test_id,
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            variant_count=variant_count,
            parallel_workers=parallel_workers,
            max_pending_tasks=max_pending_tasks,
            file_tracking_enabled=file_tracking_enabled,
            config_snapshot=config_snapshot or {},
        )
        self.publish_event(event, batch_test_id)
    
    def on_batch_test_completed(
        self,
        batch_test_id: str,
        workflow_id: str,
        variants_succeeded: int,
        variants_failed: int,
        variants_cancelled: int = 0,
        best_combination_name: Optional[str] = None,
        best_overall_score: float = 0.0,
        total_files_tracked: int = 0,
        has_comparison_matrix: bool = False,
    ) -> None:
        """Emit event when batch test completes."""
        duration_ms = 0
        with self._lock:
            if batch_test_id in self._batch_start_times:
                duration = datetime.now() - self._batch_start_times[batch_test_id]
                duration_ms = int(duration.total_seconds() * 1000)
            
            # Finalize audit trail
            if batch_test_id in self._batch_audit_trails:
                self._batch_audit_trails[batch_test_id].ended_at = datetime.now()
        
        event = RayBatchTestCompletedEvent(
            batch_test_id=batch_test_id,
            workflow_id=workflow_id,
            duration_ms=duration_ms,
            variants_succeeded=variants_succeeded,
            variants_failed=variants_failed,
            variants_cancelled=variants_cancelled,
            best_combination_name=best_combination_name,
            best_overall_score=best_overall_score,
            total_files_tracked=total_files_tracked,
            has_comparison_matrix=has_comparison_matrix,
        )
        self.publish_event(event, batch_test_id)
    
    def on_batch_test_failed(
        self,
        batch_test_id: str,
        workflow_id: str,
        error_type: str,
        error_message: str,
        failed_at_variant: Optional[str] = None,
        variants_succeeded: int = 0,
        variants_failed: int = 0,
        variants_pending: int = 0,
    ) -> None:
        """Emit event when batch test fails."""
        duration_ms = 0
        with self._lock:
            if batch_test_id in self._batch_start_times:
                duration = datetime.now() - self._batch_start_times[batch_test_id]
                duration_ms = int(duration.total_seconds() * 1000)
        
        event = RayBatchTestFailedEvent(
            batch_test_id=batch_test_id,
            workflow_id=workflow_id,
            duration_ms=duration_ms,
            error_type=error_type,
            error_message=error_message,
            failed_at_variant=failed_at_variant,
            variants_succeeded=variants_succeeded,
            variants_failed=variants_failed,
            variants_pending=variants_pending,
        )
        self.publish_event(event, batch_test_id)
    
    def on_batch_test_cancelled(
        self,
        batch_test_id: str,
        workflow_id: str,
        reason: str,
        cancelled_by: str = "user",
        variants_completed: int = 0,
        variants_cancelled: int = 0,
    ) -> None:
        """Emit event when batch test is cancelled."""
        duration_ms = 0
        with self._lock:
            if batch_test_id in self._batch_start_times:
                duration = datetime.now() - self._batch_start_times[batch_test_id]
                duration_ms = int(duration.total_seconds() * 1000)
        
        event = RayBatchTestCancelledEvent(
            batch_test_id=batch_test_id,
            workflow_id=workflow_id,
            duration_ms=duration_ms,
            reason=reason,
            cancelled_by=cancelled_by,
            variants_completed=variants_completed,
            variants_cancelled=variants_cancelled,
        )
        self.publish_event(event, batch_test_id)
    
    def on_batch_test_progress(
        self,
        batch_test_id: str,
        total_variants: int,
        completed_variants: int,
        failed_variants: int,
        in_progress_variants: int,
        estimated_remaining_ms: Optional[int] = None,
    ) -> None:
        """Emit progress event for batch test."""
        elapsed_ms = 0
        with self._lock:
            if batch_test_id in self._batch_start_times:
                duration = datetime.now() - self._batch_start_times[batch_test_id]
                elapsed_ms = int(duration.total_seconds() * 1000)
        
        variants_per_second = 0.0
        if elapsed_ms > 0:
            variants_per_second = (completed_variants + failed_variants) / (elapsed_ms / 1000)
        
        event = RayBatchTestProgressEvent(
            batch_test_id=batch_test_id,
            total_variants=total_variants,
            completed_variants=completed_variants,
            failed_variants=failed_variants,
            in_progress_variants=in_progress_variants,
            elapsed_ms=elapsed_ms,
            estimated_remaining_ms=estimated_remaining_ms,
            variants_per_second=variants_per_second,
        )
        # Progress events don't need outbox (non-critical)
        self.publish_event(event, batch_test_id, use_outbox=False)
    
    # ═══════════════════════════════════════════════════════════════
    # Actor Lifecycle Events
    # ═══════════════════════════════════════════════════════════════
    
    def on_actor_pool_created(
        self,
        batch_test_id: str,
        num_actors: int,
        cpus_per_actor: float,
        memory_per_actor_gb: float,
        actor_ids: List[str],
    ) -> None:
        """Emit event when actor pool is created."""
        event = RayActorPoolCreatedEvent(
            batch_test_id=batch_test_id,
            num_actors=num_actors,
            cpus_per_actor=cpus_per_actor,
            memory_per_actor_gb=memory_per_actor_gb,
            actor_ids=actor_ids,
        )
        self.publish_event(event, batch_test_id)
    
    def on_actor_initialized(
        self,
        actor_id: str,
        batch_test_id: str,
        initialization_time_ms: int,
        state_adapter_type: str,
        file_tracking_enabled: bool = False,
    ) -> None:
        """Emit event when actor completes initialization."""
        event = RayActorInitializedEvent(
            actor_id=actor_id,
            batch_test_id=batch_test_id,
            initialization_time_ms=initialization_time_ms,
            state_adapter_type=state_adapter_type,
            file_tracking_enabled=file_tracking_enabled,
        )
        self.publish_event(event, batch_test_id)
    
    def on_actor_failed(
        self,
        actor_id: str,
        batch_test_id: str,
        error_type: str,
        error_message: str,
        executing_variant: Optional[str] = None,
        executions_completed: int = 0,
    ) -> None:
        """Emit event when actor fails."""
        event = RayActorFailedEvent(
            actor_id=actor_id,
            batch_test_id=batch_test_id,
            error_type=error_type,
            error_message=error_message,
            executing_variant=executing_variant,
            executions_completed=executions_completed,
        )
        self.publish_event(event, batch_test_id)
    
    # ═══════════════════════════════════════════════════════════════
    # Variant Execution Events
    # ═══════════════════════════════════════════════════════════════
    
    def on_variant_execution_started(
        self,
        execution_id: str,
        batch_test_id: str,
        actor_id: str,
        combination_name: str,
        variants: Dict[str, str],
        queue_position: int = 0,
        total_in_queue: int = 0,
    ) -> None:
        """Emit event when variant execution starts."""
        event = RayVariantExecutionStartedEvent(
            execution_id=execution_id,
            batch_test_id=batch_test_id,
            actor_id=actor_id,
            combination_name=combination_name,
            variants=variants,
            queue_position=queue_position,
            total_in_queue=total_in_queue,
        )
        self.publish_event(event, batch_test_id)
    
    def on_variant_execution_completed(
        self,
        execution_id: str,
        batch_test_id: str,
        actor_id: str,
        combination_name: str,
        variants: Dict[str, str],
        duration_ms: int,
        checkpoint_count: int = 0,
        node_count: int = 0,
        metrics: Optional[Dict[str, float]] = None,
        overall_score: float = 0.0,
        files_tracked: int = 0,
        file_commit_id: Optional[str] = None,
    ) -> None:
        """Emit event when variant execution completes."""
        event = RayVariantExecutionCompletedEvent(
            execution_id=execution_id,
            batch_test_id=batch_test_id,
            actor_id=actor_id,
            combination_name=combination_name,
            variants=variants,
            duration_ms=duration_ms,
            checkpoint_count=checkpoint_count,
            node_count=node_count,
            metrics=metrics or {},
            overall_score=overall_score,
            files_tracked=files_tracked,
            file_commit_id=file_commit_id,
        )
        self.publish_event(event, batch_test_id)
    
    def on_variant_execution_failed(
        self,
        execution_id: str,
        batch_test_id: str,
        actor_id: str,
        combination_name: str,
        variants: Dict[str, str],
        error_type: str,
        error_message: str,
        failed_at_node: Optional[str] = None,
        duration_ms: int = 0,
        nodes_completed: int = 0,
        checkpoints_created: int = 0,
    ) -> None:
        """Emit event when variant execution fails."""
        event = RayVariantExecutionFailedEvent(
            execution_id=execution_id,
            batch_test_id=batch_test_id,
            actor_id=actor_id,
            combination_name=combination_name,
            variants=variants,
            error_type=error_type,
            error_message=error_message,
            failed_at_node=failed_at_node,
            duration_ms=duration_ms,
            nodes_completed=nodes_completed,
            checkpoints_created=checkpoints_created,
        )
        self.publish_event(event, batch_test_id)
    
    # ═══════════════════════════════════════════════════════════════
    # File Tracking Events
    # ═══════════════════════════════════════════════════════════════
    
    def on_variant_files_tracked(
        self,
        execution_id: str,
        batch_test_id: str,
        actor_id: str,
        combination_name: str,
        files_tracked: int,
        file_commit_id: str,
        checkpoint_id: int,
        file_paths: List[str],
    ) -> None:
        """Emit event when variant files are tracked."""
        event = RayVariantFilesTrackedEvent(
            execution_id=execution_id,
            batch_test_id=batch_test_id,
            actor_id=actor_id,
            combination_name=combination_name,
            files_tracked=files_tracked,
            file_commit_id=file_commit_id,
            checkpoint_id=checkpoint_id,
            file_paths=file_paths,
        )
        self.publish_event(event, batch_test_id)
    
    def on_variant_files_tracking_failed(
        self,
        execution_id: str,
        batch_test_id: str,
        actor_id: str,
        combination_name: str,
        error_message: str,
        failed_files: List[str],
    ) -> None:
        """Emit event when file tracking fails."""
        event = RayVariantFilesTrackingFailedEvent(
            execution_id=execution_id,
            batch_test_id=batch_test_id,
            actor_id=actor_id,
            combination_name=combination_name,
            error_message=error_message,
            failed_files=failed_files,
        )
        self.publish_event(event, batch_test_id)
    
    # ═══════════════════════════════════════════════════════════════
    # Resource Events
    # ═══════════════════════════════════════════════════════════════
    
    def on_backpressure_applied(
        self,
        batch_test_id: str,
        pending_tasks: int,
        max_pending_tasks: int,
        wait_time_ms: int = 0,
    ) -> None:
        """Emit event when backpressure is applied."""
        event = RayBackpressureAppliedEvent(
            batch_test_id=batch_test_id,
            pending_tasks=pending_tasks,
            max_pending_tasks=max_pending_tasks,
            wait_time_ms=wait_time_ms,
        )
        # Backpressure events don't need outbox
        self.publish_event(event, batch_test_id, use_outbox=False)
    
    def on_backpressure_released(
        self,
        batch_test_id: str,
        pending_tasks: int,
        tasks_completed: int,
    ) -> None:
        """Emit event when backpressure is released."""
        event = RayBackpressureReleasedEvent(
            batch_test_id=batch_test_id,
            pending_tasks=pending_tasks,
            tasks_completed=tasks_completed,
        )
        self.publish_event(event, batch_test_id, use_outbox=False)
    
    # ═══════════════════════════════════════════════════════════════
    # Audit Trail Access
    # ═══════════════════════════════════════════════════════════════
    
    def get_batch_audit_trail(self, batch_test_id: str) -> Optional[Any]:
        """
        Get the audit trail for a batch test.
        
        Returns:
            WTBAuditTrail for the batch test or None
        """
        with self._lock:
            return self._batch_audit_trails.get(batch_test_id)
    
    def cleanup_batch(self, batch_test_id: str) -> None:
        """
        Clean up resources for a completed batch test.
        
        Call this after batch test finishes to free memory.
        """
        with self._lock:
            self._batch_start_times.pop(batch_test_id, None)
            self._batch_audit_trails.pop(batch_test_id, None)
    
    # ═══════════════════════════════════════════════════════════════
    # Static Event Factories (for Ray actors)
    # ═══════════════════════════════════════════════════════════════
    
    @staticmethod
    def create_variant_completed_event_dict(
        execution_id: str,
        batch_test_id: str,
        actor_id: str,
        combination_name: str,
        variants: Dict[str, str],
        duration_ms: int,
        checkpoint_count: int = 0,
        node_count: int = 0,
        metrics: Optional[Dict[str, float]] = None,
        overall_score: float = 0.0,
        files_tracked: int = 0,
        file_commit_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a serializable event dict for Ray actors.
        
        Use this in actors to create events that can be sent back
        to the main process via Ray object store.
        """
        return {
            "__event_type__": "RayVariantExecutionCompletedEvent",
            "execution_id": execution_id,
            "batch_test_id": batch_test_id,
            "actor_id": actor_id,
            "combination_name": combination_name,
            "variants": variants,
            "duration_ms": duration_ms,
            "checkpoint_count": checkpoint_count,
            "node_count": node_count,
            "metrics": metrics or {},
            "overall_score": overall_score,
            "files_tracked": files_tracked,
            "file_commit_id": file_commit_id,
            "timestamp": datetime.now().isoformat(),
        }
    
    @staticmethod
    def create_variant_failed_event_dict(
        execution_id: str,
        batch_test_id: str,
        actor_id: str,
        combination_name: str,
        variants: Dict[str, str],
        error_type: str,
        error_message: str,
        failed_at_node: Optional[str] = None,
        duration_ms: int = 0,
        nodes_completed: int = 0,
        checkpoints_created: int = 0,
    ) -> Dict[str, Any]:
        """Create a serializable failure event dict for Ray actors."""
        return {
            "__event_type__": "RayVariantExecutionFailedEvent",
            "execution_id": execution_id,
            "batch_test_id": batch_test_id,
            "actor_id": actor_id,
            "combination_name": combination_name,
            "variants": variants,
            "error_type": error_type,
            "error_message": error_message,
            "failed_at_node": failed_at_node,
            "duration_ms": duration_ms,
            "nodes_completed": nodes_completed,
            "checkpoints_created": checkpoints_created,
            "timestamp": datetime.now().isoformat(),
        }


# ═══════════════════════════════════════════════════════════════
# Global Instance Management
# ═══════════════════════════════════════════════════════════════

_global_ray_event_bridge: Optional[RayEventBridge] = None


def get_ray_event_bridge() -> Optional[RayEventBridge]:
    """Get the global Ray event bridge instance."""
    return _global_ray_event_bridge


def set_ray_event_bridge(bridge: RayEventBridge) -> None:
    """Set the global Ray event bridge instance."""
    global _global_ray_event_bridge
    _global_ray_event_bridge = bridge


def reset_ray_event_bridge() -> None:
    """Reset the global Ray event bridge."""
    global _global_ray_event_bridge
    _global_ray_event_bridge = None
