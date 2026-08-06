"""
Health check endpoints.

Provides:
- /health - Overall health status
- /health/ready - Readiness check
- /health/live - Liveness check
"""

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter

from wtb.api.rest.dependencies import get_app_state
from wtb.api.rest.models import HealthResponse

router = APIRouter(prefix="/api/v1/health", tags=["Health"])


@router.get("", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """
    Overall health check.
    
    Returns status of all components.
    """
    state = get_app_state()
    
    components = {
        "api": "healthy",
        "event_bus": "healthy" if state.event_bus else "unhealthy",
    }
    
    if state.execution_controller:
        components["execution_controller"] = "healthy"
    else:
        components["execution_controller"] = "not_configured"
    
    # Determine overall status
    unhealthy_count = sum(1 for v in components.values() if v == "unhealthy")
    if unhealthy_count > 0:
        overall_status: Literal["healthy", "degraded", "unhealthy"] = "unhealthy"
    elif "not_configured" in components.values():
        overall_status = "degraded"
    else:
        overall_status = "healthy"
    
    return HealthResponse(
        status=overall_status,
        version="1.0.0",
        timestamp=datetime.now(timezone.utc),
        components=components,
    )


@router.get("/ready")
async def readiness_check() -> dict:
    """
    Readiness check for load balancers.
    
    Returns 200 if ready to receive traffic.
    """
    state = get_app_state()
    
    # Check if essential services are ready
    ready = state.is_initialized and state.event_bus is not None
    
    return {
        "ready": ready,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/live")
async def liveness_check() -> dict:
    """
    Liveness check for orchestrators.
    
    Returns 200 if process is alive.
    """
    return {
        "alive": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
