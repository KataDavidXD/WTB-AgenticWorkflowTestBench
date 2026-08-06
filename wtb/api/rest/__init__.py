"""
REST API implementation using FastAPI.

Provides:
- Workflow management (CRUD)
- Execution control (start, pause, resume, stop, rollback)
- Audit trail access
- Batch test management
- Health checks

Note: FastAPI is an optional dependency. Models can be imported without FastAPI,
but create_app/get_app require FastAPI to be installed.
"""

# Lazy imports for FastAPI app functions (FastAPI is optional)
def create_app(*args, **kwargs):
    """Create FastAPI app. Requires fastapi to be installed."""
    from .app import create_app as _create_app
    return _create_app(*args, **kwargs)

def get_app(*args, **kwargs):
    """Get FastAPI app instance. Requires fastapi to be installed."""
    from .app import get_app as _get_app
    return _get_app(*args, **kwargs)

# Models can be imported without FastAPI (only need pydantic)
from .models import (
    AuditEventListResponse,
    # Audit models
    AuditEventResponse,
    AuditSummaryResponse,
    # Batch test models
    BatchTestCreateRequest,
    BatchTestListResponse,
    BatchTestResponse,
    CheckpointListResponse,
    # Checkpoint models
    CheckpointResponse,
    ComparisonMatrixResponse,
    # Common models
    ErrorResponse,
    # Execution models
    ExecutionCreateRequest,
    ExecutionListResponse,
    ExecutionResponse,
    HealthResponse,
    PauseRequest,
    ResumeRequest,
    RollbackRequest,
    StateModifyRequest,
    VariantCombinationRequest,
    # Workflow models
    WorkflowCreateRequest,
    WorkflowListResponse,
    WorkflowResponse,
    WorkflowUpdateRequest,
)

__all__ = [
    # App
    "create_app",
    "get_app",
    # Workflow models
    "WorkflowCreateRequest",
    "WorkflowUpdateRequest",
    "WorkflowResponse",
    "WorkflowListResponse",
    # Execution models
    "ExecutionCreateRequest",
    "ExecutionResponse",
    "ExecutionListResponse",
    "PauseRequest",
    "ResumeRequest",
    "RollbackRequest",
    "StateModifyRequest",
    # Checkpoint models
    "CheckpointResponse",
    "CheckpointListResponse",
    # Audit models
    "AuditEventResponse",
    "AuditEventListResponse",
    "AuditSummaryResponse",
    # Batch test models
    "BatchTestCreateRequest",
    "BatchTestResponse",
    "BatchTestListResponse",
    "VariantCombinationRequest",
    "ComparisonMatrixResponse",
    # Common models
    "ErrorResponse",
    "HealthResponse",
]
