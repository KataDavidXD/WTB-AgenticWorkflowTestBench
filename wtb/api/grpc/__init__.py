"""
gRPC API for internal services and high-throughput communication.

Created: 2026-01-28
Status: Active

Provides:
- Execution control (start, pause, resume, stop, rollback)
- Event streaming (execution events, audit events)
- Batch test management with progress streaming
- Interactive debugging session (bidirectional)

Port: 50051 (default)

Usage:
    from wtb.api.grpc import create_grpc_server, WTBServicer
    
    servicer = WTBServicer(
        execution_service=execution_api_service,
        audit_service=audit_api_service,
        batch_test_service=batch_test_api_service,
    )
    server = create_grpc_server(servicer, port=50051)
    server.start()

Proto Compilation:
    python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. wtb_service.proto
"""

from .servicer import (
    GRPC_AVAILABLE,
    WTBServicer,
    create_async_grpc_server,
    create_grpc_server,
)

__all__ = [
    "WTBServicer",
    "create_grpc_server",
    "create_async_grpc_server",
    "GRPC_AVAILABLE",
]
