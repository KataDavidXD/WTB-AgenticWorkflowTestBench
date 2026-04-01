"""Domain audit log entry (persistence contract)."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


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
    execution_id: Optional[str] = None
    id: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    node_id: Optional[str] = None
    severity: str = "info"
    error: Optional[str] = None
    duration_ms: Optional[float] = None
