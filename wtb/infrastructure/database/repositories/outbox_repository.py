"""
SQLAlchemy Outbox Repository Implementation.

Implements IOutboxRepository for persistent outbox event storage.
"""

from typing import Optional, List
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import and_
import json

from wtb.domain.models.outbox import OutboxEvent, OutboxEventType, OutboxStatus
from wtb.domain.interfaces.repositories import IOutboxRepository
from wtb.infrastructure.database.models import OutboxEventORM


class SQLAlchemyOutboxRepository(IOutboxRepository):
    """
    SQLAlchemy implementation of IOutboxRepository.
    
    Stores outbox events in wtb_outbox table for cross-database
    transaction consistency via the Outbox Pattern.
    """
    
    def __init__(self, session: Session):
        self._session = session
    
    def _to_domain(self, orm: OutboxEventORM) -> OutboxEvent:
        """Convert ORM model to domain model."""
        return OutboxEvent(
            id=orm.id,
            event_id=orm.event_id,
            event_type=OutboxEventType(orm.event_type),
            aggregate_type=orm.aggregate_type,
            aggregate_id=orm.aggregate_id,
            payload=json.loads(orm.payload) if orm.payload else {},
            status=OutboxStatus(orm.status),
            retry_count=orm.retry_count,
            max_retries=orm.max_retries,
            created_at=orm.created_at,
            processed_at=orm.processed_at,
            last_error=orm.last_error,
        )
    
    def _to_orm(self, event: OutboxEvent) -> OutboxEventORM:
        """Convert domain model to ORM model."""
        return OutboxEventORM(
            id=event.id,
            event_id=event.event_id,
            event_type=event.event_type.value,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            payload=json.dumps(event.payload),
            status=event.status.value,
            retry_count=event.retry_count,
            max_retries=event.max_retries,
            created_at=event.created_at,
            processed_at=event.processed_at,
            last_error=event.last_error,
        )
    
    def add(self, event: OutboxEvent) -> OutboxEvent:
        """Add a new outbox event."""
        orm = self._to_orm(event)
        self._session.add(orm)
        self._session.flush()
        return self._to_domain(orm)
    
    def get_by_id(self, event_id: str) -> Optional[OutboxEvent]:
        """Get event by event_id (UUID)."""
        orm = (
            self._session.query(OutboxEventORM)
            .filter(OutboxEventORM.event_id == event_id)
            .first()
        )
        return self._to_domain(orm) if orm else None
    
    def get_by_pk(self, id: int) -> Optional[OutboxEvent]:
        """Get event by database primary key."""
        orm = (
            self._session.query(OutboxEventORM)
            .filter(OutboxEventORM.id == id)
            .first()
        )
        return self._to_domain(orm) if orm else None
    
    def get_pending(self, limit: int = 100) -> List[OutboxEvent]:
        """Get pending events for processing, ordered by created_at."""
        orms = (
            self._session.query(OutboxEventORM)
            .filter(OutboxEventORM.status == OutboxStatus.PENDING.value)
            .order_by(OutboxEventORM.created_at.asc())
            .limit(limit)
            .all()
        )
        return [self._to_domain(orm) for orm in orms]
    
    def get_failed_for_retry(self, limit: int = 50) -> List[OutboxEvent]:
        """Get failed events that can be retried."""
        orms = (
            self._session.query(OutboxEventORM)
            .filter(
                and_(
                    OutboxEventORM.status == OutboxStatus.FAILED.value,
                    OutboxEventORM.retry_count < OutboxEventORM.max_retries
                )
            )
            .order_by(OutboxEventORM.created_at.asc())
            .limit(limit)
            .all()
        )
        return [self._to_domain(orm) for orm in orms]
    
    def update(self, event: OutboxEvent) -> OutboxEvent:
        """Update event status."""
        orm = (
            self._session.query(OutboxEventORM)
            .filter(OutboxEventORM.id == event.id)
            .first()
        )
        if not orm:
            raise ValueError(f"OutboxEvent with id {event.id} not found")
        
        # Update fields
        orm.status = event.status.value
        orm.retry_count = event.retry_count
        orm.processed_at = event.processed_at
        orm.last_error = event.last_error
        
        self._session.flush()
        return self._to_domain(orm)
    
    def delete_processed(self, before: datetime, limit: int = 1000) -> int:
        """Delete processed events older than a given time."""
        count = (
            self._session.query(OutboxEventORM)
            .filter(
                and_(
                    OutboxEventORM.status == OutboxStatus.PROCESSED.value,
                    OutboxEventORM.processed_at < before
                )
            )
            .limit(limit)
            .delete(synchronize_session=False)
        )
        return count
    
    def list_all(self, limit: int = 100) -> List[OutboxEvent]:
        """List all events (for admin/debugging)."""
        orms = (
            self._session.query(OutboxEventORM)
            .order_by(OutboxEventORM.created_at.desc())
            .limit(limit)
            .all()
        )
        return [self._to_domain(orm) for orm in orms]

