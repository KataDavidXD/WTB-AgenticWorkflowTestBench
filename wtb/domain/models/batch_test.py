"""
Batch Test Domain Model.

Represents a batch A/B test with multiple variant combinations.
Orchestrates parallel execution and comparison.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional
from datetime import datetime
from enum import Enum
import uuid


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
    """
    name: str  # Human-readable name (e.g., "Config A")
    variants: Dict[str, str] = field(default_factory=dict)  # node_id -> variant_id
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "variants": self.variants,
            "metadata": self.metadata,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VariantCombination":
        return cls(
            name=data.get("name", ""),
            variants=data.get("variants", {}),
            metadata=data.get("metadata", {}),
        )


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
    metrics: Dict[str, float] = field(default_factory=dict)
    overall_score: float = 0.0
    duration_ms: int = 0
    error_message: Optional[str] = None
    # v1.8: Rollback support fields
    file_commit_id: Optional[str] = None      # FileTracker commit ID
    checkpoint_count: int = 0                  # Number of checkpoints
    last_checkpoint_id: Optional[str] = None   # Most recent checkpoint ID
    
    def to_dict(self) -> Dict[str, Any]:
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
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BatchTestResult":
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
    variant_combinations: List[VariantCombination] = field(default_factory=list)
    parallel_count: int = 1  # Max concurrent executions
    
    # Initial state for all runs
    initial_state: Dict[str, Any] = field(default_factory=dict)
    
    # Status
    status: BatchTestStatus = BatchTestStatus.PENDING
    
    # Timing
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    
    # Results
    execution_ids: List[str] = field(default_factory=list)
    results: List[BatchTestResult] = field(default_factory=list)
    comparison_matrix: Optional[Dict[str, Any]] = None
    
    # Best variant
    best_combination_name: Optional[str] = None
    
    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # === Lifecycle Methods ===
    
    def start(self):
        """Start the batch test."""
        if self.status != BatchTestStatus.PENDING:
            raise ValueError(f"Cannot start batch test in status {self.status.value}")
        self.status = BatchTestStatus.RUNNING
        self.started_at = datetime.now()
    
    def complete(self):
        """Mark batch test as completed."""
        self.status = BatchTestStatus.COMPLETED
        self.completed_at = datetime.now()
        self._determine_best()
    
    def fail(self, error_message: str):
        """Mark batch test as failed."""
        self.status = BatchTestStatus.FAILED
        self.completed_at = datetime.now()
        self.metadata["error_message"] = error_message
    
    def cancel(self):
        """Cancel the batch test."""
        self.status = BatchTestStatus.CANCELLED
        self.completed_at = datetime.now()
    
    # === Result Management ===
    
    def add_result(self, result: BatchTestResult):
        """Add a result from a variant combination run."""
        self.results.append(result)
        if result.execution_id not in self.execution_ids:
            self.execution_ids.append(result.execution_id)
    
    def _determine_best(self):
        """Determine the best variant combination based on scores."""
        if not self.results:
            return
        
        successful = [r for r in self.results if r.success]
        if not successful:
            return
        
        best = max(successful, key=lambda r: r.overall_score)
        self.best_combination_name = best.combination_name
    
    def build_comparison_matrix(self) -> Dict[str, Any]:
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
        }
        
        for result in self.results:
            row = {
                "name": result.combination_name,
                "execution_id": result.execution_id,
                "success": result.success,
                "overall_score": result.overall_score,
                "duration_ms": result.duration_ms,
                # v1.8: Rollback support fields for coordinator access
                "file_commit_id": result.file_commit_id,
                "checkpoint_count": result.checkpoint_count,
                "last_checkpoint_id": result.last_checkpoint_id,
            }
            for metric in all_metrics:
                row[metric] = result.metrics.get(metric)
            
            matrix["combinations"].append(result.combination_name)
            matrix["data"].append(row)
        
        self.comparison_matrix = matrix
        return matrix
    
    # === Query Methods ===
    
    def get_duration_seconds(self) -> Optional[float]:
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
        """Check if all variant combinations have been executed."""
        return len(self.results) >= len(self.variant_combinations)
    
    # === Serialization ===
    
    def to_dict(self) -> Dict[str, Any]:
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
    def from_dict(cls, data: Dict[str, Any]) -> "BatchTest":
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

