"""
Audit trail endpoints.

Provides:
- Query audit events with filtering
- Audit summary statistics
- Execution timeline visualization
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query

from wtb.api.rest.dependencies import AuditService, get_audit_service
from wtb.api.rest.models import (
    AuditEventListResponse,
    AuditEventResponse,
    AuditEventTypeEnum,
    AuditSeverityEnum,
    AuditSummaryResponse,
    AuditTimelineEntry,
    AuditTimelineResponse,
    PaginationMeta,
)

router = APIRouter(prefix="/api/v1/audit", tags=["Audit"])


@router.get("/events", response_model=AuditEventListResponse)
async def list_audit_events(
    execution_id: str | None = Query(None, description="Filter by execution ID"),
    event_type: list[AuditEventTypeEnum] | None = Query(None, description="Filter by event type"),
    severity: list[AuditSeverityEnum] | None = Query(None, description="Filter by severity"),
    node_id: str | None = Query(None, description="Filter by node ID"),
    since: datetime | None = Query(None, description="Events since this time"),
    until: datetime | None = Query(None, description="Events until this time"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    audit_service: AuditService = Depends(get_audit_service),
) -> AuditEventListResponse:
    """
    Query audit events with filtering.
    
    Supports filtering by:
    - execution_id: Specific execution
    - event_type: EXECUTION_STARTED, NODE_COMPLETED, CHECKPOINT_CREATED, etc.
    - severity: info, success, warning, error, debug
    - node_id: Specific node
    - time range: since/until
    
    Results are ordered by timestamp (newest first).
    """
    # Convert enums to string values for service
    event_type_values = [e.value for e in event_type] if event_type else None
    severity_values = [s.value for s in severity] if severity else None
    
    result = await audit_service.query_events(
        execution_id=execution_id,
        event_types=event_type_values,
        severities=severity_values,
        node_id=node_id,
        since=since.isoformat() if since else None,
        until=until.isoformat() if until else None,
        limit=limit,
        offset=offset,
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


@router.get("/summary", response_model=AuditSummaryResponse)
async def get_audit_summary(
    execution_id: str | None = Query(None, description="Filter by execution ID"),
    time_range: str = Query("1h", description="Time range: 1h, 6h, 24h, 7d"),
    audit_service: AuditService = Depends(get_audit_service),
) -> AuditSummaryResponse:
    """
    Get audit summary statistics.
    
    Returns:
    - Total events by type
    - Error rate
    - Checkpoint frequency
    - Rollback count
    - Average execution duration
    
    Time range options:
    - 1h: Last hour
    - 6h: Last 6 hours
    - 24h: Last 24 hours
    - 7d: Last 7 days
    """
    result = await audit_service.get_summary(
        execution_id=execution_id,
        time_range=time_range,
    )
    
    return AuditSummaryResponse(**result)


@router.get("/timeline/{execution_id}", response_model=AuditTimelineResponse)
async def get_execution_timeline(
    execution_id: str,
    include_debug: bool = Query(False, description="Include debug events"),
    audit_service: AuditService = Depends(get_audit_service),
) -> AuditTimelineResponse:
    """
    Get visual timeline data for an execution.
    
    Returns structured data suitable for timeline visualization:
    - Nodes with start/end times
    - Checkpoints
    - Errors
    - State changes
    
    This data can be used to render a Gantt-style timeline
    showing the execution flow.
    """
    result = await audit_service.get_timeline(
        execution_id=execution_id,
        include_debug=include_debug,
    )
    
    return AuditTimelineResponse(
        execution_id=execution_id,
        entries=[
            AuditTimelineEntry(
                timestamp=e["timestamp"],
                event_type=e["event_type"],
                node_id=e.get("node_id"),
                duration_ms=e.get("duration_ms"),
                status=e["status"],
            )
            for e in result["entries"]
        ],
        total_duration_ms=result.get("total_duration_ms"),
    )
