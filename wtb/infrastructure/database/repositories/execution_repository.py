"""Execution repository implementation."""

import json
from typing import Optional, List
from sqlalchemy.orm import Session

from wtb.domain.interfaces.repositories import IExecutionRepository
from wtb.domain.models import Execution, ExecutionState, ExecutionStatus
from ..models import ExecutionORM
from .base import BaseRepository


class ExecutionRepository(BaseRepository[Execution, ExecutionORM], IExecutionRepository):
    """SQLAlchemy implementation of execution repository."""
    
    def __init__(self, session: Session):
        super().__init__(session, ExecutionORM)
    
    def _to_domain(self, orm: ExecutionORM) -> Execution:
        """Convert ORM to domain model."""
        # Parse JSON fields
        current_state = json.loads(orm.current_state) if orm.current_state else {}
        execution_path = json.loads(orm.execution_path) if orm.execution_path else []
        breakpoints = json.loads(orm.breakpoints) if orm.breakpoints else []
        metadata = json.loads(orm.metadata_) if orm.metadata_ else {}
        
        state = ExecutionState(
            current_node_id=orm.current_node_id,
            workflow_variables=current_state.get("workflow_variables", {}),
            execution_path=execution_path,
            node_results=current_state.get("node_results", {}),
            node_boundaries=current_state.get("node_boundaries", {}),
        )
        
        # v1.6: ORM agentgit_* fields are now String(128), direct mapping
        return Execution(
            id=orm.id,
            workflow_id=orm.workflow_id,
            status=ExecutionStatus(orm.status),
            state=state,
            session_id=orm.agentgit_session_id,
            checkpoint_id=orm.agentgit_checkpoint_id,
            started_at=orm.started_at,
            completed_at=orm.completed_at,
            created_at=orm.created_at,
            error_message=orm.error_message,
            error_node_id=orm.error_node_id,
            breakpoints=breakpoints,
            metadata=metadata,
        )
    
    def _to_orm(self, domain: Execution) -> ExecutionORM:
        """Convert domain model to ORM."""
        current_state = {
            "workflow_variables": domain.state.workflow_variables,
            "node_results": domain.state.node_results,
            "node_boundaries": domain.state.node_boundaries,
        }
        
        # v1.6: ORM agentgit_* fields are now String(128), direct mapping
        return ExecutionORM(
            id=domain.id,
            workflow_id=domain.workflow_id,
            status=domain.status.value,
            current_node_id=domain.state.current_node_id,
            current_state=json.dumps(current_state),
            execution_path=json.dumps(domain.state.execution_path),
            agentgit_session_id=domain.session_id,
            agentgit_checkpoint_id=domain.checkpoint_id,
            started_at=domain.started_at,
            completed_at=domain.completed_at,
            created_at=domain.created_at,
            error_message=domain.error_message,
            error_node_id=domain.error_node_id,
            breakpoints=json.dumps(domain.breakpoints),
            metadata_=json.dumps(domain.metadata),
        )
    
    def find_by_workflow(self, workflow_id: str) -> List[Execution]:
        """Find all executions for a workflow."""
        orms = (
            self._session.query(ExecutionORM)
            .filter_by(workflow_id=workflow_id)
            .order_by(ExecutionORM.created_at.desc())
            .all()
        )
        return [self._to_domain(orm) for orm in orms]
    
    def find_by_status(self, status: ExecutionStatus) -> List[Execution]:
        """Find executions by status."""
        orms = (
            self._session.query(ExecutionORM)
            .filter_by(status=status.value)
            .order_by(ExecutionORM.created_at.desc())
            .all()
        )
        return [self._to_domain(orm) for orm in orms]
    
    def find_running(self) -> List[Execution]:
        """Find all currently running executions."""
        return self.find_by_status(ExecutionStatus.RUNNING)

