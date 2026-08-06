"""
Batch Test Domain Model.

Represents a batch A/B test with multiple variant combinations.
Orchestrates parallel execution and comparison.
"""

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from numbers import Real
from typing import Any


def normalize_finite_metrics(metrics: dict[str, Any]) -> dict[str, float]:
    """Return numeric finite metrics or reject the entire metric payload."""
    if not isinstance(metrics, dict):
        raise ValueError("metrics must be a dictionary of finite real numbers")

    normalized: dict[str, float] = {}
    for name, value in metrics.items():
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError(f"metric '{name}' must be a finite real number")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError(f"metric '{name}' must be a finite real number")
        normalized[name] = numeric
    return normalized


def normalize_finite_score(value: Any, name: str = "overall_score") -> float:
    """Normalize one score with the same contract as metric values."""
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"metric '{name}' must be a finite real number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"metric '{name}' must be a finite real number")
    return normalized


class BatchTestStatus(Enum):
    """Batch test lifecycle states."""
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class VariantCombination:
    """
    Value Object - A specific combination of node variants.
    
    Represents which variant to use for each node in a single test run.
    
    v1.8 (2026-02-06): Added graph factory reference for LangGraph execution.
    This enables distributed actors (Ray) to recreate LangGraph graphs locally
    for proper checkpoint support.
    
    Graph Factory Pattern (data only, DIP-compliant):
    - graph_factory_module: Python module containing the factory function
    - graph_factory_name: Name of the factory function
    - Application layer uses graph_loader.load_graph_factory() to resolve these
    
    Example:
        vc = VariantCombination(
            name="Config_A",
            graph_factory_module="myapp.workflows",
            graph_factory_name="create_my_graph",
        )
    """
    name: str  # Human-readable name (e.g., "Config A")
    variants: dict[str, str] = field(default_factory=dict)  # node_id -> variant_id
    metadata: dict[str, Any] = field(default_factory=dict)
    
    # v1.8: Graph factory reference for distributed execution with checkpoints
    graph_factory_module: str | None = None  # e.g., "examples.ray_batch_demo.run_demo"
    graph_factory_name: str | None = None    # e.g., "create_demo_graph"

    # Thread-local fallback for graph objects that cloudpickle cannot safely
    # round-trip. Deliberately excluded from to_dict() and value equality.
    _runtime_graph: Any | None = field(default=None, repr=False, compare=False)
    
    def to_dict(self) -> dict[str, Any]:
        result = {
            "name": self.name,
            "variants": self.variants,
            "metadata": self.metadata,
        }
        # Include graph factory ref if set (for Ray actors)
        if self.graph_factory_module:
            result["graph_factory_module"] = self.graph_factory_module
        if self.graph_factory_name:
            result["graph_factory_name"] = self.graph_factory_name
        return result
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VariantCombination":
        return cls(
            name=data.get("name", ""),
            variants=data.get("variants", {}),
            metadata=data.get("metadata", {}),
            graph_factory_module=data.get("graph_factory_module"),
            graph_factory_name=data.get("graph_factory_name"),
        )
    
    def has_graph_factory(self) -> bool:
        """Check if graph factory reference is set."""
        return bool(self.graph_factory_module and self.graph_factory_name)


@dataclass
class BatchTestResult:
    """
    Value Object - Results from a single variant combination run.
    
    v1.8 (2026-02-05): Added rollback support fields:
    - file_commit_id: FileTracker commit ID for file restoration
    - checkpoint_count: Number of checkpoints created during execution
    - last_checkpoint_id: Most recent checkpoint ID for rollback operations
    
    These fields enable BatchExecutionCoordinator to perform rollback/fork
    operations without information loss from Ray actor results.
    """
    combination_name: str
    execution_id: str
    success: bool
    metrics: dict[str, float] = field(default_factory=dict)
    overall_score: float = 0.0
    duration_ms: int = 0
    error_message: str | None = None
    # v1.8: Rollback support fields
    file_commit_id: str | None = None      # FileTracker commit ID
    checkpoint_count: int = 0                  # Number of checkpoints
    last_checkpoint_id: str | None = None   # Most recent checkpoint ID
    test_case_index: int | None = None       # Multi-case result identity
    
    def __post_init__(self) -> None:
        self.metrics = normalize_finite_metrics(self.metrics)
        self.overall_score = normalize_finite_score(self.overall_score)

    def to_dict(self) -> dict[str, Any]:
        return {
            "combination_name": self.combination_name,
            "execution_id": self.execution_id,
            "success": self.success,
            "metrics": self.metrics,
            "overall_score": self.overall_score,
            "duration_ms": self.duration_ms,
            "error_message": self.error_message,
            # v1.8: Rollback support fields
            "file_commit_id": self.file_commit_id,
            "checkpoint_count": self.checkpoint_count,
            "last_checkpoint_id": self.last_checkpoint_id,
            "test_case_index": self.test_case_index,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BatchTestResult":
        return cls(
            combination_name=data.get("combination_name", ""),
            execution_id=data.get("execution_id", ""),
            success=data.get("success", False),
            metrics=data.get("metrics", {}),
            overall_score=data.get("overall_score", 0.0),
            duration_ms=data.get("duration_ms", 0),
            error_message=data.get("error_message"),
            # v1.8: Rollback support fields
            file_commit_id=data.get("file_commit_id"),
            checkpoint_count=data.get("checkpoint_count", 0),
            last_checkpoint_id=data.get("last_checkpoint_id"),
            test_case_index=data.get("test_case_index"),
        )


@dataclass
class BatchTest:
    """
    Aggregate Root - Batch A/B test orchestration.
    
    Manages parallel execution of multiple variant combinations
    and aggregates results for comparison.
    """
    # Identity
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    description: str = ""
    
    # Reference
    workflow_id: str = ""
    
    # Configuration
    variant_combinations: list[VariantCombination] = field(default_factory=list)
    parallel_count: int = 1  # Max concurrent executions
    
    # Initial state for all runs
    initial_state: dict[str, Any] = field(default_factory=dict)
    
    # Status
    status: BatchTestStatus = BatchTestStatus.PENDING
    
    # Timing
    created_at: datetime = field(default_factory=datetime.now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    
    # Results
    execution_ids: list[str] = field(default_factory=list)
    results: list[BatchTestResult] = field(default_factory=list)
    comparison_matrix: dict[str, Any] | None = None
    
    # Best variant
    best_combination_name: str | None = None
    
    # Metadata
    metadata: dict[str, Any] = field(default_factory=dict)
    
    # Transient: workflow object cache (not persisted, avoids UoW lookup)
    _workflow: Any | None = field(default=None, repr=False, compare=False)
    
    # === Lifecycle Methods ===
    
    def start(self):
        """Start the batch test."""
        if self.status != BatchTestStatus.PENDING:
            raise ValueError(f"Cannot start batch test in status {self.status.value}")
        self.status = BatchTestStatus.RUNNING
        self.started_at = datetime.now()
    
    def complete(self):
        """
        Mark batch test as completed.
        
        Raises:
            ValueError: If not in RUNNING status
        """
        if self.status != BatchTestStatus.RUNNING:
            raise ValueError(f"Cannot complete batch test in status {self.status.value}")
        self._determine_best()
        self.status = BatchTestStatus.COMPLETED
        self.completed_at = datetime.now()
    
    def fail(self, error_message: str):
        """
        Mark batch test as failed.
        
        Raises:
            ValueError: If not in RUNNING or PENDING status
        """
        if self.status not in (BatchTestStatus.RUNNING, BatchTestStatus.PENDING):
            raise ValueError(f"Cannot fail batch test in status {self.status.value}")
        self.status = BatchTestStatus.FAILED
        self.completed_at = datetime.now()
        self.metadata["error_message"] = error_message
    
    def cancel(self):
        """
        Cancel the batch test.
        
        Raises:
            ValueError: If already in a terminal state
        """
        if self.status in (BatchTestStatus.COMPLETED, BatchTestStatus.FAILED, BatchTestStatus.CANCELLED):
            raise ValueError(f"Cannot cancel batch test in status {self.status.value}")
        self.status = BatchTestStatus.CANCELLED
        self.completed_at = datetime.now()
    
    # === Result Management ===
    
    def add_result(self, result: BatchTestResult):
        """Add a result from a variant combination run."""
        self.results.append(result)
        if result.execution_id not in self.execution_ids:
            self.execution_ids.append(result.execution_id)
    
    def _expected_test_case_count(self) -> int:
        """Return the number of cases required for each combination."""
        try:
            return max(1, int(self.metadata.get("test_case_count", 1)))
        except (TypeError, ValueError):
            return 1

    def _results_by_combination(self) -> dict[str, list[BatchTestResult]]:
        """Group results while preserving combination insertion order."""
        grouped: dict[str, list[BatchTestResult]] = {}
        for result in self.results:
            grouped.setdefault(result.combination_name, []).append(result)
        return grouped

    def _aggregate_scores(self) -> dict[str, float]:
        """Average complete per-case scores for each combination."""
        expected = self._expected_test_case_count()
        aggregate_scores: dict[str, float] = {}
        for name, results in self._results_by_combination().items():
            if len(results) != expected:
                continue
            if expected > 1:
                case_indexes = {
                    result.test_case_index for result in results
                }
                if case_indexes != set(range(expected)):
                    continue
            aggregate_scores[name] = sum(
                result.overall_score for result in results
            ) / expected
        return aggregate_scores

    def _determine_best(self):
        """Determine the best complete combination by mean score across cases."""
        if not self.results:
            return
        
        expected = self._expected_test_case_count()
        candidates: list[tuple[float, str]] = []
        for name, results in self._results_by_combination().items():
            if len(results) != expected or any(not result.success for result in results):
                continue

            if expected > 1:
                case_indexes = [result.test_case_index for result in results]
                if set(case_indexes) != set(range(expected)):
                    continue

            candidates.append((
                sum(result.overall_score for result in results) / expected,
                name,
            ))

        if candidates:
            _, self.best_combination_name = max(candidates, key=lambda item: item[0])
    
    def build_comparison_matrix(self) -> dict[str, Any]:
        """Build a comparison matrix of all results."""
        if not self.results:
            return {}
        
        # Get all unique metric names
        all_metrics = set()
        for result in self.results:
            all_metrics.update(result.metrics.keys())
        
        matrix = {
            "combinations": [],
            "metrics": list(all_metrics),
            "data": [],
            "aggregate_scores": self._aggregate_scores(),
        }
        
        for result in self.results:
            row = {
                "name": result.combination_name,
                "execution_id": result.execution_id,
                "success": result.success,
                "overall_score": result.overall_score,
                "test_case_index": result.test_case_index,
                "duration_ms": result.duration_ms,
                # v1.8: Rollback support fields for coordinator access
                "file_commit_id": result.file_commit_id,
                "checkpoint_count": result.checkpoint_count,
                "last_checkpoint_id": result.last_checkpoint_id,
            }
            for metric in all_metrics:
                row[metric] = result.metrics.get(metric)
            
            row_label = result.combination_name
            if (
                self._expected_test_case_count() > 1
                and result.test_case_index is not None
            ):
                row_label = (
                    f"{result.combination_name}[case_{result.test_case_index}]"
                )
            matrix["combinations"].append(row_label)
            matrix["data"].append(row)
        
        self.comparison_matrix = matrix
        return matrix
    
    # === Query Methods ===
    
    def get_duration_seconds(self) -> float | None:
        """Get total duration in seconds."""
        if not self.started_at:
            return None
        end_time = self.completed_at or datetime.now()
        return (end_time - self.started_at).total_seconds()
    
    def get_success_rate(self) -> float:
        """Get the success rate of all runs."""
        if not self.results:
            return 0.0
        successful = sum(1 for r in self.results if r.success)
        return successful / len(self.results)
    
    def is_complete(self) -> bool:
        """Check if every variant has a result for every requested test case."""
        expected_cases = self._expected_test_case_count()
        combination_names = [item.name for item in self.variant_combinations]
        if len(combination_names) != len(set(combination_names)):
            return False

        expected_identities = {
            (name, case_index)
            for name in combination_names
            for case_index in range(expected_cases)
        }
        actual_identities = [
            (
                result.combination_name,
                0 if expected_cases == 1 and result.test_case_index is None
                else result.test_case_index,
            )
            for result in self.results
        ]
        return (
            len(actual_identities) == len(expected_identities)
            and len(actual_identities) == len(set(actual_identities))
            and set(actual_identities) == expected_identities
        )
    
    # === Serialization ===
    
    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "workflow_id": self.workflow_id,
            "variant_combinations": [vc.to_dict() for vc in self.variant_combinations],
            "parallel_count": self.parallel_count,
            "initial_state": self.initial_state,
            "status": self.status.value,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "execution_ids": self.execution_ids,
            "results": [r.to_dict() for r in self.results],
            "comparison_matrix": self.comparison_matrix,
            "best_combination_name": self.best_combination_name,
            "metadata": self.metadata,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BatchTest":
        """Deserialize from dictionary."""
        bt = cls(
            id=data.get("id", str(uuid.uuid4())),
            name=data.get("name", ""),
            description=data.get("description", ""),
            workflow_id=data.get("workflow_id", ""),
            parallel_count=data.get("parallel_count", 1),
            initial_state=data.get("initial_state", {}),
            status=BatchTestStatus(data.get("status", "pending")),
            execution_ids=data.get("execution_ids", []),
            comparison_matrix=data.get("comparison_matrix"),
            best_combination_name=data.get("best_combination_name"),
            metadata=data.get("metadata", {}),
        )
        
        # Parse variant combinations
        for vc_data in data.get("variant_combinations", []):
            bt.variant_combinations.append(VariantCombination.from_dict(vc_data))
        
        # Parse results
        for result_data in data.get("results", []):
            bt.results.append(BatchTestResult.from_dict(result_data))
        
        # Parse dates
        if data.get("created_at"):
            bt.created_at = datetime.fromisoformat(data["created_at"])
        if data.get("started_at"):
            bt.started_at = datetime.fromisoformat(data["started_at"])
        if data.get("completed_at"):
            bt.completed_at = datetime.fromisoformat(data["completed_at"])
        
        return bt

