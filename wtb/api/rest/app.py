"""
FastAPI Application Factory.

Creates and configures the main FastAPI application with:
- REST API routes
- WebSocket endpoint
- CORS middleware
- Exception handlers
- OpenTelemetry integration (optional)
- Prometheus metrics endpoint
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from wtb.api.rest.dependencies import AppState, get_app_state
from wtb.api.rest.models import ErrorResponse
from wtb.api.rest.routes import (
    audit_router,
    batch_tests_router,
    executions_router,
    health_router,
    workflows_router,
)

# Global app instance
_app: FastAPI | None = None


def create_app(
    title: str = "WTB API",
    version: str = "1.0.0",
    debug: bool = False,
    enable_cors: bool = True,
    cors_origins: list | None = None,
    enable_opentelemetry: bool = False,
    app_state: AppState | None = None,
) -> FastAPI:
    """
    Create and configure the FastAPI application.
    
    Args:
        title: API title for documentation
        version: API version
        debug: Enable debug mode
        enable_cors: Enable CORS middleware
        cors_origins: Allowed CORS origins (default: ["*"])
        enable_opentelemetry: Enable OpenTelemetry instrumentation
        app_state: Pre-configured application state
        
    Returns:
        Configured FastAPI application
    """
    
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Application lifespan handler."""
        # Startup
        state = app_state or get_app_state()
        if not state.is_initialized:
            state.initialize()
        
        # Import WebSocket handler
        
        yield
        
        # Shutdown
        # Cleanup resources
        pass
    
    app = FastAPI(
        title=title,
        description="""
## WTB (Workflow Test Bench) External API

A comprehensive API for controlling LangGraph workflow executions with:

- **Workflow Management**: CRUD operations for workflow definitions
- **Execution Control**: Fine-grained control (start, pause, resume, stop, rollback)
- **Batch Testing**: Variant A/B testing with comparison metrics
- **Audit Trail**: Complete execution history and observability
- **Real-time Updates**: WebSocket for live progress tracking

### API Protocols

| Protocol | Port | Use Case |
|----------|------|----------|
| REST | 8000 | Web UI, external clients, CRUD |
| WebSocket | 8000/ws | Real-time UI updates |
| gRPC | 50051 | Internal services, streaming |

### Authentication

All endpoints require authentication via JWT Bearer token.
See `/docs` for detailed API documentation.
        """,
        version=version,
        debug=debug,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Middleware
    # ═══════════════════════════════════════════════════════════════════════════
    
    if enable_cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins or ["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    
    # OpenTelemetry instrumentation
    if enable_opentelemetry:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            FastAPIInstrumentor.instrument_app(app)
        except ImportError:
            pass  # OpenTelemetry not installed
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Exception Handlers
    # ═══════════════════════════════════════════════════════════════════════════
    
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        """Handle HTTP exceptions with standard error format."""
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                error=f"HTTP_{exc.status_code}",
                message=exc.detail,
                timestamp=datetime.now(timezone.utc),
            ).model_dump(mode="json"),
        )
    
    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        """Handle value errors as bad requests."""
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                error="VALIDATION_ERROR",
                message=str(exc),
                timestamp=datetime.now(timezone.utc),
            ).model_dump(mode="json"),
        )
    
    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Handle unexpected exceptions."""
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error="INTERNAL_ERROR",
                message="An unexpected error occurred" if not debug else str(exc),
                details={"type": type(exc).__name__} if debug else None,
                timestamp=datetime.now(timezone.utc),
            ).model_dump(mode="json"),
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Routes
    # ═══════════════════════════════════════════════════════════════════════════
    
    # Include API routers
    app.include_router(health_router)
    app.include_router(workflows_router)
    app.include_router(executions_router)
    app.include_router(audit_router)
    app.include_router(batch_tests_router)
    
    # WebSocket endpoint
    from wtb.api.websocket.handlers import get_connection_manager, websocket_endpoint
    
    @app.websocket("/ws")
    async def websocket_route(websocket):
        """WebSocket endpoint for real-time updates."""
        await websocket_endpoint(websocket, get_connection_manager())
    
    # Prometheus metrics endpoint
    @app.get("/metrics")
    async def metrics_endpoint():
        """Prometheus metrics endpoint."""
        try:
            from fastapi.responses import Response
            from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
            return Response(
                content=generate_latest(),
                media_type=CONTENT_TYPE_LATEST,
            )
        except ImportError:
            return {"error": "prometheus_client not installed"}
    
    # Root endpoint
    @app.get("/")
    async def root():
        """Root endpoint with API info."""
        return {
            "name": title,
            "version": version,
            "docs": "/docs",
            "health": "/api/v1/health",
        }
    
    # Store in global
    global _app
    _app = app
    
    return app


def get_app() -> FastAPI:
    """Get the global FastAPI application instance."""
    global _app
    if _app is None:
        _app = create_app()
    return _app


# ═══════════════════════════════════════════════════════════════════════════════
# CLI Entry Point
# ═══════════════════════════════════════════════════════════════════════════════

def run_server(
    host: str = "0.0.0.0",
    port: int = 8000,
    reload: bool = False,
    workers: int = 1,
):
    """
    Run the API server.
    
    Args:
        host: Host to bind to
        port: Port to listen on
        reload: Enable auto-reload for development
        workers: Number of worker processes
    """
    import uvicorn
    
    uvicorn.run(
        "wtb.api.rest.app:get_app",
        host=host,
        port=port,
        reload=reload,
        workers=workers,
        factory=True,
    )


if __name__ == "__main__":
    run_server(reload=True)
