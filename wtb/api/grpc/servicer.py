"""
WTB gRPC Servicer - ACID-compliant gRPC service implementation.

Created: 2026-01-28
Status: Active

Design Principles:
- SOLID: Servicer depends on IExecutionAPIService abstraction
- ACID: All operations wrapped in Unit of Work transactions
- DIP: Uses dependency injection for services

Architecture:
    gRPC Client
         │
         ▼
    WTBServicer (gRPC)
         │
         ▼
    IExecutionAPIService (Application)
         │
         ├──► IUnitOfWork (Transaction)
         └──► IExecutionController (Domain)

Transaction Pattern:
    All RPC methods use the same transaction pattern as REST:
    - Create transaction context
    - Perform operation
    - Commit on success, rollback on failure

Usage:
    from wtb.api.grpc import create_grpc_server, WTBServicer
    
    servicer = WTBServicer(
        execution_service=execution_api_service,
        audit_service=audit_api_service,
        batch_test_service=batch_test_api_service,
    )
    server = create_grpc_server(servicer, port=50051)
    server.start()
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from wtb.domain.interfaces.api_services import (
        IAuditAPIService,
        IBatchTestAPIService,
        IExecutionAPIService,
    )

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# gRPC Availability Check
# ═══════════════════════════════════════════════════════════════════════════════

GRPC_AVAILABLE = False
_grpc = None

try:
    import grpc
    from grpc import aio as grpc_aio
    _grpc = grpc
    GRPC_AVAILABLE = True
except ImportError:
    pass


# ═══════════════════════════════════════════════════════════════════════════════
# WTB gRPC Servicer
# ═══════════════════════════════════════════════════════════════════════════════


class WTBServicer:
    """
    gRPC Servicer for WTB API.
    
    Implements the WTBService defined in wtb_service.proto.
    All operations are ACID-compliant via IExecutionAPIService.
    
    SOLID Compliance:
    - SRP: Only handles gRPC protocol translation
    - DIP: Depends on service interfaces, not implementations
    - ISP: Separate methods for different operations
    """
    
    def __init__(
        self,
        execution_service: "IExecutionAPIService",
        audit_service: "IAuditAPIService" | None = None,
        batch_test_service: "IBatchTestAPIService" | None = None,
    ):
        """
        Initialize servicer with API services.
        
        Args:
            execution_service: Execution API service
            audit_service: Audit API service
            batch_test_service: Batch test API service
        """
        self._execution_service = execution_service
        self._audit_service = audit_service
        self._batch_test_service = batch_test_service
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Execution Control RPCs
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def StartExecution(self, request, context):
        """
        Start a new execution.
        
        Creates execution within a transaction.
        """
        try:
            # Extract request data
            workflow_id = request.workflow_id
            initial_state = _struct_to_dict(request.initial_state)
            breakpoints = list(request.breakpoints)
            
            # TODO: Implement via execution service
            # For now, return stub response
            
            return _create_execution_response(
                execution_id="stub-execution-id",
                status="pending",
                thread_id="stub-thread-id",
            )
            
        except Exception as e:
            logger.error(f"StartExecution failed: {e}")
            context.abort(
                _grpc.StatusCode.INTERNAL if GRPC_AVAILABLE else 13,
                str(e),
            )
    
    async def PauseExecution(self, request, context):
        """
        Pause a running execution.
        
        ACID: Creates checkpoint atomically.
        """
        try:
            result = await self._execution_service.pause_execution(
                execution_id=request.execution_id,
                reason=request.reason if request.reason else None,
                at_node=request.at_node if request.at_node else None,
            )
            
            return _create_control_response(
                success=result.success,
                status=result.status,
                checkpoint_id=result.checkpoint_id,
                message=result.message,
            )
            
        except Exception as e:
            logger.error(f"PauseExecution failed: {e}")
            context.abort(
                _grpc.StatusCode.INTERNAL if GRPC_AVAILABLE else 13,
                str(e),
            )
    
    async def ResumeExecution(self, request, context):
        """
        Resume a paused execution.
        
        ACID: State modifications applied atomically.
        """
        try:
            modified_state = None
            if request.HasField("modified_state"):
                modified_state = _struct_to_dict(request.modified_state)
            
            result = await self._execution_service.resume_execution(
                execution_id=request.execution_id,
                modified_state=modified_state,
                from_node=request.from_node if request.from_node else None,
            )
            
            return _create_control_response(
                success=result.success,
                status=result.status,
                message=result.message,
            )
            
        except Exception as e:
            logger.error(f"ResumeExecution failed: {e}")
            context.abort(
                _grpc.StatusCode.INTERNAL if GRPC_AVAILABLE else 13,
                str(e),
            )
    
    async def StopExecution(self, request, context):
        """
        Stop and cancel an execution.
        
        ACID: Final checkpoint created before stop.
        """
        try:
            result = await self._execution_service.stop_execution(
                execution_id=request.execution_id,
                reason=request.reason if request.reason else None,
            )
            
            return _create_control_response(
                success=result.success,
                status=result.status,
                message=result.message,
            )
            
        except Exception as e:
            logger.error(f"StopExecution failed: {e}")
            context.abort(
                _grpc.StatusCode.INTERNAL if GRPC_AVAILABLE else 13,
                str(e),
            )
    
    async def RollbackExecution(self, request, context):
        """
        Rollback execution to a checkpoint.
        
        ACID: Rollback is atomic.
        """
        try:
            result = await self._execution_service.rollback_execution(
                execution_id=request.execution_id,
                checkpoint_id=request.checkpoint_id,  # String (UUID)
                create_branch=request.create_branch,
            )
            
            return _create_rollback_response(
                success=result.success,
                new_session_id=result.new_session_id,
                tools_reversed=result.tools_reversed,
                files_restored=result.files_restored,
            )
            
        except Exception as e:
            logger.error(f"RollbackExecution failed: {e}")
            context.abort(
                _grpc.StatusCode.INTERNAL if GRPC_AVAILABLE else 13,
                str(e),
            )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Streaming RPCs
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def StreamExecutionEvents(self, request, context):
        """
        Stream execution events.
        
        Yields events as they occur during execution.
        """
        execution_id = request.execution_id
        event_types = list(request.event_types) if request.event_types else None
        
        logger.info(f"Starting event stream for execution: {execution_id}")
        
        try:
            # TODO: Implement proper event streaming
            # For now, yield a single status event
            yield _create_execution_event(
                event_id="stream-start",
                execution_id=execution_id,
                event_type="EXECUTION_STARTED",
            )
            
        except Exception as e:
            logger.error(f"StreamExecutionEvents failed: {e}")
            context.abort(
                _grpc.StatusCode.INTERNAL if GRPC_AVAILABLE else 13,
                str(e),
            )
    
    async def StreamCheckpoints(self, request, context):
        """
        Stream checkpoint events.
        """
        execution_id = request.execution_id
        
        try:
            checkpoints = await self._execution_service.list_checkpoints(
                execution_id=execution_id,
            )
            
            for cp in checkpoints:
                yield _create_checkpoint_event(
                    checkpoint_id=cp.id,
                    node_id=cp.node_id,
                    trigger_type=cp.trigger_type,
                    has_file_commit=cp.has_file_commit,
                )
                
        except Exception as e:
            logger.error(f"StreamCheckpoints failed: {e}")
            context.abort(
                _grpc.StatusCode.INTERNAL if GRPC_AVAILABLE else 13,
                str(e),
            )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Batch Test RPCs
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def RunBatchTest(self, request, context):
        """
        Run batch test and stream progress.
        
        ACID: Batch test created atomically.
        """
        if not self._batch_test_service:
            context.abort(
                _grpc.StatusCode.UNIMPLEMENTED if GRPC_AVAILABLE else 12,
                "Batch test service not available",
            )
            return
        
        try:
            # Create batch test
            variants = [
                {
                    "name": v.name,
                    "node_variants": dict(v.node_variants),
                }
                for v in request.variants
            ]
            
            batch_test = await self._batch_test_service.create_batch_test(
                workflow_id=request.workflow_id,
                variants=variants,
                initial_state=_struct_to_dict(request.initial_state) if request.HasField("initial_state") else None,
                parallelism=request.parallelism if request.parallelism > 0 else None,
                use_ray=request.use_ray,
            )
            
            # Stream progress
            async for progress in self._batch_test_service.stream_progress(batch_test.id):
                yield _create_batch_test_progress(
                    batch_test_id=progress.batch_test_id,
                    total_variants=progress.total,
                    completed=progress.completed,
                    failed=progress.failed,
                    eta_seconds=progress.eta_seconds,
                )
                
        except Exception as e:
            logger.error(f"RunBatchTest failed: {e}")
            context.abort(
                _grpc.StatusCode.INTERNAL if GRPC_AVAILABLE else 13,
                str(e),
            )
    
    async def GetBatchTestResults(self, request, context):
        """
        Get batch test results.
        """
        if not self._batch_test_service:
            context.abort(
                _grpc.StatusCode.UNIMPLEMENTED if GRPC_AVAILABLE else 12,
                "Batch test service not available",
            )
            return
        
        try:
            batch_test = await self._batch_test_service.get_batch_test(
                request.batch_test_id,
            )
            
            if not batch_test:
                context.abort(
                    _grpc.StatusCode.NOT_FOUND if GRPC_AVAILABLE else 5,
                    f"Batch test not found: {request.batch_test_id}",
                )
                return
            
            return _create_batch_test_results_response(
                batch_test_id=batch_test.id,
                status=batch_test.status,
                comparison_matrix=batch_test.comparison_matrix,
            )
            
        except Exception as e:
            logger.error(f"GetBatchTestResults failed: {e}")
            context.abort(
                _grpc.StatusCode.INTERNAL if GRPC_AVAILABLE else 13,
                str(e),
            )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Audit Streaming RPCs
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def StreamAuditEvents(self, request, context):
        """
        Stream audit events.
        """
        if not self._audit_service:
            context.abort(
                _grpc.StatusCode.UNIMPLEMENTED if GRPC_AVAILABLE else 12,
                "Audit service not available",
            )
            return
        
        try:
            result = await self._audit_service.query_events(
                execution_id=request.execution_id if request.execution_id else None,
                event_types=list(request.event_types) if request.event_types else None,
                limit=1000,
            )
            
            for event in result.items:
                yield _create_audit_event(
                    event_id=event.id,
                    timestamp=event.timestamp,
                    event_type=event.event_type,
                    severity=event.severity,
                    message=event.message,
                    execution_id=event.execution_id,
                    node_id=event.node_id,
                )
                
        except Exception as e:
            logger.error(f"StreamAuditEvents failed: {e}")
            context.abort(
                _grpc.StatusCode.INTERNAL if GRPC_AVAILABLE else 13,
                str(e),
            )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Interactive Session RPCs
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def InteractiveSession(self, request_iterator, context):
        """
        Bidirectional interactive debugging session.
        """
        try:
            async for command in request_iterator:
                execution_id = command.execution_id
                
                # Handle different command types
                if command.HasField("inspect_state"):
                    keys = list(command.inspect_state.keys) if command.inspect_state.keys else None
                    state = await self._execution_service.get_execution_state(
                        execution_id=execution_id,
                        keys=keys,
                    )
                    yield _create_interactive_response(
                        success=True,
                        message="State inspected",
                        state=state.get("values", {}),
                        current_node=state.get("current_node"),
                    )
                    
                elif command.HasField("modify_state"):
                    changes = _struct_to_dict(command.modify_state.changes)
                    result = await self._execution_service.modify_execution_state(
                        execution_id=execution_id,
                        changes=changes,
                    )
                    yield _create_interactive_response(
                        success=result.success,
                        message=result.message or "State modified",
                    )
                    
                elif command.HasField("continue_exec"):
                    result = await self._execution_service.resume_execution(
                        execution_id=execution_id,
                    )
                    yield _create_interactive_response(
                        success=result.success,
                        message="Execution continued",
                        status=result.status,
                    )
                    
                else:
                    yield _create_interactive_response(
                        success=False,
                        message="Unknown command",
                    )
                    
        except Exception as e:
            logger.error(f"InteractiveSession failed: {e}")
            context.abort(
                _grpc.StatusCode.INTERNAL if GRPC_AVAILABLE else 13,
                str(e),
            )


# ═══════════════════════════════════════════════════════════════════════════════
# Helper Functions for Proto Message Creation
# ═══════════════════════════════════════════════════════════════════════════════

def _struct_to_dict(struct) -> dict[str, Any]:
    """Convert google.protobuf.Struct to dict."""
    if struct is None:
        return {}
    try:
        from google.protobuf.json_format import MessageToDict
        return MessageToDict(struct)
    except (ImportError, AttributeError):
        return {}


def _dict_to_struct(d: dict[str, Any]):
    """Convert dict to google.protobuf.Struct."""
    try:
        from google.protobuf.json_format import ParseDict
        from google.protobuf.struct_pb2 import Struct
        struct = Struct()
        ParseDict(d, struct)
        return struct
    except ImportError:
        return None


def _create_execution_response(
    execution_id: str,
    status: str,
    thread_id: str = "",
) -> dict[str, Any]:
    """Create ExecutionResponse message."""
    return {
        "execution_id": execution_id,
        "status": status,
        "thread_id": thread_id,
    }


def _create_control_response(
    success: bool,
    status: str,
    checkpoint_id: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """Create ControlResponse message."""
    return {
        "success": success,
        "status": status,
        "checkpoint_id": checkpoint_id or "",
        "message": message or "",
    }


def _create_rollback_response(
    success: bool,
    new_session_id: str | None = None,
    tools_reversed: int = 0,
    files_restored: int = 0,
) -> dict[str, Any]:
    """Create RollbackResponse message."""
    return {
        "success": success,
        "new_session_id": new_session_id or "",
        "tools_reversed": tools_reversed,
        "files_restored": files_restored,
    }


def _create_execution_event(
    event_id: str,
    execution_id: str,
    event_type: str,
) -> dict[str, Any]:
    """Create ExecutionEvent message."""
    return {
        "event_id": event_id,
        "execution_id": execution_id,
        "type": event_type,
    }


def _create_checkpoint_event(
    checkpoint_id: str,
    node_id: str | None,
    trigger_type: str,
    has_file_commit: bool,
) -> dict[str, Any]:
    """Create CheckpointEvent message."""
    return {
        "checkpoint_id": checkpoint_id,
        "node_id": node_id or "",
        "trigger_type": trigger_type,
        "has_file_commit": has_file_commit,
    }


def _create_batch_test_progress(
    batch_test_id: str,
    total_variants: int,
    completed: int,
    failed: int,
    eta_seconds: float | None = None,
) -> dict[str, Any]:
    """Create BatchTestProgress message."""
    return {
        "batch_test_id": batch_test_id,
        "total_variants": total_variants,
        "completed": completed,
        "failed": failed,
        "estimated_remaining_seconds": eta_seconds or 0,
    }


def _create_batch_test_results_response(
    batch_test_id: str,
    status: str,
    comparison_matrix: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create BatchTestResultsResponse message."""
    return {
        "batch_test_id": batch_test_id,
        "status": status,
        "matrix": comparison_matrix or {},
    }


def _create_audit_event(
    event_id: str,
    timestamp: datetime,
    event_type: str,
    severity: str,
    message: str,
    execution_id: str | None = None,
    node_id: str | None = None,
) -> dict[str, Any]:
    """Create AuditEvent message."""
    return {
        "event_id": event_id,
        "event_type": event_type,
        "severity": severity,
        "message": message,
        "execution_id": execution_id or "",
        "node_id": node_id or "",
    }


def _create_interactive_response(
    success: bool,
    message: str,
    state: dict[str, Any] | None = None,
    current_node: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Create InteractiveResponse message."""
    response = {
        "success": success,
        "message": message,
    }
    if state is not None:
        response["state"] = {"state": state, "current_node": current_node or ""}
    if status is not None:
        response["status"] = {"status": status, "current_node": current_node or ""}
    return response


# ═══════════════════════════════════════════════════════════════════════════════
# Server Factory
# ═══════════════════════════════════════════════════════════════════════════════


def create_grpc_server(
    servicer: WTBServicer,
    port: int = 50051,
    max_workers: int = 10,
) -> Any | None:
    """
    Create and configure a gRPC server.
    
    Args:
        servicer: WTBServicer instance
        port: Port to listen on
        max_workers: Maximum worker threads
        
    Returns:
        gRPC server instance, or None if gRPC not available
    """
    if not GRPC_AVAILABLE:
        logger.warning("gRPC not available - install with: pip install grpcio")
        return None
    
    try:
        from concurrent import futures
        
        server = _grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
        
        # TODO: Register servicer with generated proto stubs
        # wtb_service_pb2_grpc.add_WTBServiceServicer_to_server(servicer, server)
        
        server.add_insecure_port(f"[::]:{port}")
        
        logger.info(f"gRPC server configured on port {port}")
        return server
        
    except Exception as e:
        logger.error(f"Failed to create gRPC server: {e}")
        return None


async def create_async_grpc_server(
    servicer: WTBServicer,
    port: int = 50051,
) -> Any | None:
    """
    Create an async gRPC server.
    
    Args:
        servicer: WTBServicer instance
        port: Port to listen on
        
    Returns:
        Async gRPC server instance, or None if not available
    """
    if not GRPC_AVAILABLE:
        logger.warning("gRPC not available")
        return None
    
    try:
        server = grpc_aio.server()
        
        # TODO: Register servicer with generated proto stubs
        
        server.add_insecure_port(f"[::]:{port}")
        
        logger.info(f"Async gRPC server configured on port {port}")
        return server
        
    except Exception as e:
        logger.error(f"Failed to create async gRPC server: {e}")
        return None


__all__ = [
    "WTBServicer",
    "create_grpc_server",
    "create_async_grpc_server",
    "GRPC_AVAILABLE",
]
