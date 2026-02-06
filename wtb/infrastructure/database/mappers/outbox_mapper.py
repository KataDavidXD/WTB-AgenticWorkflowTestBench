"""
Outbox Event Mapper - Shared domain ↔ ORM conversion.

Created: 2026-01-28
Reference: ARCHITECTURE_REVIEW_2026_01_28.md - ISSUE-FS-002

Purpose:
- Single source of truth for OutboxEvent ↔ OutboxEventORM conversion
- Used by both sync and async outbox repositories
- Ensures consistent serialization/deserialization
"""

import json
from datetime import datetime
from typing import TYPE_CHECKING

from wtb.domain.models.outbox import OutboxEvent, OutboxEventType, OutboxStatus

if TYPE_CHECKING:
    from wtb.infrastructure.database.models import OutboxEventORM


class OutboxMapper:
    """
    Mapper for OutboxEvent domain model ↔ OutboxEventORM.
    
    This class extracts the mapping logic that was duplicated between:
    - SQLAlchemyOutboxRepository (sync)
    - AsyncOutboxRepository (async)
    
    Usage:
        # In repository
        from wtb.infrastructure.database.mappers import OutboxMapper
        
        def _to_domain(self, orm):
            return OutboxMapper.to_domain(orm)
        
        def _to_orm(self, domain):
            return OutboxMapper.to_orm(domain, ORM_CLASS)
    """
    
    @staticmethod
    def to_domain(orm: "OutboxEventORM") -> OutboxEvent:
        """
        Convert ORM model to domain model.
        
        Args:
            orm: OutboxEventORM instance
            
        Returns:
            OutboxEvent domain model
        """
        # Parse payload from JSON string
        payload = {}
        if orm.payload:
            if isinstance(orm.payload, str):
                try:
                    payload = json.loads(orm.payload)
                except json.JSONDecodeError:
                    payload = {"raw": orm.payload}
            elif isinstance(orm.payload, dict):
                payload = orm.payload
        
        # Parse event_type enum
        try:
            event_type = OutboxEventType(orm.event_type)
        except ValueError:
            # Handle unknown event types gracefully
            event_type = OutboxEventType.CHECKPOINT_CREATE
        
        # Parse status enum
        try:
            status = OutboxStatus(orm.status)
        except ValueError:
            status = OutboxStatus.PENDING
        
        return OutboxEvent(
            id=orm.id,
            event_id=orm.event_id or "",
            event_type=event_type,
            aggregate_type=orm.aggregate_type or "",
            aggregate_id=orm.aggregate_id or "",
            payload=payload,
            idempotency_key=getattr(orm, 'idempotency_key', None),  # Support idempotency
            status=status,
            retry_count=orm.retry_count or 0,
            max_retries=orm.max_retries if hasattr(orm, 'max_retries') and orm.max_retries else 5,
            created_at=orm.created_at or datetime.now(),
            processed_at=orm.processed_at,
            last_error=orm.last_error,
        )
    
    @staticmethod
    def to_orm_dict(event: OutboxEvent) -> dict:
        """
        Convert domain model to ORM-compatible dictionary.
        
        Args:
            event: OutboxEvent domain model
            
        Returns:
            Dictionary suitable for creating ORM instance
            
        Note:
            Excludes 'id' when None to let SQLAlchemy auto-generate the primary key.
        """
        result = {
            "event_id": event.event_id,
            "event_type": event.event_type.value if hasattr(event.event_type, 'value') else str(event.event_type),
            "aggregate_type": event.aggregate_type,
            "aggregate_id": event.aggregate_id,
            "payload": OutboxMapper.serialize_payload(event.payload) if isinstance(event.payload, dict) else str(event.payload),
            "idempotency_key": event.idempotency_key,  # Support idempotent processing
            "status": event.status.value if hasattr(event.status, 'value') else str(event.status),
            "retry_count": event.retry_count,
            "max_retries": event.max_retries,
            "created_at": event.created_at,
            "processed_at": event.processed_at,
            "last_error": event.last_error,
        }
        # Only include id if it's set (for updates, not inserts)
        if event.id is not None:
            result["id"] = event.id
        return result
    
    @staticmethod
    def update_orm_from_event(orm: "OutboxEventORM", event: OutboxEvent) -> None:
        """
        Update ORM instance with values from domain event.
        
        Args:
            orm: OutboxEventORM instance to update
            event: OutboxEvent with new values
        """
        orm.status = event.status.value if hasattr(event.status, 'value') else str(event.status)
        orm.retry_count = event.retry_count
        orm.processed_at = event.processed_at
        orm.last_error = event.last_error
    
    @staticmethod
    def serialize_payload(payload: dict) -> str:
        """
        Serialize payload dict to JSON string.
        
        Handles special types that json.dumps can't handle directly:
        - datetime: ISO format string
        - Enums: their .value
        - Value Objects (BlobId, CheckpointId, etc.): their .value or str()
        - Objects with __dict__: as dict
        
        Design Note (Robustness Principle):
        While callers SHOULD pass primitive types in payloads, this serializer
        gracefully handles domain Value Objects to prevent runtime errors.
        """
        def default_serializer(obj):
            # datetime → ISO string
            if isinstance(obj, datetime):
                return obj.isoformat()
            # Value Objects with .value property (BlobId, CheckpointId, Enums, etc.)
            if hasattr(obj, 'value'):
                value = obj.value
                # Handle nested value objects
                if hasattr(value, 'value'):
                    return value.value
                return value
            # Objects with string representation (fallback for Value Objects)
            if hasattr(obj, '__str__') and type(obj).__str__ is not object.__str__:
                return str(obj)
            # Objects with __dict__ (dataclasses, etc.)
            if hasattr(obj, '__dict__'):
                return obj.__dict__
            # Final fallback
            return str(obj)
        
        return json.dumps(payload, default=default_serializer)
    
    @staticmethod
    def deserialize_payload(payload_str: str) -> dict:
        """
        Deserialize payload string to dict.
        
        Handles malformed JSON gracefully.
        """
        if not payload_str:
            return {}
        
        try:
            return json.loads(payload_str)
        except json.JSONDecodeError:
            return {"raw": payload_str}


__all__ = ["OutboxMapper"]
