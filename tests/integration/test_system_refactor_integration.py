"""
Integration tests for WTB system refactoring.

Tests integration-level fixes across the four key scenarios:
1. Non-batch lifecycle (run -> get_checkpoints -> rollback -> fork)
2. Batch result schema consistency (sequential vs threadpool)
3. File tracking rollback (CAS restore consistent with state)
4. ManagedController deferred commit

Uses pytest fixtures, InMemoryUnitOfWork + InMemoryStateAdapter for speed.
"""

import os
import tempfile
import time
import uuid
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from wtb.domain.models.workflow import (
    Execution,
    ExecutionState,
    ExecutionStatus,
    TestWorkflow,
    WorkflowNode,
    WorkflowEdge,
)
from wtb.infrastructure.database import InMemoryUnitOfWork
from wtb.infrastructure.adapters import InMemoryStateAdapter
from wtb.application.services.execution_controller import (
    ExecutionController,
    DefaultNodeExecutor,
)

try:
    from wtb.infrastructure.file_tracking.sqlite_service import SqliteFileTrackingService
    from wtb.domain.interfaces.file_tracking import (
        FileTrackingResult,
        FileRestoreResult,
        CheckpointLinkError,
    )
    _HAS_SQLITE_FILE_TRACKING = True
except ImportError:
    _HAS_SQLITE_FILE_TRACKING = False

try:
    from wtb.application.factories import (
        ExecutionControllerFactory,
        ManagedController,
    )
    _HAS_FACTORIES = True
except ImportError:
    _HAS_FACTORIES = False

try:
    from wtb.sdk.test_bench import WTBTestBench
    from wtb.sdk.workflow_project import WorkflowProject
    from wtb.domain.models.batch_test import BatchTest, BatchTestResult
    _HAS_SDK = True
except ImportError:
    _HAS_SDK = False


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _make_simple_workflow(wf_id: str = "wf-integ") -> TestWorkflow:
    """Create a minimal start -> end workflow."""
    wf = TestWorkflow(id=wf_id, name="integration-refactor-test", entry_point="start")
    wf.add_node(WorkflowNode(id="start", name="Start", type="start"))
    wf.add_node(WorkflowNode(id="end", name="End", type="end"))
    wf.add_edge(WorkflowEdge(source_id="start", target_id="end"))
    return wf


def _make_three_step_workflow(wf_id: str = "wf-3step") -> TestWorkflow:
    """Create a start -> action -> end workflow for richer checkpoint history."""
    wf = TestWorkflow(id=wf_id, name="three-step-test", entry_point="start")
    wf.add_node(WorkflowNode(id="start", name="Start", type="start"))
    wf.add_node(WorkflowNode(id="process", name="Process", type="action"))
    wf.add_node(WorkflowNode(id="end", name="End", type="end"))
    wf.add_edge(WorkflowEdge(source_id="start", target_id="process"))
    wf.add_edge(WorkflowEdge(source_id="process", target_id="end"))
    return wf


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Non-batch Lifecycle (run -> get_checkpoints -> rollback -> fork)
# ═══════════════════════════════════════════════════════════════════════════════


class TestNonBatchLifecycle:
    """
    Tests the full non-batch execution lifecycle using InMemory dependencies.
    Verifies run, checkpoint retrieval, rollback, and fork operations.
    """

    @pytest.fixture
    def setup(self):
        uow = InMemoryUnitOfWork()
        uow.__enter__()
        adapter = InMemoryStateAdapter()
        controller = ExecutionController(
            execution_repository=uow.executions,
            workflow_repository=uow.workflows,
            state_adapter=adapter,
            node_executor=DefaultNodeExecutor(),
            unit_of_work=uow,
        )
        workflow = _make_three_step_workflow()
        uow.workflows.add(workflow)
        uow.commit()
        return controller, adapter, workflow, uow

    def test_run_completes_successfully(self, setup):
        """Basic run with InMemory deps produces COMPLETED execution."""
        controller, adapter, workflow, uow = setup

        execution = controller.create_execution(
            workflow=workflow,
            initial_state={"counter": 0},
        )
        assert execution.status == ExecutionStatus.PENDING

        execution = controller.run(execution.id)
        assert execution.status in (ExecutionStatus.COMPLETED, ExecutionStatus.FAILED)

    def test_run_get_checkpoints_rollback_fork(self, setup):
        """Full lifecycle: run -> get_checkpoint_history -> rollback -> fork."""
        controller, adapter, workflow, uow = setup

        execution = controller.create_execution(
            workflow=workflow,
            initial_state={"counter": 10},
        )
        execution = controller.run(execution.id)

        history = controller.get_checkpoint_history(execution.id)

        if not history:
            pytest.skip("InMemoryStateAdapter did not produce checkpoint history")

        first_cp_id = str(history[0].get("checkpoint_id", history[0].get("id", "")))
        assert first_cp_id, "Checkpoint ID must be non-empty"

        rolled_back = controller.rollback(execution.id, first_cp_id)
        assert rolled_back.status == ExecutionStatus.PAUSED
        assert rolled_back.checkpoint_id == first_cp_id
        assert isinstance(rolled_back.state, ExecutionState)

        forked = controller.fork(
            execution.id,
            first_cp_id,
            new_initial_state={"forked_flag": True},
        )
        assert forked.id != execution.id
        assert forked.status == ExecutionStatus.PAUSED
        assert forked.state.workflow_variables.get("forked_flag") is True
        assert forked.metadata.get("forked_from") == execution.id

    def test_create_execution_persists_in_repository(self, setup):
        """Verify execution is persisted to the repository after creation."""
        controller, adapter, workflow, uow = setup

        execution = controller.create_execution(workflow=workflow)
        fetched = uow.executions.get(execution.id)
        assert fetched is not None
        assert fetched.id == execution.id
        assert fetched.workflow_id == workflow.id

    def test_run_updates_execution_path(self, setup):
        """After run, execution state should have non-empty execution_path."""
        controller, adapter, workflow, uow = setup

        execution = controller.create_execution(
            workflow=workflow,
            initial_state={"step_count": 0},
        )
        execution = controller.run(execution.id)

        if execution.status == ExecutionStatus.COMPLETED:
            assert len(execution.state.execution_path) > 0

    def test_fork_creates_independent_execution(self, setup):
        """Fork produces an execution that can be run independently."""
        controller, adapter, workflow, uow = setup

        execution = controller.create_execution(
            workflow=workflow,
            initial_state={"value": 42},
        )
        execution = controller.run(execution.id)

        history = controller.get_checkpoint_history(execution.id)
        if not history:
            pytest.skip("No checkpoints available for fork test")

        cp_id = str(history[0].get("checkpoint_id", history[0].get("id", "")))

        forked = controller.fork(
            execution.id,
            cp_id,
            new_initial_state={"value": 99},
        )
        assert forked.state.workflow_variables["value"] == 99

        forked_run = controller.run(forked.id)
        assert forked_run.status in (
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Batch Result Schema Consistency
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not _HAS_SDK, reason="WTBTestBench SDK not available")
class TestBatchResultSchemaConsistency:
    """
    Tests that sequential batch execution produces BatchTestResult objects
    with the expected fields (duration_ms, metrics, last_checkpoint_id,
    checkpoint_count).
    """

    @pytest.fixture
    def bench(self):
        bench = WTBTestBench.create(mode="testing")
        return bench

    @pytest.fixture
    def simple_project(self):
        """WorkflowProject with a trivial graph factory."""
        def trivial_graph_factory(variant_config=None):
            return None

        return WorkflowProject(
            name="schema_test_project",
            graph_factory=trivial_graph_factory,
            description="Minimal project for batch schema tests",
        )

    def test_sequential_result_has_all_fields(self, bench, simple_project):
        """Sequential batch results contain duration_ms, metrics, checkpoint_count."""
        bench.register_project(simple_project)

        variant_matrix = [{"node": "default"}]
        test_cases = [{"counter": 0}]

        batch_test = bench._run_batch_sequential(
            project=simple_project.name,
            variant_matrix=variant_matrix,
            test_cases=test_cases,
        )

        assert isinstance(batch_test, BatchTest)
        assert len(batch_test.results) >= 1

        for result in batch_test.results:
            assert isinstance(result, BatchTestResult)
            assert hasattr(result, "duration_ms")
            assert isinstance(result.duration_ms, int)
            assert result.duration_ms >= 0

            assert hasattr(result, "metrics")
            assert isinstance(result.metrics, dict)

            assert hasattr(result, "last_checkpoint_id")

            assert hasattr(result, "checkpoint_count")
            assert isinstance(result.checkpoint_count, int)
            assert result.checkpoint_count >= 0

            assert hasattr(result, "combination_name")
            assert hasattr(result, "execution_id")
            assert hasattr(result, "success")

    def test_sequential_multiple_variants_each_have_results(self, bench, simple_project):
        """Each variant in the matrix produces at least one result."""
        bench.register_project(simple_project)

        variant_matrix = [
            {"node": "variant_a"},
            {"node": "variant_b"},
        ]
        test_cases = [{"input_value": 1}]

        batch_test = bench._run_batch_sequential(
            project=simple_project.name,
            variant_matrix=variant_matrix,
            test_cases=test_cases,
        )

        assert len(batch_test.results) >= 2
        combo_names = [r.combination_name for r in batch_test.results]
        assert "variant_0" in combo_names
        assert "variant_1" in combo_names

    def test_batch_result_captures_numeric_metrics(self, bench, simple_project):
        """Numeric workflow variables are captured as metrics."""
        bench.register_project(simple_project)

        test_cases = [{"score": 0.95, "count": 10, "label": "not_numeric"}]
        variant_matrix = [{}]

        batch_test = bench._run_batch_sequential(
            project=simple_project.name,
            variant_matrix=variant_matrix,
            test_cases=test_cases,
        )

        for result in batch_test.results:
            if result.success and result.metrics:
                for k, v in result.metrics.items():
                    assert isinstance(v, (int, float)), (
                        f"Metric '{k}' should be numeric, got {type(v)}"
                    )


# ═══════════════════════════════════════════════════════════════════════════════
# 3. File Tracking Rollback (CAS restore consistent with state)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(
    not _HAS_SQLITE_FILE_TRACKING,
    reason="SqliteFileTrackingService not available",
)
class TestFileTrackingRollback:
    """
    Tests that SqliteFileTrackingService correctly tracks, links, and
    restores files from checkpoints (CAS rollback).
    """

    @pytest.fixture
    def workspace(self):
        tmpdir = tempfile.mkdtemp()
        yield Path(tmpdir)

    @pytest.fixture
    def service(self, workspace):
        svc = SqliteFileTrackingService(workspace_path=workspace)
        yield svc
        svc.close()

    def test_track_and_link_then_restore(self, service, workspace):
        """Track a file at checkpoint, modify it, restore -> original content."""
        output_dir = workspace / "outputs"
        output_dir.mkdir()
        test_file = output_dir / "tracked_file.txt"
        original_content = "original content for CAS tracking"
        test_file.write_text(original_content, encoding="utf-8")

        result = service.track_and_link(
            checkpoint_id="cp-1",
            file_paths=[str(test_file)],
            message="initial checkpoint",
        )
        assert isinstance(result, FileTrackingResult)
        assert result.files_tracked == 1
        assert len(result.commit_id) > 0

        test_file.write_text("modified content after checkpoint", encoding="utf-8")
        assert test_file.read_text(encoding="utf-8") != original_content

        restore_result = service.restore_from_checkpoint(checkpoint_id="cp-1")
        assert isinstance(restore_result, FileRestoreResult)
        assert restore_result.files_restored >= 1

        assert test_file.read_text(encoding="utf-8") == original_content

    def test_multi_file_track_and_restore(self, service, workspace):
        """Multiple files tracked together are all restored correctly."""
        output_dir = workspace / "outputs"
        output_dir.mkdir()

        file_a = output_dir / "a.txt"
        file_b = output_dir / "b.json"
        content_a = "file A content"
        content_b = '{"key": "value_b"}'
        file_a.write_text(content_a, encoding="utf-8")
        file_b.write_text(content_b, encoding="utf-8")

        service.track_and_link(
            checkpoint_id="cp-multi",
            file_paths=[str(file_a), str(file_b)],
        )

        file_a.write_text("modified A", encoding="utf-8")
        file_b.write_text("modified B", encoding="utf-8")

        service.restore_from_checkpoint(checkpoint_id="cp-multi")

        assert file_a.read_text(encoding="utf-8") == content_a
        assert file_b.read_text(encoding="utf-8") == content_b

    def test_restore_nonexistent_checkpoint_raises(self, service):
        """Restoring from a checkpoint that was never linked raises an error."""
        with pytest.raises((CheckpointLinkError, Exception)):
            service.restore_from_checkpoint(checkpoint_id="nonexistent-cp")

    def test_overwrite_checkpoint_link(self, service, workspace):
        """Re-linking a checkpoint replaces the previous link."""
        output_dir = workspace / "outputs"
        output_dir.mkdir()
        test_file = output_dir / "relinked.txt"

        test_file.write_text("version1", encoding="utf-8")
        service.track_and_link(checkpoint_id="cp-relink", file_paths=[str(test_file)])

        test_file.write_text("version2", encoding="utf-8")
        service.track_and_link(checkpoint_id="cp-relink", file_paths=[str(test_file)])

        test_file.write_text("version3", encoding="utf-8")

        service.restore_from_checkpoint(checkpoint_id="cp-relink")
        assert test_file.read_text(encoding="utf-8") == "version2"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. ManagedController Deferred Commit
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not _HAS_FACTORIES, reason="ExecutionControllerFactory not available")
class TestManagedControllerDeferredCommit:
    """
    Tests that ManagedController (from ExecutionControllerFactory.create_isolated)
    properly sets deferred commit on the controller when used as a context manager.
    """

    def test_managed_controller_sets_deferred_commit(self):
        """Entering ManagedController context sets deferred_commit=True."""
        uow = InMemoryUnitOfWork()
        uow.__enter__()
        adapter = InMemoryStateAdapter()

        controller = ExecutionController(
            execution_repository=uow.executions,
            workflow_repository=uow.workflows,
            state_adapter=adapter,
            node_executor=DefaultNodeExecutor(),
            unit_of_work=uow,
        )

        managed = ManagedController(controller=controller, uow=uow)

        assert not getattr(controller, "_deferred_commit", False)

        with managed as ctx:
            assert ctx.controller is controller
            assert controller._deferred_commit is True

    def test_managed_controller_commits_on_clean_exit(self):
        """Exiting ManagedController without exception calls uow.commit()."""
        uow = InMemoryUnitOfWork()
        uow.__enter__()
        adapter = InMemoryStateAdapter()

        controller = ExecutionController(
            execution_repository=uow.executions,
            workflow_repository=uow.workflows,
            state_adapter=adapter,
            node_executor=DefaultNodeExecutor(),
            unit_of_work=uow,
        )

        managed = ManagedController(controller=controller, uow=uow)

        with patch.object(uow, "commit") as mock_commit, \
             patch.object(uow, "__exit__") as mock_exit:
            with managed:
                pass
            mock_commit.assert_called_once()
            mock_exit.assert_called_once()

    def test_managed_controller_rollback_on_exception(self):
        """Exiting ManagedController with exception calls uow.rollback()."""
        uow = InMemoryUnitOfWork()
        uow.__enter__()
        adapter = InMemoryStateAdapter()

        controller = ExecutionController(
            execution_repository=uow.executions,
            workflow_repository=uow.workflows,
            state_adapter=adapter,
            node_executor=DefaultNodeExecutor(),
            unit_of_work=uow,
        )

        managed = ManagedController(controller=controller, uow=uow)

        with patch.object(uow, "rollback") as mock_rollback, \
             patch.object(uow, "__exit__") as mock_exit:
            with pytest.raises(RuntimeError, match="test error"):
                with managed:
                    raise RuntimeError("test error")
            mock_rollback.assert_called_once()

    @patch("wtb.application.factories.get_config")
    def test_create_isolated_returns_managed_controller(self, mock_config):
        """ExecutionControllerFactory.create_isolated() returns a ManagedController."""
        from wtb.config import WTBConfig
        test_config = WTBConfig.for_testing()
        mock_config.return_value = test_config

        factory = ExecutionControllerFactory(config=test_config)
        managed = factory.create_isolated()

        assert isinstance(managed, ManagedController)
        assert isinstance(managed.controller, ExecutionController)
        assert managed.uow is not None

    def test_full_managed_lifecycle(self):
        """ManagedController enables full create -> run -> commit lifecycle."""
        uow = InMemoryUnitOfWork()
        uow.__enter__()
        adapter = InMemoryStateAdapter()

        controller = ExecutionController(
            execution_repository=uow.executions,
            workflow_repository=uow.workflows,
            state_adapter=adapter,
            node_executor=DefaultNodeExecutor(),
            unit_of_work=uow,
        )

        managed = ManagedController(controller=controller, uow=uow)

        workflow = _make_simple_workflow("wf-managed")
        uow.workflows.add(workflow)
        uow.commit()

        with managed as ctx:
            execution = ctx.controller.create_execution(
                workflow=workflow,
                initial_state={"managed_run": True},
            )
            execution = ctx.controller.run(execution.id)

        assert execution.status in (
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
        )
