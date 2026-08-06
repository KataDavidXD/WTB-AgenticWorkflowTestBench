"""Base repository implementation with common functionality."""

import builtins
from typing import Generic, TypeVar

from sqlalchemy.orm import Session

T = TypeVar('T')
ORM = TypeVar('ORM')


class BaseRepository(Generic[T, ORM]):
    """
    Base repository with common CRUD operations.
    
    Subclasses must implement:
    - _to_domain(orm) -> T: Convert ORM model to domain model
    - _to_orm(domain) -> ORM: Convert domain model to ORM model
    """
    
    def __init__(self, session: Session, orm_class: type[ORM]):
        self._session = session
        self._orm_class = orm_class
    
    def _to_domain(self, orm: ORM) -> T:
        """Convert ORM model to domain model. Override in subclass."""
        raise NotImplementedError
    
    def _to_orm(self, domain: T) -> ORM:
        """Convert domain model to ORM model. Override in subclass."""
        raise NotImplementedError
    
    def get(self, id: str) -> T | None:
        """Get entity by ID."""
        orm = self._session.query(self._orm_class).filter_by(id=id).first()
        return self._to_domain(orm) if orm else None
    
    def list(self, limit: int = 100, offset: int = 0) -> list[T]:
        """List entities with pagination."""
        orms = (
            self._session.query(self._orm_class)
            .offset(offset)
            .limit(limit)
            .all()
        )
        return [self._to_domain(orm) for orm in orms]
    
    def list_all(self) -> builtins.list[T]:
        """List all entities without pagination. Use sparingly."""
        orms = self._session.query(self._orm_class).all()
        return [self._to_domain(orm) for orm in orms]
    
    def exists(self, id: str) -> bool:
        """Check if entity exists."""
        return (
            self._session.query(self._orm_class)
            .filter_by(id=id)
            .count() > 0
        )
    
    def add(self, entity: T) -> T:
        """Add a new entity."""
        orm = self._to_orm(entity)
        self._session.add(orm)
        self._session.flush()
        return self._to_domain(orm)
    
    def update(self, entity: T) -> T:
        """Update an existing entity."""
        orm = self._to_orm(entity)
        merged = self._session.merge(orm)
        self._session.flush()
        return self._to_domain(merged)
    
    def delete(self, id: str) -> bool:
        """Delete entity by ID."""
        orm = self._session.query(self._orm_class).filter_by(id=id).first()
        if orm:
            self._session.delete(orm)
            self._session.flush()
            return True
        return False

