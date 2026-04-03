"""
Unit Tests for WorkspaceManager and Workspace Domain Model.

Tests workspace isolation functionality including:
- Workspace creation and lifecycle
- File linking (hard links and copy fallback)
- Orphan cleanup
- Event publishing
- Cross-platform compatibility

Design Reference: docs/issues/WORKSPACE_ISOLATION_DESIGN.md
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from wtb.domain.models.workspace import (
    Workspace,
    WorkspaceConfig,
    WorkspaceStrategy,
    LinkMethod,
    LinkResult,
    OrphanWorkspace,
    CleanupReport,
    compute_venv_spec_hash,
)
from wtb.domain.events.workspace_events import (
    WorkspaceCreatedEvent,
    WorkspaceActivatedEvent,
    WorkspaceDeactivatedEvent,
    WorkspaceCleanedUpEvent,
    FileSnapshotCreatedEvent,
    OrphanWorkspaceDetectedEvent,
    OrphanCleanupCompletedEvent,
)
from wtb.infrastructure.workspace.manager import (
    WorkspaceManager,
    WorkspaceManagerError,
    WorkspaceNotFoundError,
    WorkspaceCreationError,
    WorkspaceCleanupError,
    create_file_link,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    temp = tempfile.mkdtemp(prefix="wtb_test_workspace_")
    yield Path(temp)
    # Cleanup
    shutil.rmtree(temp, ignore_errors=True)


@pytest.fixture
def workspace_config(temp_dir):
    """Create a workspace config for testing."""
    return WorkspaceConfig(
        enabled=True,
        strategy=WorkspaceStrategy.WORKSPACE,
        base_dir=temp_dir,
        cleanup_on_complete=True,
        preserve_on_failure=True,
        use_hard_links=True,
        orphan_cleanup_grace_hours=0.001,  # Very short for testing
    )


@pytest.fixture
def mock_event_bus():
    """Create a mock event bus."""
    bus = MagicMock()
    bus.published_events = []
    
    def capture_event(event):
        bus.published_events.append(event)
    
    bus.publish = capture_event
    return bus


@pytest.fixture
def workspace_manager(workspace_config, mock_event_bus):
    """Create a workspace manager for testing."""
    manager = WorkspaceManager(
        config=workspace_config,
        event_bus=mock_event_bus,
        session_id="test-session",
    )
    yield manager
    # Cleanup all workspaces
    for ws_id in list(manager._workspaces.keys()):
        try:
            manager.cleanup_workspace(ws_id)
        except Exception:
            pass


@pytest.fixture
def source_files(temp_dir):
    """Create source files for testing."""
    source_dir = temp_dir / "source"
    source_dir.mkdir(parents=True)
    
    # Create test files
    (source_dir / "data.csv").write_text("a,b,c\n1,2,3\n")
    (source_dir / "config.json").write_text('{"key": "value"}')
    
    # Create subdirectory with files
    sub_dir = source_dir / "models"
    sub_dir.mkdir()
    (sub_dir / "model.pkl").write_bytes(b"fake model data")
    
    return source_dir


# ═══════════════════════════════════════════════════════════════════════════════
# Workspace Model Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestWorkspaceModel:
    """Tests for Workspace domain model."""
    
    def test_workspace_creation(self, temp_dir):
        """Test basic workspace creation."""
        workspace = Workspace(
            workspace_id="ws-001",
            batch_test_id="batch-001",
            variant_name="bm25",
            execution_id="exec-001",
            root_path=temp_dir / "workspace",
        )
        
        assert workspace.workspace_id == "ws-001"
        assert workspace.batch_test_id == "batch-001"
        assert workspace.variant_name == "bm25"
        assert workspace.execution_id == "exec-001"
    
    def test_derived_paths(self, temp_dir):
        """Test that derived paths are computed correctly."""
        root = temp_dir / "workspace"
        workspace = Workspace(
            workspace_id="ws-001",
            batch_test_id="batch-001",
            variant_name="test",
            execution_id="exec-001",
            root_path=root,
        )
        
        assert workspace.input_dir == root / "input"
        assert workspace.output_dir == root / "output"
        assert workspace.venv_dir == root / ".venv"
        assert workspace.lock_file == root / ".workspace_lock"
        assert workspace.meta_file == root / ".workspace_meta.json"
    
    def test_python_path_windows(self, temp_dir):
        """Test Python path on Windows."""
        workspace = Workspace(
            workspace_id="ws-001",
            batch_test_id="batch-001",
            variant_name="test",
            execution_id="exec-001",
            root_path=temp_dir,
        )
        
        if sys.platform == "win32":
            assert workspace.python_path == temp_dir / ".venv" / "Scripts" / "python.exe"
        else:
            assert workspace.python_path == temp_dir / ".venv" / "bin" / "python"
    
    def test_workspace_activation(self, temp_dir):
        """Test workspace activation changes CWD."""
        root = temp_dir / "workspace"
        root.mkdir(parents=True)
        
        workspace = Workspace(
            workspace_id="ws-001",
            batch_test_id="batch-001",
            variant_name="test",
            execution_id="exec-001",
            root_path=root,
        )
        
        original_cwd = Path.cwd()
        
        try:
            workspace.activate()
            assert workspace.is_active
            assert Path.cwd() == root
            
            workspace.deactivate()
            assert not workspace.is_active
            assert Path.cwd() == original_cwd
        finally:
            os.chdir(original_cwd)
    
    def test_workspace_serialization(self, temp_dir):
        """Test workspace to_dict and from_dict."""
        workspace = Workspace(
            workspace_id="ws-001",
            batch_test_id="batch-001",
            variant_name="test",
            execution_id="exec-001",
            root_path=temp_dir,
            strategy=WorkspaceStrategy.WORKSPACE,
            source_paths=["./data.csv"],
            file_count=5,
            total_size_bytes=1024,
        )
        
        data = workspace.to_dict()
        restored = Workspace.from_dict(data)
        
        assert restored.workspace_id == workspace.workspace_id
        assert restored.batch_test_id == workspace.batch_test_id
        assert restored.variant_name == workspace.variant_name
        assert restored.strategy == workspace.strategy
        assert restored.source_paths == workspace.source_paths
    
    def test_lock_file_operations(self, temp_dir):
        """Test lock file creation and reading."""
        root = temp_dir / "workspace"
        root.mkdir(parents=True)
        
        workspace = Workspace(
            workspace_id="ws-001",
            batch_test_id="batch-001",
            variant_name="test",
            execution_id="exec-001",
            root_path=root,
        )
        
        # Create lock file
        workspace.create_lock_file(pid=12345, session_id="test-session")
        
        assert workspace.lock_file.exists()
        
        # Read lock file
        lock_data = workspace.read_lock_file()
        assert lock_data is not None
        assert lock_data["pid"] == 12345
        assert lock_data["session_id"] == "test-session"
        assert lock_data["workspace_id"] == "ws-001"
        
        # Remove lock file
        assert workspace.remove_lock_file()
        assert not workspace.lock_file.exists()
    
    def test_workspace_equality(self, temp_dir):
        """Test workspace equality based on workspace_id."""
        ws1 = Workspace(
            workspace_id="ws-001",
            batch_test_id="batch-001",
            variant_name="test",
            execution_id="exec-001",
            root_path=temp_dir,
        )
        
        ws2 = Workspace(
            workspace_id="ws-001",
            batch_test_id="batch-002",  # Different batch
            variant_name="other",
            execution_id="exec-002",
            root_path=temp_dir / "other",
        )
        
        ws3 = Workspace(
            workspace_id="ws-002",
            batch_test_id="batch-001",
            variant_name="test",
            execution_id="exec-001",
            root_path=temp_dir,
        )
        
        assert ws1 == ws2  # Same workspace_id
        assert ws1 != ws3  # Different workspace_id
        assert hash(ws1) == hash(ws2)


class TestWorkspaceConfig:
    """Tests for WorkspaceConfig."""
    
    def test_default_base_dir(self):
        """Test default base directory is system temp."""
        config = WorkspaceConfig()
        base_dir = config.get_base_dir()
        
        assert "wtb_workspaces" in str(base_dir)
    
    def test_custom_base_dir(self, temp_dir):
        """Test custom base directory."""
        config = WorkspaceConfig(base_dir=temp_dir)
        assert config.get_base_dir() == temp_dir
    
    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific test")
    def test_cross_partition_validation(self, temp_dir):
        """Test cross-partition detection on Windows."""
        config = WorkspaceConfig(base_dir=Path("D:\\workspaces"))
        
        source_paths = [
            Path("D:\\source\\file1.txt"),
            Path("C:\\other\\file2.txt"),
        ]
        
        cross = config.validate_cross_partition(source_paths)
        assert len(cross) == 1
        assert cross[0] == Path("C:\\other\\file2.txt")


class TestVenvSpecHash:
    """Tests for venv spec hash computation."""
    
    def test_hash_consistency(self):
        """Test hash is consistent for same inputs."""
        hash1 = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain", "faiss-cpu"],
        )
        
        hash2 = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain", "faiss-cpu"],
        )
        
        assert hash1 == hash2
    
    def test_hash_changes_with_deps(self):
        """Test hash changes when dependencies change."""
        hash1 = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain"],
        )
        
        hash2 = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain", "faiss-cpu"],
        )
        
        assert hash1 != hash2
    
    def test_hash_order_independent(self):
        """Test hash is independent of dependency order."""
        hash1 = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["faiss-cpu", "langchain"],
        )
        
        hash2 = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain", "faiss-cpu"],
        )
        
        assert hash1 == hash2


# ═══════════════════════════════════════════════════════════════════════════════
# File Link Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCreateFileLink:
    """Tests for create_file_link utility."""
    
    def test_link_file_with_hardlink(self, temp_dir):
        """Test file linking with hard links."""
        source = temp_dir / "source.txt"
        source.write_text("test content")
        
        target = temp_dir / "target" / "linked.txt"
        
        result = create_file_link(source, target, use_hard_links=True)
        
        assert target.exists()
        assert target.read_text() == "test content"
        
        # On same partition, should use hard link
        if result.method == LinkMethod.HARDLINK:
            assert result.size_bytes == 0
        else:
            # Fallback to copy is acceptable
            assert result.method == LinkMethod.COPY
    
    def test_link_file_with_copy(self, temp_dir):
        """Test file linking with copy."""
        source = temp_dir / "source.txt"
        source.write_text("test content")
        
        target = temp_dir / "target" / "copied.txt"
        
        result = create_file_link(source, target, use_hard_links=False)
        
        assert target.exists()
        assert target.read_text() == "test content"
        assert result.method == LinkMethod.COPY
        assert result.size_bytes > 0
    
    def test_link_nonexistent_file(self, temp_dir):
        """Test linking nonexistent file raises error."""
        source = temp_dir / "nonexistent.txt"
        target = temp_dir / "target.txt"
        
        with pytest.raises(FileNotFoundError):
            create_file_link(source, target)
    
    def test_link_creates_parent_directories(self, temp_dir):
        """Test linking creates parent directories."""
        source = temp_dir / "source.txt"
        source.write_text("test")
        
        target = temp_dir / "deep" / "nested" / "path" / "file.txt"
        
        result = create_file_link(source, target)
        
        assert target.exists()
        assert target.parent.exists()


# ═══════════════════════════════════════════════════════════════════════════════
# WorkspaceManager Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestWorkspaceManager:
    """Tests for WorkspaceManager."""
    
    def test_create_workspace(self, workspace_manager, source_files):
        """Test basic workspace creation."""
        workspace = workspace_manager.create_workspace(
            batch_id="batch-001",
            variant_name="bm25",
            execution_id="exec-001",
            source_paths=[source_files / "data.csv"],
        )
        
        assert workspace.workspace_id is not None
        assert workspace.batch_test_id == "batch-001"
        assert workspace.variant_name == "bm25"
        assert workspace.root_path.exists()
        assert workspace.input_dir.exists()
        assert workspace.output_dir.exists()
        assert workspace.lock_file.exists()
    
    def test_create_workspace_with_directory(self, workspace_manager, source_files):
        """Test workspace creation with directory source."""
        workspace = workspace_manager.create_workspace(
            batch_id="batch-001",
            variant_name="test",
            execution_id="exec-001",
            source_paths=[source_files],
        )
        
        # Check files were copied
        assert (workspace.input_dir / source_files.name / "data.csv").exists()
        assert (workspace.input_dir / source_files.name / "config.json").exists()
        assert (workspace.input_dir / source_files.name / "models" / "model.pkl").exists()
    
    def test_create_workspace_disabled(self, temp_dir, mock_event_bus):
        """Test workspace creation when disabled."""
        config = WorkspaceConfig(enabled=False, base_dir=temp_dir)
        manager = WorkspaceManager(config, mock_event_bus)
        
        workspace = manager.create_workspace(
            batch_id="batch-001",
            variant_name="test",
            execution_id="exec-001",
        )
        
        assert workspace.strategy == WorkspaceStrategy.NONE
        assert workspace.root_path == Path.cwd()
    
    def test_get_workspace(self, workspace_manager, source_files):
        """Test getting workspace by ID."""
        created = workspace_manager.create_workspace(
            batch_id="batch-001",
            variant_name="test",
            execution_id="exec-001",
        )
        
        retrieved = workspace_manager.get_workspace(created.workspace_id)
        
        assert retrieved is not None
        assert retrieved.workspace_id == created.workspace_id
    
    def test_get_workspace_by_execution(self, workspace_manager):
        """Test getting workspace by execution ID."""
        created = workspace_manager.create_workspace(
            batch_id="batch-001",
            variant_name="test",
            execution_id="exec-unique-001",
        )
        
        retrieved = workspace_manager.get_workspace_by_execution("exec-unique-001")
        
        assert retrieved is not None
        assert retrieved.execution_id == "exec-unique-001"
    
    def test_list_workspaces(self, workspace_manager):
        """Test listing workspaces."""
        # Create multiple workspaces
        ws1 = workspace_manager.create_workspace(
            batch_id="batch-001",
            variant_name="v1",
            execution_id="exec-001",
        )
        ws2 = workspace_manager.create_workspace(
            batch_id="batch-001",
            variant_name="v2",
            execution_id="exec-002",
        )
        ws3 = workspace_manager.create_workspace(
            batch_id="batch-002",
            variant_name="v1",
            execution_id="exec-003",
        )
        
        # List all
        all_ws = workspace_manager.list_workspaces()
        assert len(all_ws) == 3
        
        # List by batch
        batch1_ws = workspace_manager.list_workspaces(batch_id="batch-001")
        assert len(batch1_ws) == 2
        
        batch2_ws = workspace_manager.list_workspaces(batch_id="batch-002")
        assert len(batch2_ws) == 1
    
    def test_cleanup_workspace(self, workspace_manager, mock_event_bus):
        """Test workspace cleanup."""
        workspace = workspace_manager.create_workspace(
            batch_id="batch-001",
            variant_name="test",
            execution_id="exec-001",
        )
        
        root_path = workspace.root_path
        assert root_path.exists()
        
        result = workspace_manager.cleanup_workspace(workspace.workspace_id)
        
        assert result is True
        assert not root_path.exists()
        assert workspace_manager.get_workspace(workspace.workspace_id) is None
        
        # Check cleanup event was published
        cleanup_events = [
            e for e in mock_event_bus.published_events
            if isinstance(e, WorkspaceCleanedUpEvent)
        ]
        assert len(cleanup_events) == 1
    
    def test_cleanup_workspace_preserve(self, workspace_manager):
        """Test workspace cleanup with preserve flag."""
        workspace = workspace_manager.create_workspace(
            batch_id="batch-001",
            variant_name="test",
            execution_id="exec-001",
        )
        
        root_path = workspace.root_path
        
        result = workspace_manager.cleanup_workspace(
            workspace.workspace_id,
            preserve=True,
        )
        
        assert result is True
        assert root_path.exists()  # Directory preserved
        assert workspace_manager.get_workspace(workspace.workspace_id) is None
    
    def test_cleanup_batch(self, workspace_manager):
        """Test batch cleanup."""
        # Create multiple workspaces in same batch
        ws1 = workspace_manager.create_workspace(
            batch_id="batch-cleanup-test",
            variant_name="v1",
            execution_id="exec-001",
        )
        ws2 = workspace_manager.create_workspace(
            batch_id="batch-cleanup-test",
            variant_name="v2",
            execution_id="exec-002",
        )
        
        count = workspace_manager.cleanup_batch("batch-cleanup-test")
        
        assert count == 2
        assert workspace_manager.get_workspace(ws1.workspace_id) is None
        assert workspace_manager.get_workspace(ws2.workspace_id) is None
    
    def test_create_branch_workspace(self, workspace_manager, source_files):
        """Test creating branch workspace from existing."""
        # Create source workspace
        source_ws = workspace_manager.create_workspace(
            batch_id="batch-001",
            variant_name="original",
            execution_id="exec-001",
            source_paths=[source_files / "data.csv"],
        )
        
        # Create branch
        branch_ws = workspace_manager.create_branch_workspace(
            source_workspace_id=source_ws.workspace_id,
            fork_checkpoint_id=42,
            new_execution_id="branch-001",
            file_commit_id="commit-abc123",
        )
        
        assert branch_ws.workspace_id != source_ws.workspace_id
        assert branch_ws.batch_test_id == source_ws.batch_test_id
        assert "branch" in branch_ws.variant_name
        assert branch_ws.source_snapshot_commit_id == "commit-abc123"
    
    def test_create_branch_workspace_not_found(self, workspace_manager):
        """Test branch creation with nonexistent source."""
        with pytest.raises(WorkspaceNotFoundError):
            workspace_manager.create_branch_workspace(
                source_workspace_id="nonexistent",
                fork_checkpoint_id=42,
                new_execution_id="branch-001",
            )
    
    def test_event_publishing(self, workspace_manager, mock_event_bus):
        """Test events are published correctly."""
        workspace = workspace_manager.create_workspace(
            batch_id="batch-001",
            variant_name="test",
            execution_id="exec-001",
        )
        
        # Check creation event
        create_events = [
            e for e in mock_event_bus.published_events
            if isinstance(e, WorkspaceCreatedEvent)
        ]
        assert len(create_events) == 1
        assert create_events[0].workspace_id == workspace.workspace_id
        assert create_events[0].batch_test_id == "batch-001"
    
    def test_context_manager(self, workspace_config, mock_event_bus):
        """Test WorkspaceManager as context manager."""
        with WorkspaceManager(workspace_config, mock_event_bus) as manager:
            ws = manager.create_workspace(
                batch_id="batch-001",
                variant_name="test",
                execution_id="exec-001",
            )
            root_path = ws.root_path
            assert root_path.exists()
        
        # After context exit, workspace should be cleaned up
        assert not root_path.exists()
    
    def test_get_stats(self, workspace_manager):
        """Test statistics retrieval."""
        workspace_manager.create_workspace(
            batch_id="batch-001",
            variant_name="v1",
            execution_id="exec-001",
        )
        workspace_manager.create_workspace(
            batch_id="batch-001",
            variant_name="v2",
            execution_id="exec-002",
        )
        
        stats = workspace_manager.get_stats()
        
        assert stats["active_workspaces"] == 2
        assert stats["active_batches"] == 1
        assert stats["session_id"] == "test-session"


class TestOrphanCleanup:
    """Tests for orphan workspace cleanup."""
    
    def test_orphan_detection(self, temp_dir, mock_event_bus):
        """Test orphan workspace detection."""
        # Create a workspace directory with lock file pointing to dead PID
        orphan_dir = temp_dir / "batch_old" / "orphan_exec"
        orphan_dir.mkdir(parents=True)
        
        lock_file = orphan_dir / ".workspace_lock"
        lock_data = {
            "pid": 99999999,  # Unlikely to be a real PID
            "hostname": "test-host",
            "created_at": (datetime.now() - timedelta(hours=2)).isoformat(),
            "session_id": "old-session",
            "batch_test_id": "batch_old",
            "workspace_id": "ws-orphan",
        }
        lock_file.write_text(json.dumps(lock_data))
        
        # Create manager (triggers orphan cleanup)
        config = WorkspaceConfig(
            enabled=True,
            base_dir=temp_dir,
            orphan_cleanup_grace_hours=0.001,  # Very short
        )
        manager = WorkspaceManager(config, mock_event_bus)
        
        # Give cleanup a moment
        time.sleep(0.1)
        
        # Check orphan was detected and cleaned
        orphan_events = [
            e for e in mock_event_bus.published_events
            if isinstance(e, OrphanWorkspaceDetectedEvent)
        ]
        
        # Should have detected the orphan
        assert len(orphan_events) >= 1
    
    def test_orphan_grace_period(self, temp_dir, mock_event_bus):
        """Test orphan grace period is respected."""
        # Create a "recent" orphan
        orphan_dir = temp_dir / "batch_recent" / "recent_exec"
        orphan_dir.mkdir(parents=True)
        
        lock_file = orphan_dir / ".workspace_lock"
        lock_data = {
            "pid": 99999999,
            "hostname": "test-host",
            "created_at": datetime.now().isoformat(),  # Just created
            "session_id": "recent-session",
            "batch_test_id": "batch_recent",
            "workspace_id": "ws-recent",
        }
        lock_file.write_text(json.dumps(lock_data))
        
        # Create manager with long grace period
        config = WorkspaceConfig(
            enabled=True,
            base_dir=temp_dir,
            orphan_cleanup_grace_hours=24.0,  # 24 hours
        )
        manager = WorkspaceManager(config, mock_event_bus)
        
        # Orphan should be detected but preserved
        orphan_events = [
            e for e in mock_event_bus.published_events
            if isinstance(e, OrphanWorkspaceDetectedEvent) and e.action == "preserved"
        ]
        
        # Directory should still exist
        assert orphan_dir.exists()


class TestThreadSafety:
    """Tests for thread safety."""
    
    def test_concurrent_workspace_creation(self, workspace_config, mock_event_bus):
        """Test concurrent workspace creation is thread-safe."""
        manager = WorkspaceManager(workspace_config, mock_event_bus)
        workspaces = []
        errors = []
        
        def create_workspace(i):
            try:
                ws = manager.create_workspace(
                    batch_id="batch-concurrent",
                    variant_name=f"v{i}",
                    execution_id=f"exec-{i}",
                )
                workspaces.append(ws)
            except Exception as e:
                errors.append(e)
        
        threads = [
            threading.Thread(target=create_workspace, args=(i,))
            for i in range(10)
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0
        assert len(workspaces) == 10
        
        # All workspace IDs should be unique
        ids = [ws.workspace_id for ws in workspaces]
        assert len(ids) == len(set(ids))
        
        # Cleanup
        manager.cleanup_batch("batch-concurrent")
    
    def test_concurrent_cleanup(self, workspace_config, mock_event_bus):
        """Test concurrent cleanup is thread-safe."""
        manager = WorkspaceManager(workspace_config, mock_event_bus)
        
        # Create workspaces
        workspace_ids = []
        for i in range(10):
            ws = manager.create_workspace(
                batch_id="batch-cleanup-concurrent",
                variant_name=f"v{i}",
                execution_id=f"exec-{i}",
            )
            workspace_ids.append(ws.workspace_id)
        
        results = []
        
        def cleanup_workspace(ws_id):
            result = manager.cleanup_workspace(ws_id)
            results.append(result)
        
        threads = [
            threading.Thread(target=cleanup_workspace, args=(ws_id,))
            for ws_id in workspace_ids
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # All should succeed (or return False if already cleaned)
        assert all(isinstance(r, bool) for r in results)
        assert sum(results) == 10  # All should have been cleaned


# ═══════════════════════════════════════════════════════════════════════════════
# Event Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestWorkspaceEvents:
    """Tests for workspace domain events."""
    
    def test_workspace_created_event(self):
        """Test WorkspaceCreatedEvent properties."""
        event = WorkspaceCreatedEvent(
            workspace_id="ws-001",
            batch_test_id="batch-001",
            execution_id="exec-001",
            variant_name="bm25",
            workspace_path="/tmp/workspace",
            file_count=10,
            total_size_bytes=1024 * 1024,  # 1MB
        )
        
        assert event.event_type == "WorkspaceCreatedEvent"
        assert event.total_size_mb == 1.0
        assert event.workspace_id == "ws-001"
    
    def test_workspace_cleaned_up_event(self):
        """Test WorkspaceCleanedUpEvent."""
        event = WorkspaceCleanedUpEvent(
            workspace_id="ws-001",
            reason="completed",
            files_removed=10,
            space_freed_bytes=2048,
            preserved=False,
        )
        
        assert event.reason == "completed"
        assert not event.preserved
    
    def test_fork_completed_event(self):
        """Test ForkCompletedEvent."""
        from wtb.domain.events.workspace_events import ForkCompletedEvent
        
        event = ForkCompletedEvent(
            new_execution_id="branch-001",
            workspace_id="ws-branch",
            thread_id="thread-branch-001",
            parent_execution_id="exec-001",
            fork_checkpoint_id=42,
            ready_to_execute=True,
        )
        
        assert event.ready_to_execute
        assert event.fork_checkpoint_id == 42


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
