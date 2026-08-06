"""Evaluation result repository implementation."""

import json

from sqlalchemy.orm import Session

from wtb.domain.interfaces.repositories import IEvaluationResultRepository
from wtb.domain.models import EvaluationResult, MetricValue

from ..models import EvaluationResultORM
from .base import BaseRepository


class EvaluationResultRepository(BaseRepository[EvaluationResult, EvaluationResultORM], IEvaluationResultRepository):
    """SQLAlchemy implementation of evaluation result repository."""
    
    def __init__(self, session: Session):
        super().__init__(session, EvaluationResultORM)
    
    def _to_domain(self, orm: EvaluationResultORM) -> EvaluationResult:
        """Convert ORM to domain model."""
        # Parse JSON fields
        metrics_data = json.loads(orm.metrics) if orm.metrics else []
        raw_output = json.loads(orm.raw_output) if orm.raw_output else None
        metadata = json.loads(orm.metadata_) if orm.metadata_ else {}
        
        # Convert metrics
        metrics = [MetricValue.from_dict(m) for m in metrics_data]
        
        return EvaluationResult(
            id=orm.id,
            execution_id=orm.execution_id,
            batch_test_id=orm.batch_test_id,
            evaluator_name=orm.evaluator_name,
            evaluator_version=orm.evaluator_version,
            overall_score=orm.overall_score,
            passed=orm.passed,
            metrics=metrics,
            details=orm.details,
            raw_output=raw_output,
            evaluated_at=orm.evaluated_at,
            evaluation_duration_ms=orm.evaluation_duration_ms,
            metadata=metadata,
        )
    
    def _to_orm(self, domain: EvaluationResult) -> EvaluationResultORM:
        """Convert domain model to ORM."""
        metrics = [m.to_dict() for m in domain.metrics]
        
        return EvaluationResultORM(
            id=domain.id,
            execution_id=domain.execution_id,
            batch_test_id=domain.batch_test_id,
            evaluator_name=domain.evaluator_name,
            evaluator_version=domain.evaluator_version,
            overall_score=domain.overall_score,
            passed=domain.passed,
            metrics=json.dumps(metrics),
            details=domain.details,
            raw_output=json.dumps(domain.raw_output) if domain.raw_output else None,
            evaluated_at=domain.evaluated_at,
            evaluation_duration_ms=domain.evaluation_duration_ms,
            metadata_=json.dumps(domain.metadata),
        )
    
    def find_by_execution(self, execution_id: str) -> list[EvaluationResult]:
        """Find evaluation results for an execution."""
        orms = (
            self._session.query(EvaluationResultORM)
            .filter_by(execution_id=execution_id)
            .order_by(EvaluationResultORM.evaluated_at.desc())
            .all()
        )
        return [self._to_domain(orm) for orm in orms]
    
    def find_by_evaluator(self, evaluator_name: str) -> list[EvaluationResult]:
        """Find results from a specific evaluator."""
        orms = (
            self._session.query(EvaluationResultORM)
            .filter_by(evaluator_name=evaluator_name)
            .order_by(EvaluationResultORM.evaluated_at.desc())
            .all()
        )
        return [self._to_domain(orm) for orm in orms]

