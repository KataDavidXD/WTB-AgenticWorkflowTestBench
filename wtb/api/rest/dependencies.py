"""
FastAPI Dependencies for dependency injection.

Architecture (v2.0 - 2026-01-28):
─────────────────────────────────────────────────────────────────────────────────
Following DIP (Dependency Inversion Principle):
- API layer depends on abstractions (IExecutionAPIService, IAuditAPIService)
- Services are injected via FastAPI dependency system
- Enables easy testing and swapping implementations

Transaction Management:
- Unit of Work pattern for database transactions
- API services handle transaction boundaries internally
- Automatic commit/rollback on request completion

SOLID Compliance:
- SRP: Dependencies only handle DI wiring
- DIP: API depends on interfaces (IExecutionAPIService), not implementations
- ISP: Separate services for different concerns
"""


from wtb.application.services import ExecutionController
from wtb.infrastructure.events import (
    WTBAuditTrail,
    WTBEventBus,
    get_wtb_event_bus,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Application State (Singleton services)
# ═══════════════════════════════════════════════════════════════════════════════

class AppState:
    """
    Application state container.
    
    Holds singleton instances of services that are shared across requests.
    Initialized once at startup, used throughout the application lifetime.
    
    SOLID Compliance:
    - SRP: Only manages service lifecycle
    - DIP: Stores abstractions, not implementations
    """
    
    def __init__(self):
        self._event_bus: WTBEventBus | None = None
        self._audit_trail: WTBAuditTrail | None = None
        self._execution_controller: ExecutionController | None = None
        self._unit_of_work = None  # IUnitOfWork
        self._initialized: bool = False
        
        # API Services (v2.0)
        self._execution_service = None
        self._audit_service = None
        self._batch_test_service = None
        self._workflow_service = None
    
    def initialize(
        self,
        event_bus: WTBEventBus | None = None,
        execution_controller: ExecutionController | None = None,
        unit_of_work = None,
    ) -> None:
        """
        Initialize application state with services.
        
        Args:
            event_bus: WTB event bus instance
            execution_controller: Execution controller instance
            unit_of_work: Unit of work for transactions
        """
        self._event_bus = event_bus or get_wtb_event_bus()
        self._audit_trail = WTBAuditTrail()
        self._execution_controller = execution_controller
        self._unit_of_work = unit_of_work
        self._initialized = True
        
        # Initialize API services with proper DI
        self._initialize_api_services()
    
    def _initialize_api_services(self) -> None:
        """Initialize API services with dependencies."""
        try:
            from wtb.application.services.api_services import APIServiceFactory
            
            if self._unit_of_work and self._execution_controller:
                self._execution_service = APIServiceFactory.create_execution_service(
                    uow=self._unit_of_work,
                    controller=self._execution_controller,
                    event_bus=self._event_bus,
                    audit_trail=self._audit_trail,
                )
            
            if self._audit_trail:
                self._audit_service = APIServiceFactory.create_audit_service(
                    audit_trail=self._audit_trail,
                    uow=self._unit_of_work,
                )
            
            if self._unit_of_work:
                self._batch_test_service = APIServiceFactory.create_batch_test_service(
                    uow=self._unit_of_work,
                    event_bus=self._event_bus,
                )
                
                self._workflow_service = APIServiceFactory.create_workflow_service(
                    uow=self._unit_of_work,
                )
        except ImportError:
            # Fallback to legacy services if new ones not available
            pass
    
    @property
    def event_bus(self) -> WTBEventBus:
        """Get the event bus instance."""
        if self._event_bus is None:
            self._event_bus = get_wtb_event_bus()
        return self._event_bus
    
    @property
    def audit_trail(self) -> WTBAuditTrail:
        """Get the audit trail instance."""
        if self._audit_trail is None:
            self._audit_trail = WTBAuditTrail()
        return self._audit_trail
    
    @property
    def execution_controller(self) -> ExecutionController | None:
        """Get the execution controller instance."""
        return self._execution_controller
    
    @property
    def unit_of_work(self):
        """Get the unit of work instance."""
        return self._unit_of_work
    
    @property
    def execution_service(self):
        """Get the execution API service."""
        return self._execution_service
    
    @property
    def audit_service(self):
        """Get the audit API service."""
        return self._audit_service
    
    @property
    def batch_test_service(self):
        """Get the batch test API service."""
        return self._batch_test_service
    
    @property
    def workflow_service(self):
        """Get the workflow API service."""
        return self._workflow_service
    
    @property
    def is_initialized(self) -> bool:
        """Check if app state is initialized."""
        return self._initialized


# Global application state
_app_state: AppState | None = None


def get_app_state() -> AppState:
    """Get the global application state."""
    global _app_state
    if _app_state is None:
        _app_state = AppState()
    return _app_state


def set_app_state(state: AppState) -> None:
    """Set the global application state (for testing)."""
    global _app_state
    _app_state = state


def reset_app_state() -> None:
    """Reset the global application state (for testing)."""
    global _app_state
    _app_state = None


# ═══════════════════════════════════════════════════════════════════════════════
# FastAPI Dependencies
# ═══════════════════════════════════════════════════════════════════════════════

def get_event_bus() -> WTBEventBus:
    """FastAPI dependency for event bus."""
    return get_app_state().event_bus


def get_audit_trail() -> WTBAuditTrail:
    """FastAPI dependency for audit trail."""
    return get_app_state().audit_trail


def get_execution_controller() -> ExecutionController:
    """FastAPI dependency for execution controller."""
    controller = get_app_state().execution_controller
    if controller is None:
        raise RuntimeError(
            "ExecutionController not initialized. "
            "Call AppState.initialize() at startup."
        )
    return controller


# ═══════════════════════════════════════════════════════════════════════════════
# API Service Dependencies (v2.0 - 2026-01-28)
# ═══════════════════════════════════════════════════════════════════════════════

def get_execution_service():
    """
    FastAPI dependency for execution API service.
    
    Returns the ACID-compliant execution service that handles
    all transaction boundaries internally.
    """
    state = get_app_state()
    
    # Use new API service if available
    if state.execution_service:
        return state.execution_service
    
    # Fallback to legacy service
    return LegacyExecutionService(
        controller=state.execution_controller,
        event_bus=state.event_bus,
        audit_trail=state.audit_trail,
    )


def get_audit_service():
    """
    FastAPI dependency for audit API service.
    
    Returns the audit service for querying audit events.
    """
    state = get_app_state()
    
    if state.audit_service:
        return state.audit_service
    
    return LegacyAuditService(state.audit_trail)


def get_batch_test_service():
    """
    FastAPI dependency for batch test API service.
    """
    state = get_app_state()
    
    if state.batch_test_service:
        return state.batch_test_service
    
    return LegacyBatchTestService(state.event_bus)


def get_workflow_service():
    """
    FastAPI dependency for workflow API service.
    """
    state = get_app_state()
    
    if state.workflow_service:
        return state.workflow_service
    
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Legacy Service Wrappers (Backward Compatibility)
# DEPRECATED: These will be removed in v3.0
# ═══════════════════════════════════════════════════════════════════════════════

class LegacyExecutionService:
    """
    Legacy execution service for backward compatibility.
    
    DEPRECATED: Use ExecutionAPIService from wtb.application.services.api_services
    
    Issues Fixed:
    - checkpoint_id is now treated as string (UUID), not int
    - Uses proper async/await pattern
    """
    
    def __init__(
        self,
        controller: ExecutionController,
        event_bus: WTBEventBus,
        audit_trail: WTBAuditTrail,
    ):
        self._controller = controller
        self._event_bus = event_bus
        self._audit_trail = audit_trail
    
    async def list_executions(
        self,
        workflow_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """List executions with optional filtering."""
        executions = []
        return {
            "executions": executions,
            "pagination": {
                "total": len(executions),
                "limit": limit,
                "offset": offset,
                "has_more": False,
            }
        }
    
    async def get_execution(self, execution_id: str) -> dict | None:
        """Get execution by ID."""
        try:
            execution = self._controller.get_status(execution_id)
            return {
                "id": execution.id,
                "workflow_id": execution.workflow_id,
                "status": execution.status.value,
                "state": {
                    "current_node_id": execution.state.current_node_id,
                    "workflow_variables": execution.state.workflow_variables,
                    "execution_path": execution.state.execution_path,
                    "node_results": execution.state.node_results,
                },
                "breakpoints": execution.breakpoints,
                "current_node_id": execution.state.current_node_id,
                "error": execution.error,
                "error_node_id": execution.error_node_id,
                "started_at": execution.started_at,
                "completed_at": execution.completed_at,
                "checkpoint_count": execution.checkpoint_id or 0,
                "nodes_executed": len(execution.state.execution_path),
            }
        except ValueError:
            return None
    
    async def pause(
        self,
        execution_id: str,
        reason: str | None = None,
        at_node: str | None = None,
    ) -> dict:
        """Pause execution."""
        execution = self._controller.pause(execution_id)
        return {
            "checkpoint_id": str(execution.checkpoint_id) if execution.checkpoint_id else None,
        }
    
    async def resume(
        self,
        execution_id: str,
        modified_state: dict | None = None,
        from_node: str | None = None,
    ) -> dict:
        """Resume execution."""
        execution = self._controller.resume(execution_id, modified_state)
        return {
            "resumed_from_node": execution.state.current_node_id,
        }
    
    async def rollback(
        self,
        execution_id: str,
        checkpoint_id: str,
        create_branch: bool = False,
    ) -> dict:
        """
        Rollback to checkpoint.
        
        FIXED: checkpoint_id is treated as string (UUID), not converted to int.
        """
        # NOTE: checkpoint_id is a string (UUID) - do NOT convert to int
        execution = self._controller.rollback(execution_id, checkpoint_id)
        return {
            "new_session_id": execution.session_id,
            "tools_reversed": 0,
        }
    
    async def inspect_state(
        self,
        execution_id: str,
        keys: list | None = None,
    ) -> dict:
        """Inspect execution state."""
        state = self._controller.get_state(execution_id)
        values = state.workflow_variables
        if keys:
            values = {k: v for k, v in values.items() if k in keys}
        return {
            "values": values,
            "current_node": state.current_node_id,
        }
    
    async def modify_state(
        self,
        execution_id: str,
        changes: dict,
    ) -> bool:
        """Modify execution state."""
        return self._controller.update_execution_state(execution_id, changes)


class LegacyAuditService:
    """
    Legacy audit service for backward compatibility.
    
    DEPRECATED: Use AuditAPIService from wtb.application.services.api_services
    """
    
    def __init__(self, audit_trail: WTBAuditTrail):
        self._audit_trail = audit_trail
    
    async def query_events(
        self,
        execution_id: str | None = None,
        event_types: list | None = None,
        severities: list | None = None,
        node_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
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
        entries = entries[offset:offset + limit]
        
        return {
            "events": [
                {
                    "id": str(i),
                    "timestamp": e.timestamp,
                    "event_type": e.event_type.value,
                    "severity": e.severity.value,
                    "message": e.message,
                    "execution_id": e.execution_id,
                    "node_id": e.node_id,
                    "details": e.details,
                    "error": e.error,
                    "duration_ms": e.duration_ms,
                }
                for i, e in enumerate(entries)
            ],
            "pagination": {
                "total": total,
                "limit": limit,
                "offset": offset,
                "has_more": offset + limit < total,
            }
        }
    
    async def get_summary(
        self,
        execution_id: str | None = None,
        time_range: str = "1h",
    ) -> dict:
        """Get audit summary statistics."""
        summary = self._audit_trail.get_summary()
        return {
            "total_events": summary["total_entries"],
            "execution_id": execution_id,
            "time_range": time_range,
            "events_by_type": {},
            "events_by_severity": {},
            "error_rate": 0.0,
            "checkpoint_count": summary["checkpoints_created"],
            "rollback_count": summary["rollbacks"],
            "nodes_executed": summary["nodes_executed"],
            "nodes_failed": summary["nodes_failed"],
            "avg_node_duration_ms": None,
        }
    
    async def get_timeline(
        self,
        execution_id: str,
        include_debug: bool = False,
    ) -> dict:
        """Get execution timeline for visualization."""
        entries = self._audit_trail.entries
        entries = [e for e in entries if e.execution_id == execution_id]
        
        if not include_debug:
            from wtb.infrastructure.events import WTBAuditSeverity
            entries = [e for e in entries if e.severity != WTBAuditSeverity.DEBUG]
        
        return {
            "execution_id": execution_id,
            "entries": [
                {
                    "timestamp": e.timestamp,
                    "event_type": e.event_type.value,
                    "node_id": e.node_id,
                    "duration_ms": e.duration_ms,
                    "status": "completed" if "completed" in e.event_type.value else "started",
                }
                for e in entries
            ],
            "total_duration_ms": None,
        }
    
    async def get_trail_for_execution(
        self,
        execution_id: str,
        include_debug: bool = False,
        limit: int = 100,
    ) -> dict:
        """Get audit trail for a specific execution."""
        return await self.query_events(
            execution_id=execution_id,
            limit=limit,
        )


class LegacyBatchTestService:
    """
    Legacy batch test service for backward compatibility.
    
    DEPRECATED: Use BatchTestAPIService from wtb.application.services.api_services
    """
    
    def __init__(self, event_bus: WTBEventBus):
        self._event_bus = event_bus
        self._batch_tests: dict = {}
    
    async def create_batch_test(
        self,
        workflow_id: str,
        variants: list,
        initial_state: dict | None = None,
        parallelism: int | None = None,
        use_ray: bool = True,
    ) -> str:
        """Create and start a batch test."""
        import uuid
        batch_test_id = str(uuid.uuid4())
        
        self._batch_tests[batch_test_id] = {
            "id": batch_test_id,
            "workflow_id": workflow_id,
            "variants": variants,
            "status": "pending",
            "created_at": None,
        }
        
        return batch_test_id
    
    async def get_batch_test(self, batch_test_id: str) -> dict | None:
        """Get batch test by ID."""
        return self._batch_tests.get(batch_test_id)
    
    async def stream_progress(self, batch_test_id: str):
        """Stream batch test progress (generator)."""
        yield {
            "total": 0,
            "completed": 0,
            "failed": 0,
            "current": None,
            "eta_seconds": 0,
        }


# Type aliases for backward compatibility
ExecutionService = LegacyExecutionService
AuditService = LegacyAuditService
BatchTestService = LegacyBatchTestService
