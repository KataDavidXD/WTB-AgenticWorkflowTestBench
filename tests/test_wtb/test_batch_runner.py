"""
Tests for Batch Test Runners.

Tests IBatchTestRunner interface, ThreadPoolBatchTestRunner, and BatchTestRunnerFactory.
"""

import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch

from wtb.domain.models.batch_test import (
    BatchTest,
    BatchTestStatus,
    VariantCombination,
    BatchTestResult,
)
from wtb.domain.models.workflow import TestWorkflow
from wtb.domain.interfaces.batch_runner import (
    IBatchTestRunner,
    BatchRunnerStatus,
    BatchRunnerProgress,
    BatchRunnerError,
)
from wtb.application.services.batch_test_runner import ThreadPoolBatchTestRunner
from wtb.application.services.ray_batch_runner import RAY_AVAILABLE
from wtb.config import RayConfig
from wtb.application.factories import BatchTestRunnerFactory
from wtb.infrastructure.database import InMemoryUnitOfWork
from wtb.infrastructure.adapters import InMemoryStateAdapter


class TestRayConfig:
    """Tests for RayConfig dataclass."""
    
    def test_default_config(self):
        """Default config has sensible values."""
        config = RayConfig()
        
        assert config.ray_address == "auto"
        assert config.num_cpus_per_task == 1.0
        assert config.max_retries == 3
    
    def test_for_local_development(self):
        """Local development config is appropriate."""
        config = RayConfig.for_local_development()
        
        assert config.ray_address == "auto"
        assert config.max_pending_tasks == 4
        assert config.max_retries == 1
    
    def test_for_production(self):
        """Production config with custom address."""
        config = RayConfig.for_production(
            ray_address="ray://cluster:10001",
            num_workers=20,
            memory_gb=8.0,
        )
        
        assert config.ray_address == "ray://cluster:10001"
        assert config.memory_per_task_gb == 8.0
        assert config.max_pending_tasks == 40
    
    def test_for_testing(self):
        """Testing config uses minimal resources."""
        config = RayConfig.for_testing()
        
        assert config.num_cpus_per_task == 0.5
        assert config.memory_per_task_gb == 0.5
        assert config.max_pending_tasks == 2
    
    def test_to_dict(self):
        """Config can be serialized to dict."""
        config = RayConfig(ray_address="auto", max_retries=5)
        d = config.to_dict()
        
        assert d["ray_address"] == "auto"
        assert d["max_retries"] == 5


class TestThreadPoolBatchTestRunner:
    """Tests for ThreadPoolBatchTestRunner."""
    
    @pytest.fixture
    def runner(self):
        """Create a test runner."""
        runner = ThreadPoolBatchTestRunner(
            uow_factory=lambda: InMemoryUnitOfWork(),
            state_adapter_factory=lambda: InMemoryStateAdapter(),
            max_workers=2,
            execution_timeout_seconds=60.0,
        )
        yield runner
        runner.shutdown()
    
    @pytest.fixture
    def batch_test(self):
        """Create a test batch test."""
        return BatchTest(
            id="batch-1",
            name="Test Batch",
            workflow_id="wf-1",
            variant_combinations=[
                VariantCombination(name="Config A", variants={"node-1": "variant-a"}),
                VariantCombination(name="Config B", variants={"node-1": "variant-b"}),
            ],
            initial_state={"input": "test"},
            parallel_count=2,
        )
    
    def test_create_runner(self, runner):
        """Runner can be created."""
        assert runner is not None
        assert isinstance(runner, IBatchTestRunner)
    
    def test_run_batch_test_empty_combinations(self, runner):
        """Empty combinations raises error."""
        batch_test = BatchTest(
            id="batch-1",
            name="Empty Test",
            workflow_id="wf-1",
            variant_combinations=[],
        )
        
        with pytest.raises(BatchRunnerError):
            runner.run_batch_test(batch_test)
    
    def test_run_batch_test_basic(self, runner, batch_test):
        """Can run a basic batch test."""
        # Pre-populate workflow in UoW
        # Note: The runner creates its own UoW per thread, so we can't pre-populate
        # The test will fail gracefully
        
        result = runner.run_batch_test(batch_test)
        
        # Should have results (possibly failed due to missing workflow)
        assert result.status in [BatchTestStatus.COMPLETED, BatchTestStatus.FAILED]
        assert len(result.results) == 2
    
    def test_get_status_idle(self, runner, batch_test):
        """Status is IDLE when not running."""
        status = runner.get_status(batch_test.id)
        assert status == BatchRunnerStatus.IDLE
    
    def test_get_progress_not_running(self, runner, batch_test):
        """Progress is None when not running."""
        progress = runner.get_progress(batch_test.id)
        assert progress is None
    
    def test_cancel_not_running(self, runner, batch_test):
        """Cancel returns False when not running."""
        result = runner.cancel(batch_test.id)
        assert result is False
    
    def test_shutdown(self, runner):
        """Runner can be shut down."""
        runner.shutdown()
        
        batch_test = BatchTest(
            id="batch-1",
            name="Test",
            workflow_id="wf-1",
            variant_combinations=[VariantCombination(name="A", variants={})],
        )
        
        with pytest.raises(BatchRunnerError):
            runner.run_batch_test(batch_test)


class TestBatchRunnerProgress:
    """Tests for BatchRunnerProgress."""
    
    def test_progress_pct_calculation(self):
        """Progress percentage is calculated correctly."""
        progress = BatchRunnerProgress(
            batch_test_id="batch-1",
            total_variants=10,
            completed_variants=5,
            failed_variants=1,
            in_progress_variants=4,
            elapsed_ms=5000.0,
        )
        
        assert progress.progress_pct == 50.0
    
    def test_progress_pct_zero_total(self):
        """Progress is 100% when total is 0."""
        progress = BatchRunnerProgress(
            batch_test_id="batch-1",
            total_variants=0,
            completed_variants=0,
            failed_variants=0,
            in_progress_variants=0,
            elapsed_ms=0.0,
        )
        
        assert progress.progress_pct == 100.0


class TestBatchTestRunnerFactory:
    """Tests for BatchTestRunnerFactory."""
    
    def test_create_for_testing(self):
        """Can create runner for testing."""
        runner = BatchTestRunnerFactory.create_for_testing()
        
        assert runner is not None
        assert isinstance(runner, IBatchTestRunner)
        
        runner.shutdown()
    
    def test_create_threadpool(self):
        """Can create ThreadPool runner."""
        runner = BatchTestRunnerFactory.create_threadpool(max_workers=4)
        
        assert runner is not None
        assert isinstance(runner, ThreadPoolBatchTestRunner)
        
        runner.shutdown()
    
    def test_create_uses_config(self):
        """Create uses config to select implementation."""
        from wtb.config import WTBConfig
        
        config = WTBConfig.for_testing()
        runner = BatchTestRunnerFactory.create(config)
        
        # Should be ThreadPool since ray_enabled is False
        assert isinstance(runner, ThreadPoolBatchTestRunner)
        
        runner.shutdown()


class TestEnvironmentProviders:
    """Tests for environment providers."""
    
    def test_inprocess_provider(self):
        """InProcessEnvironmentProvider works."""
        from wtb.infrastructure.environment.providers import InProcessEnvironmentProvider
        
        provider = InProcessEnvironmentProvider()
        
        env = provider.create_environment("variant-1", {"pip": ["numpy"]})
        assert env["type"] == "inprocess"
        
        runtime_env = provider.get_runtime_env("variant-1")
        assert runtime_env is None  # No runtime env for inprocess
        
        provider.cleanup_environment("variant-1")
    
    def test_ray_environment_provider(self):
        """RayEnvironmentProvider creates runtime_env."""
        from wtb.infrastructure.environment.providers import RayEnvironmentProvider
        
        provider = RayEnvironmentProvider()
        
        env = provider.create_environment("variant-1", {
            "pip": ["numpy==1.24.0"],
            "env_vars": {"MODEL_VERSION": "v1"},
        })
        
        assert "pip" in env
        assert "numpy==1.24.0" in env["pip"]
        
        runtime_env = provider.get_runtime_env("variant-1")
        assert runtime_env is not None
        assert "env_vars" in runtime_env
        
        provider.cleanup_environment("variant-1")
    
    def test_ray_provider_merges_base_env(self):
        """RayEnvironmentProvider merges with base_env."""
        from wtb.infrastructure.environment.providers import RayEnvironmentProvider
        
        provider = RayEnvironmentProvider(base_env={
            "pip": ["base-package"],
            "env_vars": {"BASE_VAR": "1"},
        })
        
        env = provider.create_environment("variant-1", {
            "pip": ["extra-package"],
            "env_vars": {"EXTRA_VAR": "2"},
        })
        
        assert len(env["pip"]) == 2
        assert "base-package" in env["pip"]
        assert "extra-package" in env["pip"]
        assert env["env_vars"]["BASE_VAR"] == "1"
        assert env["env_vars"]["EXTRA_VAR"] == "2"


class TestRayBatchTestRunner:
    """Tests for RayBatchTestRunner (stub tests - no actual Ray)."""
    
    def test_is_available_check(self):
        """Can check if Ray is available."""
        from wtb.application.services.ray_batch_runner import RayBatchTestRunner
        
        # This returns True or False based on whether ray is installed
        available = RayBatchTestRunner.is_available()
        assert isinstance(available, bool)
    
    @pytest.mark.skipif(
        not RAY_AVAILABLE,
        reason="Ray not installed"
    )
    def test_create_ray_runner(self):
        """Can create Ray runner (requires Ray)."""
        from wtb.application.services.ray_batch_runner import RayBatchTestRunner, RayConfig
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="sqlite:///data/wtb.db",
        )
        
        assert runner is not None
        runner.shutdown()

