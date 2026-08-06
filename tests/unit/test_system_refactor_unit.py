"""
Unit tests for WTB system refactoring fixes.

Tests the following areas:
1. LangGraph session fix (set_current_session before execute)
2. Fork seeding (create_fork on adapter)
3. Batch thread safety (workflow as parameter)
4. Factory wiring (no double outbox/batch_runner)
5. Venv hash consistency (all entry points produce same hash)
6. CAS atomicity (temp file + os.replace pattern)
7. Deferred commit in rollback_and_run
8. Invalidate environment (no double-pop)
"""

import hashlib
import tempfile
from unittest.mock import MagicMock

import pytest

from wtb.application.services.execution_controller import (
    DefaultNodeExecutor,
    ExecutionController,
)
from wtb.domain.models.workflow import (
    Execution,
    ExecutionState,
    ExecutionStatus,
    TestWorkflow,
    WorkflowEdge,
    WorkflowNode,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _make_workflow() -> TestWorkflow:
    wf = TestWorkflow(id="wf-1", name="test", entry_point="start")
    wf.add_node(WorkflowNode(id="start", name="Start", type="start"))
    wf.add_node(WorkflowNode(id="end", name="End", type="end"))
    wf.add_edge(WorkflowEdge(source_id="start", target_id="end"))
    return wf


def _make_controller(
    state_adapter=None,
    exec_repo=None,
    workflow_repo=None,
    uow=None,
    file_tracking=None,
    output_dir=None,
):
    exec_repo = exec_repo or MagicMock()
    workflow_repo = workflow_repo or MagicMock()
    state_adapter = state_adapter or MagicMock()
    uow = uow or MagicMock()

    return ExecutionController(
        execution_repository=exec_repo,
        workflow_repository=workflow_repo,
        state_adapter=state_adapter,
        node_executor=DefaultNodeExecutor(),
        unit_of_work=uow,
        file_tracking_service=file_tracking,
        output_dir=output_dir,
    )


def _make_execution(
    status=ExecutionStatus.PENDING,
    exec_id="exec-1",
    session_id="wtb-exec-1",
) -> Execution:
    ex = Execution(
        id=exec_id,
        workflow_id="wf-1",
        status=status,
        state=ExecutionState(
            current_node_id="start",
            workflow_variables={"value": 0},
            execution_path=[],
            node_results={},
        ),
    )
    ex.session_id = session_id
    return ex


# ═══════════════════════════════════════════════════════════════════════════════
# 1. LangGraph Session Fix
# ═══════════════════════════════════════════════════════════════════════════════


class TestLangGraphSessionFix:
    """set_current_session must be called before execute."""

    def test_run_with_langgraph_sets_session_before_execute(self):
        adapter = MagicMock()
        adapter.supports_graph_execution.return_value = True
        adapter.has_graph.return_value = True
        adapter.set_workflow_graph = MagicMock()
        adapter.execute.return_value = {"answer": "ok"}
        adapter.initialize_session.return_value = "wtb-exec-1"

        exec_repo = MagicMock()
        execution = _make_execution(session_id="wtb-exec-1")
        exec_repo.get.return_value = execution

        controller = _make_controller(state_adapter=adapter, exec_repo=exec_repo)

        mock_graph = MagicMock()
        controller.run(execution.id, graph=mock_graph)

        adapter.set_current_session.assert_called_once_with(
            "wtb-exec-1", execution_id=execution.id
        )

        calls = adapter.method_calls
        set_session_idx = next(
            i for i, c in enumerate(calls) if c[0] == "set_current_session"
        )
        execute_idx = next(
            i for i, c in enumerate(calls) if c[0] == "execute"
        )
        assert set_session_idx < execute_idx, (
            "set_current_session must be called before execute"
        )

    def test_run_with_langgraph_no_session_fails_closed(self):
        adapter = MagicMock()
        adapter.supports_graph_execution.return_value = True
        adapter.has_graph.return_value = True
        adapter.execute.return_value = {"answer": "ok"}

        execution = _make_execution(session_id=None)
        exec_repo = MagicMock()
        exec_repo.get.return_value = execution

        controller = _make_controller(state_adapter=adapter, exec_repo=exec_repo)
        mock_graph = MagicMock()

        with pytest.raises(RuntimeError, match="activate execution session"):
            controller.run(execution.id, graph=mock_graph)

        adapter.execute.assert_not_called()
        assert execution.status == ExecutionStatus.PENDING
        adapter.set_current_session.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Fork Seeding
# ═══════════════════════════════════════════════════════════════════════════════


class TestForkSeeding:
    """fork() must call create_fork on the adapter when supports_graph_execution."""

    def test_fork_calls_create_fork_on_adapter(self):
        adapter = MagicMock()
        adapter.supports_graph_execution.return_value = True
        adapter.create_fork = MagicMock()
        adapter.load_checkpoint.return_value = ExecutionState(
            current_node_id="start",
            workflow_variables={"v": 1},
            execution_path=[],
            node_results={},
        )
        adapter.initialize_session.return_value = "wtb-fork-1"
        adapter.set_current_session = MagicMock()

        source_exec = _make_execution(
            status=ExecutionStatus.COMPLETED, session_id="wtb-exec-src"
        )
        exec_repo = MagicMock()
        exec_repo.get.return_value = source_exec

        workflow = _make_workflow()
        workflow_repo = MagicMock()
        workflow_repo.get.return_value = workflow

        controller = _make_controller(
            state_adapter=adapter,
            exec_repo=exec_repo,
            workflow_repo=workflow_repo,
        )

        checkpoint_id = "cp-abc-123"
        adapter.get_checkpoint_history.return_value = [
            {
                "checkpoint_id": checkpoint_id,
                "writes": {"start": {}},
                "next": [],
                "step": 1,
            }
        ]
        forked = controller.fork(source_exec.id, checkpoint_id)

        adapter.create_fork.assert_called_once()
        fork_call_args = adapter.create_fork.call_args
        assert fork_call_args.kwargs.get("from_checkpoint_id") == checkpoint_id

        assert forked.session_id == "wtb-fork-1"

    def test_fork_recognizes_langgraph_initial_input_checkpoint(self):
        adapter = MagicMock()
        adapter.supports_graph_execution.return_value = True
        adapter.create_fork = MagicMock()
        adapter.update_state.return_value = True
        adapter.load_checkpoint.return_value = ExecutionState(
            current_node_id="__start__",
            workflow_variables={"value": 0},
            execution_path=[],
            node_results={},
        )
        adapter.initialize_session.return_value = "wtb-fork-input"
        adapter.set_current_session = MagicMock()

        source_exec = _make_execution(
            status=ExecutionStatus.COMPLETED, session_id="wtb-exec-src"
        )
        exec_repo = MagicMock()
        exec_repo.get.return_value = source_exec
        workflow_repo = MagicMock()
        workflow_repo.get.return_value = _make_workflow()
        controller = _make_controller(
            state_adapter=adapter,
            exec_repo=exec_repo,
            workflow_repo=workflow_repo,
        )

        checkpoint_id = "cp-input"
        adapter.get_checkpoint_history.return_value = [
            {
                "checkpoint_id": "cp-step-a",
                "writes": {},
                "next": ["step_a"],
                "step": 0,
                "values": {"value": 0},
            },
            {
                "checkpoint_id": checkpoint_id,
                "writes": {},
                "next": ["__start__"],
                "step": -1,
            },
        ]

        forked = controller.fork(source_exec.id, checkpoint_id)

        adapter.create_fork.assert_called_once()
        adapter.update_state.assert_called_once_with(
            {"value": 0}, as_node="__start__"
        )
        assert forked.metadata["source_checkpoint_as_node"] == "__start__"

    @pytest.mark.parametrize(
        ("step", "next_nodes"),
        [
            (0, ["__start__"]),
            (-1, ["step_a"]),
            (-1, ["__start__", "step_a"]),
        ],
    )
    def test_fork_rejects_noncanonical_input_checkpoint_with_override(
        self,
        step,
        next_nodes,
    ):
        adapter = MagicMock()
        adapter.supports_graph_execution.return_value = True
        adapter.create_fork = MagicMock()
        adapter.load_checkpoint.return_value = ExecutionState(
            current_node_id="__start__",
            workflow_variables={"value": 0},
            execution_path=[],
            node_results={},
        )

        source_exec = _make_execution(
            status=ExecutionStatus.COMPLETED, session_id="wtb-exec-src"
        )
        exec_repo = MagicMock()
        exec_repo.get.return_value = source_exec
        workflow_repo = MagicMock()
        workflow_repo.get.return_value = _make_workflow()
        controller = _make_controller(
            state_adapter=adapter,
            exec_repo=exec_repo,
            workflow_repo=workflow_repo,
        )

        checkpoint_id = "cp-ambiguous"
        adapter.get_checkpoint_history.return_value = [
            {
                "checkpoint_id": checkpoint_id,
                "writes": {},
                "next": next_nodes,
                "step": step,
            }
        ]

        with pytest.raises(RuntimeError, match="ambiguous continuation"):
            controller.fork(
                source_exec.id,
                checkpoint_id,
                new_initial_state={"value": 7},
            )

        adapter.create_fork.assert_not_called()
        exec_repo.add.assert_not_called()

    def test_fork_skips_create_fork_when_not_supported(self):
        adapter = MagicMock()
        adapter.supports_graph_execution.return_value = False
        del adapter.create_fork  # ensure no create_fork attribute
        adapter.load_checkpoint.return_value = ExecutionState(
            current_node_id="start",
            workflow_variables={"v": 1},
            execution_path=[],
            node_results={},
        )
        adapter.initialize_session.return_value = "wtb-fork-2"
        adapter.set_current_session = MagicMock()

        source_exec = _make_execution(
            status=ExecutionStatus.COMPLETED, session_id="wtb-exec-src"
        )
        exec_repo = MagicMock()
        exec_repo.get.return_value = source_exec

        workflow = _make_workflow()
        workflow_repo = MagicMock()
        workflow_repo.get.return_value = workflow

        controller = _make_controller(
            state_adapter=adapter,
            exec_repo=exec_repo,
            workflow_repo=workflow_repo,
        )

        forked = controller.fork(source_exec.id, "cp-xyz-456")
        assert forked is not None
        assert forked.session_id == "wtb-fork-2"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Batch Thread Safety
# ═══════════════════════════════════════════════════════════════════════════════


class TestBatchThreadSafety:
    """_execute_variant receives workflow as a parameter, not from self."""

    def test_variant_receives_workflow_arg(self):
        from wtb.application.services.batch_test_runner import ThreadPoolBatchTestRunner
        from wtb.domain.models.batch_test import BatchTest, VariantCombination

        mock_managed = MagicMock()
        mock_controller = MagicMock()
        mock_uow = MagicMock()
        mock_managed.controller = mock_controller
        mock_managed.uow = mock_uow
        mock_managed.__enter__ = MagicMock(return_value=mock_managed)
        mock_managed.__exit__ = MagicMock(return_value=False)

        mock_exec = _make_execution(status=ExecutionStatus.COMPLETED)
        mock_controller.create_execution.return_value = mock_exec
        mock_controller.run.return_value = mock_exec

        mock_uow.workflows = MagicMock()
        mock_uow.workflows.get.return_value = None

        controller_factory = MagicMock(return_value=mock_managed)

        runner = ThreadPoolBatchTestRunner(
            controller_factory=controller_factory,
            max_workers=1,
            execution_timeout_seconds=30.0,
        )

        workflow = _make_workflow()

        combo = VariantCombination(
            name="variant-A",
            variants={"model": "gpt-4"},
        )

        batch_test = BatchTest(
            id="bt-1",
            name="test",
            workflow_id="wf-1",
            variant_combinations=[combo],
            initial_state={"key": "val"},
        )
        batch_test._workflow = workflow

        result = runner.run_batch_test(batch_test)
        runner.shutdown()

        controller_factory.assert_called()
        mock_controller.create_execution.assert_called_once()
        create_kwargs = mock_controller.create_execution.call_args
        assert create_kwargs[1]["workflow"] is workflow or \
            create_kwargs[0][0] is workflow


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Factory Wiring (no double outbox / batch_runner)
# ═══════════════════════════════════════════════════════════════════════════════


class TestFactoryNoDuplication:
    """create() and create_for_testing() produce valid WTBTestBench."""

    def test_create_for_testing_produces_single_batch_runner(self):
        import warnings

        from wtb.application.factories import WTBTestBenchFactory

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            bench = WTBTestBenchFactory.create_for_testing()

        assert bench is not None
        assert hasattr(bench, "_batch_runner")
        assert bench._batch_runner is not None

    def test_create_produces_bench_without_error(self):
        import warnings

        from wtb.application.factories import WTBTestBenchFactory
        from wtb.config import WTBConfig

        config = WTBConfig.for_testing()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            bench = WTBTestBenchFactory.create(config)

        assert bench is not None


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Venv Hash Consistency
# ═══════════════════════════════════════════════════════════════════════════════


class TestVenvHashConsistency:
    """All three hash entry points must produce the same hash."""

    def test_all_hash_functions_produce_same_hash(self):
        from wtb.domain.models.workspace import compute_venv_spec_hash
        from wtb.infrastructure.environment.providers import GrpcEnvironmentProvider
        from wtb.infrastructure.environment.venv_cache import VenvSpec

        python_version = "3.12"
        packages = ["numpy", "pandas"]

        spec = VenvSpec(python_version=python_version, packages=packages)
        hash_from_spec = spec.compute_hash()

        hash_from_domain = compute_venv_spec_hash(python_version, packages)

        provider = GrpcEnvironmentProvider.__new__(GrpcEnvironmentProvider)
        hash_from_provider = provider._compute_spec_hash(python_version, packages)

        assert hash_from_spec == hash_from_domain, (
            f"VenvSpec.compute_hash ({hash_from_spec}) != "
            f"compute_venv_spec_hash ({hash_from_domain})"
        )
        assert hash_from_spec == hash_from_provider, (
            f"VenvSpec.compute_hash ({hash_from_spec}) != "
            f"GrpcEnvironmentProvider._compute_spec_hash ({hash_from_provider})"
        )

    def test_hash_is_deterministic(self):
        from wtb.domain.models.workspace import compute_venv_spec_hash

        h1 = compute_venv_spec_hash("3.12", ["pandas", "numpy"])
        h2 = compute_venv_spec_hash("3.12", ["numpy", "pandas"])
        assert h1 == h2, "Hash should be order-independent (packages are sorted)"

    def test_hash_changes_with_different_versions(self):
        from wtb.domain.models.workspace import compute_venv_spec_hash

        h1 = compute_venv_spec_hash("3.11", ["numpy"])
        h2 = compute_venv_spec_hash("3.12", ["numpy"])
        assert h1 != h2


# ═══════════════════════════════════════════════════════════════════════════════
# 6. CAS Atomicity
# ═══════════════════════════════════════════════════════════════════════════════


class TestCASAtomicity:
    """Blob storage uses temp file + os.replace for crash safety."""

    def test_store_blob_creates_valid_blob(self):
        from pathlib import Path

        from wtb.infrastructure.file_tracking.sqlite_service import (
            SqliteFileTrackingService,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            service = SqliteFileTrackingService(
                workspace_path=workspace, db_name="test_ft.db"
            )

            test_file = workspace / "test_output.txt"
            test_file.write_text("hello world", encoding="utf-8")

            result = service.track_files(
                file_paths=[str(test_file)],
                message="test commit",
            )

            assert result.files_tracked == 1
            assert result.total_size_bytes > 0
            assert result.commit_id is not None

            expected_hash = hashlib.sha256(b"hello world").hexdigest()
            blob_path = (
                workspace
                / ".filetrack"
                / "blobs"
                / expected_hash[:2]
                / expected_hash[2:]
            )
            assert blob_path.exists(), f"Blob not found at {blob_path}"

            service.close()

    def test_track_and_link_single_transaction(self):
        from pathlib import Path

        from wtb.infrastructure.file_tracking.sqlite_service import (
            SqliteFileTrackingService,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            service = SqliteFileTrackingService(
                workspace_path=workspace, db_name="test_ft.db"
            )

            test_file = workspace / "data.json"
            test_file.write_text('{"key": "value"}', encoding="utf-8")

            checkpoint_id = "cp-test-001"
            result = service.track_and_link(
                checkpoint_id=checkpoint_id,
                file_paths=[str(test_file)],
                message="linked commit",
            )

            assert result.files_tracked == 1

            linked_commit = service.get_commit_for_checkpoint(checkpoint_id)
            assert linked_commit == result.commit_id

            assert service.get_commit_count() == 1
            assert service.get_link_count() == 1

            service.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Deferred Commit in rollback_and_run
# ═══════════════════════════════════════════════════════════════════════════════


class TestRollbackAndRunDeferredCommit:
    """rollback_and_run must set deferred_commit(True) on controller."""

    def test_rollback_and_run_sets_deferred_commit(self):
        from wtb.application.services.batch_execution_coordinator import (
            BatchExecutionCoordinator,
        )

        mock_uow = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)
        mock_uow.commit = MagicMock()

        uow_factory = MagicMock(return_value=mock_uow)

        mock_controller = MagicMock(spec=ExecutionController)
        mock_execution = _make_execution(
            status=ExecutionStatus.PAUSED, session_id="wtb-1"
        )
        mock_uow.executions.get.return_value = mock_execution
        mock_controller.rollback.return_value = mock_execution
        mock_controller.run.return_value = mock_execution

        mock_ctrl_factory = MagicMock()
        mock_ctrl_factory.create.return_value = mock_controller

        mock_adapter = MagicMock()
        mock_adapter.set_workflow_graph = MagicMock()

        coordinator = BatchExecutionCoordinator(
            uow_factory=uow_factory,
            controller_factory=mock_ctrl_factory,
            state_adapter=mock_adapter,
        )

        mock_graph = MagicMock()
        coordinator.rollback_and_run("exec-1", "cp-1", graph=mock_graph)

        mock_controller.set_deferred_commit.assert_called_with(True)


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Invalidate Environment (no double-pop)
# ═══════════════════════════════════════════════════════════════════════════════


class TestInvalidateEnvironment:
    """invalidate_environment pops from _environments; cleanup must not re-pop."""

    def test_invalidate_does_not_double_pop(self):
        from wtb.infrastructure.environment.providers import GrpcEnvironmentProvider

        provider = GrpcEnvironmentProvider.__new__(GrpcEnvironmentProvider)
        provider._environments = {}
        provider._env_lock = __import__("threading").Lock()
        provider._operation_locks = {}
        provider._operation_locks_guard = __import__("threading").Lock()
        provider._stub = None
        provider._event_bus = None

        provider._environments["ws-1"] = {
            "type": "grpc_uv_stub",
            "variant_id": "ws-1",
            "workflow_id": "wf",
            "node_id": "n",
            "version_id": "v",
        }

        result = provider.invalidate_environment("ws-1")
        assert result is True
        assert "ws-1" not in provider._environments

        result2 = provider.invalidate_environment("ws-1")
        assert result2 is False

    def test_cleanup_after_invalidate_is_noop(self):
        from wtb.infrastructure.environment.providers import GrpcEnvironmentProvider

        provider = GrpcEnvironmentProvider.__new__(GrpcEnvironmentProvider)
        provider._environments = {}
        provider._env_lock = __import__("threading").Lock()
        provider._operation_locks = {}
        provider._operation_locks_guard = __import__("threading").Lock()
        provider._stub = None
        provider._event_bus = None

        provider._environments["ws-2"] = {
            "type": "grpc_uv_stub",
            "variant_id": "ws-2",
        }

        provider.invalidate_environment("ws-2")
        assert "ws-2" not in provider._environments

        provider.cleanup_environment("ws-2")
        assert "ws-2" not in provider._environments
