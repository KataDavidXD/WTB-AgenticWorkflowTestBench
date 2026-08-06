"""Node variant repository implementation."""

import json

from sqlalchemy.orm import Session

from wtb.domain.interfaces.repositories import INodeVariantRepository
from wtb.domain.models import NodeVariant, WorkflowNode

from ..models import NodeVariantORM
from .base import BaseRepository


class NodeVariantRepository(BaseRepository[NodeVariant, NodeVariantORM], INodeVariantRepository):
    """SQLAlchemy implementation of node variant repository."""
    
    def __init__(self, session: Session):
        super().__init__(session, NodeVariantORM)
    
    def _to_domain(self, orm: NodeVariantORM) -> NodeVariant:
        """Convert ORM to domain model."""
        variant_def = json.loads(orm.variant_definition) if orm.variant_definition else {}
        metadata = json.loads(orm.metadata_) if orm.metadata_ else {}
        
        variant_node = WorkflowNode(
            id=variant_def.get("id", orm.original_node_id),
            name=variant_def.get("name", orm.variant_name),
            type=variant_def.get("type", "action"),
            tool_name=variant_def.get("tool_name"),
            config=variant_def.get("config", {}),
        )
        
        return NodeVariant(
            id=orm.id,
            workflow_id=orm.workflow_id,
            original_node_id=orm.original_node_id,
            variant_name=orm.variant_name,
            variant_node=variant_node,
            description=orm.description or "",
            is_active=orm.is_active,
            created_at=orm.created_at,
            metadata=metadata,
        )
    
    def _to_orm(self, domain: NodeVariant) -> NodeVariantORM:
        """
        Convert domain model to ORM.
        
        ARCHITECTURE NOTE (2026-01-17):
        - Callable implementations (lambdas, functions) cannot be serialized to JSON
        - We only persist JSON-safe config values
        - Implementations must be re-registered at startup (kept in memory)
        - This is consistent with how LangGraph nodes work (in-memory callables)
        """
        # Filter out non-serializable values from config
        safe_config = self._make_json_safe(domain.variant_node.config)
        
        variant_def = {
            "id": domain.variant_node.id,
            "name": domain.variant_node.name,
            "type": domain.variant_node.type,
            "tool_name": domain.variant_node.tool_name,
            "config": safe_config,
        }
        
        return NodeVariantORM(
            id=domain.id,
            workflow_id=domain.workflow_id,
            original_node_id=domain.original_node_id,
            variant_name=domain.variant_name,
            description=domain.description,
            variant_definition=json.dumps(variant_def),
            is_active=domain.is_active,
            created_at=domain.created_at,
            metadata_=json.dumps(self._make_json_safe(domain.metadata)),
        )
    
    def _make_json_safe(self, obj):
        """
        Recursively filter out non-serializable values.
        
        Callables (functions, lambdas) are replaced with placeholder strings.
        This allows the variant metadata to be persisted while noting that
        the actual implementation must be re-registered at runtime.
        """
        if obj is None:
            return None
        if isinstance(obj, (str, int, float, bool)):
            return obj
        if callable(obj):
            return f"<callable:{getattr(obj, '__name__', 'anonymous')}>"
        if isinstance(obj, dict):
            return {k: self._make_json_safe(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self._make_json_safe(item) for item in obj]
        # For other types, try str() as last resort
        try:
            json.dumps(obj)
            return obj
        except (TypeError, ValueError):
            return f"<non-serializable:{type(obj).__name__}>"
    
    def find_by_workflow(self, workflow_id: str) -> list[NodeVariant]:
        """Find all variants for a workflow."""
        orms = (
            self._session.query(NodeVariantORM)
            .filter_by(workflow_id=workflow_id)
            .all()
        )
        return [self._to_domain(orm) for orm in orms]
    
    def find_by_node(self, workflow_id: str, node_id: str) -> list[NodeVariant]:
        """Find variants for a specific node."""
        orms = (
            self._session.query(NodeVariantORM)
            .filter_by(workflow_id=workflow_id, original_node_id=node_id)
            .all()
        )
        return [self._to_domain(orm) for orm in orms]
    
    def find_active(self, workflow_id: str) -> list[NodeVariant]:
        """Find active variants for a workflow."""
        orms = (
            self._session.query(NodeVariantORM)
            .filter_by(workflow_id=workflow_id, is_active=True)
            .all()
        )
        return [self._to_domain(orm) for orm in orms]

