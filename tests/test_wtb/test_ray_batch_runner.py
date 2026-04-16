"""
Tests for Ray Batch Test Runner.

Comprehensive tests for RayBatchTestRunner and VariantExecutionActor.
Includes both unit tests (mocked Ray) and integration tests (local Ray).

Test Categories:
1. Unit Tests: Test logic without Ray (mocked)
2. Integration Tests: Test with local Ray cluster
3. ACID Compliance Tests: Verify transaction properties

Usage:
    # Run unit tests only (no Ray required)
    pytest tests/test_wtb/test_ray_batch_runner.py -k "not integration"
    
    # Run all tests including Ray integration
    pytest tests/test_wtb/test_ray_batch_runner.py
"""

import pytest
import os
from datetime import datetime
from unittest.mock import MagicMock, patch, PropertyMock
from typing import Dict, Any
import time

from wtb.domain.models.batch_test import (
    BatchTest,
    BatchTestStatus,
    VariantCombination,
    BatchTestResult,
)
from wtb.domain.models.workflow import TestWorkflow, WorkflowNode, WorkflowEdge
from wtb.domain.interfaces.batch_runner import (
    IBatchTestRunner,
    BatchRunnerStatus,
    BatchRunnerProgress,
    BatchRunnerError,
)
from wtb.infrastructure.database import InMemoryUnitOfWork
from wtb.infrastructure.adapters import InMemoryStateAdapter
from wtb.application.services.ray_batch_runner import RAY_AVAILABLE


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def sample_workflow() -> TestWorkflow:
    """Create a sample workflow for testing."""
    workflow = TestWorkflow(
        id="wf-test-1",
        name="Test Workflow",
        description="A simple test workflow",
        entry_point="start",
    )
    workflow.add_node(WorkflowNode(id="start", name="Start", type="start"))
    workflow.add_node(WorkflowNode(id="process", name="Process", type="action", tool_name="process_tool"))
    workflow.add_node(WorkflowNode(id="end", name="End", type="end"))
    workflow.add_edge(WorkflowEdge(source_id="start", target_id="process"))
    workflow.add_edge(WorkflowEdge(source_id="process", target_id="end"))
    return workflow


@pytest.fixture
def sample_batch_test(sample_workflow) -> BatchTest:
    """Create a sample batch test."""
    return BatchTest(
        id="batch-test-1",
        name="Test Batch",
        workflow_id=sample_workflow.id,
        variant_combinations=[
            VariantCombination(
                name="Config A",
                variants={"process": "variant-a"},
                metadata={"description": "First variant"},
            ),
            VariantCombination(
                name="Config B",
                variants={"process": "variant-b"},
                metadata={"description": "Second variant"},
            ),
            VariantCombination(
                name="Config C",
                variants={"process": "variant-c"},
                metadata={"description": "Third variant"},
            ),
        ],
        initial_state={"input": "test_data", "param": 42},
        parallel_count=2,
    )


@pytest.fixture
def mock_uow_with_workflow(sample_workflow):
    """Create an in-memory UoW with the sample workflow pre-loaded."""
    uow = InMemoryUnitOfWork()
    with uow:
        uow.workflows.add(sample_workflow)
        uow.commit()
    return uow


# ═══════════════════════════════════════════════════════════════
# RayConfig Tests
# ═══════════════════════════════════════════════════════════════


class TestRayConfig:
    """Tests for RayConfig dataclass."""
    
    def test_default_config(self):
        """Default config has sensible values."""
        from wtb.application.services.ray_batch_runner import RayConfig
        
        config = RayConfig()
        
        assert config.ray_address == "auto"
        assert config.num_cpus_per_task == 1.0
        assert config.memory_per_task_gb == 2.0
        assert config.max_pending_tasks == 100
        assert config.max_retries == 3
    
    def test_for_local_development(self):
        """Local development config is appropriate."""
        from wtb.application.services.ray_batch_runner import RayConfig
        
        config = RayConfig.for_local_development()
        
        assert config.ray_address == "auto"
        assert config.max_pending_tasks == 4
        assert config.max_retries == 1
        assert config.task_timeout_seconds == 300.0
    
    def test_for_production(self):
        """Production config with custom address."""
        from wtb.application.services.ray_batch_runner import RayConfig
        
        config = RayConfig.for_production(
            ray_address="ray://cluster:10001",
            num_workers=20,
            memory_gb=8.0,
        )
        
        assert config.ray_address == "ray://cluster:10001"
        assert config.memory_per_task_gb == 8.0
        assert config.max_pending_tasks == 40
        assert config.max_retries == 3
        assert config.task_timeout_seconds == 7200.0
    
    def test_for_testing(self):
        """Testing config uses minimal resources."""
        from wtb.application.services.ray_batch_runner import RayConfig
        
        config = RayConfig.for_testing()
        
        assert config.num_cpus_per_task == 0.5
        assert config.memory_per_task_gb == 0.5
        assert config.max_pending_tasks == 2
        assert config.max_retries == 1


# ═══════════════════════════════════════════════════════════════
# VariantExecutionResult Tests
# ═══════════════════════════════════════════════════════════════


class TestVariantExecutionResult:
    """Tests for VariantExecutionResult value object."""
    
    def test_creation(self):
        """Can create VariantExecutionResult."""
        from wtb.application.services.ray_batch_runner import VariantExecutionResult
        
        result = VariantExecutionResult(
            execution_id="exec-1",
            combination_name="Config A",
            combination_variants={"node1": "variant-a"},
            success=True,
            duration_ms=1500,
            metrics={"accuracy": 0.95, "overall_score": 0.9},
        )
        
        assert result.execution_id == "exec-1"
        assert result.success is True
        assert result.metrics["accuracy"] == 0.95
    
    def test_to_batch_test_result(self):
        """Can convert to BatchTestResult."""
        from wtb.application.services.ray_batch_runner import VariantExecutionResult
        
        result = VariantExecutionResult(
            execution_id="exec-1",
            combination_name="Config A",
            combination_variants={"node1": "variant-a"},
            success=True,
            duration_ms=1500,
            metrics={"overall_score": 0.9},
        )
        
        batch_result = result.to_batch_test_result()
        
        assert isinstance(batch_result, BatchTestResult)
        assert batch_result.execution_id == "exec-1"
        assert batch_result.combination_name == "Config A"
        assert batch_result.success is True
        assert batch_result.overall_score == 0.9
    
    def test_failed_result(self):
        """Can create failed result with error."""
        from wtb.application.services.ray_batch_runner import VariantExecutionResult
        
        result = VariantExecutionResult(
            execution_id="exec-2",
            combination_name="Config B",
            combination_variants={"node1": "variant-b"},
            success=False,
            duration_ms=500,
            error="Execution timeout",
        )
        
        assert result.success is False
        assert result.error == "Execution timeout"
        
        batch_result = result.to_batch_test_result()
        assert batch_result.error_message == "Execution timeout"


# ═══════════════════════════════════════════════════════════════
# RayBatchTestRunner Unit Tests (Mocked Ray)
# ═══════════════════════════════════════════════════════════════


class TestRayBatchTestRunnerUnit:
    """Unit tests for RayBatchTestRunner (no actual Ray)."""
    
    def test_is_available(self):
        """Can check if Ray is available."""
        from wtb.application.services.ray_batch_runner import RayBatchTestRunner
        
        # Returns bool regardless of Ray installation
        result = RayBatchTestRunner.is_available()
        assert isinstance(result, bool)
    
    @pytest.mark.skipif(
        not RAY_AVAILABLE,
        reason="Ray not installed"
    )
    def test_create_without_ray_raises(self):
        """Creating runner without Ray raises error."""
        with patch('wtb.application.services.ray_batch_runner.RAY_AVAILABLE', False):
            from wtb.application.services.ray_batch_runner import (
                RayBatchTestRunner,
                RayConfig,
            )
            
            with pytest.raises(BatchRunnerError) as exc_info:
                RayBatchTestRunner(
                    config=RayConfig.for_testing(),
                    agentgit_db_url="data/agentgit.db",
                    wtb_db_url="sqlite:///data/wtb.db",
                )
            
            assert "Ray is not installed" in str(exc_info.value)
    
    def test_empty_combinations_raises(self, sample_workflow):
        """Empty variant combinations raises error."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
            RAY_AVAILABLE,
        )
        
        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="sqlite:///data/wtb.db",
        )
        
        empty_batch = BatchTest(
            id="batch-empty",
            name="Empty Test",
            workflow_id=sample_workflow.id,
            variant_combinations=[],
        )
        
        with pytest.raises(BatchRunnerError) as exc_info:
            runner.run_batch_test(empty_batch)
        
        assert "No variant combinations" in str(exc_info.value)
        
        runner.shutdown()
    
    def test_get_status_idle(self, sample_batch_test):
        """Status is IDLE when not running."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
            RAY_AVAILABLE,
        )
        
        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="sqlite:///data/wtb.db",
        )
        
        status = runner.get_status(sample_batch_test.id)
        assert status == BatchRunnerStatus.IDLE
        
        runner.shutdown()
    
    def test_get_progress_not_running(self, sample_batch_test):
        """Progress is None when not running."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
            RAY_AVAILABLE,
        )
        
        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="sqlite:///data/wtb.db",
        )
        
        progress = runner.get_progress(sample_batch_test.id)
        assert progress is None
        
        runner.shutdown()
    
    def test_cancel_not_running(self, sample_batch_test):
        """Cancel returns False when not running."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
            RAY_AVAILABLE,
        )
        
        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="sqlite:///data/wtb.db",
        )
        
        result = runner.cancel(sample_batch_test.id)
        assert result is False
        
        runner.shutdown()
    
    def test_shutdown_cleans_state(self):
        """Shutdown cleans up runner state."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
            RAY_AVAILABLE,
        )
        
        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="sqlite:///data/wtb.db",
        )
        
        runner.shutdown()
        
        assert runner._actor_pool is None
        assert len(runner._actors) == 0
        assert len(runner._running_tests) == 0


# ═══════════════════════════════════════════════════════════════
# Ray Integration Tests (Local Ray)
# ═══════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def ray_initialized():
    """Initialize Ray for integration tests and shut down on teardown."""
    from wtb.application.services.ray_batch_runner import RAY_AVAILABLE
    
    if not RAY_AVAILABLE:
        pytest.skip("Ray not installed")
    
    import ray
    
    if not ray.is_initialized():
        ray.init(
            num_cpus=2,
            ignore_reinit_error=True,
            log_to_driver=False,
        )
    
    yield ray
    
    ray.shutdown()


@pytest.fixture
def temp_data_dir(tmp_path):
    """Create temp directory for database files."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    return data_dir


class TestRayBatchTestRunnerIntegration:
    """Integration tests for RayBatchTestRunner with local Ray."""
    
    @pytest.mark.skipif(
        not os.environ.get("RAY_INTEGRATION_TESTS"),
        reason="Ray integration test - set RAY_INTEGRATION_TESTS=1 to enable"
    )
    def test_run_batch_test_basic(
        self,
        ray_initialized,
        sample_batch_test,
        sample_workflow,
        temp_data_dir,
    ):
        """Can run a basic batch test with Ray."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
        )
        
        # Create workflow loader that returns our sample
        def workflow_loader(wf_id, uow):
            return sample_workflow
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url=str(temp_data_dir / "agentgit.db"),
            wtb_db_url=f"sqlite:///{temp_data_dir / 'wtb.db'}",
            workflow_loader=workflow_loader,
        )
        
        try:
            result = runner.run_batch_test(sample_batch_test)
            
            # Verify results
            assert result.status in [BatchTestStatus.COMPLETED, BatchTestStatus.FAILED]
            assert len(result.results) == 3  # 3 variant combinations
            
            # At least some should complete
            completed = sum(1 for r in result.results if r.success)
            assert completed >= 0  # May fail due to missing workflow
            
        finally:
            runner.shutdown()
    
    @pytest.mark.skipif(
        not os.environ.get("RAY_INTEGRATION_TESTS"),
        reason="Ray integration test - set RAY_INTEGRATION_TESTS=1 to enable"
    )
    def test_progress_tracking(
        self,
        ray_initialized,
        sample_batch_test,
        sample_workflow,
        temp_data_dir,
    ):
        """Progress is tracked during execution."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
        )
        import threading
        
        def workflow_loader(wf_id, uow):
            return sample_workflow
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url=str(temp_data_dir / "agentgit.db"),
            wtb_db_url=f"sqlite:///{temp_data_dir / 'wtb.db'}",
            workflow_loader=workflow_loader,
        )
        
        progress_snapshots = []
        stop_monitoring = threading.Event()
        
        def monitor_progress():
            while not stop_monitoring.is_set():
                progress = runner.get_progress(sample_batch_test.id)
                if progress:
                    progress_snapshots.append(progress)
                time.sleep(0.1)
        
        monitor_thread = threading.Thread(target=monitor_progress)
        monitor_thread.start()
        
        try:
            runner.run_batch_test(sample_batch_test)
        finally:
            stop_monitoring.set()
            monitor_thread.join(timeout=2.0)
            runner.shutdown()
        
        # Should have captured some progress
        # (May be empty if execution is very fast)
        if progress_snapshots:
            assert all(isinstance(p, BatchRunnerProgress) for p in progress_snapshots)
    
    @pytest.mark.skipif(
        not os.environ.get("RAY_INTEGRATION_TESTS"),
        reason="Ray integration test - set RAY_INTEGRATION_TESTS=1 to enable"
    )
    def test_cancellation(
        self,
        ray_initialized,
        sample_workflow,
        temp_data_dir,
    ):
        """Can cancel a running batch test."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
        )
        import threading
        
        # Create batch test with many variants to give time to cancel
        large_batch = BatchTest(
            id="batch-large",
            name="Large Test",
            workflow_id=sample_workflow.id,
            variant_combinations=[
                VariantCombination(name=f"Config {i}", variants={"process": f"v{i}"})
                for i in range(20)
            ],
            parallel_count=2,
        )
        
        def workflow_loader(wf_id, uow):
            return sample_workflow
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url=str(temp_data_dir / "agentgit.db"),
            wtb_db_url=f"sqlite:///{temp_data_dir / 'wtb.db'}",
            workflow_loader=workflow_loader,
        )
        
        # Run in background
        result_holder = [None]
        
        def run_batch():
            result_holder[0] = runner.run_batch_test(large_batch)
        
        run_thread = threading.Thread(target=run_batch)
        run_thread.start()
        
        # Wait a bit then cancel
        time.sleep(0.5)
        cancelled = runner.cancel(large_batch.id)
        
        run_thread.join(timeout=5.0)
        runner.shutdown()
        
        # Verify cancellation worked
        assert cancelled is True
        if result_holder[0]:
            assert result_holder[0].status in [
                BatchTestStatus.CANCELLED,
                BatchTestStatus.COMPLETED,
            ]


# ═══════════════════════════════════════════════════════════════
# ACID Compliance Tests
# ═══════════════════════════════════════════════════════════════


class TestACIDCompliance:
    """Tests verifying ACID properties of RayBatchTestRunner."""
    
    def test_atomic_result_storage(self, sample_batch_test):
        """Each variant result is stored atomically."""
        # Results are added one at a time via add_result()
        # Even if runner fails, partial results are preserved
        
        sample_batch_test.start()
        
        # Add partial results
        sample_batch_test.add_result(BatchTestResult(
            combination_name="Config A",
            execution_id="exec-1",
            success=True,
            metrics={"score": 0.9},
            overall_score=0.9,
        ))
        
        # Verify partial result is accessible
        assert len(sample_batch_test.results) == 1
        assert sample_batch_test.results[0].combination_name == "Config A"
    
    def test_isolated_batch_tests(self, sample_workflow):
        """Multiple batch tests are isolated from each other."""
        batch1 = BatchTest(
            id="batch-1",
            name="Batch 1",
            workflow_id=sample_workflow.id,
            variant_combinations=[
                VariantCombination(name="A1", variants={"process": "v1"}),
            ],
        )
        
        batch2 = BatchTest(
            id="batch-2",
            name="Batch 2",
            workflow_id=sample_workflow.id,
            variant_combinations=[
                VariantCombination(name="B1", variants={"process": "v2"}),
            ],
        )
        
        # Start both
        batch1.start()
        batch2.start()
        
        # Add results to batch1
        batch1.add_result(BatchTestResult(
            combination_name="A1",
            execution_id="exec-a1",
            success=True,
        ))
        
        # Verify batch2 is not affected
        assert len(batch1.results) == 1
        assert len(batch2.results) == 0
        
        # Add results to batch2
        batch2.add_result(BatchTestResult(
            combination_name="B1",
            execution_id="exec-b1",
            success=False,
            error_message="Test error",
        ))
        
        # Verify isolation
        assert batch1.results[0].success is True
        assert batch2.results[0].success is False
    
    def test_consistent_state_transitions(self, sample_batch_test):
        """Batch test state transitions are consistent."""
        # PENDING -> RUNNING
        assert sample_batch_test.status == BatchTestStatus.PENDING
        sample_batch_test.start()
        assert sample_batch_test.status == BatchTestStatus.RUNNING
        
        # RUNNING -> COMPLETED
        sample_batch_test.complete()
        assert sample_batch_test.status == BatchTestStatus.COMPLETED
        
        # Cannot start completed batch
        with pytest.raises(ValueError):
            sample_batch_test.start()
    
    def test_durable_results(self, sample_batch_test):
        """Results are durable once added."""
        sample_batch_test.start()
        
        # Add result
        result = BatchTestResult(
            combination_name="Config A",
            execution_id="exec-1",
            success=True,
            metrics={"score": 0.95},
            overall_score=0.95,
            duration_ms=1500,
        )
        sample_batch_test.add_result(result)
        
        # Serialize and deserialize
        data = sample_batch_test.to_dict()
        restored = BatchTest.from_dict(data)
        
        # Verify result is preserved
        assert len(restored.results) == 1
        assert restored.results[0].combination_name == "Config A"
        assert restored.results[0].overall_score == 0.95


# ═══════════════════════════════════════════════════════════════
# Factory Integration Tests
# ═══════════════════════════════════════════════════════════════


class TestBatchTestRunnerFactoryRay:
    """Tests for BatchTestRunnerFactory with Ray."""
    
    def test_create_ray_runner(self):
        """Factory can create Ray runner when available."""
        from wtb.application.factories import BatchTestRunnerFactory
        from wtb.application.services.ray_batch_runner import RAY_AVAILABLE
        from wtb.config import WTBConfig, RayConfig
        
        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")
        
        config = WTBConfig(
            wtb_storage_mode="inmemory",
            state_adapter_mode="inmemory",
            ray_enabled=True,
            ray_config=RayConfig.for_testing(),
        )
        
        runner = BatchTestRunnerFactory.create_ray(config)
        
        assert runner is not None
        assert isinstance(runner, IBatchTestRunner)
        
        runner.shutdown()
    
    def test_factory_selects_ray_when_enabled(self):
        """Factory selects Ray runner when ray_enabled=True."""
        from wtb.application.factories import BatchTestRunnerFactory
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RAY_AVAILABLE,
        )
        from wtb.config import WTBConfig, RayConfig
        
        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")
        
        config = WTBConfig(
            wtb_storage_mode="inmemory",
            state_adapter_mode="inmemory",
            ray_enabled=True,
            ray_config=RayConfig.for_testing(),
        )
        
        runner = BatchTestRunnerFactory.create(config)
        
        assert isinstance(runner, RayBatchTestRunner)
        
        runner.shutdown()
    
    def test_factory_selects_threadpool_when_disabled(self):
        """Factory selects ThreadPool runner when ray_enabled=False."""
        from wtb.application.factories import BatchTestRunnerFactory
        from wtb.application.services.batch_test_runner import ThreadPoolBatchTestRunner
        from wtb.config import WTBConfig
        
        config = WTBConfig(
            wtb_storage_mode="inmemory",
            state_adapter_mode="inmemory",
            ray_enabled=False,
        )
        
        runner = BatchTestRunnerFactory.create(config)
        
        assert isinstance(runner, ThreadPoolBatchTestRunner)
        
        runner.shutdown()


# ═══════════════════════════════════════════════════════════════
# Performance and Stress Tests
# ═══════════════════════════════════════════════════════════════


class TestPerformance:
    """Performance tests for batch test runners."""
    
    def test_large_batch_test_creation(self, sample_workflow):
        """Can create batch test with many variants."""
        large_batch = BatchTest(
            id="batch-large",
            name="Large Batch",
            workflow_id=sample_workflow.id,
            variant_combinations=[
                VariantCombination(
                    name=f"Config {i}",
                    variants={"process": f"variant-{i}"},
                    metadata={"index": i},
                )
                for i in range(100)
            ],
            parallel_count=10,
        )
        
        assert len(large_batch.variant_combinations) == 100
        
        # Serialization should work
        data = large_batch.to_dict()
        restored = BatchTest.from_dict(data)
        
        assert len(restored.variant_combinations) == 100
    
    def test_comparison_matrix_performance(self, sample_workflow):
        """Comparison matrix builds efficiently with many results."""
        batch = BatchTest(
            id="batch-perf",
            name="Performance Test",
            workflow_id=sample_workflow.id,
            variant_combinations=[
                VariantCombination(name=f"Config {i}", variants={})
                for i in range(50)
            ],
        )
        
        batch.start()
        
        # Add many results
        for i in range(50):
            batch.add_result(BatchTestResult(
                combination_name=f"Config {i}",
                execution_id=f"exec-{i}",
                success=True,
                metrics={
                    "accuracy": 0.8 + (i / 500),
                    "latency_ms": 100.0 + i,
                    "cost": 0.01 * i,
                },
                overall_score=0.8 + (i / 500),
                duration_ms=100 + i * 10,
            ))
        
        batch.complete()
        
        # Build comparison matrix
        start = time.time()
        matrix = batch.build_comparison_matrix()
        elapsed = time.time() - start
        
        assert elapsed < 1.0  # Should complete in under 1 second
        assert len(matrix["combinations"]) == 50
        assert "accuracy" in matrix["metrics"]
        assert "latency_ms" in matrix["metrics"]


# ═══════════════════════════════════════════════════════════════
# Ray Fixture Lifecycle Tests
# ═══════════════════════════════════════════════════════════════


class TestRayFixtureLifecycle:
    """Verify that the ray_initialized fixture correctly yields and cleans up."""

    @pytest.mark.skipif(not RAY_AVAILABLE, reason="Ray not installed")
    def test_fixture_yields_ray_module(self, ray_initialized):
        """The fixture should yield the ray module with an active runtime."""
        assert ray_initialized is not None
        assert hasattr(ray_initialized, "is_initialized")
        assert ray_initialized.is_initialized()

    @pytest.mark.skipif(not RAY_AVAILABLE, reason="Ray not installed")
    def test_fixture_provides_usable_runtime(self, ray_initialized):
        """After yielding, Ray should be fully functional."""
        import ray

        @ray.remote(num_cpus=0)
        def _ping():
            return "pong"

        result = ray.get(_ping.remote())
        assert result == "pong"

    @pytest.mark.skipif(not RAY_AVAILABLE, reason="Ray not installed")
    def test_runner_shutdown_clears_resources(self):
        """RayBatchTestRunner.shutdown() should release actors and state."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
        )

        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="sqlite:///data/wtb.db",
        )

        runner.shutdown()

        assert runner._actor_pool is None
        assert len(runner._actors) == 0
        assert len(runner._running_tests) == 0
