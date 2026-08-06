"""Batch test repository implementation."""

import json

from sqlalchemy.orm import Session

from wtb.domain.interfaces.repositories import IBatchTestRepository
from wtb.domain.models import (
    BatchTest,
    BatchTestResult,
    BatchTestStatus,
    VariantCombination,
)

from ..models import BatchTestORM
from .base import BaseRepository


class BatchTestRepository(BaseRepository[BatchTest, BatchTestORM], IBatchTestRepository):
    """SQLAlchemy implementation of batch test repository."""
    
    def __init__(self, session: Session):
        super().__init__(session, BatchTestORM)
    
    def _to_domain(self, orm: BatchTestORM) -> BatchTest:
        """Convert ORM to domain model."""
        # Parse JSON fields
        variant_combinations_data = json.loads(orm.variant_combinations) if orm.variant_combinations else []
        initial_state = json.loads(orm.initial_state) if orm.initial_state else {}
        execution_ids = json.loads(orm.execution_ids) if orm.execution_ids else []
        results_data = json.loads(orm.results) if orm.results else []
        comparison_matrix = json.loads(orm.comparison_matrix) if orm.comparison_matrix else None
        metadata = json.loads(orm.metadata_) if orm.metadata_ else {}
        
        # Convert variant combinations
        variant_combinations = [
            VariantCombination.from_dict(vc) for vc in variant_combinations_data
        ]
        
        # Convert results
        results = [BatchTestResult.from_dict(r) for r in results_data]
        
        return BatchTest(
            id=orm.id,
            name=orm.name,
            description=orm.description or "",
            workflow_id=orm.workflow_id,
            variant_combinations=variant_combinations,
            parallel_count=orm.parallel_count,
            initial_state=initial_state,
            status=BatchTestStatus(orm.status),
            created_at=orm.created_at,
            started_at=orm.started_at,
            completed_at=orm.completed_at,
            execution_ids=execution_ids,
            results=results,
            comparison_matrix=comparison_matrix,
            best_combination_name=orm.best_combination_name,
            metadata=metadata,
        )
    
    def _to_orm(self, domain: BatchTest) -> BatchTestORM:
        """Convert domain model to ORM."""
        variant_combinations = [vc.to_dict() for vc in domain.variant_combinations]
        results = [r.to_dict() for r in domain.results]
        
        return BatchTestORM(
            id=domain.id,
            name=domain.name,
            description=domain.description,
            workflow_id=domain.workflow_id,
            variant_combinations=json.dumps(variant_combinations),
            initial_state=json.dumps(domain.initial_state),
            parallel_count=domain.parallel_count,
            status=domain.status.value,
            created_at=domain.created_at,
            started_at=domain.started_at,
            completed_at=domain.completed_at,
            execution_ids=json.dumps(domain.execution_ids),
            results=json.dumps(results),
            comparison_matrix=json.dumps(domain.comparison_matrix) if domain.comparison_matrix else None,
            best_combination_name=domain.best_combination_name,
            metadata_=json.dumps(domain.metadata),
        )
    
    def find_by_workflow(self, workflow_id: str) -> list[BatchTest]:
        """Find batch tests for a workflow."""
        orms = (
            self._session.query(BatchTestORM)
            .filter_by(workflow_id=workflow_id)
            .order_by(BatchTestORM.created_at.desc())
            .all()
        )
        return [self._to_domain(orm) for orm in orms]
    
    def find_pending(self) -> list[BatchTest]:
        """Find pending batch tests."""
        orms = (
            self._session.query(BatchTestORM)
            .filter_by(status='pending')
            .all()
        )
        return [self._to_domain(orm) for orm in orms]

