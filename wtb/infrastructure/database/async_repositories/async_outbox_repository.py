"""
Async Outbox Repository Implementation.

Uses shared OutboxMapper for consistent domain ↔ ORM conversion.
Updated: 2026-01-28 - Refactored to use OutboxMapper (ISSUE-FS-002)
"""

from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from wtb.domain.interfaces.async_repositories import IAsyncOutboxRepository
from wtb.domain.models.outbox import OutboxEvent
from wtb.infrastructure.database.mappers import OutboxMapper
from wtb.infrastructure.database.models import OutboxEventORM


class AsyncOutboxRepository(IAsyncOutboxRepository):
    """
    Async Outbox Repository implementation.
    
    DRY: Uses shared OutboxMapper for domain ↔ ORM conversion.
    """
    
    def __init__(self, session: AsyncSession):
        self._session = session

    async def aadd(self, event: OutboxEvent) -> OutboxEvent:
        """Add outbox event using shared mapper."""
        # Convert domain to ORM using shared mapper
        orm_dict = OutboxMapper.to_orm_dict(event)
        # Ensure event_id is set
        if not orm_dict.get("event_id"):
            orm_dict["event_id"] = str(datetime.now().timestamp())
        # Ensure created_at is set
        if not orm_dict.get("created_at"):
            orm_dict["created_at"] = datetime.now()
            
        orm = OutboxEventORM(**orm_dict)
        self._session.add(orm)
        # We don't flush to keep in transaction
        return event

    async def aget_pending(
        self,
        limit: int = 100,
        order_by: str = "created_at",
    ) -> list[OutboxEvent]:
        """
        Get pending events in FIFO order.
        """
        # status == 'pending' - assuming case matches
        stmt = (
            select(OutboxEventORM)
            .where(OutboxEventORM.status == 'pending')
            .order_by(getattr(OutboxEventORM, order_by)) # Dangerous if input unchecked, but internal use
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [self._to_domain(orm) for orm in result.scalars().all()]

    async def aupdate(self, event: OutboxEvent) -> OutboxEvent:
        """Update outbox event status."""
        if not event.event_id:
            raise ValueError("Event ID required for update")
            
        stmt = select(OutboxEventORM).where(OutboxEventORM.event_id == event.event_id)
        result = await self._session.execute(stmt)
        orm = result.scalar_one_or_none()
        
        if orm:
            orm.status = event.status.value if hasattr(event.status, 'value') else event.status
            orm.retry_count = event.retry_count
            orm.last_error = event.last_error
            orm.processed_at = event.processed_at
        
        return event

    async def adelete_processed(self, older_than_hours: int = 24) -> int:
        """Delete processed events older than threshold."""
        cutoff = datetime.now() - timedelta(hours=older_than_hours)
        stmt = (
            delete(OutboxEventORM)
            .where(OutboxEventORM.status == 'processed')
            .where(OutboxEventORM.processed_at < cutoff)
        )
        result = await self._session.execute(stmt)
        return result.rowcount or 0

    def _to_domain(self, orm: OutboxEventORM) -> OutboxEvent:
        """Convert ORM to domain using shared mapper."""
        return OutboxMapper.to_domain(orm)
