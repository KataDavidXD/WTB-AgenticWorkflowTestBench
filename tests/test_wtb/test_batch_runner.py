"""
Tests for Batch Test Runners.

Tests IBatchTestRunner interface, ThreadPoolBatchTestRunner, and BatchTestRunnerFactory.
"""

import base64
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from wtb.application.factories import BatchTestRunnerFactory
from wtb.application.services.batch_test_runner import ThreadPoolBatchTestRunner
from wtb.application.services.ray_batch_runner import RAY_AVAILABLE
from wtb.config import RayConfig
from wtb.domain.interfaces.batch_runner import (
    BatchRunnerError,
    BatchRunnerProgress,
    BatchRunnerStatus,
    IBatchTestRunner,
)
from wtb.domain.models.batch_test import (
    BatchTest,
    BatchTestResult,
    BatchTestStatus,
    VariantCombination,
)
from wtb.domain.models.workflow import TestWorkflow
from wtb.infrastructure.adapters import InMemoryStateAdapter
from wtb.infrastructure.database import InMemoryUnitOfWork


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
    

    def test_run_plain_python_batch_all_results_succeed(self, runner, batch_test):
        """ThreadPool mode must execute a plain Python workflow end to end."""
        from wtb.domain.models.workflow import WorkflowEdge, WorkflowNode

        workflow = TestWorkflow(
            id=batch_test.workflow_id,
            name="Plain Python workflow",
            entry_point="start",
        )
        workflow.add_node(WorkflowNode(id="start", name="Start", type="start"))
        workflow.add_node(WorkflowNode(id="work", name="Work", type="action"))
        workflow.add_node(WorkflowNode(id="end", name="End", type="end"))
        workflow.add_edge(WorkflowEdge(source_id="start", target_id="work"))
        workflow.add_edge(WorkflowEdge(source_id="work", target_id="end"))
        batch_test._workflow = workflow

        result = runner.run_batch_test(batch_test)

        assert result.status is BatchTestStatus.COMPLETED
        assert len(result.results) == 2
        assert all(item.success for item in result.results)
        assert all(item.error_message is None for item in result.results)
        assert len({item.execution_id for item in result.results}) == 2

    @pytest.mark.parametrize("metadata_format", ["base64", "legacy_bytes"])
    def test_execute_variant_decodes_pickled_graph_metadata(
        self,
        runner,
        metadata_format,
    ):
        """Both JSON-safe metadata and legacy in-memory bytes remain readable."""
        raw_payload = b"serialized-graph-payload"
        payload = (
            base64.b64encode(raw_payload).decode("ascii")
            if metadata_format == "base64"
            else raw_payload
        )
        combo = VariantCombination(
            name=f"Graph {metadata_format}",
            variants={},
            metadata={"_graph_pickled": payload},
        )
        try:
            import cloudpickle
        except ImportError:
            from ray import cloudpickle

        graph = object()
        expected = BatchTestResult(
            combination_name=combo.name,
            execution_id="exec-graph",
            success=True,
        )
        runner._controller_factory = MagicMock()

        with (
            patch.object(cloudpickle, "loads", return_value=graph) as loads,
            patch.object(
                runner,
                "_execute_with_controller_factory",
                return_value=expected,
            ) as execute,
        ):
            result = runner._execute_variant("wf-graph", combo, {})

        assert result is expected
        loads.assert_called_once_with(raw_payload)
        assert execute.call_args.args[3] is graph

    def test_invalid_base64_pickled_graph_fails_closed(self, runner):
        """Invalid public metadata must not silently fall back to another graph."""
        combo = VariantCombination(
            name="Invalid graph",
            variants={},
            metadata={"_graph_pickled": "not-valid-base64!"},
        )
        runner._controller_factory = MagicMock()

        with patch.object(runner, "_execute_with_controller_factory") as execute:
            result = runner._execute_variant("wf-graph", combo, {})

        assert result.success is False
        assert "Failed to load serialized variant graph" in (
            result.error_message or ""
        )
        execute.assert_not_called()

    def test_batch_metadata_pickled_nested_graph_factory_builds_graph(self, runner):
        """SDK batch metadata can carry an interactive graph factory."""
        try:
            import cloudpickle
        except ImportError:
            from ray import cloudpickle

        def nested_graph_factory():
            return {"graph": "from-nested-factory"}

        nested_graph_factory.__module__ = "__main__"
        payload = base64.b64encode(
            cloudpickle.dumps(nested_graph_factory)
        ).decode("ascii")
        batch = BatchTest(
            id="batch-nested-factory",
            workflow_id="wf-nested-factory",
            variant_combinations=[
                VariantCombination(name="Nested factory", variants={}),
            ],
            metadata={"_graph_factory_pickled": payload},
        )
        expected = BatchTestResult(
            combination_name="Nested factory",
            execution_id="exec-nested-factory",
            success=True,
        )
        runner._controller_factory = MagicMock()

        with patch.object(
            runner,
            "_execute_with_controller_factory",
            return_value=expected,
        ) as execute:
            result = runner.run_batch_test(batch)

        assert result.status is BatchTestStatus.COMPLETED
        assert execute.call_args.args[3] == {"graph": "from-nested-factory"}

    def test_nested_initial_state_isolated_across_parallel_variants(self):
        """Each worker receives an independent deep copy of nested state."""
        runner = ThreadPoolBatchTestRunner(
            uow_factory=lambda: InMemoryUnitOfWork(),
            state_adapter_factory=lambda: InMemoryStateAdapter(),
            max_workers=2,
            execution_timeout_seconds=2.0,
        )
        batch = BatchTest(
            id="batch-nested-state",
            workflow_id="wf-nested-state",
            initial_state={"nested": {"seen": []}},
            variant_combinations=[
                VariantCombination(name="A", variants={}),
                VariantCombination(name="B", variants={}),
            ],
        )
        both_started = threading.Barrier(2)
        worker_snapshots = {}

        def mutate_nested_state(workflow_id, combo, initial_state, *args, **kwargs):
            initial_state["nested"]["seen"].append(combo.name)
            both_started.wait(timeout=1.0)
            worker_snapshots[combo.name] = list(initial_state["nested"]["seen"])
            return BatchTestResult(
                combination_name=combo.name,
                execution_id=f"exec-{combo.name.lower()}",
                success=True,
            )

        try:
            with patch.object(
                runner,
                "_execute_variant",
                side_effect=mutate_nested_state,
            ):
                result = runner.run_batch_test(batch)
        finally:
            runner.shutdown()

        assert result.status is BatchTestStatus.COMPLETED
        assert batch.initial_state == {"nested": {"seen": []}}
        assert worker_snapshots == {"A": ["A"], "B": ["B"]}

    def test_uncopyable_initial_state_fails_before_workers_start(self):
        """Isolation failure cannot leave a RUNNING batch or partial workers."""
        class Uncopyable:
            def __deepcopy__(self, memo):
                raise TypeError("cannot isolate this value")

        runner = ThreadPoolBatchTestRunner(
            uow_factory=lambda: InMemoryUnitOfWork(),
            state_adapter_factory=lambda: InMemoryStateAdapter(),
            max_workers=2,
        )
        batch = BatchTest(
            id="batch-uncopyable-state",
            workflow_id="wf-uncopyable-state",
            initial_state={"nested": Uncopyable()},
            variant_combinations=[
                VariantCombination(name="A", variants={}),
                VariantCombination(name="B", variants={}),
            ],
        )

        try:
            with patch.object(runner, "_execute_variant") as execute:
                with pytest.raises(BatchRunnerError, match="isolate initial state"):
                    runner.run_batch_test(batch)
        finally:
            runner.shutdown()

        assert batch.status is BatchTestStatus.PENDING
        execute.assert_not_called()

    @pytest.mark.parametrize("mode", ["controller", "legacy"])
    def test_variant_config_isolated_from_workflow_mutation(self, mode):
        """Workflow mutation cannot corrupt the combination used by later cases."""
        from types import SimpleNamespace

        from wtb.domain.models.workflow import ExecutionStatus

        combo = VariantCombination(
            name="isolated-config",
            variants={"node": {"choice": "original"}},
        )
        workflow = SimpleNamespace(id="wf-isolated-config")
        uow = MagicMock()
        uow.__enter__.return_value = uow
        uow.workflows.exists.return_value = True
        uow.workflows.get.return_value = workflow
        controller = MagicMock()

        def create_execution(*, workflow, initial_state, execution_id=None):
            initial_state["_variant_config"]["node"]["choice"] = "mutated"
            return SimpleNamespace(id=execution_id)

        controller.create_execution.side_effect = create_execution
        controller.run.return_value = SimpleNamespace(
            id="requested-exec",
            status=ExecutionStatus.COMPLETED,
            error_message=None,
            checkpoint_id=None,
            state=SimpleNamespace(workflow_variables={}, execution_path=[]),
        )

        if mode == "controller":
            managed = SimpleNamespace(controller=controller, uow=uow)

            class Context:
                def __enter__(self):
                    return managed

                def __exit__(self, *args):
                    return False

            runner = ThreadPoolBatchTestRunner(
                controller_factory=lambda: Context(),
            )
            result = runner._execute_with_controller_factory(
                workflow.id,
                combo,
                {},
                None,
                time.time(),
                workflow,
                "requested-exec",
            )
        else:
            runner = ThreadPoolBatchTestRunner(
                uow_factory=lambda: uow,
                state_adapter_factory=MagicMock,
            )
            with patch(
                "wtb.application.services.execution_controller.ExecutionController",
                return_value=controller,
            ):
                result = runner._execute_with_legacy_factories(
                    workflow.id,
                    combo,
                    {},
                    time.time(),
                    "requested-exec",
                    None,
                    workflow,
                )

        assert result.success is True
        assert result.execution_id == "requested-exec"
        assert controller.create_execution.call_args.kwargs["execution_id"] == "requested-exec"
        assert combo.variants == {"node": {"choice": "original"}}

    def test_execute_variant_failure_preserves_requested_execution_id(self):
        """An outer worker failure must not return an unrelated execution ID."""
        combo = VariantCombination(name="failed-variant", variants={})
        workflow = MagicMock(id="wf-stable-id")
        runner = ThreadPoolBatchTestRunner(controller_factory=MagicMock())

        with patch.object(
            runner,
            "_execute_with_controller_factory",
            side_effect=RuntimeError("worker failed"),
        ) as execute:
            result = runner._execute_variant(
                workflow.id,
                combo,
                {},
                batch_workflow=workflow,
                execution_id="requested-exec",
            )

        assert result.success is False
        assert result.execution_id == "requested-exec"
        assert execute.call_args.args[-1] == "requested-exec"

    @pytest.mark.parametrize(
        "timeout_value",
        [None, -1.0, float("nan"), float("inf")],
    )
    def test_invalid_execution_timeout_fails_before_batch_start(
        self,
        timeout_value,
    ):
        """Invalid soft deadlines cannot strand a started batch or worker."""
        runner = ThreadPoolBatchTestRunner(
            max_workers=1,
            execution_timeout_seconds=timeout_value,
        )
        batch = BatchTest(
            id="batch-invalid-timeout",
            workflow_id="wf-invalid-timeout",
            variant_combinations=[VariantCombination(name="A", variants={})],
        )

        try:
            with patch.object(runner, "_execute_variant") as execute:
                with pytest.raises(
                    BatchRunnerError,
                    match="execution_timeout_seconds",
                ):
                    runner.run_batch_test(batch)
        finally:
            runner.shutdown()

        assert batch.status is BatchTestStatus.PENDING
        execute.assert_not_called()

    @pytest.mark.parametrize("max_workers", [0, -1, True, 1.5, None])
    def test_invalid_max_workers_fails_before_batch_start(self, max_workers):
        """Worker-pool configuration is validated before lifecycle mutation."""
        runner = ThreadPoolBatchTestRunner(
            max_workers=max_workers,
            execution_timeout_seconds=1.0,
        )
        batch = BatchTest(
            id="batch-invalid-workers",
            workflow_id="wf-invalid-workers",
            variant_combinations=[VariantCombination(name="A", variants={})],
        )

        try:
            with patch.object(runner, "_execute_variant") as execute:
                with pytest.raises(BatchRunnerError, match="max_workers"):
                    runner.run_batch_test(batch)
        finally:
            runner.shutdown()

        assert batch.status is BatchTestStatus.PENDING
        execute.assert_not_called()

    def test_duplicate_combination_names_fail_before_batch_start(self):
        """Names are result identities and therefore must be unique."""
        runner = ThreadPoolBatchTestRunner(max_workers=1)
        batch = BatchTest(
            id="batch-duplicate-names",
            workflow_id="wf-duplicate-names",
            variant_combinations=[
                VariantCombination(name="duplicate", variants={}),
                VariantCombination(name="duplicate", variants={}),
            ],
        )

        try:
            with patch.object(runner, "_execute_variant") as execute:
                with pytest.raises(BatchRunnerError, match="Duplicate combination name"):
                    runner.run_batch_test(batch)
        finally:
            runner.shutdown()

        assert batch.status is BatchTestStatus.PENDING
        execute.assert_not_called()

    def test_executor_constructor_failure_keeps_batch_pending(self):
        """Pool allocation must succeed before the batch lifecycle starts."""
        runner = ThreadPoolBatchTestRunner(max_workers=1)
        batch = BatchTest(
            id="batch-executor-constructor-failure",
            workflow_id="wf-executor-constructor-failure",
            variant_combinations=[VariantCombination(name="A", variants={})],
        )

        with patch(
            "wtb.application.services.batch_test_runner.ThreadPoolExecutor",
            side_effect=RuntimeError("executor allocation failed"),
        ):
            with pytest.raises(RuntimeError, match="executor allocation failed"):
                runner.run_batch_test(batch)

        assert batch.status is BatchTestStatus.PENDING
        assert runner.get_status(batch.id) is BatchRunnerStatus.IDLE

    @pytest.mark.parametrize(
        "invalid_metric",
        [True, "0.9", float("nan"), float("inf")],
    )
    def test_invalid_execution_metric_becomes_failed_result(
        self,
        invalid_metric,
    ):
        """Invalid worker metrics fail that cell without corrupting the batch."""
        from types import SimpleNamespace

        from wtb.domain.models.workflow import ExecutionStatus

        workflow = SimpleNamespace(id="wf-invalid-metric")
        controller = MagicMock()
        controller.create_execution.return_value = SimpleNamespace(id="exec-metric")
        controller.run.return_value = SimpleNamespace(
            id="exec-metric",
            status=ExecutionStatus.COMPLETED,
            error_message=None,
            checkpoint_id=None,
            state=SimpleNamespace(
                workflow_variables={"_metrics": {"score": invalid_metric}},
                execution_path=[],
            ),
        )
        managed = SimpleNamespace(controller=controller, uow=MagicMock())

        class Context:
            def __enter__(self):
                return managed

            def __exit__(self, *args):
                return False

        runner = ThreadPoolBatchTestRunner(controller_factory=lambda: Context())
        result = runner._execute_variant(
            workflow.id,
            VariantCombination(name="invalid-metric", variants={}),
            {},
            batch_workflow=workflow,
        )

        assert result.success is False
        assert result.metrics == {}
        assert "metric" in (result.error_message or "").lower()

    def test_concurrent_runs_share_one_executor_and_shutdown_closes_it(self):
        """Lazy executor creation is atomic across concurrent batch calls."""
        from concurrent.futures import Future

        first_factory_entered = threading.Event()
        release_first_factory = threading.Event()
        created_executors = []

        class FakeExecutor:
            def __init__(self):
                self.shutdown_called = False

            def submit(self, fn, *args):
                future = Future()
                try:
                    future.set_result(fn(*args))
                except BaseException as error:
                    future.set_exception(error)
                return future

            def shutdown(self, wait=True):
                self.shutdown_called = True

        def create_executor(*args, **kwargs):
            executor = FakeExecutor()
            created_executors.append(executor)
            if len(created_executors) == 1:
                first_factory_entered.set()
                assert release_first_factory.wait(timeout=2.0)
            return executor

        runner = ThreadPoolBatchTestRunner(max_workers=1)
        runner._execute_variant = lambda workflow_id, combo, *args, **kwargs: (
            BatchTestResult(combo.name, combo.name, True)
        )
        batches = [
            BatchTest(
                id=f"concurrent-{index}",
                workflow_id="wf-concurrent",
                variant_combinations=[
                    VariantCombination(name=f"variant-{index}", variants={})
                ],
            )
            for index in range(2)
        ]
        errors = []

        def run_batch(batch):
            try:
                runner.run_batch_test(batch)
            except BaseException as error:
                errors.append(error)

        with patch(
            "wtb.application.services.batch_test_runner.ThreadPoolExecutor",
            side_effect=create_executor,
        ):
            first = threading.Thread(target=run_batch, args=(batches[0],))
            second = threading.Thread(target=run_batch, args=(batches[1],))
            first.start()
            assert first_factory_entered.wait(timeout=2.0)
            second.start()
            time.sleep(0.05)
            release_first_factory.set()
            first.join(timeout=2.0)
            second.join(timeout=2.0)
            runner.shutdown()

        assert errors == []
        assert not first.is_alive()
        assert not second.is_alive()
        assert len(created_executors) == 1
        assert created_executors[0].shutdown_called is True
        assert all(batch.status is BatchTestStatus.COMPLETED for batch in batches)

    def test_partial_submission_failure_drains_started_worker_before_return(self):
        """A failed submit cannot return while an accepted worker still runs."""
        from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor

        worker_started = threading.Event()
        release_worker = threading.Event()
        worker_finished = threading.Event()
        run_returned = threading.Event()
        errors = []

        class FailingSubmitExecutor:
            def __init__(self):
                self._inner = RealThreadPoolExecutor(max_workers=1)
                self._submit_count = 0

            def submit(self, fn, *args):
                self._submit_count += 1
                if self._submit_count == 2:
                    raise RuntimeError("submit rejected")
                return self._inner.submit(fn, *args)

            def shutdown(self, wait=True):
                self._inner.shutdown(wait=wait)

        fake_executor = FailingSubmitExecutor()
        runner = ThreadPoolBatchTestRunner(max_workers=2)
        batch = BatchTest(
            id="batch-partial-submit",
            workflow_id="wf-partial-submit",
            variant_combinations=[
                VariantCombination(name="A", variants={}),
                VariantCombination(name="B", variants={}),
            ],
        )

        def controlled_worker(workflow_id, combo, *args, **kwargs):
            worker_started.set()
            assert release_worker.wait(timeout=2.0)
            worker_finished.set()
            return BatchTestResult(combo.name, "exec-a", True)

        runner._execute_variant = controlled_worker

        def run_batch():
            try:
                runner.run_batch_test(batch)
            except BaseException as error:
                errors.append(error)
            finally:
                run_returned.set()

        with patch(
            "wtb.application.services.batch_test_runner.ThreadPoolExecutor",
            return_value=fake_executor,
        ):
            thread = threading.Thread(target=run_batch)
            thread.start()
            assert worker_started.wait(timeout=2.0)
            returned_before_worker_finished = run_returned.wait(timeout=0.1)
            release_worker.set()
            thread.join(timeout=2.0)
            runner.shutdown()

        assert returned_before_worker_finished is False
        assert worker_finished.is_set()
        assert not thread.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], BatchRunnerError)
        assert "submit" in str(errors[0]).lower()
        assert batch.status is BatchTestStatus.FAILED
        assert runner.get_status(batch.id) is BatchRunnerStatus.IDLE

    @pytest.mark.parametrize("factory_failure", ["import", "raise", "none"])
    def test_configured_graph_factory_failure_fails_closed(
        self,
        factory_failure,
    ):
        """A configured graph cannot silently downgrade to legacy execution."""
        from types import SimpleNamespace

        from wtb.domain.models.workflow import ExecutionStatus

        workflow = SimpleNamespace(id="wf-required-graph")
        combo = VariantCombination(
            name="required-graph",
            variants={},
            graph_factory_module="workflow.graphs",
            graph_factory_name="build_graph",
        )
        uow = MagicMock()
        uow.workflows.exists.return_value = True
        controller = MagicMock()
        controller.create_execution.return_value = SimpleNamespace(id="exec-graph")
        controller.run.return_value = SimpleNamespace(
            id="exec-graph",
            status=ExecutionStatus.COMPLETED,
            error_message=None,
            checkpoint_id=None,
            state=SimpleNamespace(workflow_variables={}, execution_path=[]),
        )
        managed = SimpleNamespace(controller=controller, uow=uow)

        class Context:
            def __enter__(self):
                return managed

            def __exit__(self, *args):
                return False

        loader = MagicMock()
        if factory_failure == "import":
            loader.side_effect = ImportError("missing graph module")
        elif factory_failure == "raise":
            loader.return_value = MagicMock(
                side_effect=RuntimeError("graph construction failed")
            )
        else:
            loader.return_value = MagicMock(return_value=None)

        runner = ThreadPoolBatchTestRunner(controller_factory=lambda: Context())
        with patch(
            "wtb.application.services.graph_loader.load_graph_factory",
            loader,
        ):
            result = runner._execute_variant(
                workflow.id,
                combo,
                {},
                batch_workflow=workflow,
            )

        assert result.success is False
        assert "graph factory" in (result.error_message or "").lower()
        controller.run.assert_not_called()

    def test_variant_timeout_fails_result_and_drains_worker(self):
        """The soft deadline marks failure only after the worker safely drains."""
        runner = ThreadPoolBatchTestRunner(
            uow_factory=lambda: InMemoryUnitOfWork(),
            state_adapter_factory=lambda: InMemoryStateAdapter(),
            max_workers=1,
            execution_timeout_seconds=0.02,
        )
        batch = BatchTest(
            id="batch-timeout",
            workflow_id="wf-timeout",
            variant_combinations=[
                VariantCombination(name="Slow variant", variants={}),
            ],
        )
        worker_finished = threading.Event()

        def slow_variant(*args, **kwargs):
            time.sleep(0.10)
            worker_finished.set()
            return BatchTestResult(
                combination_name="Slow variant",
                execution_id="exec-slow",
                success=True,
            )

        try:
            with patch.object(runner, "_execute_variant", side_effect=slow_variant):
                result = runner.run_batch_test(batch)
        finally:
            runner.shutdown()

        assert worker_finished.is_set()
        assert result.status is BatchTestStatus.FAILED
        assert len(result.results) == 1
        assert result.results[0].success is False
        assert result.results[0].execution_id == "exec-slow"
        assert "timed out" in (result.results[0].error_message or "").lower()

    def test_worker_future_exception_preserves_submitted_execution_id(self):
        """A failed future remains linked to its preallocated execution ID."""
        runner = ThreadPoolBatchTestRunner(max_workers=1)
        batch = BatchTest(
            id="batch-worker-error",
            workflow_id="wf-worker-error",
            variant_combinations=[
                VariantCombination(name="Broken variant", variants={}),
            ],
        )

        try:
            with (
                patch(
                    "wtb.application.services.batch_test_runner.uuid.uuid4",
                    return_value="submitted-exec",
                ),
                patch.object(
                    runner,
                    "_execute_variant_task",
                    side_effect=RuntimeError("future failed"),
                ),
            ):
                result = runner.run_batch_test(batch)
        finally:
            runner.shutdown()

        assert result.status is BatchTestStatus.FAILED
        assert len(result.results) == 1
        assert result.results[0].success is False
        assert result.results[0].execution_id == "submitted-exec"
        assert "future failed" in (result.results[0].error_message or "")

    def test_cancel_waits_for_all_started_workers_before_returning(self):
        """CANCELLED is terminal: no started worker remains after return."""
        runner = ThreadPoolBatchTestRunner(
            uow_factory=lambda: InMemoryUnitOfWork(),
            state_adapter_factory=lambda: InMemoryStateAdapter(),
            max_workers=2,
            execution_timeout_seconds=5.0,
        )
        batch = BatchTest(
            id="batch-cancel-drain",
            workflow_id="wf-cancel-drain",
            variant_combinations=[
                VariantCombination(name="First", variants={}),
                VariantCombination(name="Second", variants={}),
            ],
        )
        both_started = threading.Barrier(3)
        release_first = threading.Event()
        release_second = threading.Event()
        second_side_effect_done = threading.Event()
        run_returned = threading.Event()
        result_holder = []

        def controlled_variant(workflow_id, combo, *args, **kwargs):
            both_started.wait(timeout=2.0)
            if combo.name == "First":
                release_first.wait(timeout=2.0)
            else:
                release_second.wait(timeout=2.0)
                second_side_effect_done.set()
            return BatchTestResult(
                combination_name=combo.name,
                execution_id=f"exec-{combo.name.lower()}",
                success=True,
            )

        def run_batch():
            try:
                result_holder.append(runner.run_batch_test(batch))
            finally:
                run_returned.set()

        thread = threading.Thread(target=run_batch)
        returned_before_second_finished = False
        try:
            with patch.object(
                runner,
                "_execute_variant",
                side_effect=controlled_variant,
            ):
                thread.start()
                both_started.wait(timeout=2.0)
                assert runner.cancel(batch.id) is True
                release_first.set()
                returned_before_second_finished = run_returned.wait(timeout=0.15)
                release_second.set()
                thread.join(timeout=2.0)
        finally:
            release_first.set()
            release_second.set()
            thread.join(timeout=2.0)
            runner.shutdown()

        assert returned_before_second_finished is False
        assert thread.is_alive() is False
        assert second_side_effect_done.is_set()
        assert run_returned.is_set()
        assert result_holder[0].status is BatchTestStatus.CANCELLED

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
        from wtb.infrastructure.environment.providers import (
            InProcessEnvironmentProvider,
        )
        
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
        from wtb.application.services.ray_batch_runner import (
            RayBatchTestRunner,
            RayConfig,
        )
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/agentgit.db",
            wtb_db_url="sqlite:///data/wtb.db",
        )
        
        assert runner is not None
        runner.shutdown()

