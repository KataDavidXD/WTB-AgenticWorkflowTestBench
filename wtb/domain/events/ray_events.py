"""
Ray Batch Test Events - Domain events for Ray-based distributed execution.

Tracks:
- Batch test lifecycle (started/completed/failed/cancelled)
- Actor lifecycle (created/initialized/failed)
- Variant execution (started/completed/failed)
- File tracking integration events

Design Principles:
- SOLID: Events are immutable value objects, single responsibility
- ACID: Events are designed to be persisted atomically with execution state
- DIP: Events depend on WTBEvent base class abstraction

Usage:
    from wtb.domain.events.ray_events import RayBatchTestStartedEvent
    
    event = RayBatchTestStartedEvent(
        batch_test_id="bt-1",
        workflow_id="wf-1",
        variant_count=10,
        parallel_workers=4,
    )
    event_bus.publish(event)
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from wtb.domain.events.execution_events import WTBEvent


class RayEventType(Enum):
    """Ray-specific event types for categorization."""
    # Batch test lifecycle
    BATCH_TEST_STARTED = "ray_batch_test_started"
    BATCH_TEST_COMPLETED = "ray_batch_test_completed"
    BATCH_TEST_FAILED = "ray_batch_test_failed"
    BATCH_TEST_CANCELLED = "ray_batch_test_cancelled"
    BATCH_TEST_PROGRESS = "ray_batch_test_progress"
    
    # Actor lifecycle
    ACTOR_POOL_CREATED = "ray_actor_pool_created"
    ACTOR_INITIALIZED = "ray_actor_initialized"
    ACTOR_FAILED = "ray_actor_failed"
    ACTOR_RESTARTED = "ray_actor_restarted"
    
    # Variant execution
    VARIANT_EXECUTION_STARTED = "ray_variant_execution_started"
    VARIANT_EXECUTION_COMPLETED = "ray_variant_execution_completed"
    VARIANT_EXECUTION_FAILED = "ray_variant_execution_failed"
    VARIANT_EXECUTION_CANCELLED = "ray_variant_execution_cancelled"
    
    # File tracking
    VARIANT_FILES_TRACKED = "ray_variant_files_tracked"
    VARIANT_FILES_TRACKING_FAILED = "ray_variant_files_tracking_failed"
    
    # Resource events
    BACKPRESSURE_APPLIED = "ray_backpressure_applied"
    BACKPRESSURE_RELEASED = "ray_backpressure_released"


# ═══════════════════════════════════════════════════════════════
# Base Ray Event
# ═══════════════════════════════════════════════════════════════


@dataclass
class RayEvent(WTBEvent):
    """Base class for Ray-specific events."""
    event_type: str = field(default="ray_event", init=False)
    
    # Common Ray metadata
    ray_job_id: str | None = None
    ray_address: str | None = None


# ═══════════════════════════════════════════════════════════════
# Batch Test Lifecycle Events
# ═══════════════════════════════════════════════════════════════


@dataclass
class RayBatchTestStartedEvent(RayEvent):
    """Published when a Ray batch test begins execution."""
    event_type: str = field(default="ray_batch_test_started", init=False)
    
    batch_test_id: str = ""
    workflow_id: str = ""
    workflow_name: str = ""
    variant_count: int = 0
    parallel_workers: int = 0
    max_pending_tasks: int = 0
    file_tracking_enabled: bool = False
    
    # Configuration snapshot
    config_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass
class RayBatchTestCompletedEvent(RayEvent):
    """Published when a Ray batch test completes successfully."""
    event_type: str = field(default="ray_batch_test_completed", init=False)
    
    batch_test_id: str = ""
    workflow_id: str = ""
    duration_ms: int = 0
    
    # Results summary
    variants_succeeded: int = 0
    variants_failed: int = 0
    variants_cancelled: int = 0
    
    # Best performer
    best_combination_name: str | None = None
    best_overall_score: float = 0.0
    
    # Files tracked (aggregated)
    total_files_tracked: int = 0
    
    # Comparison matrix available
    has_comparison_matrix: bool = False


@dataclass
class RayBatchTestFailedEvent(RayEvent):
    """Published when a Ray batch test fails."""
    event_type: str = field(default="ray_batch_test_failed", init=False)
    
    batch_test_id: str = ""
    workflow_id: str = ""
    duration_ms: int = 0
    
    # Failure details
    error_type: str = ""
    error_message: str = ""
    failed_at_variant: str | None = None
    
    # Partial progress
    variants_succeeded: int = 0
    variants_failed: int = 0
    variants_pending: int = 0


@dataclass
class RayBatchTestCancelledEvent(RayEvent):
    """Published when a Ray batch test is cancelled."""
    event_type: str = field(default="ray_batch_test_cancelled", init=False)
    
    batch_test_id: str = ""
    workflow_id: str = ""
    duration_ms: int = 0
    
    # Cancellation details
    reason: str = ""
    cancelled_by: str = "user"  # "user", "timeout", "resource_limit"
    
    # Progress at cancellation
    variants_completed: int = 0
    variants_cancelled: int = 0


@dataclass
class RayBatchTestProgressEvent(RayEvent):
    """Published periodically to report batch test progress."""
    event_type: str = field(default="ray_batch_test_progress", init=False)
    
    batch_test_id: str = ""
    
    # Progress metrics
    total_variants: int = 0
    completed_variants: int = 0
    failed_variants: int = 0
    in_progress_variants: int = 0
    
    # Timing
    elapsed_ms: int = 0
    estimated_remaining_ms: int | None = None
    
    # Rate metrics
    variants_per_second: float = 0.0


# ═══════════════════════════════════════════════════════════════
# Actor Lifecycle Events
# ═══════════════════════════════════════════════════════════════


@dataclass
class RayActorPoolCreatedEvent(RayEvent):
    """Published when the Ray actor pool is created."""
    event_type: str = field(default="ray_actor_pool_created", init=False)
    
    batch_test_id: str = ""
    num_actors: int = 0
    cpus_per_actor: float = 0.0
    memory_per_actor_gb: float = 0.0
    
    # Actor IDs
    actor_ids: list[str] = field(default_factory=list)


@dataclass
class RayActorInitializedEvent(RayEvent):
    """Published when a Ray actor completes lazy initialization."""
    event_type: str = field(default="ray_actor_initialized", init=False)
    
    actor_id: str = ""
    batch_test_id: str = ""
    initialization_time_ms: int = 0
    
    # Adapter type being used
    state_adapter_type: str = ""
    file_tracking_enabled: bool = False


@dataclass
class RayActorFailedEvent(RayEvent):
    """Published when a Ray actor fails."""
    event_type: str = field(default="ray_actor_failed", init=False)
    
    actor_id: str = ""
    batch_test_id: str = ""
    
    # Failure details
    error_type: str = ""
    error_message: str = ""
    
    # Context
    executing_variant: str | None = None
    executions_completed: int = 0


@dataclass
class RayActorRestartedEvent(RayEvent):
    """Published when a Ray actor is restarted (fault tolerance)."""
    event_type: str = field(default="ray_actor_restarted", init=False)
    
    actor_id: str = ""
    batch_test_id: str = ""
    restart_count: int = 0
    reason: str = ""


# ═══════════════════════════════════════════════════════════════
# Variant Execution Events
# ═══════════════════════════════════════════════════════════════


@dataclass
class RayVariantExecutionStartedEvent(RayEvent):
    """Published when variant execution begins on an actor."""
    event_type: str = field(default="ray_variant_execution_started", init=False)
    
    execution_id: str = ""
    batch_test_id: str = ""
    actor_id: str = ""
    
    # Variant details
    combination_name: str = ""
    variants: dict[str, str] = field(default_factory=dict)
    
    # Queue position
    queue_position: int = 0
    total_in_queue: int = 0


@dataclass
class RayVariantExecutionCompletedEvent(RayEvent):
    """Published when variant execution completes successfully."""
    event_type: str = field(default="ray_variant_execution_completed", init=False)
    
    execution_id: str = ""
    batch_test_id: str = ""
    actor_id: str = ""
    
    # Variant details
    combination_name: str = ""
    variants: dict[str, str] = field(default_factory=dict)
    
    # Execution metrics
    duration_ms: int = 0
    checkpoint_count: int = 0
    node_count: int = 0
    
    # Evaluation metrics
    metrics: dict[str, float] = field(default_factory=dict)
    overall_score: float = 0.0
    
    # File tracking
    files_tracked: int = 0
    file_commit_id: str | None = None


@dataclass
class RayVariantExecutionFailedEvent(RayEvent):
    """Published when variant execution fails."""
    event_type: str = field(default="ray_variant_execution_failed", init=False)
    
    execution_id: str = ""
    batch_test_id: str = ""
    actor_id: str = ""
    
    # Variant details
    combination_name: str = ""
    variants: dict[str, str] = field(default_factory=dict)
    
    # Failure details
    error_type: str = ""
    error_message: str = ""
    failed_at_node: str | None = None
    duration_ms: int = 0
    
    # Partial progress
    nodes_completed: int = 0
    checkpoints_created: int = 0


@dataclass
class RayVariantExecutionCancelledEvent(RayEvent):
    """Published when variant execution is cancelled."""
    event_type: str = field(default="ray_variant_execution_cancelled", init=False)
    
    execution_id: str = ""
    batch_test_id: str = ""
    actor_id: str = ""
    
    combination_name: str = ""
    reason: str = ""
    duration_ms: int = 0


# ═══════════════════════════════════════════════════════════════
# File Tracking Events
# ═══════════════════════════════════════════════════════════════


@dataclass
class RayVariantFilesTrackedEvent(RayEvent):
    """Published when variant output files are tracked."""
    event_type: str = field(default="ray_variant_files_tracked", init=False)
    
    execution_id: str = ""
    batch_test_id: str = ""
    actor_id: str = ""
    
    combination_name: str = ""
    files_tracked: int = 0
    file_commit_id: str = ""
    checkpoint_id: str = ""
    
    # File details
    file_paths: list[str] = field(default_factory=list)


@dataclass
class RayVariantFilesTrackingFailedEvent(RayEvent):
    """Published when file tracking fails for a variant."""
    event_type: str = field(default="ray_variant_files_tracking_failed", init=False)
    
    execution_id: str = ""
    batch_test_id: str = ""
    actor_id: str = ""
    
    combination_name: str = ""
    error_message: str = ""
    
    # Files that failed to track
    failed_files: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# Resource Events
# ═══════════════════════════════════════════════════════════════


@dataclass
class RayBackpressureAppliedEvent(RayEvent):
    """Published when backpressure is applied (too many pending tasks)."""
    event_type: str = field(default="ray_backpressure_applied", init=False)
    
    batch_test_id: str = ""
    pending_tasks: int = 0
    max_pending_tasks: int = 0
    wait_time_ms: int = 0


@dataclass
class RayBackpressureReleasedEvent(RayEvent):
    """Published when backpressure is released."""
    event_type: str = field(default="ray_backpressure_released", init=False)
    
    batch_test_id: str = ""
    pending_tasks: int = 0
    tasks_completed: int = 0


# ═══════════════════════════════════════════════════════════════
# Exports
# ═══════════════════════════════════════════════════════════════

__all__ = [
    # Enums
    "RayEventType",
    # Base
    "RayEvent",
    # Batch test lifecycle
    "RayBatchTestStartedEvent",
    "RayBatchTestCompletedEvent",
    "RayBatchTestFailedEvent",
    "RayBatchTestCancelledEvent",
    "RayBatchTestProgressEvent",
    # Actor lifecycle
    "RayActorPoolCreatedEvent",
    "RayActorInitializedEvent",
    "RayActorFailedEvent",
    "RayActorRestartedEvent",
    # Variant execution
    "RayVariantExecutionStartedEvent",
    "RayVariantExecutionCompletedEvent",
    "RayVariantExecutionFailedEvent",
    "RayVariantExecutionCancelledEvent",
    # File tracking
    "RayVariantFilesTrackedEvent",
    "RayVariantFilesTrackingFailedEvent",
    # Resource
    "RayBackpressureAppliedEvent",
    "RayBackpressureReleasedEvent",
]
