"""
WTB Audit Trail - Execution/Node-level audit tracking.

v1.8 (2026-02-06): Enhanced for batch rollback/fork operations.

Tracks workflow execution at the WTB abstraction level:
- Execution lifecycle (start/pause/resume/complete/fail)
- Node execution (start/complete/fail/skip)
- Checkpoint operations (create/rollback/branch)
- Batch rollback/fork operations (v1.8)

Differs from AgentGit AuditTrail:
- AgentGit: Tool-level, LLM calls, fine-grained
- WTB: Node-level, execution lifecycle, checkpoint operations

v1.8 Enhancements:
- Tracks ROLLBACK_PERFORMED and EXECUTION_FORKED events
- Records graph factory usage for distributed execution
- Captures checkpoint_id and last_checkpoint_id for batch results

Usage:
    trail = WTBAuditTrail(execution_id="exec-1")
    listener = AuditEventListener(trail)
    
    # Register with event bus
    listener.attach(event_bus)
    
    # Events are automatically recorded
    # Or manually record
    trail.record_event(ExecutionStartedEvent(...))
    
    # Get summary
    print(trail.format_display())
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Any, Optional, Callable, Type
from enum import Enum

from wtb.domain.events import (
    WTBEvent,
    ExecutionStartedEvent, ExecutionCompletedEvent, ExecutionFailedEvent,
    ExecutionPausedEvent, ExecutionResumedEvent, ExecutionCancelledEvent,
    NodeStartedEvent, NodeCompletedEvent, NodeFailedEvent, NodeSkippedEvent,
    CheckpointCreatedEvent, RollbackPerformedEvent, BranchCreatedEvent,
    # Ray events
    RayBatchTestStartedEvent, RayBatchTestCompletedEvent, RayBatchTestFailedEvent,
    RayBatchTestCancelledEvent, RayActorPoolCreatedEvent, RayActorInitializedEvent,
    RayActorFailedEvent, RayVariantExecutionStartedEvent, RayVariantExecutionCompletedEvent,
    RayVariantExecutionFailedEvent, RayVariantFilesTrackedEvent, RayBackpressureAppliedEvent,
)


class WTBAuditEventType(Enum):
    """WTB-specific audit event types."""
    # Execution lifecycle
    EXECUTION_STARTED = "execution_started"
    EXECUTION_PAUSED = "execution_paused"
    EXECUTION_RESUMED = "execution_resumed"
    EXECUTION_COMPLETED = "execution_completed"
    EXECUTION_FAILED = "execution_failed"
    EXECUTION_CANCELLED = "execution_cancelled"
    
    # Node lifecycle
    NODE_STARTED = "node_started"
    NODE_COMPLETED = "node_completed"
    NODE_FAILED = "node_failed"
    NODE_SKIPPED = "node_skipped"
    
    # Checkpoint operations
    CHECKPOINT_CREATED = "checkpoint_created"
    ROLLBACK_PERFORMED = "rollback_performed"
    BRANCH_CREATED = "branch_created"
    EXECUTION_FORKED = "execution_forked"  # v1.8: Fork from checkpoint
    FILES_CLEANED_UP = "files_cleaned_up"  # v1.9: Orphaned files deleted on rollback
    
    # State modifications
    STATE_MODIFIED = "state_modified"
    BREAKPOINT_HIT = "breakpoint_hit"
    
    # Variant testing
    VARIANT_STARTED = "variant_started"
    VARIANT_COMPLETED = "variant_completed"
    
    # Ray batch test events (2026-01-15)
    RAY_BATCH_TEST_STARTED = "ray_batch_test_started"
    RAY_BATCH_TEST_COMPLETED = "ray_batch_test_completed"
    RAY_BATCH_TEST_FAILED = "ray_batch_test_failed"
    RAY_BATCH_TEST_CANCELLED = "ray_batch_test_cancelled"
    RAY_ACTOR_POOL_CREATED = "ray_actor_pool_created"
    RAY_ACTOR_INITIALIZED = "ray_actor_initialized"
    RAY_ACTOR_FAILED = "ray_actor_failed"
    RAY_VARIANT_EXECUTION_STARTED = "ray_variant_execution_started"
    RAY_VARIANT_EXECUTION_COMPLETED = "ray_variant_execution_completed"
    RAY_VARIANT_EXECUTION_FAILED = "ray_variant_execution_failed"
    RAY_FILES_TRACKED = "ray_files_tracked"
    RAY_BACKPRESSURE = "ray_backpressure"


class WTBAuditSeverity(Enum):
    """Severity levels for WTB audit events."""
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    DEBUG = "debug"


@dataclass
class WTBAuditEntry:
    """Single audit entry in WTB audit trail."""
    timestamp: datetime
    event_type: WTBAuditEventType
    severity: WTBAuditSeverity
    message: str
    execution_id: Optional[str] = None
    node_id: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    duration_ms: Optional[float] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "event_type": self.event_type.value,
            "severity": self.severity.value,
            "message": self.message,
            "execution_id": self.execution_id,
            "node_id": self.node_id,
            "details": self.details,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WTBAuditEntry":
        """Create from dictionary."""
        return cls(
            timestamp=datetime.fromisoformat(data["timestamp"]),
            event_type=WTBAuditEventType(data["event_type"]),
            severity=WTBAuditSeverity(data["severity"]),
            message=data["message"],
            execution_id=data.get("execution_id"),
            node_id=data.get("node_id"),
            details=data.get("details", {}),
            error=data.get("error"),
            duration_ms=data.get("duration_ms"),
        )
    
    def format_display(self) -> str:
        """Format for user-friendly display."""
        time_str = self.timestamp.strftime("%H:%M:%S.%f")[:-3]
        
        icons = {
            WTBAuditSeverity.INFO: "[i]",
            WTBAuditSeverity.SUCCESS: "[+]",
            WTBAuditSeverity.WARNING: "[!]",
            WTBAuditSeverity.ERROR: "[X]",
            WTBAuditSeverity.DEBUG: "[D]",
        }
        icon = icons.get(self.severity, "   ")
        
        msg = f"[{time_str}] {icon} {self.message}"
        
        if self.node_id:
            msg += f" (node: {self.node_id})"
        
        if self.duration_ms:
            msg += f" [{self.duration_ms:.1f}ms]"
        
        if self.error:
            msg += f"\n              Error: {self.error}"
        
        return msg


@dataclass
class WTBAuditTrail:
    """
    WTB Audit Trail - Tracks workflow execution at WTB abstraction level.
    
    Memory Management:
    - Implements a flushing mechanism to avoid memory bloat
    - Use flush() to get and clear entries for persistence
    """
    entries: List[WTBAuditEntry] = field(default_factory=list)
    execution_id: Optional[str] = None
    workflow_id: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    
    # Imported AgentGit audits (keyed by checkpoint_id or node_id)
    _agentgit_audits: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    
    # ═══════════════════════════════════════════════════════════════
    # Entry Management
    # ═══════════════════════════════════════════════════════════════
    
    def add_entry(self, entry: WTBAuditEntry) -> None:
        """Add an audit entry."""
        self.entries.append(entry)
    
    def flush(self) -> List[WTBAuditEntry]:
        """
        Clear and return current entries for persistence.
        
        Use this to periodically persist entries to avoid memory bloat
        in long-running tests.
        
        Returns:
            List of entries that were cleared
        """
        current_entries = self.entries[:]
        self.entries.clear()
        return current_entries
    
    def record_event(self, event: WTBEvent) -> None:
        """
        Record a WTB domain event as an audit entry.
        
        Automatically maps event type to audit entry.
        
        Args:
            event: WTB domain event to record
        """
        entry = self._event_to_entry(event)
        if entry:
            self.add_entry(entry)
    
    def _event_to_entry(self, event: WTBEvent) -> Optional[WTBAuditEntry]:
        """Convert WTB event to audit entry."""
        
        # Execution events
        if isinstance(event, ExecutionStartedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.EXECUTION_STARTED,
                severity=WTBAuditSeverity.INFO,
                message=f"Execution started for workflow '{event.workflow_name}'",
                execution_id=event.execution_id,
                details={"workflow_id": event.workflow_id, "breakpoints": event.breakpoints},
            )
        
        if isinstance(event, ExecutionCompletedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.EXECUTION_COMPLETED,
                severity=WTBAuditSeverity.SUCCESS,
                message=f"Execution completed ({event.nodes_executed} nodes, {event.checkpoints_created} checkpoints)",
                execution_id=event.execution_id,
                duration_ms=event.duration_ms,
                details={"final_state_keys": list(event.final_state.keys())},
            )
        
        if isinstance(event, ExecutionFailedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.EXECUTION_FAILED,
                severity=WTBAuditSeverity.ERROR,
                message=f"Execution failed at node '{event.failed_at_node}'",
                execution_id=event.execution_id,
                node_id=event.failed_at_node,
                error=f"{event.error_type}: {event.error_message}",
                duration_ms=event.duration_ms,
            )
        
        if isinstance(event, ExecutionPausedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.EXECUTION_PAUSED,
                severity=WTBAuditSeverity.INFO,
                message=f"Execution paused: {event.reason}",
                execution_id=event.execution_id,
                node_id=event.paused_at_node,
                details={"checkpoint_id": event.checkpoint_id},
                duration_ms=event.elapsed_time_ms,
            )
        
        if isinstance(event, ExecutionResumedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.EXECUTION_RESUMED,
                severity=WTBAuditSeverity.INFO,
                message=f"Execution resumed from node '{event.resume_from_node}'",
                execution_id=event.execution_id,
                node_id=event.resume_from_node,
                details={"state_modified": event.modified_state is not None},
            )
        
        if isinstance(event, ExecutionCancelledEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.EXECUTION_CANCELLED,
                severity=WTBAuditSeverity.WARNING,
                message=f"Execution cancelled: {event.reason}",
                execution_id=event.execution_id,
                node_id=event.cancelled_at_node,
                duration_ms=event.duration_ms,
            )
        
        # Node events
        if isinstance(event, NodeStartedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.NODE_STARTED,
                severity=WTBAuditSeverity.DEBUG,
                message=f"Node '{event.node_name}' started",
                execution_id=event.execution_id,
                node_id=event.node_id,
                details={"node_type": event.node_type, "checkpoint_id": event.entry_checkpoint_id},
            )
        
        if isinstance(event, NodeCompletedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.NODE_COMPLETED,
                severity=WTBAuditSeverity.SUCCESS,
                message=f"Node '{event.node_name}' completed ({event.tool_invocations} tool calls)",
                execution_id=event.execution_id,
                node_id=event.node_id,
                duration_ms=event.duration_ms,
                details={"checkpoint_id": event.exit_checkpoint_id},
            )
        
        if isinstance(event, NodeFailedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.NODE_FAILED,
                severity=WTBAuditSeverity.ERROR,
                message=f"Node '{event.node_name}' failed",
                execution_id=event.execution_id,
                node_id=event.node_id,
                error=f"{event.error_type}: {event.error_message}",
                duration_ms=event.duration_ms,
                details={"recoverable": event.recoverable},
            )
        
        if isinstance(event, NodeSkippedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.NODE_SKIPPED,
                severity=WTBAuditSeverity.INFO,
                message=f"Node '{event.node_name}' skipped: {event.reason}",
                execution_id=event.execution_id,
                node_id=event.node_id,
            )
        
        # Checkpoint events (Updated 2026-01-15 for DDD compliance)
        if isinstance(event, CheckpointCreatedEvent):
            # Handle both old and new event format
            checkpoint_id = getattr(event, 'checkpoint_id', None)
            completed_node = getattr(event, 'completed_node', None)
            step = getattr(event, 'step', 0)
            is_terminal = getattr(event, 'is_terminal', False)
            
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.CHECKPOINT_CREATED,
                severity=WTBAuditSeverity.INFO,
                message=f"Checkpoint '{checkpoint_id}' created (step {step})",
                execution_id=event.execution_id,
                node_id=completed_node,
                details={
                    "step": step,
                    "completed_node": completed_node,
                    "is_terminal": is_terminal,
                },
            )
        
        if isinstance(event, RollbackPerformedEvent):
            # Handle both old and new event format
            to_checkpoint_id = getattr(event, 'to_checkpoint_id', None)
            from_checkpoint_id = getattr(event, 'from_checkpoint_id', None)
            rolled_back_nodes = getattr(event, 'rolled_back_nodes', [])
            
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.ROLLBACK_PERFORMED,
                severity=WTBAuditSeverity.WARNING,
                message=f"Rolled back to checkpoint '{to_checkpoint_id}' ({len(rolled_back_nodes)} nodes reverted)",
                execution_id=event.execution_id,
                details={
                    "from_checkpoint": from_checkpoint_id,
                    "to_checkpoint": to_checkpoint_id,
                    "rolled_back_nodes": rolled_back_nodes,
                },
            )
        
        if isinstance(event, BranchCreatedEvent):
            new_thread_id = getattr(event, 'new_thread_id', None)
            parent_exec = getattr(event, 'parent_execution_id', None)
            
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.BRANCH_CREATED,
                severity=WTBAuditSeverity.INFO,
                message=f"Branch created: thread {new_thread_id} from {parent_exec}",
                execution_id=event.execution_id,
                details={"reason": event.branch_reason},
            )
        
        # ═══════════════════════════════════════════════════════════════
        # Ray Batch Test Events
        # ═══════════════════════════════════════════════════════════════
        
        if isinstance(event, RayBatchTestStartedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.RAY_BATCH_TEST_STARTED,
                severity=WTBAuditSeverity.INFO,
                message=f"Ray batch test '{event.batch_test_id}' started ({event.variant_count} variants, {event.parallel_workers} workers)",
                execution_id=event.batch_test_id,
                details={
                    "workflow_id": event.workflow_id,
                    "workflow_name": event.workflow_name,
                    "file_tracking_enabled": event.file_tracking_enabled,
                    "max_pending_tasks": event.max_pending_tasks,
                },
            )
        
        if isinstance(event, RayBatchTestCompletedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.RAY_BATCH_TEST_COMPLETED,
                severity=WTBAuditSeverity.SUCCESS,
                message=f"Ray batch test completed: {event.variants_succeeded} succeeded, {event.variants_failed} failed",
                execution_id=event.batch_test_id,
                duration_ms=float(event.duration_ms),
                details={
                    "best_combination": event.best_combination_name,
                    "best_score": event.best_overall_score,
                    "files_tracked": event.total_files_tracked,
                    "has_comparison_matrix": event.has_comparison_matrix,
                },
            )
        
        if isinstance(event, RayBatchTestFailedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.RAY_BATCH_TEST_FAILED,
                severity=WTBAuditSeverity.ERROR,
                message=f"Ray batch test failed: {event.error_message}",
                execution_id=event.batch_test_id,
                duration_ms=float(event.duration_ms),
                error=f"{event.error_type}: {event.error_message}",
                details={
                    "failed_at_variant": event.failed_at_variant,
                    "variants_succeeded": event.variants_succeeded,
                    "variants_failed": event.variants_failed,
                    "variants_pending": event.variants_pending,
                },
            )
        
        if isinstance(event, RayBatchTestCancelledEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.RAY_BATCH_TEST_CANCELLED,
                severity=WTBAuditSeverity.WARNING,
                message=f"Ray batch test cancelled: {event.reason}",
                execution_id=event.batch_test_id,
                duration_ms=float(event.duration_ms),
                details={
                    "cancelled_by": event.cancelled_by,
                    "variants_completed": event.variants_completed,
                    "variants_cancelled": event.variants_cancelled,
                },
            )
        
        if isinstance(event, RayActorPoolCreatedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.RAY_ACTOR_POOL_CREATED,
                severity=WTBAuditSeverity.INFO,
                message=f"Ray actor pool created: {event.num_actors} actors",
                execution_id=event.batch_test_id,
                details={
                    "cpus_per_actor": event.cpus_per_actor,
                    "memory_per_actor_gb": event.memory_per_actor_gb,
                    "actor_ids": event.actor_ids,
                },
            )
        
        if isinstance(event, RayActorInitializedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.RAY_ACTOR_INITIALIZED,
                severity=WTBAuditSeverity.DEBUG,
                message=f"Ray actor '{event.actor_id}' initialized",
                execution_id=event.batch_test_id,
                duration_ms=float(event.initialization_time_ms),
                details={
                    "state_adapter_type": event.state_adapter_type,
                    "file_tracking_enabled": event.file_tracking_enabled,
                },
            )
        
        if isinstance(event, RayActorFailedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.RAY_ACTOR_FAILED,
                severity=WTBAuditSeverity.ERROR,
                message=f"Ray actor '{event.actor_id}' failed: {event.error_message}",
                execution_id=event.batch_test_id,
                error=f"{event.error_type}: {event.error_message}",
                details={
                    "executing_variant": event.executing_variant,
                    "executions_completed": event.executions_completed,
                },
            )
        
        if isinstance(event, RayVariantExecutionStartedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.RAY_VARIANT_EXECUTION_STARTED,
                severity=WTBAuditSeverity.DEBUG,
                message=f"Variant '{event.combination_name}' started on actor '{event.actor_id}'",
                execution_id=event.batch_test_id,
                node_id=event.execution_id,
                details={
                    "variants": event.variants,
                    "queue_position": event.queue_position,
                    "total_in_queue": event.total_in_queue,
                },
            )
        
        if isinstance(event, RayVariantExecutionCompletedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.RAY_VARIANT_EXECUTION_COMPLETED,
                severity=WTBAuditSeverity.SUCCESS,
                message=f"Variant '{event.combination_name}' completed (score: {event.overall_score:.2f})",
                execution_id=event.batch_test_id,
                node_id=event.execution_id,
                duration_ms=float(event.duration_ms),
                details={
                    "checkpoint_count": event.checkpoint_count,
                    "node_count": event.node_count,
                    "metrics": event.metrics,
                    "files_tracked": event.files_tracked,
                    "file_commit_id": event.file_commit_id,
                },
            )
        
        if isinstance(event, RayVariantExecutionFailedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.RAY_VARIANT_EXECUTION_FAILED,
                severity=WTBAuditSeverity.ERROR,
                message=f"Variant '{event.combination_name}' failed: {event.error_message}",
                execution_id=event.batch_test_id,
                node_id=event.execution_id,
                duration_ms=float(event.duration_ms),
                error=f"{event.error_type}: {event.error_message}",
                details={
                    "failed_at_node": event.failed_at_node,
                    "nodes_completed": event.nodes_completed,
                    "checkpoints_created": event.checkpoints_created,
                },
            )
        
        if isinstance(event, RayVariantFilesTrackedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.RAY_FILES_TRACKED,
                severity=WTBAuditSeverity.INFO,
                message=f"Tracked {event.files_tracked} files for variant '{event.combination_name}'",
                execution_id=event.batch_test_id,
                node_id=event.execution_id,
                details={
                    "file_commit_id": event.file_commit_id,
                    "checkpoint_id": event.checkpoint_id,
                    "file_paths": event.file_paths[:5],  # Limit for readability
                },
            )
        
        if isinstance(event, RayBackpressureAppliedEvent):
            return WTBAuditEntry(
                timestamp=event.timestamp,
                event_type=WTBAuditEventType.RAY_BACKPRESSURE,
                severity=WTBAuditSeverity.WARNING,
                message=f"Backpressure applied: {event.pending_tasks}/{event.max_pending_tasks} pending tasks",
                execution_id=event.batch_test_id,
                duration_ms=float(event.wait_time_ms),
                details={
                    "pending_tasks": event.pending_tasks,
                    "max_pending_tasks": event.max_pending_tasks,
                },
            )
        
        return None
    
    # ═══════════════════════════════════════════════════════════════
    # AgentGit Audit Integration
    # ═══════════════════════════════════════════════════════════════
    
    def import_agentgit_audit(
        self, 
        audit_dict: Dict[str, Any], 
        key: str
    ) -> None:
        """
        Import an AgentGit AuditTrail snapshot for detailed debugging.
        
        Args:
            audit_dict: AgentGit AuditTrail.to_dict() output
            key: Identifier (e.g., checkpoint_id or node_id) for retrieval
        """
        self._agentgit_audits[key] = audit_dict
    
    def get_agentgit_audit(self, key: str) -> Optional[Dict[str, Any]]:
        """Get imported AgentGit audit by key."""
        return self._agentgit_audits.get(key)
    
    # ═══════════════════════════════════════════════════════════════
    # Query Methods
    # ═══════════════════════════════════════════════════════════════
    
    def get_errors(self) -> List[WTBAuditEntry]:
        """Get all error entries."""
        return [e for e in self.entries if e.severity == WTBAuditSeverity.ERROR]
    
    def get_by_node(self, node_id: str) -> List[WTBAuditEntry]:
        """Get entries for a specific node."""
        return [e for e in self.entries if e.node_id == node_id]
    
    def get_by_type(self, event_type: WTBAuditEventType) -> List[WTBAuditEntry]:
        """Get entries by event type."""
        return [e for e in self.entries if e.event_type == event_type]
    
    def get_summary(self) -> Dict[str, Any]:
        """Get summary statistics."""
        return {
            "total_entries": len(self.entries),
            "execution_id": self.execution_id,
            "workflow_id": self.workflow_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_ms": (
                (self.ended_at - self.started_at).total_seconds() * 1000
                if self.started_at and self.ended_at else None
            ),
            "nodes_executed": len([
                e for e in self.entries 
                if e.event_type == WTBAuditEventType.NODE_COMPLETED
            ]),
            "nodes_failed": len([
                e for e in self.entries 
                if e.event_type == WTBAuditEventType.NODE_FAILED
            ]),
            "checkpoints_created": len([
                e for e in self.entries 
                if e.event_type == WTBAuditEventType.CHECKPOINT_CREATED
            ]),
            "rollbacks": len([
                e for e in self.entries 
                if e.event_type == WTBAuditEventType.ROLLBACK_PERFORMED
            ]),
            "errors": len(self.get_errors()),
            "agentgit_audits_imported": len(self._agentgit_audits),
        }
    
    # ═══════════════════════════════════════════════════════════════
    # Serialization
    # ═══════════════════════════════════════════════════════════════
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "execution_id": self.execution_id,
            "workflow_id": self.workflow_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "entries": [e.to_dict() for e in self.entries],
            "agentgit_audits": self._agentgit_audits,
            "summary": self.get_summary(),
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WTBAuditTrail":
        """Create from dictionary."""
        trail = cls(
            execution_id=data.get("execution_id"),
            workflow_id=data.get("workflow_id"),
            started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None,
            ended_at=datetime.fromisoformat(data["ended_at"]) if data.get("ended_at") else None,
        )
        
        for entry_data in data.get("entries", []):
            trail.entries.append(WTBAuditEntry.from_dict(entry_data))
        
        trail._agentgit_audits = data.get("agentgit_audits", {})
        
        return trail
    
    # ═══════════════════════════════════════════════════════════════
    # Display
    # ═══════════════════════════════════════════════════════════════
    
    def format_display(
        self, 
        show_all: bool = False, 
        include_debug: bool = False
    ) -> str:
        """
        Format for user-friendly display.
        
        Args:
            show_all: Show all entries (vs last 30)
            include_debug: Include DEBUG severity entries
        """
        if not self.entries:
            return "No audit entries recorded."
        
        output = "=" * 60 + "\n"
        output += "                   WTB AUDIT TRAIL\n"
        output += "=" * 60 + "\n\n"
        
        # Summary
        summary = self.get_summary()
        output += f"Execution: {summary['execution_id'] or 'N/A'}\n"
        output += f"Workflow:  {summary['workflow_id'] or 'N/A'}\n"
        if summary['duration_ms']:
            output += f"Duration:  {summary['duration_ms']:.0f}ms\n"
        output += "\n"
        
        output += f"Nodes: [+] {summary['nodes_executed']} completed"
        if summary['nodes_failed']:
            output += f" | [X] {summary['nodes_failed']} failed"
        output += "\n"
        
        output += f"Checkpoints: {summary['checkpoints_created']}"
        if summary['rollbacks']:
            output += f" | {summary['rollbacks']} rollbacks"
        output += "\n"
        
        if summary['errors']:
            output += f"[!] Errors: {summary['errors']}\n"
        
        output += "\n" + "-" * 60 + "\n"
        output += "                      TIMELINE\n"
        output += "-" * 60 + "\n\n"
        
        # Filter entries
        entries = self.entries
        if not include_debug:
            entries = [e for e in entries if e.severity != WTBAuditSeverity.DEBUG]
        
        if not show_all and len(entries) > 30:
            output += f"(Showing last 30 of {len(entries)} entries)\n\n"
            entries = entries[-30:]
        
        for entry in entries:
            output += entry.format_display() + "\n"
        
        output += "\n" + "=" * 60
        
        return output


class AuditEventListener:
    """
    Auto-records WTB events to an audit trail.
    
    Subscribes to a WTBEventBus and records all WTB events
    to a WTBAuditTrail instance.
    
    Usage:
        trail = WTBAuditTrail(execution_id="exec-1")
        listener = AuditEventListener(trail)
        listener.attach(event_bus)
        
        # Events are now automatically recorded
        event_bus.publish(ExecutionStartedEvent(...))
        
        # Detach when done
        listener.detach()
    """
    
    def __init__(
        self,
        audit_trail: WTBAuditTrail,
        event_types: Optional[List[Type[WTBEvent]]] = None,
    ):
        """
        Initialize listener.
        
        Args:
            audit_trail: The audit trail to record events to
            event_types: Specific event types to listen for (all if None)
        """
        self.audit_trail = audit_trail
        self._event_bus: Optional["WTBEventBus"] = None  # type: ignore
        self._subscriptions: List[tuple] = []
        
        # Default to all recordable event types
        self._event_types = event_types or [
            ExecutionStartedEvent,
            ExecutionCompletedEvent,
            ExecutionFailedEvent,
            ExecutionPausedEvent,
            ExecutionResumedEvent,
            ExecutionCancelledEvent,
            NodeStartedEvent,
            NodeCompletedEvent,
            NodeFailedEvent,
            NodeSkippedEvent,
            CheckpointCreatedEvent,
            RollbackPerformedEvent,
            BranchCreatedEvent,
            # Ray events
            RayBatchTestStartedEvent,
            RayBatchTestCompletedEvent,
            RayBatchTestFailedEvent,
            RayBatchTestCancelledEvent,
            RayActorPoolCreatedEvent,
            RayActorInitializedEvent,
            RayActorFailedEvent,
            RayVariantExecutionStartedEvent,
            RayVariantExecutionCompletedEvent,
            RayVariantExecutionFailedEvent,
            RayVariantFilesTrackedEvent,
            RayBackpressureAppliedEvent,
        ]
    
    def attach(self, event_bus: "WTBEventBus") -> None:  # type: ignore
        """
        Attach to an event bus.
        
        Args:
            event_bus: WTBEventBus to subscribe to
        """
        if self._event_bus is not None:
            self.detach()
        
        self._event_bus = event_bus
        
        for event_type in self._event_types:
            handler = self._make_handler()
            event_bus.subscribe(event_type, handler)
            self._subscriptions.append((event_type, handler))
    
    def detach(self) -> None:
        """Detach from the event bus."""
        if self._event_bus is None:
            return
        
        for event_type, handler in self._subscriptions:
            self._event_bus.unsubscribe(event_type, handler)
        
        self._subscriptions.clear()
        self._event_bus = None
    
    def _make_handler(self) -> Callable[[WTBEvent], None]:
        """Create a handler that records to the audit trail."""
        def handler(event: WTBEvent) -> None:
            self.audit_trail.record_event(event)
        return handler

