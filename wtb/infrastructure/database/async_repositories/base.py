
"""
Base Async Repository Implementation.
"""

from typing import Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar('T')
ORM = TypeVar('ORM')


class BaseAsyncRepository(Generic[T, ORM]):
    """
    Base async repository with common CRUD operations.
    
    Subclasses must implement:
    - _to_domain(orm) -> T: Convert ORM model to domain model
    - _to_orm(domain) -> ORM: Convert domain model to ORM model
    """
    
    def __init__(self, session: AsyncSession, orm_class: type[ORM]):
        self._session = session
        self._orm_class = orm_class
    
    def _to_domain(self, orm: ORM) -> T:
        """Convert ORM model to domain model. Override in subclass."""
        raise NotImplementedError
    
    def _to_orm(self, domain: T) -> ORM:
        """Convert domain model to ORM model. Override in subclass."""
        raise NotImplementedError
    
    async def aget(self, id: str) -> T | None:
        """Get entity by ID asynchronously."""
        orm = await self._session.get(self._orm_class, id)
        return self._to_domain(orm) if orm else None
    
    async def alist(self, limit: int = 100, offset: int = 0) -> list[T]:
        """List entities with pagination asynchronously."""
        stmt = select(self._orm_class).limit(limit).offset(offset)
        result = await self._session.execute(stmt)
        return [self._to_domain(orm) for orm in result.scalars().all()]
    
    async def aexists(self, id: str) -> bool:
        """Check if entity exists asynchronously."""
        orm = await self._session.get(self._orm_class, id)
        return orm is not None
    
    async def aadd(self, entity: T) -> T:
        """Add a new entity asynchronously."""
        orm = self._to_orm(entity)
        self._session.add(orm)
        # We don't flush here to allow UoW to manage transaction
        return entity
    
    async def aupdate(self, entity: T) -> T:
        """
        Update an existing entity asynchronously.
        
        Note: This assumes the entity ID is set. 
        Uses merge behavior or explicit update depending on implementation.
        AsyncSession.merge is awaitable in newer SQLAlchemy versions.
        """
        orm = self._to_orm(entity)
        merged = await self._session.merge(orm)
        return self._to_domain(merged)
    
    async def adelete(self, id: str) -> bool:
        """Delete entity by ID asynchronously."""
        orm = await self._session.get(self._orm_class, id)
        if orm:
            await self._session.delete(orm)
            return True
        return False
