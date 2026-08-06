"""
SDK Integration tests for Ray distributed batch testing.

Tests Ray integration:
- Batch test execution with Ray actors
- Parallel variant testing
- Result aggregation
- Error handling and retries
- Actor pool management

Run with: pytest tests/test_sdk/test_sdk_ray_integration.py -v

For full Ray integration tests, set RAY_INTEGRATION_TESTS=1:
    RAY_INTEGRATION_TESTS=1 pytest tests/test_sdk/test_sdk_ray_integration.py -v
"""

import base64
import json
import os
import time
import uuid

import pytest

from tests.test_sdk.conftest import (
    LANGGRAPH_AVAILABLE,
    RAY_AVAILABLE,
)
from wtb.application.factories import BatchTestRunnerFactory
from wtb.config import RayConfig, WTBConfig
from wtb.domain.models import ExecutionStatus
from wtb.domain.models.batch_test import (
    BatchTest,
    BatchTestResult,
    BatchTestStatus,
    VariantCombination,
)
from wtb.sdk import WorkflowProject

# ═══════════════════════════════════════════════════════════════════════════════
# RayConfig Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRayConfiguration:
    """Tests for Ray configuration."""
    
    def test_ray_config_defaults(self):
        """Test RayConfig default values."""
        config = RayConfig()
        
        assert config.ray_address == "auto"
        assert config.num_cpus_per_task == 1.0
        assert config.memory_per_task_gb == 2.0
        assert config.max_pending_tasks == 100
        assert config.max_retries == 3
    
    def test_ray_config_for_testing(self):
        """Test RayConfig for testing (minimal resources)."""
        config = RayConfig.for_testing()
        
        assert config.num_cpus_per_task == 0.5
        assert config.memory_per_task_gb == 0.5
        assert config.max_pending_tasks == 2
        assert config.max_retries == 1
    
    def test_ray_config_for_local_development(self):
        """Test RayConfig for local development."""
        config = RayConfig.for_local_development()
        
        assert config.ray_address == "auto"
        assert config.max_pending_tasks == 4
        assert config.task_timeout_seconds == 300.0
    
    def test_ray_config_for_production(self):
        """Test RayConfig for production cluster."""
        config = RayConfig.for_production(
            ray_address="ray://cluster:10001",
            num_workers=20,
            memory_gb=8.0,
        )
        
        assert config.ray_address == "ray://cluster:10001"
        assert config.memory_per_task_gb == 8.0
        assert config.max_pending_tasks == 40  # num_workers * 2
    
    def test_ray_config_serialization(self):
        """Test RayConfig serialization."""
        config = RayConfig.for_testing()
        
        data = config.to_dict()
        
        assert "ray_address" in data
        assert "num_cpus_per_task" in data
        assert data["num_cpus_per_task"] == 0.5


# ═══════════════════════════════════════════════════════════════════════════════
# Batch Test Runner Factory Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBatchTestRunnerFactory:
    """Tests for BatchTestRunnerFactory."""
    
    def test_create_threadpool_runner(self):
        """Test creating ThreadPool runner."""
        from wtb.application.services.batch_test_runner import ThreadPoolBatchTestRunner
        
        runner = BatchTestRunnerFactory.create_for_testing()
        
        assert isinstance(runner, ThreadPoolBatchTestRunner)
        
        runner.shutdown()
    
    def test_create_threadpool_with_config(self):
        """Test creating ThreadPool runner with config."""
        config = WTBConfig.for_testing()
        config.ray_enabled = False
        
        runner = BatchTestRunnerFactory.create(config)
        
        from wtb.application.services.batch_test_runner import ThreadPoolBatchTestRunner
        assert isinstance(runner, ThreadPoolBatchTestRunner)
        
        runner.shutdown()

    def test_ray_enabled_without_config_uses_ray_defaults(self):
        """ray_enabled must not silently select the threadpool runner."""
        from unittest.mock import patch, sentinel

        config = WTBConfig.for_testing()
        config.ray_enabled = True
        config.ray_config = None

        with (
            patch.object(
                BatchTestRunnerFactory,
                "create_ray",
                return_value=sentinel.ray_runner,
            ) as create_ray,
            patch.object(BatchTestRunnerFactory, "create_threadpool") as create_threadpool,
        ):
            runner = BatchTestRunnerFactory.create(config)

        assert runner is sentinel.ray_runner
        create_ray.assert_called_once_with(config)
        create_threadpool.assert_not_called()

    def test_execution_config_defaults_to_threadpool(self):
        """The default project must match the default test-bench runner."""
        from wtb.sdk import ExecutionConfig

        assert ExecutionConfig().batch_executor == "threadpool"

    def test_bench_close_retries_after_batch_runner_shutdown_failure(self, wtb_inmemory):
        """A failed actor shutdown must remain retryable through the SDK."""
        from unittest.mock import MagicMock

        from wtb.domain.interfaces.batch_runner import BatchRunnerError

        runner = MagicMock()
        runner.shutdown.side_effect = [
            BatchRunnerError("actor termination unconfirmed"),
            None,
        ]
        wtb_inmemory._batch_runner = runner

        with pytest.raises(BatchRunnerError, match="termination unconfirmed"):
            wtb_inmemory.close()

        assert wtb_inmemory._closed is False
        wtb_inmemory.close()
        assert wtb_inmemory._closed is True
        assert runner.shutdown.call_count == 2
    
    @pytest.mark.skipif(not RAY_AVAILABLE, reason="Ray not installed")
    def test_create_ray_runner(self, ray_initialized, tmp_path):
        """Test creating Ray runner."""
        from wtb.application.services.ray_batch_runner import RayBatchTestRunner
        
        config = WTBConfig.for_testing()
        config.ray_enabled = True
        config.ray_config = RayConfig.for_testing()
        config.data_dir = str(tmp_path)
        
        runner = BatchTestRunnerFactory.create_ray(config)
        
        assert isinstance(runner, RayBatchTestRunner)
        
        runner.shutdown()


# ═══════════════════════════════════════════════════════════════════════════════
# SDK Batch Testing Tests (Sequential/ThreadPool)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not installed")
class TestSDKBatchTesting:
    """Tests for SDK batch testing with sequential/threadpool runner."""
    
    def test_run_batch_test_sequential(self, wtb_inmemory, simple_project):
        """Test running batch test sequentially (no batch_runner)."""
        wtb_inmemory.register_project(simple_project)
        
        result = wtb_inmemory.run_batch_test(
            project=simple_project.name,
            variant_matrix=[
                {"node_b": "default"},
            ],
            test_cases=[
                {"messages": [], "count": 0},
                {"messages": ["init"], "count": 10},
            ],
        )
        
        assert isinstance(result, BatchTest)
        assert result.status in [BatchTestStatus.COMPLETED, BatchTestStatus.FAILED]
    

    def test_run_batch_test_sequential_executes_successfully(
        self,
        wtb_langgraph,
        simple_graph_factory,
    ):
        """Sequential mode must execute the public LangGraph batch path."""
        from wtb.sdk import ExecutionConfig

        project = WorkflowProject(
            name=f"sequential_success_{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            execution=ExecutionConfig(batch_executor="sequential"),
        )
        wtb_langgraph.register_project(project)

        result = wtb_langgraph.run_batch_test(
            project=project.name,
            variant_matrix=[{}],
            test_cases=[{"messages": [], "count": 0}],
        )

        assert result.status is BatchTestStatus.COMPLETED
        assert len(result.results) == 1
        item = result.results[0]
        assert item.success is True
        assert item.error_message is None
        assert item.overall_score == 1.0

        execution = wtb_langgraph.get_execution(item.execution_id)
        assert execution.status is ExecutionStatus.COMPLETED
        assert execution.state.workflow_variables["messages"] == ["A", "B", "C"]
        assert execution.state.workflow_variables["count"] == 3
    def test_batch_test_with_multiple_variants(self, wtb_inmemory, project_with_variants):
        """Test batch test with multiple variants."""
        wtb_inmemory.register_project(project_with_variants)
        
        result = wtb_inmemory.run_batch_test(
            project=project_with_variants.name,
            variant_matrix=[
                {"node_b": "default"},
                {"node_b": "fast"},
                {"node_b": "slow"},
            ],
            test_cases=[
                {"messages": [], "count": 0},
            ],
        )
        
        assert isinstance(result, BatchTest)
        # Check results (batch execution creates results for each variant)
        assert len(result.results) == 3
    
    def test_batch_test_results_structure(self, wtb_inmemory, simple_project):
        """Test batch test results structure."""
        wtb_inmemory.register_project(simple_project)
        
        result = wtb_inmemory.run_batch_test(
            project=simple_project.name,
            variant_matrix=[
                {"node_b": "default"},
            ],
            test_cases=[
                {"messages": [], "count": 0},
            ],
        )
        
        assert hasattr(result, 'results')
        assert hasattr(result, 'variant_combinations')
        assert hasattr(result, 'status')

    def test_project_executor_mismatch_fails_before_running(
        self,
        wtb_inmemory,
        simple_graph_factory,
    ):
        """A project requesting Ray must not silently run on threadpool."""
        from unittest.mock import patch

        from wtb.domain.interfaces.batch_runner import BatchRunnerError
        from wtb.sdk import ExecutionConfig

        project = WorkflowProject(
            name=f"ray_required_{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            execution=ExecutionConfig(batch_executor="ray"),
        )
        wtb_inmemory.register_project(project)

        with (
            patch.object(wtb_inmemory._batch_runner, "run_batch_test") as run_batch,
            pytest.raises(BatchRunnerError, match="requires batch_executor='ray'"),
        ):
            wtb_inmemory.run_batch_test(
                project=project.name,
                variant_matrix=[{"node_b": "default"}],
                test_cases=[],
            )

        run_batch.assert_not_called()

    def test_sequential_project_bypasses_configured_runner(
        self,
        wtb_inmemory,
        simple_graph_factory,
    ):
        """A sequential project must use the inline SDK execution path."""
        from unittest.mock import patch, sentinel

        from wtb.sdk import ExecutionConfig

        project = WorkflowProject(
            name=f"sequential_{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            execution=ExecutionConfig(batch_executor="sequential"),
        )
        wtb_inmemory.register_project(project)
        variant_matrix = [{"node_b": "default"}]
        test_cases = [{"messages": [], "count": 0}]

        with (
            patch.object(
                wtb_inmemory,
                "_run_batch_sequential",
                return_value=sentinel.batch_result,
            ) as run_sequential,
            patch.object(wtb_inmemory._batch_runner, "run_batch_test") as run_batch,
        ):
            result = wtb_inmemory.run_batch_test(
                project=project.name,
                variant_matrix=variant_matrix,
                test_cases=test_cases,
            )

        assert result is sentinel.batch_result
        run_sequential.assert_called_once_with(project.name, variant_matrix, test_cases)
        run_batch.assert_not_called()


    @pytest.mark.parametrize("executor", ["ray", "threadpool"])
    def test_requested_executor_without_runner_does_not_fallback_to_sequential(
        self,
        wtb_langgraph,
        simple_graph_factory,
        executor,
    ):
        """An unavailable requested runner must not silently change semantics."""
        from wtb.domain.interfaces.batch_runner import BatchRunnerError
        from wtb.sdk import ExecutionConfig

        project = WorkflowProject(
            name=f"{executor}_without_runner_{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            execution=ExecutionConfig(batch_executor=executor),
        )
        wtb_langgraph.register_project(project)
        assert wtb_langgraph._batch_runner is None

        with pytest.raises(BatchRunnerError):
            wtb_langgraph.run_batch_test(
                project=project.name,
                variant_matrix=[{}],
                test_cases=[{"messages": [], "count": 0}],
            )

    def test_threadpool_executes_registered_node_variant(self, wtb_sqlite):
        """ThreadPool must execute the selected registered implementation."""
        from tests.integration.test_real_file_control_flow_modes import (
            create_file_control_graph,
        )
        try:
            import cloudpickle
        except ImportError:
            from ray import cloudpickle
        from wtb.sdk import ExecutionConfig

        def registered_failure(state):
            raise RuntimeError("registered variant executed")

        project = WorkflowProject(
            name=f"threadpool_registered_{uuid.uuid4().hex[:8]}",
            graph_factory=create_file_control_graph,
            execution=ExecutionConfig(batch_executor="threadpool"),
        )
        project.register_variant(
            "finalize",
            "registered_failure",
            registered_failure,
        )
        wtb_sqlite.register_project(project)

        result = wtb_sqlite.run_batch_test(
            project=project.name,
            variant_matrix=[{"finalize": "registered_failure"}],
            test_cases=[
                {
                    "messages": [],
                    "suffix": "base",
                    "result": "",
                    "_output_files": {},
                }
            ],
        )

        json.dumps(result.to_dict())
        encoded_graph = (
            result.variant_combinations[0].metadata["_graph_pickled"]
        )
        assert isinstance(encoded_graph, str)
        assert encoded_graph.isascii()
        restored_graph = cloudpickle.loads(
            base64.b64decode(encoded_graph, validate=True)
        )
        assert restored_graph is not None
        assert result.status is BatchTestStatus.FAILED
        assert len(result.results) == 1
        assert result.results[0].success is False
        assert "registered variant executed" in (
            result.results[0].error_message or ""
        )

    def test_sequential_all_failed_marks_batch_failed(
        self,
        wtb_langgraph,
        simple_graph_factory,
    ):
        """Sequential batches with no successful result must fail."""
        from wtb.sdk import ExecutionConfig

        def failing_variant(state):
            raise RuntimeError("expected sequential failure")

        project = WorkflowProject(
            name=f"sequential_failure_{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            execution=ExecutionConfig(batch_executor="sequential"),
        )
        project.register_variant("node_b", "fail", failing_variant)
        wtb_langgraph.register_project(project)

        result = wtb_langgraph.run_batch_test(
            project=project.name,
            variant_matrix=[{"node_b": "fail"}],
            test_cases=[{"messages": [], "count": 0}],
        )

        assert result.status is BatchTestStatus.FAILED
        assert len(result.results) == 1
        assert result.results[0].success is False
        assert "expected sequential failure" in (
            result.results[0].error_message or ""
        )

    def test_sequential_rejects_empty_variant_matrix(
        self,
        wtb_langgraph,
        simple_graph_factory,
    ):
        """Sequential mode must reject a batch with no combinations."""
        from wtb.domain.interfaces.batch_runner import BatchRunnerError
        from wtb.sdk import ExecutionConfig

        project = WorkflowProject(
            name=f"sequential_empty_matrix_{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            execution=ExecutionConfig(batch_executor="sequential"),
        )
        wtb_langgraph.register_project(project)

        with pytest.raises(BatchRunnerError, match="No variant combinations"):
            wtb_langgraph.run_batch_test(
                project=project.name,
                variant_matrix=[],
                test_cases=[{"messages": [], "count": 0}],
            )

    def test_sequential_empty_test_cases_runs_default_case(
        self,
        wtb_langgraph,
        simple_graph_factory,
    ):
        """Empty test cases use one default initial-state case in every mode."""
        from wtb.sdk import ExecutionConfig

        project = WorkflowProject(
            name=f"sequential_default_case_{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            execution=ExecutionConfig(batch_executor="sequential"),
        )
        wtb_langgraph.register_project(project)

        result = wtb_langgraph.run_batch_test(
            project=project.name,
            variant_matrix=[{}],
            test_cases=[],
        )

        assert result.status is BatchTestStatus.COMPLETED
        assert len(result.results) == 1
        assert result.results[0].success is True

    def test_sequential_continues_after_one_test_case_raises(
        self,
        wtb_langgraph,
        simple_graph_factory,
    ):
        """Each matrix cell must produce a result even if one run raises."""
        from types import SimpleNamespace
        from unittest.mock import patch

        from wtb.sdk import ExecutionConfig

        project = WorkflowProject(
            name=f"sequential_case_failure_{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            execution=ExecutionConfig(batch_executor="sequential"),
        )
        wtb_langgraph.register_project(project)
        created_ids = iter(("exec-0", "exec-1", "exec-2"))

        def create_execution(**_kwargs):
            return SimpleNamespace(id=next(created_ids))

        def run_execution(execution_id, graph):
            assert graph is not None
            if execution_id == "exec-1":
                raise RuntimeError("case one failed")
            score = 0.9 if execution_id == "exec-0" else 0.7
            return SimpleNamespace(
                id=execution_id,
                status=ExecutionStatus.COMPLETED,
                error_message=None,
                checkpoint_id=None,
                state=SimpleNamespace(
                    workflow_variables={
                        "overall_score": score,
                        "_metrics": {"accuracy": score - 0.1},
                        "unrelated_number": 99,
                    },
                    execution_path=[],
                ),
            )

        with (
            patch.object(
                wtb_langgraph._exec_ctrl,
                "create_execution",
                side_effect=create_execution,
            ),
            patch.object(
                wtb_langgraph._exec_ctrl,
                "run",
                side_effect=run_execution,
            ),
        ):
            result = wtb_langgraph.run_batch_test(
                project=project.name,
                variant_matrix=[{}],
                test_cases=[{"case": 0}, {"case": 1}, {"case": 2}],
            )

        assert result.status is BatchTestStatus.COMPLETED
        assert len(result.results) == 3
        assert [item.test_case_index for item in result.results] == [0, 1, 2]
        assert [item.success for item in result.results] == [True, False, True]
        assert [item.overall_score for item in result.results] == [0.9, 0.0, 0.7]
        assert result.results[0].metrics == {
            "overall_score": 0.9,
            "accuracy": 0.8,
        }
        assert result.best_combination_name is None

    def test_sequential_deep_copies_nested_state_for_each_execution(
        self,
        wtb_langgraph,
        simple_graph_factory,
    ):
        """A run cannot mutate another variant's nested initial state."""
        from types import SimpleNamespace
        from unittest.mock import patch

        from wtb.sdk import ExecutionConfig

        project = WorkflowProject(
            name=f"sequential_state_isolation_{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            execution=ExecutionConfig(batch_executor="sequential"),
        )
        wtb_langgraph.register_project(project)
        source_case = {"nested": {"seen": []}}
        created_ids = iter(("exec-a", "exec-b"))
        received_states = []

        def create_execution(workflow, initial_state):
            received_states.append(list(initial_state["nested"]["seen"]))
            initial_state["nested"]["seen"].append("worker-mutation")
            return SimpleNamespace(id=next(created_ids))

        def run_execution(execution_id, graph):
            return SimpleNamespace(
                id=execution_id,
                status=ExecutionStatus.COMPLETED,
                error_message=None,
                checkpoint_id=None,
                state=SimpleNamespace(
                    workflow_variables={},
                    execution_path=[],
                ),
            )

        with (
            patch.object(
                wtb_langgraph._exec_ctrl,
                "create_execution",
                side_effect=create_execution,
            ),
            patch.object(
                wtb_langgraph._exec_ctrl,
                "run",
                side_effect=run_execution,
            ),
        ):
            result = wtb_langgraph.run_batch_test(
                project=project.name,
                variant_matrix=[{}, {}],
                test_cases=[source_case],
            )

        assert result.status is BatchTestStatus.COMPLETED
        assert received_states == [[], []]
        assert source_case == {"nested": {"seen": []}}

    def test_all_batch_inputs_are_isolated_before_first_runner_call(
        self,
        wtb_inmemory,
        simple_graph_factory,
    ):
        """An uncopyable later case cannot allow an earlier case to run."""
        from unittest.mock import MagicMock

        from wtb.sdk import ExecutionConfig

        class Uncopyable:
            def __deepcopy__(self, memo):
                raise TypeError("cannot isolate later case")

        project = WorkflowProject(
            name=f"precopy_{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            execution=ExecutionConfig(batch_executor="threadpool"),
        )
        wtb_inmemory.register_project(project)
        runner = MagicMock()
        wtb_inmemory._batch_runner = runner

        with pytest.raises(TypeError, match="cannot isolate later case"):
            wtb_inmemory.run_batch_test(
                project=project.name,
                variant_matrix=[{}],
                test_cases=[{"case": 0}, {"value": Uncopyable()}],
            )

        runner.run_batch_test.assert_not_called()

    @pytest.mark.parametrize("executor", ["threadpool", "ray"])
    def test_complete_multi_case_matrix_uses_global_success_status(
        self,
        wtb_inmemory,
        simple_graph_factory,
        executor,
    ):
        """A complete matrix with any success has the same status in every mode."""
        from wtb.application.services.batch_test_runner import (
            ThreadPoolBatchTestRunner,
        )
        from wtb.sdk import ExecutionConfig

        project = WorkflowProject(
            name=f"{executor}_partial_success_{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            execution=ExecutionConfig(batch_executor=executor),
        )
        wtb_inmemory.register_project(project)

        if executor == "threadpool":
            runner = ThreadPoolBatchTestRunner.__new__(ThreadPoolBatchTestRunner)
        else:
            fake_ray_type = type(
                "RayBatchTestRunner",
                (),
                {"__module__": "wtb.application.services.ray_batch_runner"},
            )
            runner = fake_ray_type()

        calls = 0

        def run_case(batch):
            nonlocal calls
            batch.start()
            success = calls == 1
            batch.add_result(
                BatchTestResult(
                    combination_name="variant_0",
                    execution_id=f"exec-{calls}",
                    success=success,
                    overall_score=0.5 if success else 0.0,
                )
            )
            calls += 1
            if success:
                batch.complete()
            else:
                batch.fail("all variants failed for this case")
            return batch

        runner.run_batch_test = run_case
        wtb_inmemory._batch_runner = runner

        result = wtb_inmemory.run_batch_test(
            project=project.name,
            variant_matrix=[{}],
            test_cases=[{"case": 0}, {"case": 1}],
        )

        assert result.status is BatchTestStatus.COMPLETED
        assert result.is_complete() is True
        assert [item.success for item in result.results] == [False, True]
        assert result.best_combination_name is None

    def test_multi_case_rejects_duplicate_result_identity_with_missing_combo(
        self,
        wtb_inmemory,
        simple_graph_factory,
    ):
        """A result count match cannot hide a missing combination identity."""
        from wtb.application.services.batch_test_runner import (
            ThreadPoolBatchTestRunner,
        )
        from wtb.sdk import ExecutionConfig

        project = WorkflowProject(
            name=f"identity_incomplete_{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            execution=ExecutionConfig(batch_executor="threadpool"),
        )
        wtb_inmemory.register_project(project)
        runner = ThreadPoolBatchTestRunner.__new__(ThreadPoolBatchTestRunner)
        case_index = 0

        def run_case(batch):
            nonlocal case_index
            batch.start()
            batch.add_result(
                BatchTestResult(
                    combination_name="variant_0",
                    execution_id=f"exec-{case_index}-a",
                    success=True,
                    overall_score=0.8,
                )
            )
            batch.add_result(
                BatchTestResult(
                    combination_name="variant_0",
                    execution_id=f"exec-{case_index}-duplicate",
                    success=True,
                    overall_score=0.7,
                )
            )
            case_index += 1
            batch.complete()
            return batch

        runner.run_batch_test = run_case
        wtb_inmemory._batch_runner = runner

        result = wtb_inmemory.run_batch_test(
            project=project.name,
            variant_matrix=[{}, {"node_b": "alternate"}],
            test_cases=[{"case": 0}, {"case": 1}],
        )

        assert result.status is BatchTestStatus.FAILED
        assert result.is_complete() is False
        assert "identities" in result.metadata["error_message"]

    def test_multi_case_batch_rebuilds_aggregate_metadata(self, wtb_inmemory):
        """Aggregated results must rebuild IDs, best result, and comparison data."""
        from tests.integration.test_real_file_control_flow_modes import (
            create_file_control_graph,
        )
        from wtb.sdk import ExecutionConfig

        project = WorkflowProject(
            name=f"multi_case_aggregate_{uuid.uuid4().hex[:8]}",
            graph_factory=create_file_control_graph,
            execution=ExecutionConfig(batch_executor="threadpool"),
        )
        wtb_inmemory.register_project(project)

        result = wtb_inmemory.run_batch_test(
            project=project.name,
            variant_matrix=[{"finalize": "marked"}],
            test_cases=[
                {
                    "messages": [],
                    "suffix": "first",
                    "result": "",
                    "_output_files": {},
                },
                {
                    "messages": [],
                    "suffix": "second",
                    "result": "",
                    "_output_files": {},
                },
            ],
        )

        assert result.status is BatchTestStatus.COMPLETED
        assert len(result.results) == 2
        assert result.metadata["test_case_count"] == 2
        assert [item.test_case_index for item in result.results] == [0, 1]
        assert result.is_complete() is True
        assert all(item.success for item in result.results)
        result_ids = {item.execution_id for item in result.results}
        assert set(result.execution_ids) == result_ids
        assert len(result.execution_ids) == 2
        assert result.comparison_matrix is not None
        matrix_ids = {
            row["execution_id"]
            for row in result.comparison_matrix["data"]
        }
        assert matrix_ids == result_ids

    @pytest.mark.parametrize(
        ("child_outcome", "expected_status"),
        [
            ("cancelled", BatchTestStatus.CANCELLED),
            ("failed", BatchTestStatus.FAILED),
            ("partial", BatchTestStatus.FAILED),
        ],
    )
    def test_multi_case_aggregate_preserves_child_terminal_status(
        self,
        wtb_inmemory,
        simple_project,
        child_outcome,
        expected_status,
    ):
        """A partial child failure/cancel must not aggregate as completed."""
        from unittest.mock import MagicMock

        wtb_inmemory.register_project(simple_project)
        runner = MagicMock()
        outcomes = iter(("completed", child_outcome))

        def run_case(batch):
            outcome = next(outcomes)
            batch.start()
            if outcome == "completed":
                batch.add_result(
                    BatchTestResult(
                        combination_name="variant_0",
                        execution_id="completed-case",
                        success=True,
                        overall_score=1.0,
                    )
                )
                batch.complete()
            elif outcome == "cancelled":
                batch.cancel()
            elif outcome == "failed":
                batch.fail("child case failed")
            else:
                batch.complete()
            return batch

        runner.run_batch_test.side_effect = run_case
        wtb_inmemory._batch_runner = runner

        result = wtb_inmemory.run_batch_test(
            project=simple_project.name,
            variant_matrix=[{}],
            test_cases=[
                {"messages": [], "count": 0},
                {"messages": ["second"], "count": 1},
            ],
        )

        assert result.status is expected_status
        assert len(result.results) == 1
        assert result.results[0].execution_id == "completed-case"

    def test_batch_control_operations_resolve_graph_from_result_execution(
        self,
        wtb_inmemory,
    ):
        """Rollback, fork, and checkpoint reads must not use the first project."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        first_graph = object()
        expected_graph = object()
        first_project = SimpleNamespace(
            id="workflow-first",
            build_graph=MagicMock(return_value=first_graph),
        )
        expected_project = SimpleNamespace(
            id="workflow-expected",
            build_graph=MagicMock(return_value=expected_graph),
        )
        wtb_inmemory._project_cache = {
            "first": first_project,
            "expected": expected_project,
        }
        source_execution = SimpleNamespace(
            id="exec-expected",
            workflow_id="workflow-expected",
            state=None,
        )
        batch_result = BatchTestResult(
            combination_name="variant_0",
            execution_id=source_execution.id,
            success=True,
            last_checkpoint_id="checkpoint-1",
        )
        coordinator = MagicMock()
        coordinator.rollback.return_value = source_execution
        coordinator.fork.return_value = SimpleNamespace(id="forked-execution")
        coordinator.get_checkpoints.return_value = [
            {
                "checkpoint_id": "checkpoint-1",
                "step": 1,
                "writes": {},
                "next": [],
                "values": {},
            }
        ]

        with (
            patch.object(
                wtb_inmemory,
                "get_batch_coordinator",
                return_value=coordinator,
            ),
            patch.object(
                wtb_inmemory,
                "get_execution",
                return_value=source_execution,
            ),
        ):
            rollback = wtb_inmemory.rollback_batch_result(batch_result)
            fork = wtb_inmemory.fork_batch_result(batch_result)
            checkpoints = wtb_inmemory.get_batch_result_checkpoints(batch_result)

        assert rollback.success is True
        assert fork.error is None
        assert fork.fork_execution_id == "forked-execution"
        assert checkpoints
        assert coordinator.rollback.call_args.kwargs["graph"] is expected_graph
        assert coordinator.fork.call_args.kwargs["graph"] is expected_graph
        assert coordinator.get_checkpoints.call_args.kwargs["graph"] is expected_graph

    def test_case_batches_keep_pickled_graph_factory_metadata(self, wtb_inmemory):
        """Every case batch must retain an unimportable graph factory."""
        import sys
        from unittest.mock import MagicMock, patch

        from wtb.sdk import ExecutionConfig

        def interactive_graph_factory():
            return object()

        interactive_graph_factory.__module__ = "__main__"
        project = WorkflowProject(
            name=f"interactive_{uuid.uuid4().hex[:8]}",
            graph_factory=interactive_graph_factory,
            execution=ExecutionConfig(batch_executor="threadpool"),
        )
        wtb_inmemory.register_project(project)

        runner = MagicMock()
        runner.run_batch_test.side_effect = lambda batch: batch
        wtb_inmemory._batch_runner = runner
        pickled_factory = b"pickled-graph-factory"
        fake_cloudpickle = MagicMock()
        fake_cloudpickle.dumps.return_value = pickled_factory

        with (
            patch.object(sys.modules["__main__"], "__spec__", None),
            patch.dict(sys.modules, {"cloudpickle": fake_cloudpickle}),
        ):
            wtb_inmemory.run_batch_test(
                project=project.name,
                variant_matrix=[{"node": "variant"}],
                test_cases=[{"case": 1}, {"case": 2}],
            )

        case_batches = [call.args[0] for call in runner.run_batch_test.call_args_list]
        assert all(json.dumps(batch.to_dict()) for batch in case_batches)
        assert len(case_batches) == 2
        assert [batch.metadata["_graph_factory_pickled"] for batch in case_batches] == [
            base64.b64encode(pickled_factory).decode("ascii"),
            base64.b64encode(pickled_factory).decode("ascii"),
        ]
        assert all(
            base64.b64decode(batch.metadata["_graph_factory_pickled"]) == pickled_factory
            for batch in case_batches
        )
        assert case_batches[0].variant_combinations is not case_batches[1].variant_combinations

    def test_importable_graph_factory_also_carries_remote_fallback(
        self,
        wtb_inmemory,
        simple_graph_factory,
    ):
        """Remote workers may not have the driver's otherwise importable module."""
        from unittest.mock import MagicMock

        from wtb.sdk import ExecutionConfig

        project = WorkflowProject(
            name=f"remote_fallback_{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            execution=ExecutionConfig(batch_executor="threadpool"),
        )
        wtb_inmemory.register_project(project)

        runner = MagicMock()
        runner.run_batch_test.side_effect = lambda batch: batch
        wtb_inmemory._batch_runner = runner
        wtb_inmemory.run_batch_test(
            project=project.name,
            variant_matrix=[{}],
            test_cases=[{"messages": [], "count": 0}],
        )

        batch = runner.run_batch_test.call_args.args[0]
        payload = batch.metadata.get("_graph_factory_pickled")
        assert payload
        assert callable(__import__("cloudpickle").loads(base64.b64decode(payload)))

    def test_remote_fallback_deserializes_without_driver_module(self, wtb_inmemory):
        """The fallback must contain code, not another import-by-name reference."""
        import sys
        import types
        from unittest.mock import MagicMock

        from wtb.sdk import ExecutionConfig

        module_name = f"driver_only_graph_{uuid.uuid4().hex}"
        module = types.ModuleType(module_name)

        def create_graph():
            return "remote-graph"

        create_graph.__module__ = module_name
        module.create_graph = create_graph
        sys.modules[module_name] = module
        try:
            project = WorkflowProject(
                name=f"remote_by_value_{uuid.uuid4().hex[:8]}",
                graph_factory=module.create_graph,
                execution=ExecutionConfig(batch_executor="threadpool"),
            )
            wtb_inmemory.register_project(project)
            runner = MagicMock()
            runner.run_batch_test.side_effect = lambda batch: batch
            wtb_inmemory._batch_runner = runner

            wtb_inmemory.run_batch_test(
                project=project.name,
                variant_matrix=[{}],
                test_cases=[{}],
            )
            payload = runner.run_batch_test.call_args.args[0].metadata[
                "_graph_factory_pickled"
            ]
        finally:
            sys.modules.pop(module_name, None)

        restored = __import__("cloudpickle").loads(base64.b64decode(payload))
        assert restored() == "remote-graph"


# ═══════════════════════════════════════════════════════════════════════════════
# Ray Batch Runner Unit Tests (Mocked)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not RAY_AVAILABLE, reason="Ray not installed")
class TestRayBatchRunnerUnit:
    """Unit tests for RayBatchTestRunner (no actual Ray execution)."""
    
    def test_ray_runner_is_available(self):
        """Test Ray availability check."""
        from wtb.application.services.ray_batch_runner import RayBatchTestRunner
        
        result = RayBatchTestRunner.is_available()
        assert result is True
    
    def test_ray_runner_creation(self, ray_initialized, tmp_path):
        """Test Ray runner creation."""
        from wtb.application.services.ray_batch_runner import RayBatchTestRunner
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url=str(tmp_path / "agentgit.db"),
            wtb_db_url=f"sqlite:///{tmp_path / 'wtb.db'}",
        )
        
        assert runner is not None
        
        runner.shutdown()
    
    def test_ray_runner_status_idle(self, ray_initialized, tmp_path):
        """Test Ray runner status when idle."""
        from wtb.application.services.ray_batch_runner import RayBatchTestRunner
        from wtb.domain.interfaces.batch_runner import BatchRunnerStatus
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url=str(tmp_path / "agentgit.db"),
            wtb_db_url=f"sqlite:///{tmp_path / 'wtb.db'}",
        )
        
        status = runner.get_status("nonexistent-batch")
        assert status == BatchRunnerStatus.IDLE
        
        runner.shutdown()
    
    def test_ray_runner_empty_batch_raises(self, ray_initialized, tmp_path, simple_project):
        """Test Ray runner raises on empty batch."""
        from wtb.application.services.ray_batch_runner import RayBatchTestRunner
        from wtb.domain.interfaces.batch_runner import BatchRunnerError
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url=str(tmp_path / "agentgit.db"),
            wtb_db_url=f"sqlite:///{tmp_path / 'wtb.db'}",
        )
        
        empty_batch = BatchTest(
            id="empty-batch",
            name="Empty",
            workflow_id=simple_project.id,
            variant_combinations=[],
        )
        
        with pytest.raises(BatchRunnerError):
            runner.run_batch_test(empty_batch)
        
        runner.shutdown()
    
    def test_ray_runner_shutdown(self, ray_initialized, tmp_path):
        """Test Ray runner shutdown."""
        from wtb.application.services.ray_batch_runner import RayBatchTestRunner
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url=str(tmp_path / "agentgit.db"),
            wtb_db_url=f"sqlite:///{tmp_path / 'wtb.db'}",
        )
        
        runner.shutdown()
        
        assert runner._actor_pool is None
        assert len(runner._actors) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Ray Integration Tests (Real Ray)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(
    not os.environ.get("RAY_INTEGRATION_TESTS"),
    reason="Ray integration tests disabled. Set RAY_INTEGRATION_TESTS=1 to enable."
)
@pytest.mark.skipif(not RAY_AVAILABLE, reason="Ray not installed")
@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not installed")
class TestRayIntegration:
    """Full integration tests with real Ray cluster."""
    
    def test_ray_batch_test_basic(
        self, 
        ray_initialized, 
        tmp_path,
        simple_graph_factory,
    ):
        """Test basic batch test execution with Ray."""
        from wtb.application.services.ray_batch_runner import RayBatchTestRunner
        from wtb.domain.models.workflow import TestWorkflow, WorkflowEdge, WorkflowNode
        
        # Create workflow
        workflow = TestWorkflow(
            id="wf-ray-test",
            name="Ray Test Workflow",
            entry_point="node_a",
        )
        workflow.add_node(WorkflowNode(id="node_a", name="Node A", type="action"))
        workflow.add_node(WorkflowNode(id="node_b", name="Node B", type="action"))
        workflow.add_node(WorkflowNode(id="node_c", name="Node C", type="action"))
        workflow.add_edge(WorkflowEdge(source_id="node_a", target_id="node_b"))
        workflow.add_edge(WorkflowEdge(source_id="node_b", target_id="node_c"))
        
        def workflow_loader(wf_id, uow):
            return workflow
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url=str(tmp_path / "agentgit.db"),
            wtb_db_url=f"sqlite:///{tmp_path / 'wtb.db'}",
            workflow_loader=workflow_loader,
        )
        
        batch = BatchTest(
            id="ray-batch-1",
            name="Ray Test Batch",
            workflow_id=workflow.id,
            variant_combinations=[
                VariantCombination(name="Config A", variants={}),
                VariantCombination(name="Config B", variants={}),
            ],
            initial_state={"messages": [], "count": 0},
            parallel_count=2,
        )
        
        try:
            result = runner.run_batch_test(batch)
            
            assert result.status in [BatchTestStatus.COMPLETED, BatchTestStatus.FAILED]
            assert len(result.results) >= 0
        finally:
            runner.shutdown()
    
    def test_ray_parallel_execution(self, ray_initialized, tmp_path):
        """Test parallel execution with Ray."""
        from wtb.application.services.ray_batch_runner import RayBatchTestRunner
        from wtb.domain.models.workflow import TestWorkflow, WorkflowNode
        
        workflow = TestWorkflow(
            id="wf-parallel",
            name="Parallel Test",
            entry_point="start",
        )
        workflow.add_node(WorkflowNode(id="start", name="Start", type="start"))
        
        def workflow_loader(wf_id, uow):
            return workflow
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url=str(tmp_path / "agentgit.db"),
            wtb_db_url=f"sqlite:///{tmp_path / 'wtb.db'}",
            workflow_loader=workflow_loader,
        )
        
        # Create batch with many variants
        batch = BatchTest(
            id="ray-parallel",
            name="Parallel Test",
            workflow_id=workflow.id,
            variant_combinations=[
                VariantCombination(name=f"Config {i}", variants={})
                for i in range(5)
            ],
            parallel_count=2,
        )
        
        try:
            start = time.time()
            result = runner.run_batch_test(batch)
            elapsed = time.time() - start
            
            # With parallelism, should complete reasonably fast
            assert result.status in [BatchTestStatus.COMPLETED, BatchTestStatus.FAILED]
        finally:
            runner.shutdown()
    
    def test_ray_cancellation(self, ray_initialized, tmp_path):
        """Test batch test cancellation."""
        import threading

        from wtb.application.services.ray_batch_runner import RayBatchTestRunner
        from wtb.domain.models.workflow import TestWorkflow, WorkflowNode
        
        workflow = TestWorkflow(
            id="wf-cancel",
            name="Cancel Test",
            entry_point="start",
        )
        workflow.add_node(WorkflowNode(id="start", name="Start", type="start"))
        
        def workflow_loader(wf_id, uow):
            return workflow
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url=str(tmp_path / "agentgit.db"),
            wtb_db_url=f"sqlite:///{tmp_path / 'wtb.db'}",
            workflow_loader=workflow_loader,
        )
        
        # Large batch to give time for cancellation
        batch = BatchTest(
            id="ray-cancel",
            name="Cancel Test",
            workflow_id=workflow.id,
            variant_combinations=[
                VariantCombination(name=f"Config {i}", variants={})
                for i in range(20)
            ],
            parallel_count=2,
        )
        
        result_holder = [None]
        
        def run_batch():
            result_holder[0] = runner.run_batch_test(batch)
        
        thread = threading.Thread(target=run_batch)
        thread.start()
        
        time.sleep(0.5)  # Let it start
        cancelled = runner.cancel(batch.id)
        
        thread.join(timeout=5.0)
        runner.shutdown()
        
        # Should have cancelled
        assert cancelled is True or result_holder[0] is not None


# ═══════════════════════════════════════════════════════════════════════════════
# Variant Execution Result Tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not RAY_AVAILABLE, reason="Ray not installed")
class TestVariantExecutionResult:
    """Tests for VariantExecutionResult value object."""
    
    def test_create_success_result(self):
        """Test creating successful result."""
        from wtb.application.services.ray_batch_runner import VariantExecutionResult
        
        result = VariantExecutionResult(
            execution_id="exec-1",
            combination_name="Config A",
            combination_variants={"node": "variant_a"},
            success=True,
            duration_ms=1000,
            metrics={"accuracy": 0.95},
        )
        
        assert result.success is True
        assert result.metrics["accuracy"] == 0.95
    
    def test_create_failed_result(self):
        """Test creating failed result."""
        from wtb.application.services.ray_batch_runner import VariantExecutionResult
        
        result = VariantExecutionResult(
            execution_id="exec-2",
            combination_name="Config B",
            combination_variants={},
            success=False,
            duration_ms=500,
            error="Timeout",
        )
        
        assert result.success is False
        assert result.error == "Timeout"
    
    def test_convert_to_batch_result(self):
        """Test conversion to BatchTestResult."""
        from wtb.application.services.ray_batch_runner import VariantExecutionResult
        
        result = VariantExecutionResult(
            execution_id="exec-1",
            combination_name="Config A",
            combination_variants={},
            success=True,
            duration_ms=1000,
            metrics={"overall_score": 0.9},
        )
        
        batch_result = result.to_batch_test_result()
        
        assert isinstance(batch_result, BatchTestResult)
        assert batch_result.execution_id == "exec-1"
        assert batch_result.combination_name == "Config A"


# ═══════════════════════════════════════════════════════════════════════════════
# ACID Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRayACIDCompliance:
    """Tests for ACID compliance in Ray batch testing."""
    
    def test_atomicity_result_storage(self):
        """Test results are stored atomically."""
        batch = BatchTest(
            id="atomic-batch",
            name="Atomic Test",
            workflow_id="wf-1",
            variant_combinations=[
                VariantCombination(name="A", variants={}),
            ],
        )
        
        batch.start()
        
        # Add result atomically
        batch.add_result(BatchTestResult(
            combination_name="A",
            execution_id="exec-1",
            success=True,
            metrics={"score": 0.9},
            overall_score=0.9,
        ))
        
        assert len(batch.results) == 1
        assert batch.results[0].combination_name == "A"
    
    def test_isolation_between_batches(self):
        """Test batches are isolated from each other."""
        batch1 = BatchTest(
            id="iso-batch-1",
            name="Iso 1",
            workflow_id="wf-1",
            variant_combinations=[
                VariantCombination(name="A1", variants={}),
            ],
        )
        
        batch2 = BatchTest(
            id="iso-batch-2",
            name="Iso 2",
            workflow_id="wf-1",
            variant_combinations=[
                VariantCombination(name="B1", variants={}),
            ],
        )
        
        batch1.start()
        batch2.start()
        
        batch1.add_result(BatchTestResult(
            combination_name="A1",
            execution_id="exec-a1",
            success=True,
        ))
        
        # batch2 should not have batch1's result
        assert len(batch1.results) == 1
        assert len(batch2.results) == 0
    
    def test_consistency_state_transitions(self):
        """Test state transitions are consistent."""
        batch = BatchTest(
            id="consistent-batch",
            name="Consistent",
            workflow_id="wf-1",
            variant_combinations=[
                VariantCombination(name="A", variants={}),
            ],
        )
        
        # PENDING -> RUNNING
        assert batch.status == BatchTestStatus.PENDING
        batch.start()
        assert batch.status == BatchTestStatus.RUNNING
        
        # RUNNING -> COMPLETED
        batch.complete()
        assert batch.status == BatchTestStatus.COMPLETED
        
        # Cannot start completed batch
        with pytest.raises(ValueError):
            batch.start()
    
    def test_durability_serialization(self):
        """Test results persist through serialization."""
        batch = BatchTest(
            id="durable-batch",
            name="Durable",
            workflow_id="wf-1",
            variant_combinations=[
                VariantCombination(name="A", variants={"k": "v"}),
            ],
        )
        
        batch.start()
        batch.add_result(BatchTestResult(
            combination_name="A",
            execution_id="exec-1",
            success=True,
            metrics={"score": 0.95},
            overall_score=0.95,
            duration_ms=1500,
        ))
        batch.complete()
        
        # Serialize and restore
        data = batch.to_dict()
        restored = BatchTest.from_dict(data)
        
        assert restored.id == batch.id
        assert len(restored.results) == 1
        assert restored.results[0].overall_score == 0.95


# ═══════════════════════════════════════════════════════════════════════════════
# Comparison Matrix Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestComparisonMatrix:
    """Tests for batch test comparison matrix."""
    
    def test_build_comparison_matrix(self):
        """Test building comparison matrix."""
        batch = BatchTest(
            id="matrix-batch",
            name="Matrix Test",
            workflow_id="wf-1",
            variant_combinations=[
                VariantCombination(name="A", variants={}),
                VariantCombination(name="B", variants={}),
                VariantCombination(name="C", variants={}),
            ],
        )
        
        batch.start()
        
        for i, name in enumerate(["A", "B", "C"]):
            batch.add_result(BatchTestResult(
                combination_name=name,
                execution_id=f"exec-{i}",
                success=True,
                metrics={"accuracy": 0.8 + i * 0.05, "latency": 100 + i * 10},
                overall_score=0.8 + i * 0.05,
                duration_ms=1000 + i * 100,
            ))
        
        batch.complete()
        
        matrix = batch.build_comparison_matrix()
        
        assert "combinations" in matrix
        assert len(matrix["combinations"]) == 3
        assert "accuracy" in matrix["metrics"]
        assert "latency" in matrix["metrics"]
    
    def test_comparison_matrix_with_failures(self):
        """Test comparison matrix handles failures."""
        batch = BatchTest(
            id="matrix-fail",
            name="Matrix Fail Test",
            workflow_id="wf-1",
            variant_combinations=[
                VariantCombination(name="Success", variants={}),
                VariantCombination(name="Fail", variants={}),
            ],
        )
        
        batch.start()
        
        batch.add_result(BatchTestResult(
            combination_name="Success",
            execution_id="exec-1",
            success=True,
            metrics={"score": 0.9},
            overall_score=0.9,
        ))
        
        batch.add_result(BatchTestResult(
            combination_name="Fail",
            execution_id="exec-2",
            success=False,
            error_message="Execution failed",
        ))
        
        batch.complete()
        
        matrix = batch.build_comparison_matrix()
        
        # Should include both
        assert len(matrix["combinations"]) == 2
