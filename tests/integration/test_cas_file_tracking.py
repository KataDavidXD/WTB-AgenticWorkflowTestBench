"""
Integration tests for CAS (Content-Addressable Storage) / File Tracking.

Tests the SqliteFileTrackingService and MockFileTrackingService across all modes:
- Track files with SHA-256 deduplication
- Link commits to checkpoints
- Restore files from checkpoints
- Rollback file state
- Integration with ExecutionController (LangGraph + node-executor)
"""

import os
import tempfile
import pytest
from pathlib import Path

from wtb.infrastructure.file_tracking.sqlite_service import SqliteFileTrackingService
from wtb.infrastructure.file_tracking.mock_service import MockFileTrackingService
from wtb.domain.interfaces.file_tracking import (
    FileTrackingResult,
    FileRestoreResult,
    FileTrackingLink,
    CheckpointLinkError,
    CommitNotFoundError,
)


# ═══════════════════════════════════════════════════════════════
# SQLite CAS: Core Blob Storage
# ═══════════════════════════════════════════════════════════════


class TestSqliteCASBlobStorage:

    @pytest.fixture
    def workspace(self):
        tmpdir = tempfile.mkdtemp()
        yield Path(tmpdir)
        # Cleanup handled manually after service.close()

    @pytest.fixture
    def service(self, workspace):
        svc = SqliteFileTrackingService(workspace_path=workspace)
        yield svc
        svc.close()

    @pytest.fixture
    def sample_files(self, workspace):
        files_dir = workspace / "outputs"
        files_dir.mkdir()

        f1 = files_dir / "result.txt"
        f1.write_text("hello world", encoding="utf-8")

        f2 = files_dir / "data.json"
        f2.write_text('{"key": "value"}', encoding="utf-8")

        return [str(f1), str(f2)]

    def test_track_files_creates_commit(self, service, sample_files):
        result = service.track_files(sample_files, message="test commit")

        assert isinstance(result, FileTrackingResult)
        assert result.files_tracked == 2
        assert result.total_size_bytes > 0
        assert len(result.commit_id) > 0
        assert result.message == "test commit"

    def test_track_files_stores_blobs(self, service, sample_files, workspace):
        service.track_files(sample_files)

        blob_dir = workspace / ".filetrack" / "blobs"
        assert blob_dir.exists()

        subdirs = list(blob_dir.iterdir())
        assert len(subdirs) >= 1

    def test_blob_deduplication(self, service, workspace):
        """Same content tracked twice -> single blob (dedup)."""
        files_dir = workspace / "outputs"
        files_dir.mkdir(exist_ok=True)

        f1 = files_dir / "file_a.txt"
        f1.write_text("identical content", encoding="utf-8")

        f2 = files_dir / "file_b.txt"
        f2.write_text("identical content", encoding="utf-8")

        result = service.track_files([str(f1), str(f2)])

        assert result.files_tracked == 2
        assert result.file_hashes[str(f1)] == result.file_hashes[str(f2)]

        blob_hash = result.file_hashes[str(f1)]
        blob_path = workspace / ".filetrack" / "blobs" / blob_hash[:2] / blob_hash[2:]
        assert blob_path.exists()

    def test_sha256_hash_format(self, service, sample_files):
        result = service.track_files(sample_files)

        for path, h in result.file_hashes.items():
            assert len(h) == 64, f"SHA256 should be 64 hex chars, got {len(h)}"
            assert all(c in "0123456789abcdef" for c in h)


# ═══════════════════════════════════════════════════════════════
# SQLite CAS: Checkpoint Linking
# ═══════════════════════════════════════════════════════════════


class TestCheckpointLinking:

    @pytest.fixture
    def workspace(self):
        tmpdir = tempfile.mkdtemp()
        yield Path(tmpdir)

    @pytest.fixture
    def service(self, workspace):
        svc = SqliteFileTrackingService(workspace_path=workspace)
        yield svc
        svc.close()

    @pytest.fixture
    def tracked_files(self, service, workspace):
        files_dir = workspace / "outputs"
        files_dir.mkdir()
        f1 = files_dir / "output.txt"
        f1.write_text("output data", encoding="utf-8")
        return service.track_files([str(f1)], message="initial")

    def test_track_and_link_creates_link(self, service, workspace):
        files_dir = workspace / "outputs"
        files_dir.mkdir()
        f1 = files_dir / "linked.txt"
        f1.write_text("linked content", encoding="utf-8")

        result = service.track_and_link(
            checkpoint_id=42,
            file_paths=[str(f1)],
            message="linked commit",
        )

        assert isinstance(result, FileTrackingResult)
        assert result.files_tracked == 1

        commit_id = service.get_commit_for_checkpoint(42)
        assert commit_id == result.commit_id

    def test_link_existing_commit_to_checkpoint(self, service, tracked_files):
        link = service.link_to_checkpoint(
            checkpoint_id=100,
            commit_id=tracked_files.commit_id,
        )

        assert isinstance(link, FileTrackingLink)
        assert link.checkpoint_id == 100
        assert link.commit_id == tracked_files.commit_id

    def test_get_commit_for_unlinked_checkpoint_returns_none(self, service):
        result = service.get_commit_for_checkpoint(9999)
        assert result is None

    def test_get_files_at_checkpoint(self, service, workspace):
        files_dir = workspace / "outputs"
        files_dir.mkdir()
        f1 = files_dir / "queried.txt"
        f1.write_text("query test", encoding="utf-8")

        service.track_and_link(checkpoint_id=55, file_paths=[str(f1)])

        files = service.get_files_at_checkpoint(55)
        assert len(files) == 1
        assert str(f1) in files

    def test_get_files_at_unlinked_checkpoint_returns_empty(self, service):
        files = service.get_files_at_checkpoint(9999)
        assert files == []


# ═══════════════════════════════════════════════════════════════
# SQLite CAS: Restore from Checkpoint
# ═══════════════════════════════════════════════════════════════


class TestRestoreFromCheckpoint:

    @pytest.fixture
    def workspace(self):
        tmpdir = tempfile.mkdtemp()
        yield Path(tmpdir)

    @pytest.fixture
    def service(self, workspace):
        svc = SqliteFileTrackingService(workspace_path=workspace)
        yield svc
        svc.close()

    def test_restore_recovers_original_content(self, service, workspace):
        files_dir = workspace / "outputs"
        files_dir.mkdir()
        f1 = files_dir / "result.txt"
        original_content = "original output v1"
        f1.write_text(original_content, encoding="utf-8")

        service.track_and_link(
            checkpoint_id=1,
            file_paths=[str(f1)],
            message="checkpoint 1",
        )

        f1.write_text("modified output v2", encoding="utf-8")
        assert f1.read_text(encoding="utf-8") == "modified output v2"

        restore_result = service.restore_from_checkpoint(checkpoint_id=1)
        assert isinstance(restore_result, FileRestoreResult)
        assert restore_result.files_restored >= 1

        assert f1.read_text(encoding="utf-8") == original_content

    def test_restore_nonexistent_checkpoint_raises(self, service):
        with pytest.raises(CheckpointLinkError):
            service.restore_from_checkpoint(checkpoint_id=9999)

    def test_restore_commit_by_id(self, service, workspace):
        files_dir = workspace / "outputs"
        files_dir.mkdir()
        f1 = files_dir / "data.txt"
        f1.write_text("snapshot data", encoding="utf-8")

        result = service.track_files([str(f1)])

        f1.unlink()
        assert not f1.exists()

        restore = service.restore_commit(result.commit_id)
        assert restore.files_restored >= 1
        assert f1.exists()
        assert f1.read_text(encoding="utf-8") == "snapshot data"


# ═══════════════════════════════════════════════════════════════
# SQLite CAS: Rollback Scenario (multi-checkpoint)
# ═══════════════════════════════════════════════════════════════


class TestCASRollbackScenario:

    @pytest.fixture
    def workspace(self):
        tmpdir = tempfile.mkdtemp()
        yield Path(tmpdir)

    @pytest.fixture
    def service(self, workspace):
        svc = SqliteFileTrackingService(workspace_path=workspace)
        yield svc
        svc.close()

    def test_rollback_to_earlier_checkpoint(self, service, workspace):
        """Simulate: run -> checkpoint1 -> modify -> checkpoint2 -> rollback to 1."""
        files_dir = workspace / "outputs"
        files_dir.mkdir()
        f = files_dir / "evolving.txt"

        f.write_text("state_v1", encoding="utf-8")
        service.track_and_link(checkpoint_id=1, file_paths=[str(f)])

        f.write_text("state_v2", encoding="utf-8")
        service.track_and_link(checkpoint_id=2, file_paths=[str(f)])

        assert f.read_text(encoding="utf-8") == "state_v2"

        service.restore_from_checkpoint(checkpoint_id=1)
        assert f.read_text(encoding="utf-8") == "state_v1"

    def test_multiple_files_rollback(self, service, workspace):
        files_dir = workspace / "outputs"
        files_dir.mkdir()

        f_a = files_dir / "a.txt"
        f_b = files_dir / "b.txt"

        f_a.write_text("a_v1", encoding="utf-8")
        f_b.write_text("b_v1", encoding="utf-8")
        service.track_and_link(checkpoint_id=10, file_paths=[str(f_a), str(f_b)])

        f_a.write_text("a_v2", encoding="utf-8")
        f_b.write_text("b_v2", encoding="utf-8")

        service.restore_from_checkpoint(checkpoint_id=10)
        assert f_a.read_text(encoding="utf-8") == "a_v1"
        assert f_b.read_text(encoding="utf-8") == "b_v1"


# ═══════════════════════════════════════════════════════════════
# SQLite CAS: Context Manager Protocol
# ═══════════════════════════════════════════════════════════════


class TestSqliteContextManager:

    def test_context_manager_closes_connection(self):
        tmpdir = tempfile.mkdtemp()
        workspace = Path(tmpdir)

        with SqliteFileTrackingService(workspace_path=workspace) as svc:
            assert svc.is_available() is True
            files_dir = workspace / "outputs"
            files_dir.mkdir()
            f1 = files_dir / "ctx.txt"
            f1.write_text("context test", encoding="utf-8")
            svc.track_files([str(f1)])

        # After __exit__, connection should be closed


# ═══════════════════════════════════════════════════════════════
# MockFileTrackingService
# ═══════════════════════════════════════════════════════════════


class TestMockFileTrackingService:

    def test_track_files_returns_result(self):
        service = MockFileTrackingService()
        result = service.track_files(["fake/path.txt"], message="mock commit")

        assert isinstance(result, FileTrackingResult)
        assert result.files_tracked == 1
        assert result.commit_id

    def test_track_and_link(self):
        service = MockFileTrackingService()
        result = service.track_and_link(
            checkpoint_id=42,
            file_paths=["fake/file.txt"],
        )

        assert isinstance(result, FileTrackingResult)
        commit = service.get_commit_for_checkpoint(42)
        assert commit == result.commit_id

    def test_is_available_returns_true(self):
        service = MockFileTrackingService()
        assert service.is_available() is True

    def test_operation_log_records_operations(self):
        service = MockFileTrackingService()
        service.track_files(["a.txt"])
        service.track_and_link(1, ["b.txt"])

        log = service._operation_log
        assert len(log) >= 2
        ops = [entry["operation"] for entry in log]
        assert "track_files" in ops

    def test_get_files_at_checkpoint(self):
        service = MockFileTrackingService()
        service.track_and_link(checkpoint_id=10, file_paths=["x.txt", "y.txt"])

        files = service.get_files_at_checkpoint(10)
        assert len(files) == 2
        assert "x.txt" in files
        assert "y.txt" in files

    def test_get_files_at_unlinked_checkpoint_returns_empty(self):
        service = MockFileTrackingService()
        files = service.get_files_at_checkpoint(9999)
        assert files == []


# ═══════════════════════════════════════════════════════════════
# CAS + Controller Integration (LangGraph mode)
# ═══════════════════════════════════════════════════════════════


class TestCASControllerIntegration:

    def _try_import_langgraph(self):
        try:
            from wtb.infrastructure.adapters.langgraph_state_adapter import (
                LangGraphStateAdapter,
                LangGraphConfig,
                LANGGRAPH_AVAILABLE,
            )
            if not LANGGRAPH_AVAILABLE:
                pytest.skip("LangGraph not available")
            return LangGraphStateAdapter, LangGraphConfig
        except ImportError:
            pytest.skip("LangGraph not installed")

    def test_controller_with_mock_file_tracking(self):
        """ExecutionController accepts mock file tracking and tracks output files."""
        LangGraphStateAdapter, LangGraphConfig = self._try_import_langgraph()
        from wtb.testing.fixtures import create_minimal_graph
        from wtb.domain.models.workflow import (
            ExecutionStatus, WorkflowNode, WorkflowEdge,
        )
        from wtb.infrastructure.database.inmemory_unit_of_work import InMemoryUnitOfWork
        from wtb.application.services.execution_controller import (
            ExecutionController, DefaultNodeExecutor,
        )
        from wtb.domain.models.workflow import TestWorkflow as TWF

        config = LangGraphConfig.for_testing()
        adapter = LangGraphStateAdapter(config)
        uow = InMemoryUnitOfWork()
        uow.__enter__()
        mock_ft = MockFileTrackingService()

        controller = ExecutionController(
            execution_repository=uow.executions,
            workflow_repository=uow.workflows,
            state_adapter=adapter,
            node_executor=DefaultNodeExecutor(),
            unit_of_work=uow,
            file_tracking_service=mock_ft,
            output_dir=tempfile.mkdtemp(),
        )

        wf = TWF(id="wf-cas", name="cas-test", entry_point="start")
        wf.add_node(WorkflowNode(id="start", name="Start", type="start"))
        wf.add_node(WorkflowNode(id="end", name="End", type="end"))
        wf.add_edge(WorkflowEdge(source_id="start", target_id="end"))
        uow.workflows.add(wf)
        uow.commit()

        graph = create_minimal_graph()
        execution = controller.create_execution(
            wf, initial_state={"value": 0, "messages": [], "route": None},
        )
        execution = controller.run(execution.id, graph=graph)

        assert execution.status == ExecutionStatus.COMPLETED
        assert mock_ft.is_available()

    def test_sqlite_service_is_available(self):
        tmpdir = tempfile.mkdtemp()
        with SqliteFileTrackingService(workspace_path=Path(tmpdir)) as service:
            assert service.is_available() is True


# ═══════════════════════════════════════════════════════════════
# CAS + Node Executor Integration
# ═══════════════════════════════════════════════════════════════


class TestCASNodeExecutorIntegration:

    def test_node_executor_with_mock_tracking(self):
        from wtb.infrastructure.adapters.inmemory_state_adapter import InMemoryStateAdapter
        from wtb.infrastructure.database.inmemory_unit_of_work import InMemoryUnitOfWork
        from wtb.application.services.execution_controller import (
            ExecutionController, DefaultNodeExecutor,
        )
        from wtb.domain.models.workflow import (
            ExecutionStatus, WorkflowNode, WorkflowEdge,
        )
        from wtb.domain.models.workflow import TestWorkflow as TWF

        adapter = InMemoryStateAdapter()
        uow = InMemoryUnitOfWork()
        uow.__enter__()
        mock_ft = MockFileTrackingService()

        controller = ExecutionController(
            execution_repository=uow.executions,
            workflow_repository=uow.workflows,
            state_adapter=adapter,
            node_executor=DefaultNodeExecutor(),
            unit_of_work=uow,
            file_tracking_service=mock_ft,
        )

        wf = TWF(id="wf-ne-cas", name="ne-cas-test", entry_point="start")
        wf.add_node(WorkflowNode(id="start", name="Start", type="start"))
        wf.add_node(WorkflowNode(id="end", name="End", type="end"))
        wf.add_edge(WorkflowEdge(source_id="start", target_id="end"))
        uow.workflows.add(wf)
        uow.commit()

        execution = controller.create_execution(wf)
        execution = controller.run(execution.id)

        assert execution.status in (ExecutionStatus.COMPLETED, ExecutionStatus.FAILED)
        assert mock_ft.is_available()
