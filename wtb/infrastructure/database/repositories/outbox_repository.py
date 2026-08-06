"""
SQLAlchemy Outbox Repository Implementation.

Implements IOutboxRepository for persistent outbox event storage.

Updated: 2026-01-28 - Refactored to use OutboxMapper for shared logic (ISSUE-FS-002)
"""

from datetime import datetime

from sqlalchemy import and_
from sqlalchemy.orm import Session

from wtb.domain.interfaces.repositories import IOutboxRepository
from wtb.domain.models.outbox import OutboxEvent, OutboxStatus
from wtb.infrastructure.database.mappers import OutboxMapper
from wtb.infrastructure.database.models import OutboxEventORM


class SQLAlchemyOutboxRepository(IOutboxRepository):
    """
    SQLAlchemy implementation of IOutboxRepository.
    
    Stores outbox events in wtb_outbox table for cross-database
    transaction consistency via the Outbox Pattern.
    
    DRY: Uses shared OutboxMapper for domain ↔ ORM conversion.
    """
    
    def __init__(self, session: Session):
        self._session = session
    
    def _to_domain(self, orm: OutboxEventORM) -> OutboxEvent:
        """Convert ORM model to domain model using shared mapper."""
        return OutboxMapper.to_domain(orm)
    
    def _to_orm(self, event: OutboxEvent) -> OutboxEventORM:
        """Convert domain model to ORM model using shared mapper."""
        orm_dict = OutboxMapper.to_orm_dict(event)
        return OutboxEventORM(**orm_dict)
    
    def add(self, event: OutboxEvent) -> OutboxEvent:
        """Add a new outbox event."""
        orm = self._to_orm(event)
        self._session.add(orm)
        self._session.flush()
        return self._to_domain(orm)
    
    def get_by_id(self, event_id: str) -> OutboxEvent | None:
        """Get event by event_id (UUID)."""
        orm = (
            self._session.query(OutboxEventORM)
            .filter(OutboxEventORM.event_id == event_id)
            .first()
        )
        return self._to_domain(orm) if orm else None
    
    def get_by_pk(self, id: int) -> OutboxEvent | None:
        """Get event by database primary key."""
        orm = (
            self._session.query(OutboxEventORM)
            .filter(OutboxEventORM.id == id)
            .first()
        )
        return self._to_domain(orm) if orm else None
    
    def get_pending(self, limit: int = 100) -> list[OutboxEvent]:
        """Get pending events for processing, ordered by created_at."""
        orms = (
            self._session.query(OutboxEventORM)
            .filter(OutboxEventORM.status == OutboxStatus.PENDING.value)
            .order_by(OutboxEventORM.created_at.asc())
            .limit(limit)
            .all()
        )
        return [self._to_domain(orm) for orm in orms]
    
    def get_failed_for_retry(self, limit: int = 50) -> list[OutboxEvent]:
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
    
    def list_all(self, limit: int = 100) -> list[OutboxEvent]:
        """List all events (for admin/debugging)."""
        orms = (
            self._session.query(OutboxEventORM)
            .order_by(OutboxEventORM.created_at.desc())
            .limit(limit)
            .all()
        )
        return [self._to_domain(orm) for orm in orms]

