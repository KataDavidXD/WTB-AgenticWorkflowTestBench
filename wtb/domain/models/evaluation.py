"""
Evaluation Result Domain Model.

Stores evaluation results from running evaluators on executions.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class MetricValue:
    """
    Value Object - Single metric measurement.
    """
    name: str
    value: float
    unit: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "metadata": self.metadata,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MetricValue":
        return cls(
            name=data.get("name", ""),
            value=data.get("value", 0.0),
            unit=data.get("unit"),
            metadata=data.get("metadata", {}),
        )


@dataclass
class EvaluationResult:
    """
    Entity - Result of evaluating an execution.
    
    Stores the output from an evaluator run against an execution.
    Multiple evaluators can evaluate the same execution.
    """
    # Identity
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    
    # References
    execution_id: str = ""
    batch_test_id: str | None = None  # If part of a batch test
    
    # Evaluator Info
    evaluator_name: str = ""
    evaluator_version: str = "1.0.0"
    
    # Results
    overall_score: float = 0.0  # Normalized 0.0 to 1.0
    passed: bool = True
    metrics: list[MetricValue] = field(default_factory=list)
    
    # Details
    details: str | None = None
    raw_output: dict[str, Any] | None = None
    
    # Timing
    evaluated_at: datetime = field(default_factory=datetime.now)
    evaluation_duration_ms: int = 0
    
    # Metadata
    metadata: dict[str, Any] = field(default_factory=dict)
    
    def add_metric(self, name: str, value: float, unit: str | None = None):
        """Add a metric to the result."""
        self.metrics.append(MetricValue(name=name, value=value, unit=unit))
    
    def get_metric(self, name: str) -> MetricValue | None:
        """Get a metric by name."""
        for m in self.metrics:
            if m.name == name:
                return m
        return None
    
    def get_metric_value(self, name: str, default: float = 0.0) -> float:
        """Get metric value by name, with default."""
        metric = self.get_metric(name)
        return metric.value if metric else default
    
    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "id": self.id,
            "execution_id": self.execution_id,
            "batch_test_id": self.batch_test_id,
            "evaluator_name": self.evaluator_name,
            "evaluator_version": self.evaluator_version,
            "overall_score": self.overall_score,
            "passed": self.passed,
            "metrics": [m.to_dict() for m in self.metrics],
            "details": self.details,
            "raw_output": self.raw_output,
            "evaluated_at": self.evaluated_at.isoformat() if self.evaluated_at else None,
            "evaluation_duration_ms": self.evaluation_duration_ms,
            "metadata": self.metadata,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvaluationResult":
        """Deserialize from dictionary."""
        result = cls(
            id=data.get("id", str(uuid.uuid4())),
            execution_id=data.get("execution_id", ""),
            batch_test_id=data.get("batch_test_id"),
            evaluator_name=data.get("evaluator_name", ""),
            evaluator_version=data.get("evaluator_version", "1.0.0"),
            overall_score=data.get("overall_score", 0.0),
            passed=data.get("passed", True),
            details=data.get("details"),
            raw_output=data.get("raw_output"),
            evaluation_duration_ms=data.get("evaluation_duration_ms", 0),
            metadata=data.get("metadata", {}),
        )
        
        # Parse metrics
        for m_data in data.get("metrics", []):
            result.metrics.append(MetricValue.from_dict(m_data))
        
        # Parse date
        if data.get("evaluated_at"):
            result.evaluated_at = datetime.fromisoformat(data["evaluated_at"])
        
        return result


@dataclass
class ComparisonResult:
    """
    Value Object - Result of comparing multiple executions.
    """
    execution_ids: list[str] = field(default_factory=list)
    evaluator_names: list[str] = field(default_factory=list)
    
    # Rankings per evaluator
    rankings: dict[str, list[str]] = field(default_factory=dict)  # evaluator -> [exec_ids ranked]
    
    # Scores matrix: execution_id -> evaluator -> score
    scores: dict[str, dict[str, float]] = field(default_factory=dict)
    
    # Overall winner
    winner_execution_id: str | None = None
    winner_overall_score: float = 0.0
    
    # Metadata
    compared_at: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_ids": self.execution_ids,
            "evaluator_names": self.evaluator_names,
            "rankings": self.rankings,
            "scores": self.scores,
            "winner_execution_id": self.winner_execution_id,
            "winner_overall_score": self.winner_overall_score,
            "compared_at": self.compared_at.isoformat(),
        }

