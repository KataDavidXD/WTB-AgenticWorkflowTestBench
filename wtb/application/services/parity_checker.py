"""
Parity Checker for ThreadPool→Ray Migration.

Validates that ThreadPoolBatchTestRunner and RayBatchTestRunner produce
equivalent results for the same batch test input.

Design Principles:
- SOLID: Single responsibility (validate parity), Open for extension
- Statistical comparison: Tolerates non-deterministic execution timing
- Dry-run mode: Compare without affecting production data

Usage:
    from wtb.application.services.parity_checker import ParityChecker
    
    checker = ParityChecker(
        threadpool_runner=threadpool_runner,
        ray_runner=ray_runner,
    )
    
    # Dry-run comparison
    result = checker.check_parity(batch_test)
    
    if result.is_equivalent:
        print("Runners produce equivalent results!")
    else:
        print(f"Discrepancies: {result.discrepancies}")
"""

import copy
import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from wtb.domain.interfaces.batch_runner import (
    IBatchTestRunner,
)
from wtb.domain.models.batch_test import (
    BatchTest,
    BatchTestStatus,
    VariantCombination,
)

logger = logging.getLogger(__name__)


class ParityDiscrepancyType(Enum):
    """Types of discrepancies between runners."""
    RESULT_COUNT_MISMATCH = "result_count_mismatch"
    SUCCESS_STATUS_MISMATCH = "success_status_mismatch"
    METRIC_VALUE_MISMATCH = "metric_value_mismatch"
    COMPLETION_STATUS_MISMATCH = "completion_status_mismatch"
    ERROR_MESSAGE_MISMATCH = "error_message_mismatch"
    METRIC_MISSING = "metric_missing"


@dataclass
class ParityDiscrepancy:
    """
    Individual discrepancy between ThreadPool and Ray results.
    
    Attributes:
        type: Type of discrepancy
        variant_name: Which variant has the discrepancy
        threadpool_value: Value from ThreadPool runner
        ray_value: Value from Ray runner
        description: Human-readable description
    """
    type: ParityDiscrepancyType
    variant_name: str
    threadpool_value: Any
    ray_value: Any
    description: str
    
    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "type": self.type.value,
            "variant_name": self.variant_name,
            "threadpool_value": str(self.threadpool_value),
            "ray_value": str(self.ray_value),
            "description": self.description,
        }


@dataclass
class ParityCheckResult:
    """
    Result of parity check between ThreadPool and Ray runners.
    
    Attributes:
        batch_test_id: ID of the batch test
        threadpool_status: Final status from ThreadPool runner
        ray_status: Final status from Ray runner
        is_equivalent: Whether results are equivalent within tolerances
        discrepancies: List of found discrepancies
        threadpool_duration_ms: ThreadPool execution time
        ray_duration_ms: Ray execution time
        speedup_factor: Ray speedup relative to ThreadPool
        comparison_timestamp: When comparison was performed
        metrics_compared: List of metrics that were compared
        tolerance_used: Tolerance values used for comparison
    """
    batch_test_id: str
    threadpool_status: BatchTestStatus
    ray_status: BatchTestStatus
    is_equivalent: bool
    discrepancies: list[ParityDiscrepancy] = field(default_factory=list)
    threadpool_duration_ms: float = 0.0
    ray_duration_ms: float = 0.0
    speedup_factor: float = 1.0
    comparison_timestamp: datetime = field(default_factory=datetime.now)
    metrics_compared: list[str] = field(default_factory=list)
    tolerance_used: dict[str, float] = field(default_factory=dict)
    
    @property
    def discrepancy_count(self) -> int:
        """Number of discrepancies found."""
        return len(self.discrepancies)
    
    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "batch_test_id": self.batch_test_id,
            "threadpool_status": self.threadpool_status.value,
            "ray_status": self.ray_status.value,
            "is_equivalent": self.is_equivalent,
            "discrepancy_count": self.discrepancy_count,
            "discrepancies": [d.to_dict() for d in self.discrepancies],
            "threadpool_duration_ms": self.threadpool_duration_ms,
            "ray_duration_ms": self.ray_duration_ms,
            "speedup_factor": self.speedup_factor,
            "comparison_timestamp": self.comparison_timestamp.isoformat(),
            "metrics_compared": self.metrics_compared,
            "tolerance_used": self.tolerance_used,
        }
    
    def summary(self) -> str:
        """Get human-readable summary."""
        status = "EQUIVALENT" if self.is_equivalent else "DISCREPANT"
        return (
            f"Parity Check: {status}\n"
            f"  Batch Test: {self.batch_test_id}\n"
            f"  ThreadPool: {self.threadpool_status.value} ({self.threadpool_duration_ms:.0f}ms)\n"
            f"  Ray: {self.ray_status.value} ({self.ray_duration_ms:.0f}ms)\n"
            f"  Speedup: {self.speedup_factor:.2f}x\n"
            f"  Discrepancies: {self.discrepancy_count}"
        )


@dataclass
class ParityCheckerConfig:
    """
    Configuration for parity checking.
    
    Attributes:
        metric_tolerance: Relative tolerance for metric comparison (e.g., 0.01 = 1%)
        absolute_tolerance: Absolute tolerance for near-zero values
        ignore_timing_metrics: Whether to ignore timing-related metrics
        ignored_metrics: Set of metric names to ignore
        require_same_success: Whether success status must match exactly
        max_discrepancies: Stop checking after this many discrepancies
    """
    metric_tolerance: float = 0.01  # 1% relative tolerance
    absolute_tolerance: float = 1e-6  # For near-zero values
    ignore_timing_metrics: bool = True
    ignored_metrics: list[str] = field(default_factory=lambda: ["latency_ms", "duration_ms"])
    require_same_success: bool = True
    max_discrepancies: int = 100
    
    @classmethod
    def strict(cls) -> "ParityCheckerConfig":
        """Strict comparison (no tolerance)."""
        return cls(
            metric_tolerance=0.0,
            absolute_tolerance=0.0,
            ignore_timing_metrics=False,
            require_same_success=True,
        )
    
    @classmethod
    def lenient(cls) -> "ParityCheckerConfig":
        """Lenient comparison (generous tolerances)."""
        return cls(
            metric_tolerance=0.05,  # 5% tolerance
            absolute_tolerance=1e-3,
            ignore_timing_metrics=True,
            require_same_success=True,
        )


class ParityChecker:
    """
    Validates parity between ThreadPool and Ray batch test runners.
    
    Use this to validate Ray migration before switching production traffic.
    
    SOLID Compliance:
    - SRP: Only validates parity, doesn't modify runners
    - OCP: Extensible comparison via config
    - DIP: Depends on IBatchTestRunner abstraction
    
    Usage:
        # Create checker
        checker = ParityChecker(
            threadpool_runner=ThreadPoolBatchTestRunner(...),
            ray_runner=RayBatchTestRunner(...),
        )
        
        # Check parity
        result = checker.check_parity(batch_test)
        
        # Analyze results
        if not result.is_equivalent:
            for d in result.discrepancies:
                print(f"{d.variant_name}: {d.description}")
    """
    
    def __init__(
        self,
        threadpool_runner: IBatchTestRunner,
        ray_runner: IBatchTestRunner,
        config: ParityCheckerConfig | None = None,
    ):
        """
        Initialize parity checker.
        
        Args:
            threadpool_runner: ThreadPool-based runner
            ray_runner: Ray-based runner
            config: Comparison configuration
        """
        self._threadpool_runner = threadpool_runner
        self._ray_runner = ray_runner
        self._config = config or ParityCheckerConfig()
    
    def check_parity(
        self,
        batch_test: BatchTest,
        dry_run: bool = True,
    ) -> ParityCheckResult:
        """
        Check parity between ThreadPool and Ray runners.
        
        Runs the same batch test through both runners and compares results.
        
        Args:
            batch_test: Batch test to run
            dry_run: If True, uses copies to avoid modifying original
            
        Returns:
            ParityCheckResult with comparison details
            
        Raises:
            BatchRunnerError: If either runner fails catastrophically
        """
        logger.info(f"Starting parity check for batch test {batch_test.id}")
        
        # Create independent copies for dry run
        if dry_run:
            threadpool_batch = self._clone_batch_test(batch_test, suffix="-threadpool")
            ray_batch = self._clone_batch_test(batch_test, suffix="-ray")
        else:
            threadpool_batch = batch_test
            ray_batch = self._clone_batch_test(batch_test, suffix="-ray")
        
        discrepancies: list[ParityDiscrepancy] = []
        
        # Run ThreadPool
        threadpool_start = datetime.now()
        try:
            threadpool_result = self._threadpool_runner.run_batch_test(threadpool_batch)
            threadpool_duration = (datetime.now() - threadpool_start).total_seconds() * 1000
        except Exception as e:
            logger.error(f"ThreadPool runner failed: {e}")
            threadpool_result = threadpool_batch
            threadpool_result.fail(str(e))
            threadpool_duration = (datetime.now() - threadpool_start).total_seconds() * 1000
        
        # Run Ray
        ray_start = datetime.now()
        try:
            ray_result = self._ray_runner.run_batch_test(ray_batch)
            ray_duration = (datetime.now() - ray_start).total_seconds() * 1000
        except Exception as e:
            logger.error(f"Ray runner failed: {e}")
            ray_result = ray_batch
            ray_result.fail(str(e))
            ray_duration = (datetime.now() - ray_start).total_seconds() * 1000
        
        # Compare completion status
        if threadpool_result.status != ray_result.status:
            discrepancies.append(ParityDiscrepancy(
                type=ParityDiscrepancyType.COMPLETION_STATUS_MISMATCH,
                variant_name="__batch__",
                threadpool_value=threadpool_result.status,
                ray_value=ray_result.status,
                description=(
                    f"Batch test status mismatch: ThreadPool={threadpool_result.status.value}, "
                    f"Ray={ray_result.status.value}"
                ),
            ))
        
        # Compare result counts
        if len(threadpool_result.results) != len(ray_result.results):
            discrepancies.append(ParityDiscrepancy(
                type=ParityDiscrepancyType.RESULT_COUNT_MISMATCH,
                variant_name="__batch__",
                threadpool_value=len(threadpool_result.results),
                ray_value=len(ray_result.results),
                description=(
                    f"Result count mismatch: ThreadPool={len(threadpool_result.results)}, "
                    f"Ray={len(ray_result.results)}"
                ),
            ))
        
        # Compare individual results
        threadpool_by_name = {r.combination_name: r for r in threadpool_result.results}
        ray_by_name = {r.combination_name: r for r in ray_result.results}
        
        metrics_compared = set()
        
        for name, tp_result in threadpool_by_name.items():
            if len(discrepancies) >= self._config.max_discrepancies:
                break
            
            ray_res = ray_by_name.get(name)
            if ray_res is None:
                discrepancies.append(ParityDiscrepancy(
                    type=ParityDiscrepancyType.RESULT_COUNT_MISMATCH,
                    variant_name=name,
                    threadpool_value=True,
                    ray_value=False,
                    description=f"Variant '{name}' missing from Ray results",
                ))
                continue
            
            # Compare success status
            if self._config.require_same_success and tp_result.success != ray_res.success:
                discrepancies.append(ParityDiscrepancy(
                    type=ParityDiscrepancyType.SUCCESS_STATUS_MISMATCH,
                    variant_name=name,
                    threadpool_value=tp_result.success,
                    ray_value=ray_res.success,
                    description=(
                        f"Success status mismatch for '{name}': "
                        f"ThreadPool={tp_result.success}, Ray={ray_res.success}"
                    ),
                ))
            
            # Compare metrics
            metric_discrepancies = self._compare_metrics(
                name,
                tp_result.metrics,
                ray_res.metrics,
                metrics_compared,
            )
            discrepancies.extend(metric_discrepancies)
        
        # Check for variants in Ray but not ThreadPool
        for name in ray_by_name:
            if name not in threadpool_by_name:
                discrepancies.append(ParityDiscrepancy(
                    type=ParityDiscrepancyType.RESULT_COUNT_MISMATCH,
                    variant_name=name,
                    threadpool_value=False,
                    ray_value=True,
                    description=f"Variant '{name}' only in Ray results",
                ))
        
        # Calculate speedup
        speedup = threadpool_duration / ray_duration if ray_duration > 0 else 1.0
        
        # Determine equivalence
        is_equivalent = len(discrepancies) == 0
        
        result = ParityCheckResult(
            batch_test_id=batch_test.id,
            threadpool_status=threadpool_result.status,
            ray_status=ray_result.status,
            is_equivalent=is_equivalent,
            discrepancies=discrepancies,
            threadpool_duration_ms=threadpool_duration,
            ray_duration_ms=ray_duration,
            speedup_factor=speedup,
            comparison_timestamp=datetime.now(),
            metrics_compared=list(metrics_compared),
            tolerance_used={
                "metric_tolerance": self._config.metric_tolerance,
                "absolute_tolerance": self._config.absolute_tolerance,
            },
        )
        
        logger.info(f"Parity check complete: {result.summary()}")
        return result
    
    def _clone_batch_test(self, batch_test: BatchTest, suffix: str) -> BatchTest:
        """Create an independent copy of a batch test."""
        return BatchTest(
            id=f"{batch_test.id}{suffix}",
            name=f"{batch_test.name}{suffix}",
            workflow_id=batch_test.workflow_id,
            variant_combinations=[
                VariantCombination(
                    name=vc.name,
                    variants=copy.deepcopy(vc.variants),
                    metadata=copy.deepcopy(vc.metadata),
                )
                for vc in batch_test.variant_combinations
            ],
            initial_state=copy.deepcopy(batch_test.initial_state),
            parallel_count=batch_test.parallel_count,
            metadata=copy.deepcopy(batch_test.metadata),
        )
    
    def _compare_metrics(
        self,
        variant_name: str,
        threadpool_metrics: dict[str, Any],
        ray_metrics: dict[str, Any],
        metrics_compared: set,
    ) -> list[ParityDiscrepancy]:
        """Compare metrics between two results."""
        discrepancies = []
        
        all_keys = set(threadpool_metrics.keys()) | set(ray_metrics.keys())
        
        for key in all_keys:
            # Skip ignored metrics
            if key in self._config.ignored_metrics:
                continue
            if self._config.ignore_timing_metrics and "time" in key.lower():
                continue
            
            metrics_compared.add(key)
            
            tp_val = threadpool_metrics.get(key)
            ray_val = ray_metrics.get(key)
            
            # Handle missing metrics
            if tp_val is None and ray_val is not None:
                discrepancies.append(ParityDiscrepancy(
                    type=ParityDiscrepancyType.METRIC_MISSING,
                    variant_name=variant_name,
                    threadpool_value=None,
                    ray_value=ray_val,
                    description=f"Metric '{key}' missing from ThreadPool",
                ))
                continue
            
            if ray_val is None and tp_val is not None:
                discrepancies.append(ParityDiscrepancy(
                    type=ParityDiscrepancyType.METRIC_MISSING,
                    variant_name=variant_name,
                    threadpool_value=tp_val,
                    ray_value=None,
                    description=f"Metric '{key}' missing from Ray",
                ))
                continue
            
            # Compare numeric values with tolerance
            if isinstance(tp_val, (int, float)) and isinstance(ray_val, (int, float)):
                if not self._values_equal(tp_val, ray_val):
                    discrepancies.append(ParityDiscrepancy(
                        type=ParityDiscrepancyType.METRIC_VALUE_MISMATCH,
                        variant_name=variant_name,
                        threadpool_value=tp_val,
                        ray_value=ray_val,
                        description=(
                            f"Metric '{key}' mismatch for '{variant_name}': "
                            f"ThreadPool={tp_val}, Ray={ray_val}"
                        ),
                    ))
            # Compare non-numeric values exactly
            elif tp_val != ray_val:
                discrepancies.append(ParityDiscrepancy(
                    type=ParityDiscrepancyType.METRIC_VALUE_MISMATCH,
                    variant_name=variant_name,
                    threadpool_value=tp_val,
                    ray_value=ray_val,
                    description=(
                        f"Metric '{key}' mismatch for '{variant_name}': "
                        f"ThreadPool={tp_val}, Ray={ray_val}"
                    ),
                ))
        
        return discrepancies
    
    def _values_equal(self, a: float, b: float) -> bool:
        """Check if two values are equal within tolerance."""
        # Absolute comparison for near-zero values
        if abs(a) < self._config.absolute_tolerance and abs(b) < self._config.absolute_tolerance:
            return True
        
        # Relative comparison
        if a == 0:
            return abs(b) < self._config.absolute_tolerance
        
        relative_diff = abs(a - b) / abs(a)
        return relative_diff <= self._config.metric_tolerance
    
    def run_statistical_comparison(
        self,
        batch_test: BatchTest,
        num_runs: int = 5,
    ) -> dict[str, Any]:
        """
        Run multiple parity checks for statistical comparison.
        
        Useful for validating that results are consistent across runs,
        accounting for non-determinism.
        
        Args:
            batch_test: Batch test to run
            num_runs: Number of runs to perform
            
        Returns:
            Statistical summary of comparison results
        """
        logger.info(f"Running statistical comparison with {num_runs} runs")
        
        results: list[ParityCheckResult] = []
        
        for i in range(num_runs):
            logger.info(f"Statistical run {i + 1}/{num_runs}")
            result = self.check_parity(batch_test, dry_run=True)
            results.append(result)
        
        # Aggregate statistics
        equivalent_runs = sum(1 for r in results if r.is_equivalent)
        speedups = [r.speedup_factor for r in results]
        discrepancy_counts = [r.discrepancy_count for r in results]
        
        return {
            "num_runs": num_runs,
            "equivalent_runs": equivalent_runs,
            "equivalence_rate": equivalent_runs / num_runs,
            "speedup_mean": statistics.mean(speedups),
            "speedup_stdev": statistics.stdev(speedups) if num_runs > 1 else 0.0,
            "discrepancy_mean": statistics.mean(discrepancy_counts),
            "discrepancy_max": max(discrepancy_counts),
            "results": [r.to_dict() for r in results],
        }
