"""
REST API Routes.

Provides:
- /api/v1/workflows - Workflow management
- /api/v1/executions - Execution control
- /api/v1/audit - Audit trail access
- /api/v1/batch-tests - Batch test management
- /api/v1/health - Health checks
"""

from .audit import router as audit_router
from .batch_tests import router as batch_tests_router
from .executions import router as executions_router
from .health import router as health_router
from .workflows import router as workflows_router

__all__ = [
    "workflows_router",
    "executions_router",
    "audit_router",
    "batch_tests_router",
    "health_router",
]
