"""
Batch test management endpoints.

Provides:
- Create and manage batch tests
- Track progress
- Get comparison results
- Ray integration for distributed execution
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from wtb.api.rest.dependencies import BatchTestService, get_batch_test_service
from wtb.api.rest.models import (
    BatchTestCreateRequest,
    BatchTestListResponse,
    BatchTestProgressSchema,
    BatchTestResponse,
    BatchTestStatusEnum,
    ComparisonMatrixResponse,
    PaginationMeta,
    VariantResultSchema,
)

router = APIRouter(prefix="/api/v1/batch-tests", tags=["Batch Tests"])


@router.get("", response_model=BatchTestListResponse)
async def list_batch_tests(
    workflow_id: str | None = Query(None, description="Filter by workflow ID"),
    status: BatchTestStatusEnum | None = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    batch_test_service: BatchTestService = Depends(get_batch_test_service),
) -> BatchTestListResponse:
    """
    List batch tests with optional filtering.
    """
    # Would query from repository
    return BatchTestListResponse(
        batch_tests=[],
        pagination=PaginationMeta(
            total=0,
            limit=limit,
            offset=offset,
            has_more=False,
        ),
    )


@router.post("", response_model=BatchTestResponse, status_code=201)
async def create_batch_test(
    request: BatchTestCreateRequest,
    batch_test_service: BatchTestService = Depends(get_batch_test_service),
) -> BatchTestResponse:
    """
    Create and start a batch test.
    
    A batch test runs a workflow with multiple variant combinations
    and collects comparison metrics.
    
    Options:
    - use_ray: Use Ray for distributed execution (recommended for large tests)
    - parallelism: Number of parallel workers (0=auto)
    - enable_file_tracking: Track file changes per variant
    """
    import uuid
    batch_test_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    
    # Start the batch test
    await batch_test_service.create_batch_test(
        workflow_id=request.workflow_id,
        variants=[v.model_dump() for v in request.variants],
        initial_state=request.initial_state,
        parallelism=request.parallelism,
        use_ray=request.use_ray,
    )
    
    return BatchTestResponse(
        id=batch_test_id,
        workflow_id=request.workflow_id,
        status=BatchTestStatusEnum.RUNNING,
        variant_count=len(request.variants),
        created_at=now,
        started_at=now,
        use_ray=request.use_ray,
    )


@router.get("/{batch_test_id}", response_model=BatchTestResponse)
async def get_batch_test(
    batch_test_id: str,
    batch_test_service: BatchTestService = Depends(get_batch_test_service),
) -> BatchTestResponse:
    """
    Get batch test status and progress.
    """
    result = await batch_test_service.get_batch_test(batch_test_id)
    if not result:
        raise HTTPException(status_code=404, detail="Batch test not found")
    
    return BatchTestResponse(
        id=result["id"],
        workflow_id=result["workflow_id"],
        status=BatchTestStatusEnum(result["status"]),
        variant_count=len(result["variants"]),
        created_at=result.get("created_at", datetime.now(timezone.utc)),
        use_ray=result.get("use_ray", True),
    )


@router.get("/{batch_test_id}/progress", response_model=BatchTestProgressSchema)
async def get_batch_test_progress(
    batch_test_id: str,
    batch_test_service: BatchTestService = Depends(get_batch_test_service),
) -> BatchTestProgressSchema:
    """
    Get current progress of a batch test.
    
    Returns:
    - Total variants
    - Completed/failed/pending counts
    - Current variant being executed
    - ETA
    """
    result = await batch_test_service.get_batch_test(batch_test_id)
    if not result:
        raise HTTPException(status_code=404, detail="Batch test not found")
    
    return BatchTestProgressSchema(
        batch_test_id=batch_test_id,
        total_variants=len(result.get("variants", [])),
        completed=result.get("completed", 0),
        failed=result.get("failed", 0),
        pending=result.get("pending", 0),
        current_variant=result.get("current_variant"),
        current_progress_percent=result.get("progress_percent", 0.0),
        eta_seconds=result.get("eta_seconds"),
    )


@router.get("/{batch_test_id}/results", response_model=ComparisonMatrixResponse)
async def get_batch_test_results(
    batch_test_id: str,
    batch_test_service: BatchTestService = Depends(get_batch_test_service),
) -> ComparisonMatrixResponse:
    """
    Get comparison matrix for completed batch test.
    
    Returns:
    - Metric names
    - Results per variant
    - Best performing variant
    """
    result = await batch_test_service.get_batch_test(batch_test_id)
    if not result:
        raise HTTPException(status_code=404, detail="Batch test not found")
    
    # Would get from actual results
    return ComparisonMatrixResponse(
        metric_names=["duration_ms", "accuracy", "memory_usage"],
        variants=[],
        best_variant=None,
        best_score=None,
    )


@router.post("/{batch_test_id}/stop")
async def stop_batch_test(
    batch_test_id: str,
    reason: str | None = Query(None, max_length=500),
    batch_test_service: BatchTestService = Depends(get_batch_test_service),
) -> dict:
    """
    Stop a running batch test.
    
    Cancels all pending variant executions.
    Completed variants retain their results.
    """
    result = await batch_test_service.get_batch_test(batch_test_id)
    if not result:
        raise HTTPException(status_code=404, detail="Batch test not found")
    
    return {
        "success": True,
        "batch_test_id": batch_test_id,
        "status": "cancelled",
        "message": f"Batch test stopped: {reason or 'User requested'}",
    }


@router.get("/{batch_test_id}/variants/{variant_name}")
async def get_variant_result(
    batch_test_id: str,
    variant_name: str,
    batch_test_service: BatchTestService = Depends(get_batch_test_service),
) -> VariantResultSchema:
    """
    Get detailed result for a specific variant.
    """
    result = await batch_test_service.get_batch_test(batch_test_id)
    if not result:
        raise HTTPException(status_code=404, detail="Batch test not found")
    
    raise HTTPException(status_code=404, detail="Variant not found")


@router.get("/{batch_test_id}/stream")
async def stream_batch_test_progress(
    batch_test_id: str,
    batch_test_service: BatchTestService = Depends(get_batch_test_service),
) -> StreamingResponse:
    """
    Stream batch test progress updates (Server-Sent Events).
    
    Use this for real-time progress monitoring in UI.
    """
    import asyncio
    import json
    
    async def event_generator():
        """Generate SSE events for progress updates."""
        result = await batch_test_service.get_batch_test(batch_test_id)
        if not result:
            yield f"event: error\ndata: {json.dumps({'error': 'Batch test not found'})}\n\n"
            return
        
        # Stream progress updates
        async for progress in batch_test_service.stream_progress(batch_test_id):
            data = json.dumps({
                "batch_test_id": batch_test_id,
                "total_variants": progress["total"],
                "completed": progress["completed"],
                "failed": progress["failed"],
                "current_variant": progress.get("current"),
                "eta_seconds": progress.get("eta_seconds"),
            })
            yield f"data: {data}\n\n"
            await asyncio.sleep(0.1)  # Prevent tight loop
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
