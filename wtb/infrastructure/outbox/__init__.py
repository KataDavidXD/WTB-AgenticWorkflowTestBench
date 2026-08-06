"""
Outbox Pattern Infrastructure - Processor, Lifecycle, and Handlers.

v1.7 ISS-006 Resolution: Added lifecycle management with:
- OutboxLifecycleManager: Managed lifecycle with auto-start, health, graceful shutdown
- create_managed_processor: Factory for production use
"""

from .lifecycle import (
    HealthStatus,
    LifecycleStatus,
    OutboxLifecycleManager,
    create_managed_processor,
)
from .processor import OutboxProcessor

__all__ = [
    "OutboxProcessor",
    "OutboxLifecycleManager",
    "LifecycleStatus",
    "HealthStatus",
    "create_managed_processor",
]

