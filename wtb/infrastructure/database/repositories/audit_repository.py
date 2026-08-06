"""
SQLAlchemy Audit Log Repository Implementation.
"""

import json

from sqlalchemy import desc
from sqlalchemy.orm import Session

from wtb.domain.interfaces.repositories import IAuditLogRepository
from wtb.domain.models.audit import AuditEntry
from wtb.infrastructure.database.models import AuditLogORM

from .base import BaseRepository


class SQLAlchemyAuditLogRepository(BaseRepository[AuditEntry, AuditLogORM], IAuditLogRepository):
    """
    SQLAlchemy implementation of Audit Log Repository.
    """
    
    def __init__(self, session: Session):
        super().__init__(session, AuditLogORM)
    
    def _to_domain(self, orm: AuditLogORM) -> AuditEntry:
        """Convert ORM to domain entity."""
        return AuditEntry(
            id=str(orm.id) if orm.id is not None else None,
            timestamp=orm.timestamp,
            event_type=orm.event_type,
            message=orm.message,
            execution_id=orm.execution_id,
            node_id=orm.node_id,
            payload=json.loads(orm.details) if orm.details else {},
            severity=orm.severity,
            error=orm.error,
            duration_ms=orm.duration_ms,
        )
    
    def _to_orm(self, entity: AuditEntry) -> AuditLogORM:
        """Convert domain entity to ORM."""
        return AuditLogORM(
            execution_id=entity.execution_id,
            node_id=entity.node_id,
            timestamp=entity.timestamp,
            event_type=entity.event_type,
            severity=entity.severity,
            message=entity.message,
            details=json.dumps(entity.payload) if entity.payload else None,
            error=entity.error,
            duration_ms=entity.duration_ms,
        )
    
    def append_logs(self, execution_id: str, logs: list[AuditEntry]) -> None:
        """
        Append a batch of logs for an execution.
        
        Args:
            execution_id: Execution identifier
            logs: List of audit entries to append
        """
        orms = []
        for log in logs:
            # Ensure execution_id matches
            if not log.execution_id:
                log.execution_id = execution_id
            
            orms.append(self._to_orm(log))
        
        self._session.add_all(orms)
    
    def find_by_execution(self, execution_id: str) -> list[AuditEntry]:
        """
        Get all logs for an execution.
        
        Args:
            execution_id: Execution identifier
            
        Returns:
            List of audit entries
        """
        orms = (
            self._session.query(AuditLogORM)
            .filter(AuditLogORM.execution_id == execution_id)
            .order_by(AuditLogORM.timestamp)
            .all()
        )
        return [self._to_domain(orm) for orm in orms]
    
    def get(self, id: str) -> AuditEntry | None:
        """Get by ID (not typically used for logs, but required by interface)."""
        orm = self._session.query(AuditLogORM).filter(AuditLogORM.id == int(id)).first()
        return self._to_domain(orm) if orm else None
    
    def exists(self, id: str) -> bool:
        """Check if exists."""
        return self._session.query(AuditLogORM).filter(AuditLogORM.id == int(id)).count() > 0
    
    def list(self, limit: int = 100, offset: int = 0) -> list[AuditEntry]:
        """List logs (recent first)."""
        orms = (
            self._session.query(AuditLogORM)
            .order_by(desc(AuditLogORM.timestamp))
            .limit(limit)
            .offset(offset)
            .all()
        )
        return [self._to_domain(orm) for orm in orms]
    
    def add(self, entity: AuditEntry) -> AuditEntry:
        """Add single log."""
        orm = self._to_orm(entity)
        self._session.add(orm)
        self._session.flush()
        return self._to_domain(orm)
    
    def update(self, entity: AuditEntry) -> AuditEntry:
        """Update log (not supported/needed)."""
        raise NotImplementedError("Audit logs are immutable")
    
    def delete(self, id: str) -> bool:
        """Delete log (not supported/needed)."""
        raise NotImplementedError("Audit logs are immutable")

