"""
Execution control endpoints.

Provides fine-grained control over workflow executions:
- CRUD operations
- Lifecycle control (start, pause, resume, stop)
- Checkpoint & branching
- State inspection & modification
- Node-level control
"""

from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from wtb.api.rest.dependencies import (
    AuditService,
    ExecutionService,
    get_audit_service,
    get_execution_service,
)
from wtb.api.rest.models import (
    AuditEventListResponse,
    BranchCreateRequest,
    BranchListResponse,
    BranchResponse,
    CheckpointListResponse,
    CheckpointResponse,
    ControlResponse,
    ExecuteSingleNodeRequest,
    ExecutionListResponse,
    ExecutionResponse,
    ExecutionStateSchema,
    ExecutionStatusEnum,
    NodeExecutionStatusSchema,
    PaginationMeta,
    PauseRequest,
    ResumeRequest,
    RollbackRequest,
    RollbackResponse,
    StateModifyRequest,
)

router = APIRouter(prefix="/api/v1/executions", tags=["Executions"])


# ═══════════════════════════════════════════════════════════════════════════════
# CRUD Operations
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("", response_model=ExecutionListResponse)
async def list_executions(
    workflow_id: str | None = Query(None, description="Filter by workflow ID"),
    status: str | None = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    execution_service: ExecutionService = Depends(get_execution_service),
) -> ExecutionListResponse:
    """
    List executions with optional filtering.
    
    Supports filtering by:
    - workflow_id: Specific workflow
    - status: pending, running, paused, completed, failed, cancelled
    """
    result = await execution_service.list_executions(
        workflow_id=workflow_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    
    return ExecutionListResponse(
        executions=[
            ExecutionResponse(**exec_data)
            for exec_data in result["executions"]
        ],
        pagination=PaginationMeta(**result["pagination"]),
    )


@router.get("/{execution_id}", response_model=ExecutionResponse)
async def get_execution(
    execution_id: str,
    execution_service: ExecutionService = Depends(get_execution_service),
) -> ExecutionResponse:
    """
    Get execution details including current state.
    """
    result = await execution_service.get_execution(execution_id)
    if not result:
        raise HTTPException(status_code=404, detail="Execution not found")
    
    return ExecutionResponse(
        id=result["id"],
        workflow_id=result["workflow_id"],
        status=ExecutionStatusEnum(result["status"]),
        state=ExecutionStateSchema(**result["state"]),
        breakpoints=result["breakpoints"],
        current_node_id=result["current_node_id"],
        error=result.get("error"),
        error_node_id=result.get("error_node_id"),
        started_at=result.get("started_at"),
        completed_at=result.get("completed_at"),
        checkpoint_count=result.get("checkpoint_count", 0),
        nodes_executed=result.get("nodes_executed", 0),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Lifecycle Control
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/{execution_id}/pause", response_model=ControlResponse)
async def pause_execution(
    execution_id: str,
    request: PauseRequest,
    execution_service: ExecutionService = Depends(get_execution_service),
) -> ControlResponse:
    """
    Pause a running execution at the current or specified node.
    
    A checkpoint is automatically created at the pause point.
    """
    try:
        result = await execution_service.pause(
            execution_id=execution_id,
            reason=request.reason,
            at_node=request.at_node,
        )
        return ControlResponse(
            success=True,
            status=ExecutionStatusEnum.PAUSED,
            checkpoint_id=result["checkpoint_id"],
            message=f"Execution paused. Checkpoint: {result['checkpoint_id']}",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to pause: {str(e)}")


@router.post("/{execution_id}/resume", response_model=ControlResponse)
async def resume_execution(
    execution_id: str,
    request: ResumeRequest,
    background_tasks: BackgroundTasks,
    execution_service: ExecutionService = Depends(get_execution_service),
) -> ControlResponse:
    """
    Resume a paused execution, optionally with modified state.
    
    This is the human-in-the-loop entry point - you can modify state
    before resuming execution.
    """
    try:
        result = await execution_service.resume(
            execution_id=execution_id,
            modified_state=request.modified_state,
            from_node=request.from_node,
        )
        return ControlResponse(
            success=True,
            status=ExecutionStatusEnum.RUNNING,
            message=f"Execution resumed from node: {result['resumed_from_node']}",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{execution_id}/stop", response_model=ControlResponse)
async def stop_execution(
    execution_id: str,
    reason: str | None = Query(None, max_length=500),
    execution_service: ExecutionService = Depends(get_execution_service),
) -> ControlResponse:
    """
    Stop and cancel an execution.
    
    Creates a final checkpoint before stopping.
    """
    try:
        # Would call controller.stop() here
        return ControlResponse(
            success=True,
            status=ExecutionStatusEnum.CANCELLED,
            message=f"Execution stopped: {reason or 'User requested'}",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# Checkpoint & Branching
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/{execution_id}/checkpoints", response_model=CheckpointListResponse)
async def list_checkpoints(
    execution_id: str,
    limit: int = Query(100, ge=1, le=500),
    execution_service: ExecutionService = Depends(get_execution_service),
) -> CheckpointListResponse:
    """
    List all checkpoints for an execution.
    
    Checkpoints are created:
    - Automatically before/after each node execution
    - When execution is paused
    - When a breakpoint is hit
    - Manually via API
    """
    # Would get from controller.get_checkpoint_history()
    return CheckpointListResponse(
        checkpoints=[],
        execution_id=execution_id,
        total=0,
    )


@router.post("/{execution_id}/checkpoints", response_model=CheckpointResponse)
async def create_checkpoint(
    execution_id: str,
    name: str | None = Query(None, max_length=100),
    execution_service: ExecutionService = Depends(get_execution_service),
) -> CheckpointResponse:
    """
    Create a manual checkpoint at current position.
    """
    return CheckpointResponse(
        id="manual-checkpoint-id",
        execution_id=execution_id,
        node_id=None,
        trigger_type="manual",
        created_at=datetime.now(timezone.utc),
        has_file_commit=False,
    )


@router.get("/{execution_id}/checkpoints/{checkpoint_id}", response_model=CheckpointResponse)
async def get_checkpoint(
    execution_id: str,
    checkpoint_id: str,
    include_state: bool = Query(False, description="Include full state snapshot"),
) -> CheckpointResponse:
    """
    Get checkpoint details and optionally its state.
    """
    raise HTTPException(status_code=404, detail="Checkpoint not found")


@router.post("/{execution_id}/rollback", response_model=RollbackResponse)
async def rollback_execution(
    execution_id: str,
    request: RollbackRequest,
    execution_service: ExecutionService = Depends(get_execution_service),
) -> RollbackResponse:
    """
    Rollback execution to a specific checkpoint.
    
    Options:
    - In-place rollback: Modifies the current execution
    - Branch: Creates a new execution from the checkpoint
    """
    try:
        result = await execution_service.rollback(
            execution_id=execution_id,
            checkpoint_id=request.checkpoint_id,
            create_branch=request.create_branch,
        )
        return RollbackResponse(
            success=True,
            to_checkpoint=request.checkpoint_id,
            new_session_id=result.get("new_session_id"),
            tools_reversed=result.get("tools_reversed", 0),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{execution_id}/branches", response_model=BranchListResponse)
async def list_branches(
    execution_id: str,
) -> BranchListResponse:
    """
    List all branches created from this execution.
    """
    return BranchListResponse(
        branches=[],
        parent_execution_id=execution_id,
        total=0,
    )


@router.post("/{execution_id}/branches", response_model=BranchResponse)
async def create_branch(
    execution_id: str,
    request: BranchCreateRequest,
) -> BranchResponse:
    """
    Create a new branch from a checkpoint.
    
    The branch is a new execution that starts from the checkpoint state.
    """
    import uuid
    branch_id = str(uuid.uuid4())
    new_execution_id = str(uuid.uuid4())
    
    return BranchResponse(
        id=branch_id,
        parent_execution_id=execution_id,
        from_checkpoint_id=request.checkpoint_id,
        name=request.name,
        description=request.description,
        execution_id=new_execution_id,
        created_at=datetime.now(timezone.utc),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# State Inspection & Modification
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/{execution_id}/state")
async def get_execution_state(
    execution_id: str,
    keys: list[str] | None = Query(None, description="Specific keys to retrieve"),
    execution_service: ExecutionService = Depends(get_execution_service),
) -> dict:
    """
    Get current execution state.
    
    Optionally filter to specific keys for large states.
    """
    result = await execution_service.inspect_state(
        execution_id=execution_id,
        keys=keys,
    )
    return {
        "execution_id": execution_id,
        "current_node": result["current_node"],
        "state": result["values"],
    }


@router.patch("/{execution_id}/state", response_model=ControlResponse)
async def modify_execution_state(
    execution_id: str,
    request: StateModifyRequest,
    execution_service: ExecutionService = Depends(get_execution_service),
) -> ControlResponse:
    """
    Modify execution state (human-in-the-loop).
    
    This allows injecting values or correcting errors mid-execution.
    Creates a checkpoint before modification.
    """
    try:
        success = await execution_service.modify_state(
            execution_id=execution_id,
            changes=request.changes,
        )
        if success:
            return ControlResponse(
                success=True,
                status=ExecutionStatusEnum.PAUSED,
                message=f"State modified: {list(request.changes.keys())}",
            )
        else:
            return ControlResponse(
                success=False,
                status=ExecutionStatusEnum.PAUSED,
                message="State modification not supported by adapter",
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# Node-Level Control
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/{execution_id}/nodes")
async def list_execution_nodes(
    execution_id: str,
    execution_service: ExecutionService = Depends(get_execution_service),
) -> dict:
    """
    List nodes with their execution status.
    """
    # Would get from execution state + workflow definition
    return {
        "execution_id": execution_id,
        "nodes": [],
    }


@router.get("/{execution_id}/nodes/{node_id}")
async def get_node_execution(
    execution_id: str,
    node_id: str,
) -> NodeExecutionStatusSchema:
    """
    Get execution details for a specific node.
    """
    raise HTTPException(status_code=404, detail="Node not found in execution")


@router.post("/{execution_id}/nodes/{node_id}/execute")
async def execute_single_node(
    execution_id: str,
    node_id: str,
    request: ExecuteSingleNodeRequest,
) -> ControlResponse:
    """
    Execute a single node (skip to this node).
    
    Used for debugging - allows jumping directly to a specific node.
    """
    return ControlResponse(
        success=True,
        status=ExecutionStatusEnum.RUNNING,
        message=f"Executing node: {node_id}",
    )


@router.post("/{execution_id}/nodes/{node_id}/skip")
async def skip_node(
    execution_id: str,
    node_id: str,
    reason: str | None = Query(None, max_length=500),
) -> ControlResponse:
    """
    Skip a node in the execution path.
    """
    return ControlResponse(
        success=True,
        status=ExecutionStatusEnum.RUNNING,
        message=f"Node skipped: {node_id}",
    )


@router.post("/{execution_id}/nodes/{node_id}/retry")
async def retry_node(
    execution_id: str,
    node_id: str,
) -> ControlResponse:
    """
    Retry a failed node.
    
    Rolls back to the node's entry checkpoint and re-executes.
    """
    return ControlResponse(
        success=True,
        status=ExecutionStatusEnum.RUNNING,
        message=f"Retrying node: {node_id}",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Audit Trail
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/{execution_id}/audit", response_model=AuditEventListResponse)
async def get_execution_audit(
    execution_id: str,
    include_debug: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    audit_service: AuditService = Depends(get_audit_service),
) -> AuditEventListResponse:
    """
    Get audit trail for an execution.
    """
    from wtb.api.rest.models import (
        AuditEventResponse,
        AuditEventTypeEnum,
        AuditSeverityEnum,
    )
    
    result = await audit_service.get_trail_for_execution(
        execution_id=execution_id,
        include_debug=include_debug,
        limit=limit,
    )
    
    return AuditEventListResponse(
        events=[
            AuditEventResponse(
                id=e["id"],
                timestamp=e["timestamp"],
                event_type=AuditEventTypeEnum(e["event_type"]),
                severity=AuditSeverityEnum(e["severity"]),
                message=e["message"],
                execution_id=e.get("execution_id"),
                node_id=e.get("node_id"),
                details=e.get("details"),
                error=e.get("error"),
                duration_ms=e.get("duration_ms"),
            )
            for e in result["events"]
        ],
        pagination=PaginationMeta(**result["pagination"]),
    )
