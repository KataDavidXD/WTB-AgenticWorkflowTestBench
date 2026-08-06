"""
Node boundary repository implementation.

Updated 2026-01-15 for DDD compliance:
- Changed from internal_session_id to execution_id
- Removed tool_count and checkpoint_count
- Checkpoint IDs are Optional[str] (LangGraph UUID format)

Updated 2026-01-28 (ARCHITECTURE_REVIEW - ISSUE-CP-002):
- Fixed: Now passes string checkpoint IDs to domain model
- Domain model expects Optional[str], not CheckpointId objects
- Use get_entry_checkpoint_id_value() / get_exit_checkpoint_id_value()
  on the domain model if you need CheckpointId value objects
"""

import builtins
import json

from sqlalchemy.orm import Session

from wtb.domain.interfaces.repositories import INodeBoundaryRepository
from wtb.domain.models import NodeBoundary

from ..models import NodeBoundaryORM


class NodeBoundaryRepository(INodeBoundaryRepository):
    """
    SQLAlchemy implementation of node boundary repository.
    
    Updated 2026-01-15 for DDD compliance:
    - Changed from internal_session_id to execution_id based queries
    - Checkpoint IDs are stored and returned as strings
    
    Updated 2026-01-28:
    - Fixed type mismatch: returns str checkpoint IDs, not CheckpointId
    """
    
    def __init__(self, session: Session):
        self._session = session
    
    def _to_domain(self, orm: NodeBoundaryORM) -> NodeBoundary:
        """
        Convert ORM to domain model.
        
        Note: Checkpoint IDs are passed as strings (Optional[str]).
        Use domain model's get_entry_checkpoint_id_value() / get_exit_checkpoint_id_value()
        methods if you need CheckpointId value objects.
        """
        error_details = json.loads(orm.error_details) if orm.error_details else None
        
        # 2026-01-28 FIX: Pass strings directly, not CheckpointId objects
        # Domain model expects Optional[str] for entry/exit checkpoint IDs
        entry_cp_id: str | None = str(orm.entry_checkpoint_id) if orm.entry_checkpoint_id else None
        exit_cp_id: str | None = str(orm.exit_checkpoint_id) if orm.exit_checkpoint_id else None
        
        return NodeBoundary(
            id=orm.id,
            execution_id=orm.execution_id,
            node_id=orm.node_id,
            entry_checkpoint_id=entry_cp_id,
            exit_checkpoint_id=exit_cp_id,
            node_status=orm.node_status,
            started_at=orm.started_at,
            completed_at=orm.completed_at,
            duration_ms=orm.duration_ms,
            error_message=orm.error_message,
            error_details=error_details,
        )
    
    def _to_orm(self, domain: NodeBoundary) -> NodeBoundaryORM:
        """Convert domain model to ORM."""
        # Handle checkpoint_id conversion (domain uses CheckpointId, ORM stores string/int)
        entry_cp = str(domain.entry_checkpoint_id) if domain.entry_checkpoint_id else None
        exit_cp = str(domain.exit_checkpoint_id) if domain.exit_checkpoint_id else None
        
        return NodeBoundaryORM(
            id=domain.id,
            execution_id=domain.execution_id,
            internal_session_id=0,  # Legacy field - kept for schema compatibility
            node_id=domain.node_id,
            entry_checkpoint_id=entry_cp,
            exit_checkpoint_id=exit_cp,
            node_status=domain.node_status,
            started_at=domain.started_at,
            completed_at=domain.completed_at,
            tool_count=0,  # Legacy field - kept for schema compatibility
            checkpoint_count=0,  # Legacy field - kept for schema compatibility
            duration_ms=domain.duration_ms,
            error_message=domain.error_message,
            error_details=json.dumps(domain.error_details) if domain.error_details else None,
        )
    
    def get(self, id: str) -> NodeBoundary | None:
        """Get by ID (id is actually int for this entity)."""
        orm = self._session.query(NodeBoundaryORM).filter_by(id=int(id)).first()
        return self._to_domain(orm) if orm else None
    
    def list(self, limit: int = 100, offset: int = 0) -> list[NodeBoundary]:
        """List with pagination."""
        orms = (
            self._session.query(NodeBoundaryORM)
            .offset(offset)
            .limit(limit)
            .all()
        )
        return [self._to_domain(orm) for orm in orms]
    
    def exists(self, id: str) -> bool:
        """Check existence."""
        return self._session.query(NodeBoundaryORM).filter_by(id=int(id)).count() > 0
    
    def add(self, entity: NodeBoundary) -> NodeBoundary:
        """Add new entity."""
        orm = self._to_orm(entity)
        # Don't set id if None (auto-increment)
        if entity.id is None:
            orm.id = None
        self._session.add(orm)
        self._session.flush()
        entity.id = orm.id
        return entity
    
    def update(self, entity: NodeBoundary) -> NodeBoundary:
        """Update existing entity."""
        orm = self._session.query(NodeBoundaryORM).filter_by(id=entity.id).first()
        if orm:
            # Handle checkpoint_id conversion
            orm.exit_checkpoint_id = str(entity.exit_checkpoint_id) if entity.exit_checkpoint_id else None
            orm.entry_checkpoint_id = str(entity.entry_checkpoint_id) if entity.entry_checkpoint_id else None
            orm.node_status = entity.node_status
            orm.completed_at = entity.completed_at
            orm.duration_ms = entity.duration_ms
            orm.error_message = entity.error_message
            orm.error_details = json.dumps(entity.error_details) if entity.error_details else None
            self._session.flush()
        return entity
    
    def delete(self, id: str) -> bool:
        """Delete entity."""
        orm = self._session.query(NodeBoundaryORM).filter_by(id=int(id)).first()
        if orm:
            self._session.delete(orm)
            self._session.flush()
            return True
        return False
    
    # ═══════════════════════════════════════════════════════════════════════════
    # New DDD-compliant methods (2026-01-15)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def find_by_execution(self, execution_id: str) -> builtins.list[NodeBoundary]:
        """Find all boundaries for an execution."""
        orms = (
            self._session.query(NodeBoundaryORM)
            .filter_by(execution_id=execution_id)
            .order_by(NodeBoundaryORM.started_at)
            .all()
        )
        return [self._to_domain(orm) for orm in orms]
    
    def find_by_execution_and_node(self, execution_id: str, node_id: str) -> NodeBoundary | None:
        """Find boundary for a specific node in an execution."""
        orm = (
            self._session.query(NodeBoundaryORM)
            .filter_by(execution_id=execution_id, node_id=node_id)
            .first()
        )
        return self._to_domain(orm) if orm else None
    
    def find_completed_by_execution(self, execution_id: str) -> builtins.list[NodeBoundary]:
        """Find completed node boundaries for an execution."""
        orms = (
            self._session.query(NodeBoundaryORM)
            .filter_by(execution_id=execution_id, node_status='completed')
            .order_by(NodeBoundaryORM.completed_at)
            .all()
        )
        return [self._to_domain(orm) for orm in orms]
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Legacy methods (deprecated, kept for backward compatibility)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def find_by_session(self, internal_session_id: int) -> builtins.list[NodeBoundary]:
        """DEPRECATED: Use find_by_execution() instead."""
        orms = (
            self._session.query(NodeBoundaryORM)
            .filter_by(internal_session_id=internal_session_id)
            .order_by(NodeBoundaryORM.started_at)
            .all()
        )
        return [self._to_domain(orm) for orm in orms]
    
    def find_by_node(self, internal_session_id: int, node_id: str) -> NodeBoundary | None:
        """DEPRECATED: Use find_by_execution_and_node() instead."""
        orm = (
            self._session.query(NodeBoundaryORM)
            .filter_by(internal_session_id=internal_session_id, node_id=node_id)
            .first()
        )
        return self._to_domain(orm) if orm else None
    
    def find_completed(self, internal_session_id: int) -> builtins.list[NodeBoundary]:
        """DEPRECATED: Use find_completed_by_execution() instead."""
        orms = (
            self._session.query(NodeBoundaryORM)
            .filter_by(internal_session_id=internal_session_id, node_status='completed')
            .order_by(NodeBoundaryORM.completed_at)
            .all()
        )
        return [self._to_domain(orm) for orm in orms]
