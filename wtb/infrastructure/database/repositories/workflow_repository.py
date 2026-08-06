"""Workflow repository implementation."""

import json

from sqlalchemy.orm import Session

from wtb.domain.interfaces.repositories import IWorkflowRepository
from wtb.domain.models import TestWorkflow

from ..models import WorkflowORM
from .base import BaseRepository


class WorkflowRepository(BaseRepository[TestWorkflow, WorkflowORM], IWorkflowRepository):
    """SQLAlchemy implementation of workflow repository."""
    
    def __init__(self, session: Session):
        super().__init__(session, WorkflowORM)
    
    def _to_domain(self, orm: WorkflowORM) -> TestWorkflow:
        """Convert ORM to domain model."""
        definition = json.loads(orm.definition) if orm.definition else {}
        metadata = json.loads(orm.metadata_) if orm.metadata_ else {}
        
        workflow = TestWorkflow.from_dict({
            "id": orm.id,
            "name": orm.name,
            "description": orm.description or "",
            "version": orm.version,
            "created_at": orm.created_at.isoformat() if orm.created_at else None,
            "updated_at": orm.updated_at.isoformat() if orm.updated_at else None,
            "metadata": metadata,
            **definition
        })
        return workflow
    
    def _to_orm(self, domain: TestWorkflow) -> WorkflowORM:
        """Convert domain model to ORM."""
        workflow_dict = domain.to_dict()
        
        # Extract workflow graph definition
        definition = {
            "nodes": workflow_dict.get("nodes", {}),
            "edges": workflow_dict.get("edges", []),
            "entry_point": workflow_dict.get("entry_point"),
        }
        
        return WorkflowORM(
            id=domain.id,
            name=domain.name,
            description=domain.description,
            definition=json.dumps(definition),
            version=domain.version,
            created_at=domain.created_at,
            updated_at=domain.updated_at,
            metadata_=json.dumps(domain.metadata),
        )
    
    def find_by_name(self, name: str) -> TestWorkflow | None:
        """Find workflow by name."""
        orm = (
            self._session.query(WorkflowORM)
            .filter_by(name=name)
            .first()
        )
        return self._to_domain(orm) if orm else None
    
    def find_by_version(self, name: str, version: str) -> TestWorkflow | None:
        """Find workflow by name and version."""
        orm = (
            self._session.query(WorkflowORM)
            .filter_by(name=name, version=version)
            .first()
        )
        return self._to_domain(orm) if orm else None
    
    def list_all(self) -> list[TestWorkflow]:
        """List all workflows without pagination."""
        orms = self._session.query(WorkflowORM).all()
        return [self._to_domain(orm) for orm in orms]

