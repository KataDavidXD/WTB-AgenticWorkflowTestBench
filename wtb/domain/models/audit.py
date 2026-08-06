"""Domain audit log entry (persistence contract)."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class AuditEntry:
    """
    Single persisted audit row at the domain layer.

    Maps to infrastructure trail rows and database audit tables; event_type and
    severity are plain strings so the domain does not depend on infra enums.
    """

    timestamp: datetime
    event_type: str
    message: str
    execution_id: str | None = None
    id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    node_id: str | None = None
    severity: str = "info"
    error: str | None = None
    duration_ms: float | None = None
