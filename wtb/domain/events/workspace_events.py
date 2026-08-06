"""
Workspace Domain Events - Events for workspace lifecycle operations.

Published during workspace creation, activation, deactivation, cleanup,
and file restore operations. These events integrate with the WTB audit
trail for complete traceability.

Design Principles:
- Event Sourcing: Each event captures complete state change
- Audit Trail: All events logged for debugging and compliance
- ACID: Events written to outbox for transaction consistency

Event Categories:
- Workspace Lifecycle: Created, Activated, Deactivated, CleanedUp
- File Operations: FileSnapshot, FileRestore
- Fork Operations: ForkRequested, ForkCompleted
- Venv Operations: VenvCreated, VenvReused, VenvInvalidated
- Actor Lifecycle: ActorCreated, ActorReset, ActorKilled

Related Documents:
- docs/issues/WORKSPACE_ISOLATION_DESIGN.md (Section 7)
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from wtb.domain.events.execution_events import WTBEvent

# ═══════════════════════════════════════════════════════════════════════════════
# Workspace Lifecycle Events
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class WorkspaceCreatedEvent(WTBEvent):
    """
    Published when a new workspace is created for variant execution.
    
    Contains complete workspace configuration and source snapshot details.
    """
    workspace_id: str = ""
    batch_test_id: str = ""
    execution_id: str = ""
    variant_name: str = ""
    workspace_path: str = ""
    source_snapshot_commit_id: str | None = None
    file_count: int = 0
    total_size_bytes: int = 0
    strategy: str = "workspace"          # workspace | snapshot | clone | none
    parent_workspace_id: str | None = None   # For branch forks
    
    @property
    def total_size_mb(self) -> float:
        """Total size in megabytes."""
        return self.total_size_bytes / (1024 * 1024)


@dataclass
class WorkspaceActivatedEvent(WTBEvent):
    """
    Published when a workspace is activated (CWD changed).
    
    Marks the start of isolated execution for a variant.
    """
    workspace_id: str = ""
    execution_id: str = ""
    previous_cwd: str = ""
    new_cwd: str = ""


@dataclass
class WorkspaceDeactivatedEvent(WTBEvent):
    """
    Published when a workspace is deactivated (CWD restored).
    
    Marks the end of isolated execution, includes output statistics.
    """
    workspace_id: str = ""
    execution_id: str = ""
    files_written: int = 0
    output_size_bytes: int = 0
    restored_cwd: str = ""


@dataclass
class WorkspaceCleanedUpEvent(WTBEvent):
    """
    Published when a workspace is cleaned up (deleted).
    
    Includes reason and cleanup statistics.
    """
    workspace_id: str = ""
    reason: str = ""               # completed | failed_preserved | batch_cleanup | orphan_cleanup
    files_removed: int = 0
    space_freed_bytes: int = 0
    preserved: bool = False        # True if kept for debugging


# ═══════════════════════════════════════════════════════════════════════════════
# File Operations Events
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class FileSnapshotCreatedEvent(WTBEvent):
    """
    Published when source files are snapshotted before fork.
    
    Part of pre-execution snapshot to capture source state.
    """
    workspace_id: str = ""
    batch_test_id: str = ""
    commit_id: str = ""
    file_count: int = 0
    total_size_bytes: int = 0
    source_paths: list[str] = field(default_factory=list)


@dataclass
class FileRestoreEvent(WTBEvent):
    """
    Published when files are restored from a checkpoint.
    
    Used during rollback and fork operations.
    """
    execution_id: str = ""
    checkpoint_id: str = ""
    file_commit_id: str = ""
    workspace_id: str = ""
    files_restored: int = 0
    total_size_bytes: int = 0
    restore_target: str = ""       # Workspace path where files restored
    success: bool = True
    error_message: str | None = None


@dataclass
class FileRestoreFailedEvent(WTBEvent):
    """
    Published when file restore operation fails.
    """
    execution_id: str = ""
    checkpoint_id: str = ""
    workspace_id: str = ""
    error_message: str = ""
    error_type: str = ""


# ═══════════════════════════════════════════════════════════════════════════════
# Fork Events
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class ForkRequestedEvent(WTBEvent):
    """
    Published when a fork operation is requested.
    
    Fork types:
    - variant: A/B testing parallel execution
    - rollback_branch: Create branch from rollback point
    - manual: User-initiated branch
    """
    parent_execution_id: str = ""
    fork_checkpoint_id: int | None = None
    new_execution_id: str = ""
    fork_type: str = "variant"     # variant | rollback_branch | manual
    workspace_id: str = ""


@dataclass
class ForkCompletedEvent(WTBEvent):
    """
    Published when a fork operation completes successfully.
    
    Includes all identifiers for the new execution branch.
    """
    new_execution_id: str = ""
    workspace_id: str = ""
    thread_id: str = ""           # LangGraph thread ID
    parent_execution_id: str = ""
    fork_checkpoint_id: int | None = None
    ready_to_execute: bool = True
    files_copied: int = 0
    total_size_bytes: int = 0


# ═══════════════════════════════════════════════════════════════════════════════
# Venv Events (Phase V1)
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class VenvCreatedEvent(WTBEvent):
    """
    Published when a new venv is created for a workspace.
    
    Tracks venv specification for invalidation detection.
    """
    workspace_id: str = ""
    execution_id: str = ""
    venv_path: str = ""
    python_version: str = ""
    packages: list[str] = field(default_factory=list)
    venv_spec_hash: str = ""
    creation_time_ms: float = 0.0


@dataclass
class VenvReusedEvent(WTBEvent):
    """
    Published when an existing venv is reused (hash match).
    """
    workspace_id: str = ""
    venv_path: str = ""
    venv_spec_hash: str = ""
    original_creation_time: datetime | None = None


@dataclass
class VenvInvalidatedEvent(WTBEvent):
    """
    Published when a venv is invalidated due to spec change.
    """
    node_id: str = ""
    old_spec_hash: str = ""
    new_spec_hash: str = ""
    reason: str = ""               # dependency_change | python_version_change
    affected_workspaces: list[str] = field(default_factory=list)


@dataclass
class VenvMismatchWarningEvent(WTBEvent):
    """
    Published when rollback detects venv hash mismatch.
    
    Warning event - doesn't prevent rollback but logs incompatibility.
    """
    checkpoint_id: str = ""
    expected_venv_hash: str = ""
    current_venv_hash: str = ""
    expected_packages: list[str] = field(default_factory=list)
    current_packages: list[str] = field(default_factory=list)


@dataclass
class VenvRestoredEvent(WTBEvent):
    """
    Published when venv is restored during rollback.
    """
    workspace_id: str = ""
    checkpoint_id: str = ""
    venv_path: str = ""
    restored_from: str = ""        # lock_snapshot | spec_recreation
    restore_time_ms: float = 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Ray Actor Lifecycle Events (Phase R1)
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class ActorCreatedEvent(WTBEvent):
    """
    Published when a Ray actor is created for execution.
    """
    actor_id: str = ""
    execution_id: str = ""
    workspace_id: str = ""
    for_fork: bool = False         # Created for fork operation
    for_resume: bool = False       # Created for cold pause resume
    resources: dict[str, Any] = field(default_factory=dict)  # num_cpus, num_gpus, memory


@dataclass
class ActorResetEvent(WTBEvent):
    """
    Published when an actor is reset for new variant (pool reuse).
    """
    actor_id: str = ""
    reason: str = ""               # variant_switch | rollback
    resources_cleared: list[str] = field(default_factory=list)


@dataclass
class ActorKilledEvent(WTBEvent):
    """
    Published when a Ray actor is killed.
    """
    actor_id: str = ""
    reason: str = ""               # cold_pause | rollback_recreate | shutdown
    resources_released: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════════
# Resource Events
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class ResourcesReleasedEvent(WTBEvent):
    """
    Published when resources are released (WARM pause).
    """
    actor_id: str = ""
    reason: str = ""               # warm_pause | memory_pressure
    gpu_memory_freed_mb: float = 0.0
    resources_list: list[str] = field(default_factory=list)


@dataclass
class ResourcesReacquiredEvent(WTBEvent):
    """
    Published when resources are reacquired after WARM resume.
    """
    actor_id: str = ""
    gpu_memory_allocated_mb: float = 0.0
    reacquisition_time_ms: float = 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Orphan Cleanup Events
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class OrphanWorkspaceDetectedEvent(WTBEvent):
    """
    Published when an orphan workspace is detected.
    """
    workspace_path: str = ""
    original_pid: int = 0
    age_hours: float = 0.0
    action: str = ""               # cleaned | preserved


@dataclass
class OrphanCleanupCompletedEvent(WTBEvent):
    """
    Published when orphan cleanup batch completes.
    """
    orphans_found: int = 0
    orphans_cleaned: int = 0
    space_freed_mb: float = 0.0
    errors: list[str] = field(default_factory=list)
