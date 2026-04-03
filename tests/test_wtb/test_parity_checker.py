"""
Tests for ParityChecker.

Validates the parity checking functionality for ThreadPool→Ray migration.

Test Categories:
1. Unit Tests: Test ParityChecker logic with mocked runners
2. Integration Tests: Test with actual runners

Usage:
    pytest tests/test_wtb/test_parity_checker.py -v
"""

import pytest
import os
import time
from datetime import datetime
from unittest.mock import MagicMock, patch
from typing import Dict, Any

from wtb.domain.models.batch_test import (
    BatchTest,
    BatchTestStatus,
    BatchTestResult,
    VariantCombination,
)
from wtb.domain.models.workflow import TestWorkflow, WorkflowNode, WorkflowEdge
from wtb.domain.interfaces.batch_runner import (
    IBatchTestRunner,
    BatchRunnerStatus,
    BatchRunnerProgress,
)
from wtb.application.services.parity_checker import (
    ParityChecker,
    ParityCheckerConfig,
    ParityCheckResult,
    ParityDiscrepancy,
    ParityDiscrepancyType,
)


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def sample_workflow() -> TestWorkflow:
    """Create a sample workflow for testing."""
    workflow = TestWorkflow(
        id="wf-parity-test",
        name="Parity Test Workflow",
        description="Workflow for parity testing",
        entry_point="start",
    )
    workflow.add_node(WorkflowNode(id="start", name="Start", type="start"))
    workflow.add_node(WorkflowNode(id="process", name="Process", type="action"))
    workflow.add_node(WorkflowNode(id="end", name="End", type="end"))
    workflow.add_edge(WorkflowEdge(source_id="start", target_id="process"))
    workflow.add_edge(WorkflowEdge(source_id="process", target_id="end"))
    return workflow


@pytest.fixture
def sample_batch_test(sample_workflow) -> BatchTest:
    """Create a sample batch test."""
    return BatchTest(
        id="batch-parity-test",
        name="Parity Test Batch",
        workflow_id=sample_workflow.id,
        variant_combinations=[
            VariantCombination(
                name="Config A",
                variants={"process": "variant-a"},
            ),
            VariantCombination(
                name="Config B",
                variants={"process": "variant-b"},
            ),
        ],
        initial_state={"input": "test_data"},
        parallel_count=2,
    )


def create_mock_runner(
    results: list[BatchTestResult],
    status: BatchTestStatus = BatchTestStatus.COMPLETED,
    delay_ms: float = 1.0,
) -> IBatchTestRunner:
    """Create a mock runner that returns specified results."""
    mock_runner = MagicMock(spec=IBatchTestRunner)
    
    def run_batch_test(batch_test):
        batch_test.start()
        # Small delay to ensure non-zero duration measurement
        time.sleep(delay_ms / 1000.0)
        for result in results:
            batch_test.add_result(result)
        if status == BatchTestStatus.COMPLETED:
            batch_test.complete()
        elif status == BatchTestStatus.FAILED:
            batch_test.fail("Test failure")
        return batch_test
    
    mock_runner.run_batch_test.side_effect = run_batch_test
    mock_runner.get_status.return_value = BatchRunnerStatus.IDLE
    mock_runner.get_progress.return_value = None
    mock_runner.cancel.return_value = False
    mock_runner.shutdown.return_value = None
    
    return mock_runner


# ═══════════════════════════════════════════════════════════════
# ParityCheckerConfig Tests
# ═══════════════════════════════════════════════════════════════


class TestParityCheckerConfig:
    """Tests for ParityCheckerConfig."""
    
    def test_default_config(self):
        """Default config has reasonable values."""
        config = ParityCheckerConfig()
        
        assert config.metric_tolerance == 0.01
        assert config.absolute_tolerance == 1e-6
        assert config.ignore_timing_metrics is True
        assert config.require_same_success is True
    
    def test_strict_config(self):
        """Strict config has no tolerance."""
        config = ParityCheckerConfig.strict()
        
        assert config.metric_tolerance == 0.0
        assert config.absolute_tolerance == 0.0
        assert config.ignore_timing_metrics is False
    
    def test_lenient_config(self):
        """Lenient config has generous tolerance."""
        config = ParityCheckerConfig.lenient()
        
        assert config.metric_tolerance == 0.05
        assert config.absolute_tolerance == 1e-3


# ═══════════════════════════════════════════════════════════════
# ParityDiscrepancy Tests
# ═══════════════════════════════════════════════════════════════


class TestParityDiscrepancy:
    """Tests for ParityDiscrepancy."""
    
    def test_create_discrepancy(self):
        """Can create discrepancy."""
        discrepancy = ParityDiscrepancy(
            type=ParityDiscrepancyType.SUCCESS_STATUS_MISMATCH,
            variant_name="Config A",
            threadpool_value=True,
            ray_value=False,
            description="Success status mismatch",
        )
        
        assert discrepancy.type == ParityDiscrepancyType.SUCCESS_STATUS_MISMATCH
        assert discrepancy.variant_name == "Config A"
    
    def test_to_dict(self):
        """Can serialize to dict."""
        discrepancy = ParityDiscrepancy(
            type=ParityDiscrepancyType.METRIC_VALUE_MISMATCH,
            variant_name="Config B",
            threadpool_value=0.95,
            ray_value=0.85,
            description="Accuracy mismatch",
        )
        
        data = discrepancy.to_dict()
        
        assert data["type"] == "metric_value_mismatch"
        assert data["variant_name"] == "Config B"
        assert "0.95" in data["threadpool_value"]


# ═══════════════════════════════════════════════════════════════
# ParityCheckResult Tests
# ═══════════════════════════════════════════════════════════════


class TestParityCheckResult:
    """Tests for ParityCheckResult."""
    
    def test_equivalent_result(self):
        """Equivalent result has no discrepancies."""
        result = ParityCheckResult(
            batch_test_id="batch-1",
            threadpool_status=BatchTestStatus.COMPLETED,
            ray_status=BatchTestStatus.COMPLETED,
            is_equivalent=True,
            discrepancies=[],
            threadpool_duration_ms=1000.0,
            ray_duration_ms=500.0,
            speedup_factor=2.0,
        )
        
        assert result.is_equivalent is True
        assert result.discrepancy_count == 0
        assert result.speedup_factor == 2.0
    
    def test_discrepant_result(self):
        """Discrepant result tracks issues."""
        discrepancy = ParityDiscrepancy(
            type=ParityDiscrepancyType.SUCCESS_STATUS_MISMATCH,
            variant_name="Config A",
            threadpool_value=True,
            ray_value=False,
            description="Mismatch",
        )
        
        result = ParityCheckResult(
            batch_test_id="batch-1",
            threadpool_status=BatchTestStatus.COMPLETED,
            ray_status=BatchTestStatus.COMPLETED,
            is_equivalent=False,
            discrepancies=[discrepancy],
        )
        
        assert result.is_equivalent is False
        assert result.discrepancy_count == 1
    
    def test_summary(self):
        """Summary is human-readable."""
        result = ParityCheckResult(
            batch_test_id="batch-1",
            threadpool_status=BatchTestStatus.COMPLETED,
            ray_status=BatchTestStatus.COMPLETED,
            is_equivalent=True,
            threadpool_duration_ms=1000.0,
            ray_duration_ms=500.0,
            speedup_factor=2.0,
        )
        
        summary = result.summary()
        
        assert "EQUIVALENT" in summary
        assert "batch-1" in summary
        assert "2.00x" in summary
    
    def test_to_dict(self):
        """Can serialize to dict."""
        result = ParityCheckResult(
            batch_test_id="batch-1",
            threadpool_status=BatchTestStatus.COMPLETED,
            ray_status=BatchTestStatus.COMPLETED,
            is_equivalent=True,
        )
        
        data = result.to_dict()
        
        assert data["batch_test_id"] == "batch-1"
        assert data["is_equivalent"] is True
        assert "comparison_timestamp" in data


# ═══════════════════════════════════════════════════════════════
# ParityChecker Unit Tests
# ═══════════════════════════════════════════════════════════════


class TestParityCheckerUnit:
    """Unit tests for ParityChecker with mocked runners."""
    
    def test_equivalent_results(self, sample_batch_test):
        """Checker detects equivalent results."""
        # Both runners return same results
        results = [
            BatchTestResult(
                combination_name="Config A",
                execution_id="exec-1",
                success=True,
                metrics={"accuracy": 0.95, "overall_score": 0.9},
                overall_score=0.9,
            ),
            BatchTestResult(
                combination_name="Config B",
                execution_id="exec-2",
                success=True,
                metrics={"accuracy": 0.85, "overall_score": 0.8},
                overall_score=0.8,
            ),
        ]
        
        threadpool_runner = create_mock_runner(results)
        ray_runner = create_mock_runner(results)
        
        checker = ParityChecker(
            threadpool_runner=threadpool_runner,
            ray_runner=ray_runner,
        )
        
        result = checker.check_parity(sample_batch_test)
        
        assert result.is_equivalent is True
        assert result.discrepancy_count == 0
    
    def test_success_status_mismatch(self, sample_batch_test):
        """Checker detects success status mismatch."""
        threadpool_results = [
            BatchTestResult(
                combination_name="Config A",
                execution_id="exec-1",
                success=True,
                metrics={"accuracy": 0.95},
            ),
        ]
        ray_results = [
            BatchTestResult(
                combination_name="Config A",
                execution_id="exec-1",
                success=False,  # Different
                metrics={"accuracy": 0.95},
                error_message="Failed",
            ),
        ]
        
        threadpool_runner = create_mock_runner(threadpool_results)
        ray_runner = create_mock_runner(ray_results)
        
        checker = ParityChecker(
            threadpool_runner=threadpool_runner,
            ray_runner=ray_runner,
        )
        
        result = checker.check_parity(sample_batch_test)
        
        assert result.is_equivalent is False
        assert any(
            d.type == ParityDiscrepancyType.SUCCESS_STATUS_MISMATCH
            for d in result.discrepancies
        )
    
    def test_metric_mismatch_detected(self, sample_batch_test):
        """Checker detects metric value mismatch."""
        threadpool_results = [
            BatchTestResult(
                combination_name="Config A",
                execution_id="exec-1",
                success=True,
                metrics={"accuracy": 0.95, "overall_score": 0.9},
            ),
        ]
        ray_results = [
            BatchTestResult(
                combination_name="Config A",
                execution_id="exec-1",
                success=True,
                metrics={"accuracy": 0.80, "overall_score": 0.7},  # Different
            ),
        ]
        
        threadpool_runner = create_mock_runner(threadpool_results)
        ray_runner = create_mock_runner(ray_results)
        
        checker = ParityChecker(
            threadpool_runner=threadpool_runner,
            ray_runner=ray_runner,
        )
        
        result = checker.check_parity(sample_batch_test)
        
        assert result.is_equivalent is False
        assert any(
            d.type == ParityDiscrepancyType.METRIC_VALUE_MISMATCH
            for d in result.discrepancies
        )
    
    def test_metric_tolerance_applied(self, sample_batch_test):
        """Checker applies tolerance for small differences."""
        threadpool_results = [
            BatchTestResult(
                combination_name="Config A",
                execution_id="exec-1",
                success=True,
                metrics={"accuracy": 0.950},
            ),
        ]
        ray_results = [
            BatchTestResult(
                combination_name="Config A",
                execution_id="exec-1",
                success=True,
                metrics={"accuracy": 0.951},  # 0.1% difference
            ),
        ]
        
        threadpool_runner = create_mock_runner(threadpool_results)
        ray_runner = create_mock_runner(ray_results)
        
        # 1% tolerance should accept 0.1% difference
        config = ParityCheckerConfig(metric_tolerance=0.01)
        checker = ParityChecker(
            threadpool_runner=threadpool_runner,
            ray_runner=ray_runner,
            config=config,
        )
        
        result = checker.check_parity(sample_batch_test)
        
        assert result.is_equivalent is True
    
    def test_strict_config_fails_on_small_difference(self, sample_batch_test):
        """Strict config fails on any difference."""
        threadpool_results = [
            BatchTestResult(
                combination_name="Config A",
                execution_id="exec-1",
                success=True,
                metrics={"accuracy": 0.950},
            ),
        ]
        ray_results = [
            BatchTestResult(
                combination_name="Config A",
                execution_id="exec-1",
                success=True,
                metrics={"accuracy": 0.951},  # Small difference
            ),
        ]
        
        threadpool_runner = create_mock_runner(threadpool_results)
        ray_runner = create_mock_runner(ray_results)
        
        config = ParityCheckerConfig.strict()
        checker = ParityChecker(
            threadpool_runner=threadpool_runner,
            ray_runner=ray_runner,
            config=config,
        )
        
        result = checker.check_parity(sample_batch_test)
        
        assert result.is_equivalent is False
    
    def test_timing_metrics_ignored(self, sample_batch_test):
        """Timing metrics are ignored by default."""
        threadpool_results = [
            BatchTestResult(
                combination_name="Config A",
                execution_id="exec-1",
                success=True,
                metrics={"accuracy": 0.95, "latency_ms": 100.0},
                duration_ms=1000,
            ),
        ]
        ray_results = [
            BatchTestResult(
                combination_name="Config A",
                execution_id="exec-1",
                success=True,
                metrics={"accuracy": 0.95, "latency_ms": 50.0},  # Different timing
                duration_ms=500,  # Different
            ),
        ]
        
        threadpool_runner = create_mock_runner(threadpool_results)
        ray_runner = create_mock_runner(ray_results)
        
        checker = ParityChecker(
            threadpool_runner=threadpool_runner,
            ray_runner=ray_runner,
        )
        
        result = checker.check_parity(sample_batch_test)
        
        # Should be equivalent since timing is ignored
        assert result.is_equivalent is True
    
    def test_result_count_mismatch(self, sample_batch_test):
        """Checker detects missing results."""
        threadpool_results = [
            BatchTestResult(combination_name="Config A", execution_id="1", success=True),
            BatchTestResult(combination_name="Config B", execution_id="2", success=True),
        ]
        ray_results = [
            BatchTestResult(combination_name="Config A", execution_id="1", success=True),
            # Config B missing
        ]
        
        threadpool_runner = create_mock_runner(threadpool_results)
        ray_runner = create_mock_runner(ray_results)
        
        checker = ParityChecker(
            threadpool_runner=threadpool_runner,
            ray_runner=ray_runner,
        )
        
        result = checker.check_parity(sample_batch_test)
        
        assert result.is_equivalent is False
        assert any(
            d.type == ParityDiscrepancyType.RESULT_COUNT_MISMATCH
            for d in result.discrepancies
        )
    
    def test_completion_status_mismatch(self, sample_batch_test):
        """Checker detects batch completion status mismatch."""
        threadpool_results = [
            BatchTestResult(combination_name="Config A", execution_id="1", success=True),
        ]
        ray_results = [
            BatchTestResult(combination_name="Config A", execution_id="1", success=True),
        ]
        
        threadpool_runner = create_mock_runner(
            threadpool_results, 
            status=BatchTestStatus.COMPLETED
        )
        ray_runner = create_mock_runner(
            ray_results, 
            status=BatchTestStatus.FAILED
        )
        
        checker = ParityChecker(
            threadpool_runner=threadpool_runner,
            ray_runner=ray_runner,
        )
        
        result = checker.check_parity(sample_batch_test)
        
        assert result.is_equivalent is False
        assert any(
            d.type == ParityDiscrepancyType.COMPLETION_STATUS_MISMATCH
            for d in result.discrepancies
        )
    
    def test_speedup_calculation(self, sample_batch_test):
        """Checker calculates speedup factor."""
        results = [
            BatchTestResult(combination_name="Config A", execution_id="1", success=True),
        ]
        
        threadpool_runner = create_mock_runner(results)
        ray_runner = create_mock_runner(results)
        
        checker = ParityChecker(
            threadpool_runner=threadpool_runner,
            ray_runner=ray_runner,
        )
        
        result = checker.check_parity(sample_batch_test)
        
        assert result.threadpool_duration_ms > 0
        assert result.ray_duration_ms > 0
        assert result.speedup_factor > 0
    
    def test_dry_run_clones_batch_test(self, sample_batch_test):
        """Dry run uses clones to avoid modifying original."""
        results = [
            BatchTestResult(combination_name="Config A", execution_id="1", success=True),
        ]
        
        threadpool_runner = create_mock_runner(results)
        ray_runner = create_mock_runner(results)
        
        checker = ParityChecker(
            threadpool_runner=threadpool_runner,
            ray_runner=ray_runner,
        )
        
        # Original should remain PENDING
        original_status = sample_batch_test.status
        
        result = checker.check_parity(sample_batch_test, dry_run=True)
        
        # Original unchanged
        assert sample_batch_test.status == original_status
        assert sample_batch_test.id == "batch-parity-test"


# ═══════════════════════════════════════════════════════════════
# Integration Tests (with actual runners)
# ═══════════════════════════════════════════════════════════════


class TestParityCheckerIntegration:
    """Integration tests for ParityChecker with actual runners."""
    
    @pytest.fixture
    def threadpool_runner(self):
        """Create ThreadPool runner."""
        from wtb.application.services.batch_test_runner import ThreadPoolBatchTestRunner
        from wtb.infrastructure.database import InMemoryUnitOfWork
        from wtb.infrastructure.adapters import InMemoryStateAdapter
        
        runner = ThreadPoolBatchTestRunner(
            max_workers=2,
            uow_factory=lambda: InMemoryUnitOfWork(),
            state_adapter_factory=lambda: InMemoryStateAdapter(),
        )
        yield runner
        runner.shutdown()
    
    @pytest.mark.skipif(
        not os.environ.get("RAY_INTEGRATION_TESTS"),
        reason="Ray integration test - requires Ray cluster"
    )
    def test_real_parity_check(self, threadpool_runner, sample_batch_test):
        """Integration test with real runners."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
            RAY_AVAILABLE,
        )
        
        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")
        
        ray_runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="sqlite:///data/wtb.db",
        )
        
        try:
            checker = ParityChecker(
                threadpool_runner=threadpool_runner,
                ray_runner=ray_runner,
            )
            
            result = checker.check_parity(sample_batch_test)
            
            # Just verify check completes
            assert result.batch_test_id.startswith("batch-parity-test")
            assert isinstance(result.is_equivalent, bool)
            
        finally:
            ray_runner.shutdown()


# ═══════════════════════════════════════════════════════════════
# Statistical Comparison Tests
# ═══════════════════════════════════════════════════════════════


class TestStatisticalComparison:
    """Tests for statistical comparison functionality."""
    
    def test_statistical_comparison(self, sample_batch_test):
        """Statistical comparison aggregates multiple runs."""
        results = [
            BatchTestResult(
                combination_name="Config A",
                execution_id="1",
                success=True,
                metrics={"accuracy": 0.95},
            ),
        ]
        
        threadpool_runner = create_mock_runner(results)
        ray_runner = create_mock_runner(results)
        
        checker = ParityChecker(
            threadpool_runner=threadpool_runner,
            ray_runner=ray_runner,
        )
        
        stats = checker.run_statistical_comparison(sample_batch_test, num_runs=3)
        
        assert stats["num_runs"] == 3
        assert stats["equivalent_runs"] == 3
        assert stats["equivalence_rate"] == 1.0
        assert len(stats["results"]) == 3
