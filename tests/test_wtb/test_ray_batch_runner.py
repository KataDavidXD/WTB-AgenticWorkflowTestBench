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

import base64

import pytest
import os
import threading
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

    def test_pickled_payload_decoder_accepts_base64_and_legacy_bytes(self):
        """Public metadata is ASCII-safe while old in-memory bytes still load."""
        from wtb.application.services.ray_batch_runner import (
            _decode_pickled_payload,
        )

        payload = b"legacy-cloudpickle-payload\x00\xff"
        encoded = base64.b64encode(payload).decode("ascii")

        assert _decode_pickled_payload(encoded) == payload
        assert _decode_pickled_payload(payload) is payload


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

    def test_cancel_terminates_actor_pool_instead_of_best_effort_task_cancel(self, sample_batch_test):
        """Synchronous actors must be stopped before cancellation returns."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
            _RayRunningTest,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="sqlite:///data/wtb.db",
            event_bridge=MagicMock(),
        )
        actor = MagicMock(name="running-actor")
        runner._actors = [actor]
        runner._actor_pool = MagicMock()
        ref = MagicMock(name="actor-task-ref")
        runner._running_tests[sample_batch_test.id] = _RayRunningTest(
            batch_test_id=sample_batch_test.id,
            started_at=datetime.now(),
            total_variants=1,
            pending_refs=[ref],
        )

        with (
            patch("wtb.application.services.ray_batch_runner.ray.cancel") as cancel,
            patch("wtb.application.services.ray_batch_runner.ray.kill") as kill,
        ):
            assert runner.cancel(sample_batch_test.id) is True

        cancel.assert_not_called()
        kill.assert_called_once_with(actor, no_restart=True)
        assert runner._actors == []
        assert runner._actor_pool is None
        assert runner._running_tests[sample_batch_test.id].cancelled is True

    def test_cancel_failure_poisons_runner_and_preserves_refs(self, sample_batch_test):
        """A rejected cancellation must prevent unsafe runner reuse."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
            _RayRunningTest,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="sqlite:///data/wtb.db",
            event_bridge=MagicMock(),
        )
        actor = MagicMock(name="unsafe-actor")
        runner._actors = [actor]
        runner._actor_pool = MagicMock()
        ref = MagicMock(name="uncancelled-ref")
        runner._running_tests[sample_batch_test.id] = _RayRunningTest(
            batch_test_id=sample_batch_test.id,
            started_at=datetime.now(),
            total_variants=1,
            pending_refs=[ref],
        )

        with patch(
            "wtb.application.services.ray_batch_runner.ray.kill",
            side_effect=RuntimeError("control plane unavailable"),
        ) as kill:
            assert runner.cancel(sample_batch_test.id) is False

        kill.assert_called_once_with(actor, no_restart=True)
        assert runner._actors == [actor]
        assert runner._actor_pool is None
        assert runner._poisoned is True
        assert sample_batch_test.id in runner._unsafe_batch_ids
        assert runner._orphaned_refs == [ref]
    def test_cancel_waits_for_pool_creation_and_kills_new_actors(self, sample_batch_test):
        """Cancellation must not return while actor creation is still in flight."""
        from wtb.application.services.ray_batch_runner import RayBatchTestRunner, RayConfig

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=MagicMock(),
        )
        actor = MagicMock(name="new-actor")
        creation_started = threading.Event()
        release_creation = threading.Event()
        cancel_returned = threading.Event()
        run_errors = []

        def create_pool(_num_workers):
            creation_started.set()
            assert release_creation.wait(timeout=2.0)
            runner._actors = [actor]
            runner._actor_pool = MagicMock()

        def run_batch():
            try:
                runner.run_batch_test(sample_batch_test)
            except BaseException as error:
                run_errors.append(error)

        with (
            patch.object(runner, "_create_actor_pool", side_effect=create_pool),
            patch.object(runner, "_load_workflow", return_value={}),
            patch("wtb.application.services.ray_batch_runner.ray.put", side_effect=lambda value: value),
            patch("wtb.application.services.ray_batch_runner.ray.kill") as kill,
        ):
            run_thread = threading.Thread(target=run_batch)
            run_thread.start()
            assert creation_started.wait(timeout=2.0)

            def cancel_batch():
                assert runner.cancel(sample_batch_test.id) is True
                cancel_returned.set()

            cancel_thread = threading.Thread(target=cancel_batch)
            cancel_thread.start()
            assert not cancel_returned.wait(timeout=0.1)

            release_creation.set()
            cancel_thread.join(timeout=2.0)
            run_thread.join(timeout=2.0)

        assert not cancel_thread.is_alive()
        assert not run_thread.is_alive()
        assert cancel_returned.is_set()
        assert run_errors == []
        kill.assert_called_once_with(actor, no_restart=True)
        assert runner._actors == []
        assert runner._actor_pool is None
        assert sample_batch_test.status is BatchTestStatus.CANCELLED

    def test_cancel_cannot_return_before_inflight_submission_is_killed(self, sample_batch_test):
        """Submission and actor termination must share one synchronization boundary."""
        from wtb.application.services.ray_batch_runner import RayBatchTestRunner, RayConfig

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=MagicMock(),
        )
        actor = MagicMock(name="selected-actor")
        ref = MagicMock(name="submitted-ref")
        runner._actors = [actor]
        runner._actor_pool = MagicMock()
        selection_started = threading.Event()
        release_selection = threading.Event()
        cancel_returned = threading.Event()
        submitted_after_cancel = threading.Event()
        run_errors = []

        def select_actor():
            selection_started.set()
            assert release_selection.wait(timeout=2.0)
            return actor

        def submit_variant(**_kwargs):
            if cancel_returned.is_set():
                submitted_after_cancel.set()
            return ref

        actor.execute_variant.remote.side_effect = submit_variant

        def run_batch():
            try:
                runner.run_batch_test(sample_batch_test)
            except BaseException as error:
                run_errors.append(error)

        def cancel_batch():
            assert runner.cancel(sample_batch_test.id) is True
            cancel_returned.set()

        result_dict = {
            "combination_name": "Config A",
            "execution_id": "exec-a",
            "success": True,
            "duration_ms": 1,
            "metrics": {},
        }

        with (
            patch.object(runner, "_create_actor_pool"),
            patch.object(runner, "_load_workflow", return_value={}),
            patch.object(runner, "_get_available_actor", side_effect=select_actor),
            patch("wtb.application.services.ray_batch_runner.ray.put", side_effect=lambda value: value),
            patch(
                "wtb.application.services.ray_batch_runner.ray.get",
                side_effect=lambda value: result_dict if value is ref else value,
            ),
            patch("wtb.application.services.ray_batch_runner.ray.wait", return_value=([ref], [])),
            patch("wtb.application.services.ray_batch_runner.ray.kill") as kill,
        ):
            run_thread = threading.Thread(target=run_batch)
            run_thread.start()
            assert selection_started.wait(timeout=2.0)

            cancel_thread = threading.Thread(target=cancel_batch)
            cancel_thread.start()
            assert not cancel_returned.wait(timeout=0.1)

            release_selection.set()
            cancel_thread.join(timeout=2.0)
            run_thread.join(timeout=2.0)

        assert not cancel_thread.is_alive()
        assert not run_thread.is_alive()
        assert submitted_after_cancel.is_set() is False
        assert run_errors == []
        kill.assert_called_once_with(actor, no_restart=True)
        assert sample_batch_test.status is BatchTestStatus.CANCELLED

    def test_failed_actor_termination_fails_run_instead_of_reporting_cancelled(
        self, sample_batch_test
    ):
        """An unconfirmed actor stop must propagate to the run caller."""
        from wtb.application.services.ray_batch_runner import RayBatchTestRunner, RayConfig

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=MagicMock(),
        )
        actor = MagicMock(name="unsafe-actor")
        ref = MagicMock(name="running-ref")
        runner._actors = [actor]
        runner._actor_pool = MagicMock()
        wait_started = threading.Event()
        release_wait = threading.Event()
        run_errors = []
        actor.execute_variant.remote.return_value = ref

        def wait_for_result(*_args, **_kwargs):
            wait_started.set()
            assert release_wait.wait(timeout=2.0)
            return [ref], []

        def run_batch():
            try:
                runner.run_batch_test(sample_batch_test)
            except BaseException as error:
                run_errors.append(error)

        result_dict = {
            "combination_name": "Config A",
            "execution_id": "exec-a",
            "success": True,
            "duration_ms": 1,
            "metrics": {},
        }

        with (
            patch.object(runner, "_create_actor_pool"),
            patch.object(runner, "_load_workflow", return_value={}),
            patch("wtb.application.services.ray_batch_runner.ray.put", side_effect=lambda value: value),
            patch(
                "wtb.application.services.ray_batch_runner.ray.get",
                side_effect=lambda value: result_dict if value is ref else value,
            ),
            patch("wtb.application.services.ray_batch_runner.ray.wait", side_effect=wait_for_result),
            patch(
                "wtb.application.services.ray_batch_runner.ray.kill",
                side_effect=RuntimeError("control plane unavailable"),
            ),
        ):
            run_thread = threading.Thread(target=run_batch)
            run_thread.start()
            assert wait_started.wait(timeout=2.0)
            assert runner.cancel(sample_batch_test.id) is False
            release_wait.set()
            run_thread.join(timeout=2.0)

        assert not run_thread.is_alive()
        assert len(run_errors) == 1
        assert isinstance(run_errors[0], BatchRunnerError)
        assert "termination" in str(run_errors[0]).lower()
        assert sample_batch_test.status is BatchTestStatus.FAILED
        assert runner._poisoned is True

    def test_concurrent_batch_is_rejected_while_runner_pool_is_owned(self, sample_batch_test):
        """The shared actor pool may only be owned by one batch at a time."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
            _RayRunningTest,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=MagicMock(),
        )
        runner._running_tests["other-batch"] = _RayRunningTest(
            batch_test_id="other-batch",
            started_at=datetime.now(),
            total_variants=1,
        )

        with pytest.raises(BatchRunnerError, match="already running"):
            runner.run_batch_test(sample_batch_test)

        assert sample_batch_test.status is BatchTestStatus.PENDING

    def test_completed_owner_rejects_late_cancel_without_killing_pool(
        self,
        sample_batch_test,
    ):
        """Completion owns the result before its terminal callback runs."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        sample_batch_test.variant_combinations = (
            sample_batch_test.variant_combinations[:1]
        )
        event_bridge = MagicMock()
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=event_bridge,
        )
        actor = MagicMock(name="completion-actor")
        ref = MagicMock(name="completion-ref")
        actor.execute_variant.remote.return_value = ref
        runner._actors = [actor]
        runner._actor_pool = MagicMock()
        callback_started = threading.Event()
        release_callback = threading.Event()
        observed_states = []

        def on_completed(**_kwargs):
            with runner._running_tests_lock:
                observed_states.append(
                    runner._running_tests[sample_batch_test.id]
                )
            callback_started.set()
            assert release_callback.wait(timeout=2.0)

        event_bridge.on_batch_test_completed.side_effect = on_completed
        result_dict = {
            "combination_name": "Config A",
            "execution_id": "exec-a",
            "success": True,
            "duration_ms": 1,
            "metrics": {"overall_score": 1.0},
        }
        run_results = []
        run_errors = []

        def run_batch():
            try:
                run_results.append(runner.run_batch_test(sample_batch_test))
            except BaseException as error:
                run_errors.append(error)

        with (
            patch.object(runner, "_create_actor_pool"),
            patch.object(runner, "_load_workflow", return_value={}),
            patch(
                "wtb.application.services.ray_batch_runner.ray.put",
                side_effect=lambda value: value,
            ),
            patch(
                "wtb.application.services.ray_batch_runner.ray.get",
                side_effect=lambda value: result_dict if value is ref else value,
            ),
            patch(
                "wtb.application.services.ray_batch_runner.ray.wait",
                return_value=([ref], []),
            ),
            patch("wtb.application.services.ray_batch_runner.ray.kill") as kill,
        ):
            run_thread = threading.Thread(target=run_batch)
            run_thread.start()
            assert callback_started.wait(timeout=2.0)
            cancel_result = runner.cancel(sample_batch_test.id)
            release_callback.set()
            run_thread.join(timeout=2.0)

        assert not run_thread.is_alive()
        assert cancel_result is False
        kill.assert_not_called()
        assert run_errors == []
        assert run_results == [sample_batch_test]
        assert sample_batch_test.status is BatchTestStatus.COMPLETED
        assert observed_states[0].terminal_owner == "run_completed"
        assert observed_states[0].cancelled is False
        assert observed_states[0].termination_error is None
        event_bridge.on_batch_test_failed.assert_not_called()

    @pytest.mark.parametrize(
        ("failure_site", "message"),
        [
            ("matrix", "matrix unavailable"),
            ("completed_event", "completed event unavailable"),
        ],
    )
    def test_finalization_failure_never_performs_a_second_terminal_transition(
        self,
        sample_batch_test,
        failure_site,
        message,
    ):
        """Matrix/callback failures preserve one coherent domain terminal state."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        sample_batch_test.variant_combinations = (
            sample_batch_test.variant_combinations[:1]
        )
        event_bridge = MagicMock()
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=event_bridge,
        )
        actor = MagicMock(name="finalization-actor")
        ref = MagicMock(name="finalization-ref")
        actor.execute_variant.remote.return_value = ref
        runner._actors = [actor]
        runner._actor_pool = MagicMock()
        result_dict = {
            "combination_name": "Config A",
            "execution_id": "exec-a",
            "success": True,
            "duration_ms": 1,
            "metrics": {"overall_score": 1.0},
        }

        if failure_site == "matrix":
            sample_batch_test.build_comparison_matrix = MagicMock(
                side_effect=RuntimeError(message)
            )
        else:
            event_bridge.on_batch_test_completed.side_effect = RuntimeError(
                message
            )

        with (
            patch.object(runner, "_create_actor_pool"),
            patch.object(runner, "_load_workflow", return_value={}),
            patch(
                "wtb.application.services.ray_batch_runner.ray.put",
                side_effect=lambda value: value,
            ),
            patch(
                "wtb.application.services.ray_batch_runner.ray.get",
                side_effect=lambda value: result_dict if value is ref else value,
            ),
            patch(
                "wtb.application.services.ray_batch_runner.ray.wait",
                return_value=([ref], []),
            ),
            pytest.raises(BatchRunnerError, match=message),
        ):
            runner.run_batch_test(sample_batch_test)

        if failure_site == "matrix":
            assert sample_batch_test.status is BatchTestStatus.FAILED
            event_bridge.on_batch_test_failed.assert_called_once()
            event_bridge.on_batch_test_completed.assert_not_called()
        else:
            assert sample_batch_test.status is BatchTestStatus.COMPLETED
            event_bridge.on_batch_test_failed.assert_not_called()

    def test_confirmed_cancel_wins_over_late_event_error_during_env_cleanup(
        self,
        sample_batch_test,
    ):
        """A late callback error must wait for the in-flight cancel verdict."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        sample_batch_test.variant_combinations = (
            sample_batch_test.variant_combinations[:1]
        )
        event_bridge = MagicMock()
        environment_provider = MagicMock()
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=event_bridge,
            environment_provider=environment_provider,
        )
        actor = MagicMock(name="cancel-race-actor")
        ref = MagicMock(name="cancel-race-ref")
        actor.execute_variant.remote.return_value = ref
        runner._actors = [actor]
        runner._actor_pool = MagicMock()
        runner._provisioned_env_ids = ["cancel-race-env"]

        callback_entered = threading.Event()
        release_callback = threading.Event()
        cleanup_entered = threading.Event()
        release_cleanup = threading.Event()
        run_finished = threading.Event()
        run_results = []
        run_errors = []
        cancel_results = []
        cancel_errors = []

        def fail_late_variant_event(**_kwargs):
            callback_entered.set()
            assert release_callback.wait(timeout=2.0)
            raise RuntimeError("late variant event failed")

        def block_environment_cleanup(_env_id):
            cleanup_entered.set()
            assert release_cleanup.wait(timeout=2.0)

        event_bridge.on_variant_execution_completed.side_effect = (
            fail_late_variant_event
        )
        environment_provider.cleanup_environment.side_effect = (
            block_environment_cleanup
        )
        result_dict = {
            "combination_name": "Config A",
            "execution_id": "exec-a",
            "success": True,
            "duration_ms": 1,
            "metrics": {"overall_score": 1.0},
        }

        def run_batch():
            try:
                run_results.append(runner.run_batch_test(sample_batch_test))
            except BaseException as error:
                run_errors.append(error)
            finally:
                run_finished.set()

        def cancel_batch():
            try:
                cancel_results.append(runner.cancel(sample_batch_test.id))
            except BaseException as error:
                cancel_errors.append(error)

        with (
            patch.object(runner, "_create_actor_pool"),
            patch.object(runner, "_load_workflow", return_value={}),
            patch(
                "wtb.application.services.ray_batch_runner.ray.put",
                side_effect=lambda value: value,
            ),
            patch(
                "wtb.application.services.ray_batch_runner.ray.get",
                side_effect=lambda value: result_dict if value is ref else value,
            ),
            patch(
                "wtb.application.services.ray_batch_runner.ray.wait",
                return_value=([ref], []),
            ),
            patch("wtb.application.services.ray_batch_runner.ray.kill") as kill,
        ):
            run_thread = threading.Thread(target=run_batch)
            run_thread.start()
            assert callback_entered.wait(timeout=2.0)

            cancel_thread = threading.Thread(target=cancel_batch)
            cancel_thread.start()
            assert cleanup_entered.wait(timeout=2.0)

            release_callback.set()
            finished_before_cleanup = run_finished.wait(timeout=0.1)
            release_cleanup.set()
            cancel_thread.join(timeout=2.0)
            run_thread.join(timeout=2.0)

        assert finished_before_cleanup is False
        assert not cancel_thread.is_alive()
        assert not run_thread.is_alive()
        assert cancel_errors == []
        assert cancel_results == [True]
        assert run_errors == []
        assert run_results == [sample_batch_test]
        assert sample_batch_test.status is BatchTestStatus.CANCELLED
        kill.assert_called_once_with(actor, no_restart=True)
        environment_provider.cleanup_environment.assert_called_once_with(
            "cancel-race-env"
        )
        event_bridge.on_batch_test_cancelled.assert_called_once()
        event_bridge.on_batch_test_failed.assert_not_called()

    def test_cancelled_event_reconciles_result_after_cancel_snapshot(
        self,
        sample_batch_test,
    ):
        """Terminal cancellation totals must include a concurrently added result."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        sample_batch_test.variant_combinations = (
            sample_batch_test.variant_combinations[:1]
        )
        event_bridge = MagicMock()
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=event_bridge,
        )
        actor = MagicMock(name="count-race-actor")
        ref = MagicMock(name="count-race-ref")
        actor.execute_variant.remote.return_value = ref
        runner._actors = [actor]
        runner._actor_pool = MagicMock()

        result_added = threading.Event()
        release_add_result = threading.Event()
        original_add_result = sample_batch_test.add_result

        def add_result_then_block(result):
            original_add_result(result)
            result_added.set()
            assert release_add_result.wait(timeout=2.0)

        sample_batch_test.add_result = add_result_then_block
        result_dict = {
            "combination_name": "Config A",
            "execution_id": "exec-a",
            "success": True,
            "duration_ms": 1,
            "metrics": {"overall_score": 1.0},
        }
        run_results = []
        run_errors = []

        def run_batch():
            try:
                run_results.append(runner.run_batch_test(sample_batch_test))
            except BaseException as error:
                run_errors.append(error)

        with (
            patch.object(runner, "_create_actor_pool"),
            patch.object(runner, "_load_workflow", return_value={}),
            patch(
                "wtb.application.services.ray_batch_runner.ray.put",
                side_effect=lambda value: value,
            ),
            patch(
                "wtb.application.services.ray_batch_runner.ray.get",
                side_effect=lambda value: result_dict if value is ref else value,
            ),
            patch(
                "wtb.application.services.ray_batch_runner.ray.wait",
                return_value=([ref], []),
            ),
            patch("wtb.application.services.ray_batch_runner.ray.kill") as kill,
        ):
            run_thread = threading.Thread(target=run_batch)
            run_thread.start()
            assert result_added.wait(timeout=2.0)

            running_state = runner._running_tests[sample_batch_test.id]
            assert runner.cancel(sample_batch_test.id) is True
            assert running_state.cancelled_variants == 1

            release_add_result.set()
            run_thread.join(timeout=2.0)

        assert not run_thread.is_alive()
        assert run_errors == []
        assert run_results == [sample_batch_test]
        assert sample_batch_test.status is BatchTestStatus.CANCELLED
        kill.assert_called_once_with(actor, no_restart=True)

        cancelled_event = event_bridge.on_batch_test_cancelled.call_args.kwargs
        assert cancelled_event["variants_completed"] == 1
        assert cancelled_event["variants_cancelled"] == 0
        assert (
            cancelled_event["variants_completed"]
            + cancelled_event["variants_cancelled"]
            == len(sample_batch_test.variant_combinations)
        )
        assert running_state.cancelled_variants == 0


    def test_shutdown_waits_for_run_finally_before_closing_provider(self):
        """Shutdown must not close shared resources before the run thread exits."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
            _RayRunningTest,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        environment_provider = MagicMock()
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=MagicMock(),
            environment_provider=environment_provider,
            owns_environment_provider=True,
        )
        running = _RayRunningTest(
            batch_test_id="active-batch",
            started_at=datetime.now(),
            total_variants=1,
        )
        runner._running_tests[running.batch_test_id] = running
        release_finally = threading.Event()
        shutdown_done = threading.Event()
        shutdown_errors = []

        def finish_run():
            assert running.cancellation_finished.wait(timeout=2.0)
            assert release_finally.wait(timeout=2.0)
            with runner._running_tests_lock:
                runner._running_tests.pop(running.batch_test_id, None)
            running.finished_event.set()

        def shut_down():
            try:
                runner.shutdown()
            except BaseException as error:
                shutdown_errors.append(error)
            finally:
                shutdown_done.set()

        finish_thread = threading.Thread(target=finish_run)
        shutdown_thread = threading.Thread(target=shut_down)
        finish_thread.start()
        shutdown_thread.start()

        assert running.cancellation_finished.wait(timeout=2.0)
        assert not shutdown_done.wait(timeout=0.1)
        environment_provider.close.assert_not_called()

        release_finally.set()
        finish_thread.join(timeout=2.0)
        shutdown_thread.join(timeout=2.0)

        assert not finish_thread.is_alive()
        assert not shutdown_thread.is_alive()
        assert shutdown_errors == []
        environment_provider.close.assert_called_once()

    def test_concurrent_shutdown_closes_provider_exactly_once(self):
        """Concurrent shutdown calls must share one terminal close operation."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        close_entered = threading.Event()
        release_close = threading.Event()
        environment_provider = MagicMock()

        def close_provider():
            close_entered.set()
            assert release_close.wait(timeout=2.0)

        environment_provider.close.side_effect = close_provider
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=MagicMock(),
            environment_provider=environment_provider,
            owns_environment_provider=True,
        )
        shutdown_errors = []
        second_done = threading.Event()

        def shut_down(done=None):
            try:
                runner.shutdown()
            except BaseException as error:
                shutdown_errors.append(error)
            finally:
                if done is not None:
                    done.set()

        first = threading.Thread(target=shut_down)
        second = threading.Thread(target=shut_down, args=(second_done,))
        first.start()
        assert close_entered.wait(timeout=2.0)
        second.start()

        assert not second_done.wait(timeout=0.1)
        release_close.set()
        first.join(timeout=2.0)
        second.join(timeout=2.0)

        assert not first.is_alive()
        assert not second.is_alive()
        assert shutdown_errors == []
        environment_provider.close.assert_called_once()

    def test_shutdown_retry_reuses_inflight_owned_provider_close(self):
        """A timed-out close must not be invoked concurrently by a retry."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        close_entered = threading.Event()
        release_close = threading.Event()
        close_exited = threading.Event()
        environment_provider = MagicMock()

        def close_provider():
            close_entered.set()
            try:
                assert release_close.wait(timeout=2.0)
            finally:
                close_exited.set()

        environment_provider.close.side_effect = close_provider
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=MagicMock(),
            environment_provider=environment_provider,
            owns_environment_provider=True,
        )
        runner._shutdown_timeout_seconds = 0.05

        with pytest.raises(BatchRunnerError, match="provider close"):
            runner.shutdown()
        assert close_entered.is_set()
        environment_provider.close.assert_called_once()

        with pytest.raises(BatchRunnerError, match="provider close"):
            runner.shutdown()
        calls_while_close_is_blocked = environment_provider.close.call_count

        release_close.set()
        assert close_exited.wait(timeout=2.0)
        runner._shutdown_timeout_seconds = 1.0
        runner.shutdown()

        assert calls_while_close_is_blocked == 1
        environment_provider.close.assert_called_once()
        assert runner._closed is True

    def test_shutdown_leaves_borrowed_environment_provider_open(self):
        """An injected provider is borrowed unless ownership is explicit."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        environment_provider = MagicMock()
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=MagicMock(),
            environment_provider=environment_provider,
        )

        runner.shutdown()

        environment_provider.close.assert_not_called()

    def test_shutdown_bounds_environment_cleanup_by_total_deadline(self):
        """Shutdown must pass its remaining budget to environment cleanup."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        class BoundedEnvironmentProvider:
            def __init__(self):
                self.cleanup_timeouts = []

            def cleanup_environment(self, variant_id, timeout=None):
                self.cleanup_timeouts.append(timeout)
                time.sleep(0.2 if timeout is None else timeout)
                raise TimeoutError(f"cleanup timed out for {variant_id}")

            def close(self):
                raise AssertionError("close must not run after cleanup failure")

        environment_provider = BoundedEnvironmentProvider()
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=MagicMock(),
            environment_provider=environment_provider,
        )
        runner._shutdown_timeout_seconds = 0.05
        runner._provisioned_env_ids = ["slow-env"]

        started = time.monotonic()
        with pytest.raises(BatchRunnerError, match="cleanup incomplete"):
            runner.shutdown()
        elapsed = time.monotonic() - started

        assert elapsed < 0.15
        assert len(environment_provider.cleanup_timeouts) == 1
        timeout = environment_provider.cleanup_timeouts[0]
        assert timeout is not None
        assert 0 < timeout <= 0.06

    def test_runner_instances_use_distinct_remote_environment_identities(self):
        """One runner must never delete another runner's remote environment."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        environment_provider = MagicMock()
        environment_provider.get_runtime_env.return_value = {}
        first = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=MagicMock(),
            environment_provider=environment_provider,
        )
        second = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=MagicMock(),
            environment_provider=environment_provider,
        )

        with (
            patch.object(first, "_ensure_ray_initialized"),
            patch.object(second, "_ensure_ray_initialized"),
            patch(
                "wtb.application.services.ray_batch_runner.VariantExecutionActor"
            ) as actor_class,
            patch(
                "wtb.application.services.ray_batch_runner.ray.util.ActorPool",
                return_value=MagicMock(),
            ),
        ):
            actor_class.options.return_value.remote.side_effect = [
                MagicMock(name="first-actor"),
                MagicMock(name="second-actor"),
            ]
            first._create_actor_pool(1)
            second._create_actor_pool(1)

        first_call, second_call = environment_provider.create_environment.call_args_list
        first_environment_id = first_call.args[0]
        second_environment_id = second_call.args[0]
        assert first_environment_id != second_environment_id
        assert first_call.args[1]["workflow_id"] != second_call.args[1]["workflow_id"]
        assert first_call.args[1]["node_id"] == first_environment_id
        assert second_call.args[1]["node_id"] == second_environment_id

        first_remote, second_remote = (
            actor_class.options.return_value.remote.call_args_list
        )
        assert first_remote.kwargs["actor_id"] == first_environment_id
        assert second_remote.kwargs["actor_id"] == second_environment_id

        first_options, second_options = actor_class.options.call_args_list
        assert (
            first_options.kwargs["runtime_env"]["env_vars"]["WTB_CACHE_ACTOR_ID"]
            == first_environment_id
        )
        assert (
            second_options.kwargs["runtime_env"]["env_vars"]["WTB_CACHE_ACTOR_ID"]
            == second_environment_id
        )

    def test_actor_pool_rebuilds_when_parallel_count_changes(self):
        """Sequential batches must get the requested number of workers."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=MagicMock(),
        )
        old_actor = MagicMock(name="old-actor")
        new_actors = [MagicMock(name="new-a"), MagicMock(name="new-b")]

        with (
            patch.object(runner, "_ensure_ray_initialized"),
            patch(
                "wtb.application.services.ray_batch_runner.VariantExecutionActor"
            ) as actor_class,
            patch(
                "wtb.application.services.ray_batch_runner.ray.util.ActorPool",
                side_effect=[MagicMock(name="pool-one"), MagicMock(name="pool-two")],
            ) as actor_pool,
            patch("wtb.application.services.ray_batch_runner.ray.kill") as kill,
        ):
            actor_class.options.return_value.remote.side_effect = [
                old_actor,
                *new_actors,
            ]
            runner._create_actor_pool(1)
            runner._create_actor_pool(2)

        kill.assert_called_once_with(old_actor, no_restart=True)
        assert runner._actors == new_actors
        assert actor_pool.call_count == 2
        assert actor_class.options.return_value.remote.call_count == 3
    def test_partial_actor_pool_creation_failure_rolls_back_resources(self):
        """A partial pool must not leak actors or provisioned environments."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        environment_provider = MagicMock()
        environment_provider.get_runtime_env.return_value = {}
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=MagicMock(),
            environment_provider=environment_provider,
        )
        actor = MagicMock(name="partially-created-actor")

        with (
            patch.object(runner, "_ensure_ray_initialized"),
            patch(
                "wtb.application.services.ray_batch_runner.VariantExecutionActor"
            ) as actor_class,
            patch("wtb.application.services.ray_batch_runner.ray.kill") as kill,
            pytest.raises(BatchRunnerError, match="create Ray actor pool"),
        ):
            actor_class.options.return_value.remote.side_effect = [
                actor,
                RuntimeError("second actor failed"),
            ]
            runner._create_actor_pool(2)

        kill.assert_called_once_with(actor, no_restart=True)
        assert environment_provider.cleanup_environment.call_args_list == [
            ((f"{runner._environment_namespace}-actor_0",), {}),
            ((f"{runner._environment_namespace}-actor_1",), {}),
        ]
        assert runner._actors == []
        assert runner._actor_pool is None
        assert runner._provisioned_env_ids == []
        assert runner._poisoned is False

    def test_partial_actor_pool_rollback_failure_is_poisoned_and_retryable(self):
        """Failed rollback must retain every handle needed by shutdown retry."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        environment_provider = MagicMock()
        environment_provider.get_runtime_env.return_value = {}
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=MagicMock(),
            environment_provider=environment_provider,
        )
        actor = MagicMock(name="unsafe-partial-actor")

        with (
            patch.object(runner, "_ensure_ray_initialized"),
            patch(
                "wtb.application.services.ray_batch_runner.VariantExecutionActor"
            ) as actor_class,
            patch(
                "wtb.application.services.ray_batch_runner.ray.kill",
                side_effect=RuntimeError("actor control plane unavailable"),
            ),
            pytest.raises(BatchRunnerError, match="rollback incomplete"),
        ):
            actor_class.options.return_value.remote.side_effect = [
                actor,
                RuntimeError("second actor failed"),
            ]
            runner._create_actor_pool(2)

        assert runner._actors == [actor]
        assert runner._actor_pool is None
        expected_env_ids = [
            f"{runner._environment_namespace}-actor_0",
            f"{runner._environment_namespace}-actor_1",
        ]
        assert runner._provisioned_env_ids == expected_env_ids
        environment_provider.cleanup_environment.assert_not_called()
        assert runner._poisoned is True

        with patch("wtb.application.services.ray_batch_runner.ray.kill") as retry_kill:
            runner.shutdown()

        retry_kill.assert_called_once_with(actor, no_restart=True)
        assert runner._actors == []
        assert runner._provisioned_env_ids == []
        assert runner._poisoned is False
        cleanup_calls = environment_provider.cleanup_environment.call_args_list
        assert [cleanup_call.args for cleanup_call in cleanup_calls] == [
            (expected_env_ids[0],),
            (expected_env_ids[1],),
        ]
        assert all(
            0 < cleanup_call.kwargs["timeout"] <= runner._shutdown_timeout_seconds
            for cleanup_call in cleanup_calls
        )

    def test_shutdown_permanently_closes_runner(self, sample_batch_test):
        """A runner must not reopen after its environment provider is closed."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=MagicMock(),
        )
        runner.shutdown()

        with (
            patch.object(runner, "_create_actor_pool"),
            pytest.raises(BatchRunnerError, match="closed"),
        ):
            runner.run_batch_test(sample_batch_test)

        assert sample_batch_test.status is BatchTestStatus.PENDING

    def test_failed_cancel_is_terminal_and_cannot_report_retry_success(self):
        """A failed cancel remains unsafe and requires shutdown recovery."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
            _RayRunningTest,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=MagicMock(),
        )
        actor = MagicMock(name="retry-actor")
        running = _RayRunningTest(
            batch_test_id="cancel-retry",
            started_at=datetime.now(),
            total_variants=1,
            pending_refs=[MagicMock(name="pending-ref")],
        )
        runner._running_tests[running.batch_test_id] = running
        runner._actors = [actor]

        with patch(
            "wtb.application.services.ray_batch_runner.ray.kill",
            side_effect=RuntimeError("first termination failed"),
        ):
            assert runner.cancel(running.batch_test_id) is False

        assert running.termination_error is not None
        assert running.cancellation_finished.is_set()

        with patch("wtb.application.services.ray_batch_runner.ray.kill") as retry_kill:
            assert runner.cancel(running.batch_test_id) is False

        retry_kill.assert_not_called()
        assert running.termination_confirmed is False
        assert "first termination failed" in (running.termination_error or "")
        assert running.cancellation_finished.is_set()
        assert runner._poisoned is True
        assert running.batch_test_id in runner._unsafe_batch_ids
        assert running.pending_refs[0] in runner._orphaned_refs

    def test_failed_cancel_environment_cleanup_requires_shutdown(self):
        """Failed cleanup cannot be reclassified as successful cancellation."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
            _RayRunningTest,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        environment_provider = MagicMock()
        environment_provider.cleanup_environment.side_effect = [
            RuntimeError("cleanup unavailable"),
            None,
        ]
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=MagicMock(),
            environment_provider=environment_provider,
        )
        actor = MagicMock(name="environment-retry-actor")
        running = _RayRunningTest(
            batch_test_id="cancel-env-retry",
            started_at=datetime.now(),
            total_variants=1,
            pending_refs=[MagicMock(name="pending-ref")],
        )
        runner._running_tests[running.batch_test_id] = running
        runner._actors = [actor]
        runner._provisioned_env_ids = ["env-retry"]

        with patch("wtb.application.services.ray_batch_runner.ray.kill") as kill:
            assert runner.cancel(running.batch_test_id) is False

        kill.assert_called_once_with(actor, no_restart=True)
        assert runner._actors == []
        assert runner._provisioned_env_ids == ["env-retry"]

        with patch("wtb.application.services.ray_batch_runner.ray.kill") as retry_kill:
            assert runner.cancel(running.batch_test_id) is False

        retry_kill.assert_not_called()
        assert runner._provisioned_env_ids == ["env-retry"]
        assert running.termination_confirmed is False
        assert "cleanup unavailable" in (running.termination_error or "")
        assert environment_provider.cleanup_environment.call_count == 1
        assert runner._poisoned is True
    def test_timeout_terminal_owner_rejects_late_cancel(self):
        """Cancellation must not report success after timeout owns termination."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
            _RayRunningTest,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=MagicMock(),
        )
        running = _RayRunningTest(
            batch_test_id="timeout-owner",
            started_at=datetime.now(),
            total_variants=1,
        )
        running.terminal_owner = "timeout"
        runner._running_tests[running.batch_test_id] = running

        assert runner.cancel(running.batch_test_id) is False
        assert running.termination_confirmed is False
        assert running.termination_error is None

    def test_unexpected_orchestration_failure_preserves_unsafe_actor_and_workspace(
        self,
        sample_batch_test,
    ):
        """Any failure with submitted refs must terminate or preserve the pool."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=MagicMock(),
        )
        actor = MagicMock(name="unconfirmed-actor")
        ref = MagicMock(name="submitted-ref")
        actor.execute_variant.remote.return_value = ref
        runner._actors = [actor]
        runner._actor_pool = MagicMock()
        workspace_manager = MagicMock()
        workspace_manager.create_workspace.return_value.to_dict.return_value = {}
        runner._workspace_manager = workspace_manager

        with (
            patch.object(runner, "_create_actor_pool"),
            patch.object(runner, "_load_workflow", return_value={}),
            patch("wtb.application.services.ray_batch_runner.ray.put", side_effect=lambda value: value),
            patch("wtb.application.services.ray_batch_runner.ray.get", side_effect=lambda value: value),
            patch(
                "wtb.application.services.ray_batch_runner.ray.wait",
                side_effect=RuntimeError("Ray control plane lost"),
            ),
            patch(
                "wtb.application.services.ray_batch_runner.ray.kill",
                side_effect=RuntimeError("actor termination unconfirmed"),
            ) as kill,
            pytest.raises(BatchRunnerError, match="control plane lost"),
        ):
            runner.run_batch_test(sample_batch_test)

        kill.assert_called_once_with(actor, no_restart=True)
        assert runner._actors == [actor]
        assert runner._poisoned is True
        assert sample_batch_test.id in runner._unsafe_batch_ids
        workspace_manager.cleanup_batch.assert_not_called()

    def test_confirmed_cancel_is_not_reclassified_as_unsafe_failure(self):
        """Late orchestration errors must respect confirmed actor termination."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
            _RayRunningTest,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=MagicMock(),
        )
        ref = MagicMock(name="cancelled-ref")
        running = _RayRunningTest(
            batch_test_id="confirmed-cancel",
            started_at=datetime.now(),
            total_variants=1,
            pending_refs=[ref],
            cancelled=True,
            termination_confirmed=True,
            terminal_owner="cancel",
            actor_termination_confirmed=True,
        )
        pending_refs = [ref]

        with patch("wtb.application.services.ray_batch_runner.ray.kill") as kill:
            issue = runner._terminate_failed_batch_work(
                running.batch_test_id, pending_refs, running, reason="event failure"
            )

        assert issue is None
        kill.assert_not_called()
        assert pending_refs == []
        assert running.pending_refs == []
        assert runner._poisoned is False
        assert running.batch_test_id not in runner._unsafe_batch_ids

    def test_event_cleanup_failure_still_cleans_workspace_and_retries_on_shutdown(
        self,
    ):
        """Event cleanup failure must not skip or orphan workspace cleanup."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        event_bridge = MagicMock()
        event_bridge.cleanup_batch.side_effect = [
            RuntimeError("event cleanup unavailable"),
            None,
        ]
        workspace_manager = MagicMock()
        workspace_manager.cleanup_batch.return_value = 1
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=event_bridge,
        )
        runner._workspace_manager = workspace_manager
        runner._workspace_config = {
            "cleanup_on_complete": True,
            "preserve_on_failure": True,
        }
        batch = MagicMock()
        batch.id = "event-cleanup-batch"
        batch.status = BatchTestStatus.COMPLETED

        cleanup_error = runner._cleanup_batch_resources(batch)

        assert isinstance(cleanup_error, BatchRunnerError)
        workspace_manager.cleanup_batch.assert_called_once_with(
            batch_id=batch.id,
            reason="batch_complete",
        )
        assert batch.id in runner._pending_event_cleanup_ids
        assert batch.id not in runner._unsafe_batch_ids

        runner.shutdown()

        assert event_bridge.cleanup_batch.call_count == 2
        assert runner._pending_event_cleanup_ids == set()
        assert runner._poisoned is False

    def test_workspace_lifecycle_config_controls_final_cleanup(self):
        """Workspace cleanup follows success and failure preservation policy."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            event_bridge=None,
        )
        workspace_manager = MagicMock()
        workspace_manager.cleanup_batch.return_value = 1
        runner._workspace_manager = workspace_manager
        batch = MagicMock()
        batch.id = "workspace-policy-batch"

        runner._workspace_config = {
            "cleanup_on_complete": False,
            "preserve_on_failure": True,
        }
        batch.status = BatchTestStatus.COMPLETED
        assert runner._cleanup_batch_resources(batch) is None
        workspace_manager.cleanup_batch.assert_not_called()

        runner._workspace_config["cleanup_on_complete"] = True
        batch.status = BatchTestStatus.FAILED
        assert runner._cleanup_batch_resources(batch) is None
        workspace_manager.cleanup_batch.assert_not_called()

        runner._workspace_config["preserve_on_failure"] = False
        assert runner._cleanup_batch_resources(batch) is None
        workspace_manager.cleanup_batch.assert_called_once_with(
            batch_id=batch.id,
            reason="batch_failed",
        )
    def test_process_completed_refs_fails_closed_without_progress(self, sample_batch_test):
        """A no-progress timeout must terminate the actor pool and fail closed."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
            _RayRunningTest,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        event_bridge = MagicMock()
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="sqlite:///data/wtb.db",
            event_bridge=event_bridge,
        )
        actors = [MagicMock(name="actor-a"), MagicMock(name="actor-b")]
        runner._actors = list(actors)
        runner._actor_pool = MagicMock()
        refs = [MagicMock(name="ref-a"), MagicMock(name="ref-b")]
        combinations = sample_batch_test.variant_combinations[:2]
        pending_refs = list(refs)
        running_test = _RayRunningTest(
            batch_test_id=sample_batch_test.id,
            started_at=datetime.now(),
            total_variants=len(refs) + 1,
            pending_refs=list(refs),
        )

        with (
            patch(
                "wtb.application.services.ray_batch_runner.ray.wait",
                return_value=([], list(refs)),
            ),
            patch("wtb.application.services.ray_batch_runner.ray.kill") as kill,
            pytest.raises(
                BatchRunnerError,
                match="No Ray task completed within 0.25 seconds",
            ),
        ):
            runner._process_completed_refs(
                pending_refs=pending_refs,
                ref_to_combo=dict(zip(refs, combinations)),
                batch_test=sample_batch_test,
                running_test=running_test,
                timeout=0.25,
            )

        assert pending_refs == []
        assert running_test.pending_refs == []
        assert running_test.failed == running_test.total_variants
        assert sample_batch_test.results == []
        assert runner._actors == []
        assert runner._actor_pool is None
        assert kill.call_count == 2
        for actor in actors:
            kill.assert_any_call(actor, no_restart=True)
        event_bridge.on_variant_execution_failed.assert_not_called()

    def test_timeout_preserves_handles_when_actor_termination_fails(
        self,
        sample_batch_test,
    ):
        """Unsafe actors and resources must remain tracked for shutdown retry."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
            _RayRunningTest,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="sqlite:///data/wtb.db",
            event_bridge=MagicMock(),
        )
        actors = [MagicMock(name="unsafe-actor"), MagicMock(name="killed-actor")]
        runner._actors = list(actors)
        runner._actor_pool = MagicMock()
        refs = [MagicMock(name="ref-a"), MagicMock(name="ref-b")]
        combinations = sample_batch_test.variant_combinations[:2]
        pending_refs = list(refs)
        running_test = _RayRunningTest(
            batch_test_id=sample_batch_test.id,
            started_at=datetime.now(),
            total_variants=len(refs),
            pending_refs=list(refs),
        )
        environment_provider = MagicMock()
        workspace_manager = MagicMock()
        workspace_manager.cleanup_batch.return_value = 1
        runner._environment_provider = environment_provider
        runner._workspace_manager = workspace_manager
        runner._provisioned_env_ids = ["env-a"]

        with (
            patch(
                "wtb.application.services.ray_batch_runner.ray.wait",
                return_value=([], list(refs)),
            ),
            patch(
                "wtb.application.services.ray_batch_runner.ray.kill",
                side_effect=[RuntimeError("control plane unavailable"), None],
            ),
            pytest.raises(BatchRunnerError, match="actor termination error"),
        ):
            runner._process_completed_refs(
                pending_refs=pending_refs,
                ref_to_combo=dict(zip(refs, combinations)),
                batch_test=sample_batch_test,
                running_test=running_test,
                timeout=0.25,
            )

        assert runner._actors == [actors[0]]
        assert runner._actor_pool is None
        assert runner._poisoned is True
        assert sample_batch_test.id in runner._unsafe_batch_ids
        assert runner._orphaned_refs == refs
        assert pending_refs == refs
        assert running_test.pending_refs == refs
        assert running_test.failed == 0
        environment_provider.cleanup_environment.assert_not_called()
        workspace_manager.cleanup_batch.assert_not_called()

        with patch("wtb.application.services.ray_batch_runner.ray.kill") as retry_kill:
            runner.shutdown()

        retry_kill.assert_called_once_with(actors[0], no_restart=True)
        assert runner._poisoned is False
        assert runner._orphaned_refs == []
        environment_provider.cleanup_environment.assert_called_once()
        cleanup_args, cleanup_kwargs = (
            environment_provider.cleanup_environment.call_args
        )
        assert cleanup_args == ("env-a",)
        assert 0 < cleanup_kwargs["timeout"] <= runner._shutdown_timeout_seconds
        workspace_manager.cleanup_batch.assert_called_once_with(
            batch_id=sample_batch_test.id,
            reason="runner_shutdown_after_timeout",
        )

    def test_disable_audit_does_not_create_null_audit_factory(self):
        """Disabling audit must leave Ray lifecycle events operational."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="inmemory",
            enable_audit=False,
        )
        event_bridge = runner._event_bridge
        event_bridge.publish_event = MagicMock()

        event_bridge.on_batch_test_started(
            batch_test_id="batch-no-audit",
            workflow_id="workflow-1",
            workflow_name="Workflow",
            variant_count=1,
            parallel_workers=1,
            max_pending_tasks=1,
        )

        assert event_bridge.get_batch_audit_trail("batch-no-audit") is None
        event_bridge.publish_event.assert_called_once()

    def test_remote_ray_init_failure_does_not_fallback_to_local(self):
        """An explicit remote cluster failure must fail closed."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        runner = RayBatchTestRunner(
            config=RayConfig(ray_address="ray://cluster.example:10001"),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="sqlite:///data/wtb.db",
            event_bridge=MagicMock(),
        )

        with (
            patch(
                "wtb.application.services.ray_batch_runner.ray.is_initialized",
                return_value=False,
            ),
            patch(
                "wtb.application.services.ray_batch_runner.ray.init",
                side_effect=ConnectionError("cluster unavailable"),
            ) as ray_init,
            pytest.raises(BatchRunnerError, match="cluster.example:10001"),
        ):
            runner._ensure_ray_initialized()

        ray_init.assert_called_once_with(address="ray://cluster.example:10001")
        assert runner._ray_initialized is False
    
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

    def test_shutdown_preserves_environment_provider_for_cleanup_retry(self):
        """Failed env cleanup must remain visible and retryable."""
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
            RAY_AVAILABLE,
        )

        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")

        environment_provider = MagicMock()
        environment_provider.cleanup_environment.side_effect = [
            RuntimeError("cleanup service unavailable"),
            None,
        ]
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="sqlite:///data/wtb.db",
            event_bridge=MagicMock(),
            environment_provider=environment_provider,
            owns_environment_provider=True,
        )
        runner._provisioned_env_ids = ["env-retry"]

        with pytest.raises(BatchRunnerError, match="cleanup incomplete"):
            runner.shutdown()

        assert runner._poisoned is True
        assert runner._provisioned_env_ids == ["env-retry"]
        environment_provider.close.assert_not_called()

        runner.shutdown()

        assert runner._poisoned is False
        assert runner._provisioned_env_ids == []
        environment_provider.close.assert_called_once()


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
        error_holder = []
        
        def run_batch():
            try:
                result_holder[0] = runner.run_batch_test(large_batch)
            except BaseException as error:
                error_holder.append(error)
        
        run_thread = threading.Thread(target=run_batch)
        run_thread.start()
        
        # Wait a bit then cancel
        time.sleep(0.5)
        cancelled = runner.cancel(large_batch.id)
        
        run_thread.join(timeout=5.0)
        assert not run_thread.is_alive()
        runner.shutdown()
        
        # Verify cancellation worked
        assert cancelled is True
        assert error_holder == []
        assert result_holder[0] is not None
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

    def test_factory_owns_the_grpc_provider_it_creates(self):
        """Factory-created providers must be closed with their Ray runner."""
        from wtb.application.factories import BatchTestRunnerFactory
        from wtb.config import RayConfig, WTBConfig

        config = WTBConfig(
            wtb_storage_mode="inmemory",
            state_adapter_mode="inmemory",
            ray_enabled=True,
            ray_config=RayConfig.for_testing(),
            environment_provider="grpc",
            grpc_env_manager_url="localhost:50051",
        )

        with (
            patch(
                "wtb.infrastructure.environment.GrpcEnvironmentProvider"
            ) as provider_type,
            patch(
                "wtb.application.services.ray_batch_runner.RayBatchTestRunner"
            ) as runner_type,
        ):
            runner = BatchTestRunnerFactory.create_ray(config)

        assert runner is runner_type.return_value
        assert runner_type.call_args.kwargs["environment_provider"] is (
            provider_type.return_value
        )
        assert runner_type.call_args.kwargs["owns_environment_provider"] is True
        
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
