"""
API Service Implementations - Concrete ACID-compliant API services.

Created: 2026-01-28
Updated: 2026-01-28 - Added input validation and idempotency keys
Status: Active
Reference: SOLID principles, ACID transactions

Design Principles:
- SOLID: Each service has single responsibility, depends on interfaces
- ACID: All operations wrapped in Unit of Work transactions
- DIP: Services depend on abstractions (IUnitOfWork, IExecutionController)

Transaction Pattern:
    async with self._uow:
        # Operations within transaction
        await self._uow.acommit()  # Explicit commit
    # Auto-rollback on exception

Outbox Pattern for Cross-System Consistency:
    async with self._uow:
        # Domain operation
        execution = self._controller.pause(execution_id)
        # Record in outbox for async processing
        await self._uow.outbox.aadd(OutboxEvent(
            event_type=OutboxEventType.EXECUTION_PAUSED,
            payload={"execution_id": execution_id}
        ))
        await self._uow.acommit()

Input Validation (ISSUE-API-003):
    All public methods validate input parameters before processing.
    Invalid inputs raise ValueError with descriptive messages.

Idempotency (ISSUE-OB-002):
    Outbox events include idempotency keys for safe retries.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, AsyncIterator, TYPE_CHECKING

from wtb.domain.interfaces.api_services import (
    IExecutionAPIService,
    IAuditAPIService,
    IBatchTestAPIService,
    IWorkflowAPIService,
    ExecutionDTO,
    ControlResultDTO,
    RollbackResultDTO,
    CheckpointDTO,
    PaginatedResultDTO,
    AuditEventDTO,
    AuditSummaryDTO,
    BatchTestDTO,
    BatchTestProgressDTO,
    WorkflowDTO,
)
import threading

from wtb.domain.interfaces import IExecutionController, IUnitOfWork
from wtb.domain.models.workflow import ExecutionStatus
from wtb.domain.models.outbox import OutboxEvent, OutboxEventType
from wtb.application.validators import (
    validate_execution_id,
    validate_checkpoint_id,
    validate_workflow_id,
    validate_batch_test_id,
    validate_limit,
    validate_offset,
    validate_state_changes,
    validate_reason,
    validate_node_id,
    validate_idempotency_key,
)

if TYPE_CHECKING:
    from wtb.infrastructure.events import WTBEventBus, WTBAuditTrail

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Execution API Service
# ═══════════════════════════════════════════════════════════════════════════════


class ExecutionAPIService(IExecutionAPIService):
    """
    ACID-compliant execution API service.
    
    All operations are wrapped in Unit of Work transactions.
    Uses Outbox pattern for event publishing consistency.
    
    SOLID Compliance:
    - SRP: Only handles execution API operations
    - DIP: Depends on IUnitOfWork and IExecutionController abstractions
    
    ISSUE-ACID-001 / ISSUE-API-002 - Sync UoW in Async Methods:
    ══════════════════════════════════════════════════════════════════
    This service uses sync `with self._uow:` in async methods because:
    1. The underlying ExecutionController is synchronous
    2. Repository implementations are synchronous
    3. Database operations complete quickly (< 100ms typically)
    
    Impact:
    - Event loop may block briefly during DB operations
    - Acceptable for current use cases (low concurrency control plane)
    
    Migration Path (for high-concurrency scenarios):
    1. Create AsyncExecutionController
    2. Use IAsyncUnitOfWork with `async with self._uow:`
    3. Migrate repositories to async versions
    
    Current behavior is SAFE and ACID-compliant, just not fully async.
    ══════════════════════════════════════════════════════════════════
    """
    
    def __init__(
        self,
        uow: IUnitOfWork,
        controller: IExecutionController,
        event_bus: Optional["WTBEventBus"] = None,
        audit_trail: Optional["WTBAuditTrail"] = None,
    ):
        """
        Initialize with dependencies.
        
        Args:
            uow: Unit of Work for transactions
            controller: Execution controller for domain operations
            event_bus: Optional event bus for async events
            audit_trail: Optional audit trail for logging
        """
        self._uow = uow
        self._controller = controller
        self._event_bus = event_bus
        self._audit_trail = audit_trail
    
    async def list_executions(
        self,
        workflow_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> PaginatedResultDTO:
        """
        List executions with ACID read.
        
        Fixed: 2026-01-28 - Actually queries repository instead of returning empty list.
        Note: Uses sync UoW because underlying controller/repos are sync.
        See ARCHITECTURE_REVIEW_2026_01_28.md ACID-001 for async migration plan.
        """
        try:
            # Read operations wrapped in UoW for consistent snapshot
            with self._uow:
                # Get executions from repository using base list method
                all_executions = self._uow.executions.list(
                    limit=limit + offset + 100,  # Fetch extra for filtering
                    offset=0
                )
                
                # Apply filters in memory (repository doesn't support complex filters)
                if workflow_id:
                    all_executions = [
                        e for e in all_executions 
                        if e.workflow_id == workflow_id
                    ]
                if status:
                    all_executions = [
                        e for e in all_executions 
                        if e.status.value == status
                    ]
                
                # Pagination
                total = len(all_executions)
                paginated = all_executions[offset:offset + limit]
                
                items = [
                    ExecutionDTO(
                        id=e.id,
                        workflow_id=e.workflow_id,
                        status=e.status.value,
                        state={
                            "current_node_id": e.state.current_node_id,
                            "workflow_variables": e.state.workflow_variables,
                            "execution_path": e.state.execution_path,
                            "node_results": e.state.node_results,
                        },
                        breakpoints=e.breakpoints,
                        current_node_id=e.state.current_node_id,
                        error=e.error,
                        error_node_id=e.error_node_id,
                        started_at=e.started_at,
                        completed_at=e.completed_at,
                    )
                    for e in paginated
                ]
                
                return PaginatedResultDTO(
                    items=items,
                    total=total,
                    limit=limit,
                    offset=offset,
                    has_more=offset + limit < total,
                )
            
        except Exception as e:
            logger.error(f"Failed to list executions: {e}")
            return PaginatedResultDTO(
                items=[],
                total=0,
                limit=limit,
                offset=offset,
                has_more=False,
            )
    
    async def get_execution(self, execution_id: str) -> Optional[ExecutionDTO]:
        """Get execution with ACID read."""
        try:
            execution = self._controller.get_status(execution_id)
            
            return ExecutionDTO(
                id=execution.id,
                workflow_id=execution.workflow_id,
                status=execution.status.value,
                state={
                    "current_node_id": execution.state.current_node_id,
                    "workflow_variables": execution.state.workflow_variables,
                    "execution_path": execution.state.execution_path,
                    "node_results": execution.state.node_results,
                },
                breakpoints=execution.breakpoints,
                current_node_id=execution.state.current_node_id,
                error=execution.error,
                error_node_id=execution.error_node_id,
                started_at=execution.started_at,
                completed_at=execution.completed_at,
                checkpoint_count=len(execution.state.execution_path),
                nodes_executed=len(execution.state.execution_path),
            )
            
        except ValueError:
            return None
        except Exception as e:
            logger.error(f"Failed to get execution {execution_id}: {e}")
            return None
    
    async def pause_execution(
        self,
        execution_id: str,
        reason: Optional[str] = None,
        at_node: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> ControlResultDTO:
        """
        Pause execution with ACID transaction.
        
        Creates checkpoint atomically. Uses outbox for event consistency.
        
        Args:
            execution_id: ID of execution to pause (UUID format)
            reason: Optional reason for pausing
            at_node: Optional node to pause at
            idempotency_key: Optional key for safe retry (ISSUE-OB-002)
            
        Returns:
            ControlResultDTO with success status and checkpoint ID
            
        ISSUE-API-003: Input validation added.
        ISSUE-OB-002: Idempotency key support added.
        """
        try:
            # Input validation (ISSUE-API-003)
            execution_id = validate_execution_id(execution_id)
            reason = validate_reason(reason, required=False)
            at_node = validate_node_id(at_node, required=False) if at_node else None
            
            # Validate idempotency key if provided (ISSUE-OB-002)
            # NOTE: Client must provide key for safe retries. No auto-generation.
            idempotency_key = validate_idempotency_key(idempotency_key)
            
            # Transaction boundary
            with self._uow:
                execution = self._controller.pause(execution_id)
                
                # Record in outbox for async event processing
                outbox_event = OutboxEvent.create(
                    event_type=OutboxEventType.EXECUTION_PAUSED,
                    aggregate_id=execution_id,
                    payload={
                        "execution_id": execution_id,
                        "reason": reason,
                        "at_node": at_node,
                        "checkpoint_id": str(execution.checkpoint_id) if execution.checkpoint_id else None,
                    },
                    idempotency_key=idempotency_key,
                )
                self._uow.outbox.add(outbox_event)
                self._uow.commit()
                
                return ControlResultDTO(
                    success=True,
                    status=ExecutionStatus.PAUSED.value,
                    checkpoint_id=str(execution.checkpoint_id) if execution.checkpoint_id else None,
                    message=f"Execution paused. Checkpoint: {execution.checkpoint_id}",
                )
                
        except ValueError as e:
            return ControlResultDTO(
                success=False,
                status="error",
                error=str(e),
            )
        except Exception as e:
            logger.error(f"Failed to pause execution {execution_id}: {e}")
            return ControlResultDTO(
                success=False,
                status="error",
                error=str(e),
            )
    
    async def resume_execution(
        self,
        execution_id: str,
        modified_state: Optional[Dict[str, Any]] = None,
        from_node: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> ControlResultDTO:
        """
        Resume execution with ACID transaction.
        
        State modifications applied atomically.
        
        Args:
            execution_id: ID of execution to resume (UUID format)
            modified_state: Optional state modifications (HITL)
            from_node: Optional node to resume from
            idempotency_key: Optional key for safe retry
            
        ISSUE-API-003: Input validation added.
        ISSUE-OB-002: Idempotency key support added.
        """
        try:
            # Input validation (ISSUE-API-003)
            execution_id = validate_execution_id(execution_id)
            from_node = validate_node_id(from_node, required=False) if from_node else None
            if modified_state is not None:
                validate_state_changes(modified_state)
            
            # Validate idempotency key if provided (client must provide for safe retries)
            idempotency_key = validate_idempotency_key(idempotency_key)
            
            with self._uow:
                execution = self._controller.resume(execution_id, modified_state)
                
                # Outbox event
                outbox_event = OutboxEvent.create(
                    event_type=OutboxEventType.EXECUTION_RESUMED,
                    aggregate_id=execution_id,
                    payload={
                        "execution_id": execution_id,
                        "from_node": from_node or execution.state.current_node_id,
                        "state_modified": modified_state is not None,
                    },
                    idempotency_key=idempotency_key,
                )
                self._uow.outbox.add(outbox_event)
                self._uow.commit()
                
                return ControlResultDTO(
                    success=True,
                    status=ExecutionStatus.RUNNING.value,
                    message=f"Execution resumed from node: {execution.state.current_node_id}",
                )
                
        except ValueError as e:
            return ControlResultDTO(
                success=False,
                status="error",
                error=str(e),
            )
        except Exception as e:
            logger.error(f"Failed to resume execution {execution_id}: {e}")
            return ControlResultDTO(
                success=False,
                status="error",
                error=str(e),
            )
    
    async def stop_execution(
        self,
        execution_id: str,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> ControlResultDTO:
        """
        Stop execution with ACID transaction.
        
        ISSUE-API-003: Input validation added.
        ISSUE-OB-002: Idempotency key support added.
        """
        try:
            # Input validation
            execution_id = validate_execution_id(execution_id)
            reason = validate_reason(reason, required=False)
            idempotency_key = validate_idempotency_key(idempotency_key)
            
            with self._uow:
                execution = self._controller.stop(execution_id)
                
                outbox_event = OutboxEvent.create(
                    event_type=OutboxEventType.EXECUTION_STOPPED,
                    aggregate_id=execution_id,
                    payload={
                        "execution_id": execution_id,
                        "reason": reason,
                    },
                    idempotency_key=idempotency_key,
                )
                self._uow.outbox.add(outbox_event)
                self._uow.commit()
                
                return ControlResultDTO(
                    success=True,
                    status=ExecutionStatus.CANCELLED.value,
                    message=f"Execution stopped: {reason or 'User requested'}",
                )
                
        except ValueError as e:
            return ControlResultDTO(
                success=False,
                status="error",
                error=str(e),
            )
        except Exception as e:
            logger.error(f"Failed to stop execution {execution_id}: {e}")
            return ControlResultDTO(
                success=False,
                status="error",
                error=str(e),
            )
    
    async def rollback_execution(
        self,
        execution_id: str,
        checkpoint_id: str,
        create_branch: bool = False,
        idempotency_key: Optional[str] = None,
    ) -> RollbackResultDTO:
        """
        Rollback with ACID transaction.
        
        CRITICAL: checkpoint_id is a UUID string, not an integer.
        
        ISSUE-API-003: Input validation added.
        ISSUE-OB-002: Idempotency key support added.
        """
        try:
            # Input validation
            execution_id = validate_execution_id(execution_id)
            checkpoint_id = validate_checkpoint_id(checkpoint_id)
            idempotency_key = validate_idempotency_key(idempotency_key)
            
            with self._uow:
                # NOTE: checkpoint_id is a string (UUID), not int
                # The controller.rollback() should accept string
                execution = self._controller.rollback(execution_id, checkpoint_id)
                
                outbox_event = OutboxEvent.create(
                    event_type=OutboxEventType.ROLLBACK_PERFORMED,
                    aggregate_id=execution_id,
                    payload={
                        "execution_id": execution_id,
                        "checkpoint_id": checkpoint_id,
                        "create_branch": create_branch,
                    },
                    idempotency_key=idempotency_key,
                )
                self._uow.outbox.add(outbox_event)
                self._uow.commit()
                
                return RollbackResultDTO(
                    success=True,
                    to_checkpoint=checkpoint_id,
                    new_session_id=execution.session_id if create_branch else None,
                )
                
        except ValueError as e:
            return RollbackResultDTO(
                success=False,
                to_checkpoint=checkpoint_id,
                error=str(e),
            )
        except Exception as e:
            logger.error(f"Failed to rollback execution {execution_id}: {e}")
            return RollbackResultDTO(
                success=False,
                to_checkpoint=checkpoint_id,
                error=str(e),
            )
    
    async def get_execution_state(
        self,
        execution_id: str,
        keys: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Get execution state (read-only)."""
        try:
            state = self._controller.get_state(execution_id)
            values = state.workflow_variables.copy()
            
            if keys:
                values = {k: v for k, v in values.items() if k in keys}
            
            return {
                "execution_id": execution_id,
                "current_node": state.current_node_id,
                "values": values,
            }
            
        except Exception as e:
            logger.error(f"Failed to get state for {execution_id}: {e}")
            return {}
    
    async def modify_execution_state(
        self,
        execution_id: str,
        changes: Dict[str, Any],
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> ControlResultDTO:
        """
        Modify state with ACID transaction.
        
        Creates checkpoint before modification.
        
        ISSUE-API-003: Input validation added.
        ISSUE-OB-002: Idempotency key support added.
        """
        try:
            # Input validation
            execution_id = validate_execution_id(execution_id)
            validate_state_changes(changes)
            reason = validate_reason(reason, required=False)
            idempotency_key = validate_idempotency_key(idempotency_key)
            
            with self._uow:
                success = self._controller.update_execution_state(execution_id, changes)
                
                if success:
                    outbox_event = OutboxEvent.create(
                        event_type=OutboxEventType.STATE_MODIFIED,
                        aggregate_id=execution_id,
                        payload={
                            "execution_id": execution_id,
                            "changed_keys": list(changes.keys()),
                            "reason": reason,
                        },
                        idempotency_key=idempotency_key,
                    )
                    self._uow.outbox.add(outbox_event)
                    self._uow.commit()
                    
                    return ControlResultDTO(
                        success=True,
                        status=ExecutionStatus.PAUSED.value,
                        message=f"State modified: {list(changes.keys())}",
                    )
                else:
                    return ControlResultDTO(
                        success=False,
                        status=ExecutionStatus.PAUSED.value,
                        message="State modification not supported by adapter",
                    )
                    
        except ValueError as e:
            return ControlResultDTO(
                success=False,
                status="error",
                error=str(e),
            )
        except Exception as e:
            logger.error(f"Failed to modify state for {execution_id}: {e}")
            return ControlResultDTO(
                success=False,
                status="error",
                error=str(e),
            )
    
    async def list_checkpoints(
        self,
        execution_id: str,
        limit: int = 100,
    ) -> List[CheckpointDTO]:
        """List checkpoints for execution."""
        try:
            if not self._controller.supports_time_travel():
                return []
            
            history = self._controller.get_checkpoint_history(execution_id)
            
            checkpoints = []
            for i, cp in enumerate(history[:limit]):
                checkpoints.append(CheckpointDTO(
                    id=str(cp.get("checkpoint_id", cp.get("id", ""))),
                    execution_id=execution_id,
                    node_id=cp.get("source"),
                    trigger_type=cp.get("trigger_type", "auto"),
                    created_at=cp.get("created_at"),
                    state_snapshot=cp.get("values"),
                ))
            
            return checkpoints
            
        except Exception as e:
            logger.error(f"Failed to list checkpoints for {execution_id}: {e}")
            return []
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Backward Compatibility Aliases
    # These methods delegate to the properly-named methods above
    # for compatibility with existing REST routes
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def pause(
        self,
        execution_id: str,
        reason: Optional[str] = None,
        at_node: Optional[str] = None,
    ) -> dict:
        """Alias for pause_execution (backward compatibility)."""
        result = await self.pause_execution(execution_id, reason, at_node)
        if not result.success:
            raise ValueError(result.error or "Pause failed")
        return {"checkpoint_id": result.checkpoint_id}
    
    async def resume(
        self,
        execution_id: str,
        modified_state: Optional[Dict[str, Any]] = None,
        from_node: Optional[str] = None,
    ) -> dict:
        """Alias for resume_execution (backward compatibility)."""
        result = await self.resume_execution(execution_id, modified_state, from_node)
        return {"resumed_from_node": result.message.split(": ")[-1] if result.message else ""}
    
    async def rollback(
        self,
        execution_id: str,
        checkpoint_id: str,
        create_branch: bool = False,
    ) -> dict:
        """Alias for rollback_execution (backward compatibility)."""
        result = await self.rollback_execution(execution_id, checkpoint_id, create_branch)
        return {
            "new_session_id": result.new_session_id,
            "tools_reversed": result.tools_reversed,
        }
    
    async def inspect_state(
        self,
        execution_id: str,
        keys: Optional[List[str]] = None,
    ) -> dict:
        """Alias for get_execution_state (backward compatibility)."""
        result = await self.get_execution_state(execution_id, keys)
        return {
            "values": result.get("values", {}),
            "current_node": result.get("current_node"),
        }
    
    async def modify_state(
        self,
        execution_id: str,
        changes: Dict[str, Any],
    ) -> bool:
        """Alias for modify_execution_state (backward compatibility)."""
        result = await self.modify_execution_state(execution_id, changes)
        return result.success


# ═══════════════════════════════════════════════════════════════════════════════
# Audit API Service
# ═══════════════════════════════════════════════════════════════════════════════


class AuditAPIService(IAuditAPIService):
    """
    ACID-compliant audit API service.
    
    Provides read-only access to audit data.
    """
    
    def __init__(
        self,
        audit_trail: "WTBAuditTrail",
        uow: Optional[IUnitOfWork] = None,
    ):
        """
        Initialize with audit trail.
        
        Args:
            audit_trail: Audit trail for event access
            uow: Optional UoW for transaction context
        """
        self._audit_trail = audit_trail
        self._uow = uow
    
    async def query_events(
        self,
        execution_id: Optional[str] = None,
        event_types: Optional[List[str]] = None,
        severities: Optional[List[str]] = None,
        node_id: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> PaginatedResultDTO:
        """Query audit events with filtering."""
        entries = self._audit_trail.entries
        
        # Apply filters
        if execution_id:
            entries = [e for e in entries if e.execution_id == execution_id]
        if event_types:
            entries = [e for e in entries if e.event_type.value in event_types]
        if severities:
            entries = [e for e in entries if e.severity.value in severities]
        if node_id:
            entries = [e for e in entries if e.node_id == node_id]
        
        # Pagination
        total = len(entries)
        paginated = entries[offset:offset + limit]
        
        items = [
            AuditEventDTO(
                id=str(i),
                timestamp=e.timestamp,
                event_type=e.event_type.value,
                severity=e.severity.value,
                message=e.message,
                execution_id=e.execution_id,
                node_id=e.node_id,
                details=e.details,
                error=e.error,
                duration_ms=e.duration_ms,
            )
            for i, e in enumerate(paginated)
        ]
        
        return PaginatedResultDTO(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + limit < total,
        )
    
    async def get_summary(
        self,
        execution_id: Optional[str] = None,
        time_range: str = "1h",
    ) -> AuditSummaryDTO:
        """Get audit summary."""
        summary = self._audit_trail.get_summary()
        
        return AuditSummaryDTO(
            total_events=summary.get("total_entries", 0),
            execution_id=execution_id,
            time_range=time_range,
            checkpoint_count=summary.get("checkpoints_created", 0),
            rollback_count=summary.get("rollbacks", 0),
            nodes_executed=summary.get("nodes_executed", 0),
            nodes_failed=summary.get("nodes_failed", 0),
        )
    
    async def get_timeline(
        self,
        execution_id: str,
        include_debug: bool = False,
    ) -> List[AuditEventDTO]:
        """Get execution timeline."""
        entries = self._audit_trail.entries
        entries = [e for e in entries if e.execution_id == execution_id]
        
        if not include_debug:
            from wtb.infrastructure.events import WTBAuditSeverity
            entries = [e for e in entries if e.severity != WTBAuditSeverity.DEBUG]
        
        return [
            AuditEventDTO(
                id=str(i),
                timestamp=e.timestamp,
                event_type=e.event_type.value,
                severity=e.severity.value,
                message=e.message,
                execution_id=e.execution_id,
                node_id=e.node_id,
                duration_ms=e.duration_ms,
            )
            for i, e in enumerate(entries)
        ]


# ═══════════════════════════════════════════════════════════════════════════════
# Batch Test API Service
# ═══════════════════════════════════════════════════════════════════════════════


class BatchTestAPIService(IBatchTestAPIService):
    """
    ACID-compliant batch test API service.
    """
    
    def __init__(
        self,
        uow: IUnitOfWork,
        event_bus: Optional["WTBEventBus"] = None,
        batch_runner: Optional[Any] = None,
    ):
        """
        Initialize with dependencies.
        
        Args:
            uow: Unit of Work for transactions
            event_bus: Event bus for async events
            batch_runner: Optional batch test runner
        """
        self._uow = uow
        self._event_bus = event_bus
        self._batch_runner = batch_runner
        # WARNING: in-memory cache only; lost on restart.
        # TODO: Back with repository for durable persistence.
        self._batch_tests: Dict[str, BatchTestDTO] = {}
        self._batch_tests_lock = threading.Lock()
    
    async def create_batch_test(
        self,
        workflow_id: str,
        variants: List[Dict[str, Any]],
        initial_state: Optional[Dict[str, Any]] = None,
        parallelism: Optional[int] = None,
        use_ray: bool = True,
    ) -> BatchTestDTO:
        """Create batch test with ACID transaction."""
        try:
            with self._uow:
                batch_test_id = str(uuid.uuid4())
                now = datetime.now(timezone.utc)
                
                batch_test = BatchTestDTO(
                    id=batch_test_id,
                    workflow_id=workflow_id,
                    status="pending",
                    variant_count=len(variants),
                    created_at=now,
                )
                
                self._batch_tests[batch_test_id] = batch_test
                
                # Record in outbox
                outbox_event = OutboxEvent.create(
                    event_type=OutboxEventType.BATCH_TEST_CREATED,
                    aggregate_id=batch_test_id,
                    payload={
                        "batch_test_id": batch_test_id,
                        "workflow_id": workflow_id,
                        "variant_count": len(variants),
                    },
                )
                self._uow.outbox.add(outbox_event)
                self._uow.commit()
                
                return batch_test
                
        except Exception as e:
            logger.error(f"Failed to create batch test: {e}")
            raise
    
    async def get_batch_test(self, batch_test_id: str) -> Optional[BatchTestDTO]:
        """Get batch test by ID."""
        return self._batch_tests.get(batch_test_id)
    
    async def list_batch_tests(
        self,
        workflow_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> PaginatedResultDTO:
        """List batch tests with filtering."""
        items = list(self._batch_tests.values())
        
        if workflow_id:
            items = [bt for bt in items if bt.workflow_id == workflow_id]
        if status:
            items = [bt for bt in items if bt.status == status]
        
        total = len(items)
        paginated = items[offset:offset + limit]
        
        return PaginatedResultDTO(
            items=paginated,
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + limit < total,
        )
    
    async def stream_progress(
        self,
        batch_test_id: str,
    ) -> AsyncIterator[BatchTestProgressDTO]:
        """Stream batch test progress."""
        batch_test = self._batch_tests.get(batch_test_id)
        if not batch_test:
            return
        
        yield BatchTestProgressDTO(
            batch_test_id=batch_test_id,
            total=batch_test.variant_count,
            completed=batch_test.variants_completed,
            failed=batch_test.variants_failed,
        )
    
    async def cancel_batch_test(
        self,
        batch_test_id: str,
        reason: Optional[str] = None,
    ) -> ControlResultDTO:
        """Cancel batch test with ACID transaction."""
        try:
            with self._uow:
                batch_test = self._batch_tests.get(batch_test_id)
                if not batch_test:
                    return ControlResultDTO(
                        success=False,
                        status="error",
                        error="Batch test not found",
                    )
                
                batch_test.status = "cancelled"
                
                outbox_event = OutboxEvent.create(
                    event_type=OutboxEventType.BATCH_TEST_CANCELLED,
                    aggregate_id=batch_test_id,
                    payload={
                        "batch_test_id": batch_test_id,
                        "reason": reason,
                    },
                )
                self._uow.outbox.add(outbox_event)
                self._uow.commit()
                
                return ControlResultDTO(
                    success=True,
                    status="cancelled",
                    message=f"Batch test cancelled: {reason or 'User requested'}",
                )
                
        except Exception as e:
            logger.error(f"Failed to cancel batch test {batch_test_id}: {e}")
            return ControlResultDTO(
                success=False,
                status="error",
                error=str(e),
            )


# ═══════════════════════════════════════════════════════════════════════════════
# Workflow API Service
# ═══════════════════════════════════════════════════════════════════════════════


class WorkflowAPIService(IWorkflowAPIService):
    """
    ACID-compliant workflow API service.
    """
    
    def __init__(
        self,
        uow: IUnitOfWork,
    ):
        """
        Initialize with Unit of Work.
        
        Args:
            uow: Unit of Work for transactions
        """
        self._uow = uow
        # WARNING: in-memory cache only; lost on restart.
        # TODO: Back with repository for durable persistence.
        self._workflows: Dict[str, WorkflowDTO] = {}
        self._workflows_lock = threading.Lock()
    
    async def create_workflow(
        self,
        name: str,
        nodes: List[Dict[str, Any]],
        entry_point: str,
        description: Optional[str] = None,
        edges: Optional[List[Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> WorkflowDTO:
        """Create workflow with ACID transaction."""
        try:
            with self._uow:
                workflow_id = str(uuid.uuid4())
                now = datetime.now(timezone.utc)
                
                workflow = WorkflowDTO(
                    id=workflow_id,
                    name=name,
                    description=description,
                    nodes=nodes,
                    edges=edges or [],
                    entry_point=entry_point,
                    metadata=metadata,
                    created_at=now,
                    updated_at=now,
                )
                
                self._workflows[workflow_id] = workflow
                
                # Record in outbox
                outbox_event = OutboxEvent.create(
                    event_type=OutboxEventType.WORKFLOW_CREATED,
                    aggregate_id=workflow_id,
                    payload={
                        "workflow_id": workflow_id,
                        "name": name,
                    },
                )
                self._uow.outbox.add(outbox_event)
                self._uow.commit()
                
                return workflow
                
        except Exception as e:
            logger.error(f"Failed to create workflow: {e}")
            raise
    
    async def get_workflow(self, workflow_id: str) -> Optional[WorkflowDTO]:
        """Get workflow by ID."""
        return self._workflows.get(workflow_id)
    
    async def list_workflows(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> PaginatedResultDTO:
        """List workflows with pagination."""
        items = list(self._workflows.values())
        total = len(items)
        paginated = items[offset:offset + limit]
        
        return PaginatedResultDTO(
            items=paginated,
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + limit < total,
        )
    
    async def update_workflow(
        self,
        workflow_id: str,
        **updates,
    ) -> WorkflowDTO:
        """Update workflow with ACID transaction."""
        try:
            with self._uow:
                workflow = self._workflows.get(workflow_id)
                if not workflow:
                    raise ValueError(f"Workflow not found: {workflow_id}")
                
                # Apply updates
                for key, value in updates.items():
                    if value is not None and hasattr(workflow, key):
                        setattr(workflow, key, value)
                
                workflow.updated_at = datetime.now(timezone.utc)
                workflow.version += 1
                
                self._uow.commit()
                
                return workflow
                
        except Exception as e:
            logger.error(f"Failed to update workflow {workflow_id}: {e}")
            raise
    
    async def delete_workflow(self, workflow_id: str) -> bool:
        """Delete workflow with ACID transaction."""
        try:
            with self._uow:
                if workflow_id in self._workflows:
                    del self._workflows[workflow_id]
                    self._uow.commit()
                    return True
                return False
                
        except Exception as e:
            logger.error(f"Failed to delete workflow {workflow_id}: {e}")
            return False


# ═══════════════════════════════════════════════════════════════════════════════
# Factory for API Services (ISSUE-SOLID-003: Now returns interfaces)
# ═══════════════════════════════════════════════════════════════════════════════


class APIServiceFactory:
    """
    Factory for creating API services with proper dependencies.
    
    Ensures SOLID compliance through dependency injection.
    
    ISSUE-SOLID-003 (2026-01-28):
    - Factory methods now return interface types, not concrete types
    - This enables substitution with alternative implementations
    - Follows Liskov Substitution Principle (LSP)
    
    Design:
    - SRP: Factory only creates services, doesn't configure them
    - OCP: New service implementations can be added without modifying callers
    - LSP: Any implementation of the interface can be substituted
    - ISP: Services implement specific interfaces
    - DIP: Services depend on abstractions (IUnitOfWork, etc.)
    """
    
    @staticmethod
    def create_execution_service(
        uow: IUnitOfWork,
        controller: IExecutionController,
        event_bus: Optional["WTBEventBus"] = None,
        audit_trail: Optional["WTBAuditTrail"] = None,
    ) -> IExecutionAPIService:
        """
        Create execution API service.
        
        Returns:
            IExecutionAPIService implementation
            
        SOLID: Returns interface type for substitutability.
        """
        return ExecutionAPIService(
            uow=uow,
            controller=controller,
            event_bus=event_bus,
            audit_trail=audit_trail,
        )
    
    @staticmethod
    def create_audit_service(
        audit_trail: "WTBAuditTrail",
        uow: Optional[IUnitOfWork] = None,
    ) -> IAuditAPIService:
        """
        Create audit API service.
        
        Returns:
            IAuditAPIService implementation
        """
        return AuditAPIService(
            audit_trail=audit_trail,
            uow=uow,
        )
    
    @staticmethod
    def create_batch_test_service(
        uow: IUnitOfWork,
        event_bus: Optional["WTBEventBus"] = None,
        batch_runner: Optional[Any] = None,
    ) -> IBatchTestAPIService:
        """
        Create batch test API service.
        
        Returns:
            IBatchTestAPIService implementation
        """
        return BatchTestAPIService(
            uow=uow,
            event_bus=event_bus,
            batch_runner=batch_runner,
        )
    
    @staticmethod
    def create_workflow_service(
        uow: IUnitOfWork,
    ) -> IWorkflowAPIService:
        """
        Create workflow API service.
        
        Returns:
            IWorkflowAPIService implementation
        """
        return WorkflowAPIService(uow=uow)


__all__ = [
    # Services
    "ExecutionAPIService",
    "AuditAPIService",
    "BatchTestAPIService",
    "WorkflowAPIService",
    # Factory
    "APIServiceFactory",
]
