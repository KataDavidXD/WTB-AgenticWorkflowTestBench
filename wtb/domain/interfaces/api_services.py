"""
API Service Interfaces - Abstract contracts for API-level operations.

Created: 2026-01-28
Status: Active
Reference: SOLID principles, DIP (Dependency Inversion Principle)

Design Principles:
- ISP: Separate interfaces for different operation domains
- DIP: API layer depends on abstractions, not concrete implementations
- SRP: Each interface has a single responsibility

Architecture:
    REST/gRPC Endpoints
           │
           ▼
    IExecutionAPIService (abstraction)
           │
           ▼
    ExecutionAPIService (concrete)
           │
           ├──► IUnitOfWork (transaction boundary)
           ├──► IExecutionController (domain operations)
           └──► IEventBus (async events)

ACID Compliance:
- All operations wrapped in Unit of Work transactions
- Automatic rollback on failure
- Audit logging for all state changes
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# ═══════════════════════════════════════════════════════════════════════════════
# Result DTOs for API Operations
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class ExecutionDTO:
    """Data Transfer Object for Execution."""
    id: str
    workflow_id: str
    status: str
    state: dict[str, Any]
    breakpoints: list[str] = field(default_factory=list)
    current_node_id: str | None = None
    error: str | None = None
    error_node_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    checkpoint_count: int = 0
    nodes_executed: int = 0
    thread_id: str | None = None


@dataclass
class ControlResultDTO:
    """Result of a control operation (pause, resume, stop)."""
    success: bool
    status: str
    checkpoint_id: str | None = None
    message: str | None = None
    error: str | None = None


@dataclass
class RollbackResultDTO:
    """Result of a rollback operation."""
    success: bool
    to_checkpoint: str
    new_session_id: str | None = None
    tools_reversed: int = 0
    files_restored: int = 0
    restored_state: dict[str, Any] | None = None
    error: str | None = None  # Error message if success=False


@dataclass
class CheckpointDTO:
    """Data Transfer Object for Checkpoint."""
    id: str
    execution_id: str
    node_id: str | None = None
    trigger_type: str = "auto"
    created_at: datetime | None = None
    state_snapshot: dict[str, Any] | None = None
    has_file_commit: bool = False
    file_commit_id: str | None = None


@dataclass
class PaginatedResultDTO:
    """Paginated result container."""
    items: list[Any]
    total: int
    limit: int
    offset: int
    has_more: bool


@dataclass
class AuditEventDTO:
    """Data Transfer Object for Audit Event."""
    id: str
    timestamp: datetime
    event_type: str
    severity: str
    message: str
    execution_id: str | None = None
    node_id: str | None = None
    details: dict[str, Any] | None = None
    error: str | None = None
    duration_ms: float | None = None


@dataclass
class AuditSummaryDTO:
    """Audit summary statistics."""
    total_events: int
    execution_id: str | None = None
    time_range: str = "1h"
    events_by_type: dict[str, int] = field(default_factory=dict)
    events_by_severity: dict[str, int] = field(default_factory=dict)
    error_rate: float = 0.0
    checkpoint_count: int = 0
    rollback_count: int = 0
    nodes_executed: int = 0
    nodes_failed: int = 0
    avg_node_duration_ms: float | None = None


@dataclass
class BatchTestDTO:
    """Data Transfer Object for Batch Test."""
    id: str
    workflow_id: str
    status: str
    variant_count: int
    variants_completed: int = 0
    variants_failed: int = 0
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: float | None = None
    comparison_matrix: dict[str, Any] | None = None
    best_variant: str | None = None


@dataclass
class BatchTestProgressDTO:
    """Progress update for batch test."""
    batch_test_id: str
    total: int
    completed: int
    failed: int
    current: str | None = None
    eta_seconds: float | None = None


# ═══════════════════════════════════════════════════════════════════════════════
# Execution API Service Interface
# ═══════════════════════════════════════════════════════════════════════════════


class IExecutionAPIService(ABC):
    """
    Interface for execution API operations.
    
    Provides ACID-compliant operations for managing workflow executions
    through the API layer. All operations are wrapped in transactions.
    
    SOLID Compliance:
    - SRP: Only handles execution-related API operations
    - ISP: Separate from audit and batch test interfaces
    - DIP: API layer depends on this abstraction
    """
    
    @abstractmethod
    async def list_executions(
        self,
        workflow_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> PaginatedResultDTO:
        """
        List executions with optional filtering.
        
        Args:
            workflow_id: Filter by workflow ID
            status: Filter by status
            limit: Maximum items to return
            offset: Pagination offset
            
        Returns:
            Paginated list of ExecutionDTO
        """
        pass
    
    @abstractmethod
    async def get_execution(self, execution_id: str) -> ExecutionDTO | None:
        """
        Get execution by ID.
        
        Args:
            execution_id: Execution ID
            
        Returns:
            ExecutionDTO if found, None otherwise
        """
        pass
    
    @abstractmethod
    async def pause_execution(
        self,
        execution_id: str,
        reason: str | None = None,
        at_node: str | None = None,
    ) -> ControlResultDTO:
        """
        Pause a running execution.
        
        ACID: Creates checkpoint atomically.
        
        Args:
            execution_id: Execution to pause
            reason: Pause reason
            at_node: Optional node to pause at
            
        Returns:
            Control result with checkpoint ID
        """
        pass
    
    @abstractmethod
    async def resume_execution(
        self,
        execution_id: str,
        modified_state: dict[str, Any] | None = None,
        from_node: str | None = None,
    ) -> ControlResultDTO:
        """
        Resume a paused execution.
        
        ACID: State modifications applied atomically.
        
        Args:
            execution_id: Execution to resume
            modified_state: Optional state modifications (HITL)
            from_node: Optional node to resume from
            
        Returns:
            Control result
        """
        pass
    
    @abstractmethod
    async def stop_execution(
        self,
        execution_id: str,
        reason: str | None = None,
    ) -> ControlResultDTO:
        """
        Stop and cancel an execution.
        
        ACID: Final checkpoint created before stop.
        
        Args:
            execution_id: Execution to stop
            reason: Stop reason
            
        Returns:
            Control result
        """
        pass
    
    @abstractmethod
    async def rollback_execution(
        self,
        execution_id: str,
        checkpoint_id: str,
        create_branch: bool = False,
    ) -> RollbackResultDTO:
        """
        Rollback execution to a checkpoint.
        
        ACID: Rollback is atomic - either fully applied or not at all.
        
        Args:
            execution_id: Execution to rollback
            checkpoint_id: Target checkpoint (UUID string)
            create_branch: Create new branch instead of in-place rollback
            
        Returns:
            Rollback result with details
        """
        pass
    
    @abstractmethod
    async def get_execution_state(
        self,
        execution_id: str,
        keys: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Get current execution state.
        
        Args:
            execution_id: Execution ID
            keys: Optional specific keys to retrieve
            
        Returns:
            State values
        """
        pass
    
    @abstractmethod
    async def modify_execution_state(
        self,
        execution_id: str,
        changes: dict[str, Any],
        reason: str | None = None,
    ) -> ControlResultDTO:
        """
        Modify execution state (human-in-the-loop).
        
        ACID: Creates checkpoint before modification.
        
        Args:
            execution_id: Execution ID
            changes: State changes to apply
            reason: Modification reason
            
        Returns:
            Control result
        """
        pass
    
    @abstractmethod
    async def list_checkpoints(
        self,
        execution_id: str,
        limit: int = 100,
    ) -> list[CheckpointDTO]:
        """
        List checkpoints for an execution.
        
        Args:
            execution_id: Execution ID
            limit: Maximum checkpoints to return
            
        Returns:
            List of checkpoints
        """
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# Audit API Service Interface
# ═══════════════════════════════════════════════════════════════════════════════


class IAuditAPIService(ABC):
    """
    Interface for audit API operations.
    
    Provides read-only access to audit events and analytics.
    
    SOLID Compliance:
    - SRP: Only handles audit-related operations
    - ISP: Separate from execution and batch test
    """
    
    @abstractmethod
    async def query_events(
        self,
        execution_id: str | None = None,
        event_types: list[str] | None = None,
        severities: list[str] | None = None,
        node_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> PaginatedResultDTO:
        """
        Query audit events with filtering.
        
        Args:
            execution_id: Filter by execution
            event_types: Filter by event types
            severities: Filter by severities
            node_id: Filter by node
            since: Events after this time
            until: Events before this time
            limit: Maximum items
            offset: Pagination offset
            
        Returns:
            Paginated list of AuditEventDTO
        """
        pass
    
    @abstractmethod
    async def get_summary(
        self,
        execution_id: str | None = None,
        time_range: str = "1h",
    ) -> AuditSummaryDTO:
        """
        Get audit summary statistics.
        
        Args:
            execution_id: Filter by execution
            time_range: Time range (1h, 24h, 7d)
            
        Returns:
            Summary statistics
        """
        pass
    
    @abstractmethod
    async def get_timeline(
        self,
        execution_id: str,
        include_debug: bool = False,
    ) -> list[AuditEventDTO]:
        """
        Get execution timeline for visualization.
        
        Args:
            execution_id: Execution ID
            include_debug: Include debug-level events
            
        Returns:
            Timeline of audit events
        """
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# Batch Test API Service Interface
# ═══════════════════════════════════════════════════════════════════════════════


class IBatchTestAPIService(ABC):
    """
    Interface for batch test API operations.
    
    Provides ACID-compliant batch test management.
    
    SOLID Compliance:
    - SRP: Only handles batch test operations
    - ISP: Separate from execution and audit
    """
    
    @abstractmethod
    async def create_batch_test(
        self,
        workflow_id: str,
        variants: list[dict[str, Any]],
        initial_state: dict[str, Any] | None = None,
        parallelism: int | None = None,
        use_ray: bool = True,
    ) -> BatchTestDTO:
        """
        Create and start a batch test.
        
        ACID: Batch test record created atomically.
        
        Args:
            workflow_id: Workflow to test
            variants: Variant configurations
            initial_state: Initial state for all variants
            parallelism: Number of parallel workers
            use_ray: Use Ray for execution
            
        Returns:
            Created batch test DTO
        """
        pass
    
    @abstractmethod
    async def get_batch_test(self, batch_test_id: str) -> BatchTestDTO | None:
        """
        Get batch test by ID.
        
        Args:
            batch_test_id: Batch test ID
            
        Returns:
            BatchTestDTO if found
        """
        pass
    
    @abstractmethod
    async def list_batch_tests(
        self,
        workflow_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> PaginatedResultDTO:
        """
        List batch tests with filtering.
        
        Args:
            workflow_id: Filter by workflow
            status: Filter by status
            limit: Maximum items
            offset: Pagination offset
            
        Returns:
            Paginated list of BatchTestDTO
        """
        pass
    
    @abstractmethod
    async def stream_progress(
        self,
        batch_test_id: str,
    ) -> AsyncIterator[BatchTestProgressDTO]:
        """
        Stream batch test progress updates.
        
        Args:
            batch_test_id: Batch test ID
            
        Yields:
            Progress updates
        """
        pass
    
    @abstractmethod
    async def cancel_batch_test(
        self,
        batch_test_id: str,
        reason: str | None = None,
    ) -> ControlResultDTO:
        """
        Cancel a running batch test.
        
        ACID: Cancellation recorded atomically.
        
        Args:
            batch_test_id: Batch test to cancel
            reason: Cancellation reason
            
        Returns:
            Control result
        """
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# Workflow API Service Interface
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class WorkflowDTO:
    """Data Transfer Object for Workflow."""
    id: str
    name: str
    description: str | None = None
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    entry_point: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int = 1


class IWorkflowAPIService(ABC):
    """
    Interface for workflow API operations.
    
    Provides CRUD operations for workflow definitions.
    """
    
    @abstractmethod
    async def create_workflow(
        self,
        name: str,
        nodes: list[dict[str, Any]],
        entry_point: str,
        description: str | None = None,
        edges: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> WorkflowDTO:
        """Create a new workflow definition."""
        pass
    
    @abstractmethod
    async def get_workflow(self, workflow_id: str) -> WorkflowDTO | None:
        """Get workflow by ID."""
        pass
    
    @abstractmethod
    async def list_workflows(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> PaginatedResultDTO:
        """List workflows with pagination."""
        pass
    
    @abstractmethod
    async def update_workflow(
        self,
        workflow_id: str,
        **updates,
    ) -> WorkflowDTO:
        """Update workflow definition."""
        pass
    
    @abstractmethod
    async def delete_workflow(self, workflow_id: str) -> bool:
        """Delete workflow definition."""
        pass


__all__ = [
    # DTOs
    "ExecutionDTO",
    "ControlResultDTO",
    "RollbackResultDTO",
    "CheckpointDTO",
    "PaginatedResultDTO",
    "AuditEventDTO",
    "AuditSummaryDTO",
    "BatchTestDTO",
    "BatchTestProgressDTO",
    "WorkflowDTO",
    # Interfaces
    "IExecutionAPIService",
    "IAuditAPIService",
    "IBatchTestAPIService",
    "IWorkflowAPIService",
]
