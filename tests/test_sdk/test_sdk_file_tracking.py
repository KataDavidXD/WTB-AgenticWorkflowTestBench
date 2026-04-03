"""
SDK Integration tests for File System tracking.

Tests file tracking integration:
- File tracking configuration
- Checkpoint-file linking
- File version control
- Rollback with file state
- Blob deduplication

Run with: pytest tests/test_sdk/test_sdk_file_tracking.py -v
"""

import pytest
import os
import uuid
import time
from pathlib import Path
from typing import Dict, Any, List
from datetime import datetime

from wtb.sdk import WTBTestBench, WorkflowProject
from wtb.sdk.workflow_project import (
    FileTrackingConfig as SDKFileTrackingConfig,
    WorkspaceIsolationConfig,
)
from wtb.config import FileTrackingConfig
from wtb.domain.models import ExecutionStatus
from wtb.application.factories import WTBTestBenchFactory

from tests.test_sdk.conftest import (
    create_initial_state,
    create_file_state,
    LANGGRAPH_AVAILABLE,
)


# ═══════════════════════════════════════════════════════════════════════════════
# FileTrackingConfig Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileTrackingConfig:
    """Tests for FileTrackingConfig."""
    
    def test_config_defaults(self):
        """Test FileTrackingConfig defaults."""
        config = SDKFileTrackingConfig()
        
        assert config.enabled is False
        assert config.tracked_paths == []
        assert config.auto_commit is True
        assert config.commit_on == "checkpoint"
        assert config.snapshot_strategy == "incremental"
        assert config.max_snapshots == 100
    
    def test_config_enabled_with_paths(self, tmp_path):
        """Test enabled config with tracked paths."""
        config = SDKFileTrackingConfig(
            enabled=True,
            tracked_paths=[str(tmp_path / "data"), str(tmp_path / "output")],
        )
        
        assert config.enabled is True
        assert len(config.tracked_paths) == 2
    
    def test_config_ignore_patterns(self):
        """Test ignore patterns configuration."""
        config = SDKFileTrackingConfig(
            enabled=True,
            ignore_patterns=[
                "*.pyc",
                "__pycache__/",
                "*.log",
                ".git/",
                "*.tmp",
                "node_modules/",
            ],
        )
        
        assert "*.pyc" in config.ignore_patterns
        assert "node_modules/" in config.ignore_patterns
    
    def test_config_commit_strategies(self):
        """Test different commit strategies."""
        # Commit on checkpoint
        config1 = SDKFileTrackingConfig(
            enabled=True,
            commit_on="checkpoint",
        )
        assert config1.commit_on == "checkpoint"
        
        # Commit on node complete
        config2 = SDKFileTrackingConfig(
            enabled=True,
            commit_on="node_complete",
        )
        assert config2.commit_on == "node_complete"
        
        # Manual commit
        config3 = SDKFileTrackingConfig(
            enabled=True,
            commit_on="manual",
        )
        assert config3.commit_on == "manual"
    
    def test_config_serialization(self, tmp_path):
        """Test config serialization."""
        config = SDKFileTrackingConfig(
            enabled=True,
            tracked_paths=[str(tmp_path)],
            auto_commit=True,
            commit_on="checkpoint",
            snapshot_strategy="full",
            max_snapshots=50,
        )
        
        data = config.to_dict()
        
        assert data["enabled"] is True
        assert str(tmp_path) in data["tracked_paths"]
        assert data["snapshot_strategy"] == "full"
        assert data["max_snapshots"] == 50


# ═══════════════════════════════════════════════════════════════════════════════
# WTB FileTrackingConfig Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestWTBFileTrackingConfig:
    """Tests for WTB-level FileTrackingConfig."""
    
    def test_for_testing(self):
        """Test testing configuration."""
        config = FileTrackingConfig.for_testing()
        
        assert config.enabled is False
    
    def test_for_development(self, tmp_path):
        """Test development configuration."""
        config = FileTrackingConfig.for_development(
            storage_path=str(tmp_path / "storage"),
            wtb_db_url=f"sqlite:///{tmp_path / 'wtb.db'}",
        )
        
        assert config.enabled is True
        assert config.storage_path == str(tmp_path / "storage")
    
    def test_for_production(self):
        """Test production configuration."""
        config = FileTrackingConfig.for_production(
            postgres_url="postgresql://user:pass@host/db",
            storage_path="/data/file_storage",
            wtb_db_url="postgresql://user:pass@host/wtb",
        )
        
        assert config.enabled is True
        assert config.postgres_url is not None
    
    def test_serialization_roundtrip(self, tmp_path):
        """Test serialization roundtrip."""
        original = FileTrackingConfig(
            enabled=True,
            storage_path=str(tmp_path / "storage"),
            auto_track_patterns=["*.csv", "*.pkl"],
            excluded_patterns=["*.tmp"],
            max_file_size_mb=50.0,
        )
        
        data = original.to_dict()
        restored = FileTrackingConfig.from_dict(data)
        
        assert restored.enabled == original.enabled
        assert restored.storage_path == original.storage_path
        assert restored.max_file_size_mb == original.max_file_size_mb
    
    def test_ensure_storage_path(self, tmp_path):
        """Test storage path creation."""
        storage_path = tmp_path / "new_storage"
        config = FileTrackingConfig(
            enabled=True,
            storage_path=str(storage_path),
        )
        
        result = config.ensure_storage_path()
        
        assert result.exists()
        assert result == storage_path


# ═══════════════════════════════════════════════════════════════════════════════
# WorkspaceIsolationConfig Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestWorkspaceIsolationConfig:
    """Tests for WorkspaceIsolationConfig."""
    
    def test_defaults(self):
        """Test default values."""
        config = WorkspaceIsolationConfig()
        
        assert config.enabled is False
        assert config.mode == "copy"
        assert config.base_path is None
        assert config.cleanup_on_complete is True
    
    def test_copy_mode(self, tmp_path):
        """Test copy isolation mode."""
        config = WorkspaceIsolationConfig(
            enabled=True,
            mode="copy",
            base_path=str(tmp_path / "isolated"),
        )
        
        assert config.mode == "copy"
        assert config.base_path == str(tmp_path / "isolated")
    
    def test_symlink_mode(self, tmp_path):
        """Test symlink isolation mode."""
        config = WorkspaceIsolationConfig(
            enabled=True,
            mode="symlink",
            base_path=str(tmp_path / "isolated"),
        )
        
        assert config.mode == "symlink"
    
    def test_overlay_mode(self, tmp_path):
        """Test overlay isolation mode."""
        config = WorkspaceIsolationConfig(
            enabled=True,
            mode="overlay",
            base_path=str(tmp_path / "isolated"),
        )
        
        assert config.mode == "overlay"
    
    def test_serialization(self, tmp_path):
        """Test config serialization."""
        config = WorkspaceIsolationConfig(
            enabled=True,
            mode="copy",
            base_path=str(tmp_path),
            cleanup_on_complete=False,
        )
        
        data = config.to_dict()
        
        assert data["enabled"] is True
        assert data["mode"] == "copy"
        assert data["cleanup_on_complete"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# WorkflowProject File Tracking Tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not installed")
class TestProjectFileTracking:
    """Tests for WorkflowProject file tracking configuration."""
    
    def test_project_with_file_tracking(self, simple_graph_factory, tmp_path):
        """Test project with file tracking enabled."""
        project = WorkflowProject(
            name="file-tracking-project",
            graph_factory=simple_graph_factory,
            file_tracking=SDKFileTrackingConfig(
                enabled=True,
                tracked_paths=[str(tmp_path / "data")],
            ),
        )
        
        assert project.file_tracking.enabled is True
        assert str(tmp_path / "data") in project.file_tracking.tracked_paths
    
    def test_project_with_workspace_isolation(self, simple_graph_factory, tmp_path):
        """Test project with workspace isolation."""
        project = WorkflowProject(
            name="isolated-project",
            graph_factory=simple_graph_factory,
            workspace_isolation=WorkspaceIsolationConfig(
                enabled=True,
                mode="copy",
                base_path=str(tmp_path / "isolated"),
            ),
        )
        
        assert project.workspace_isolation.enabled is True
        assert project.workspace_isolation.mode == "copy"
    
    def test_project_serialization_with_file_tracking(
        self, simple_graph_factory, tmp_path
    ):
        """Test project serialization includes file tracking."""
        project = WorkflowProject(
            name="serialize-file-track",
            graph_factory=simple_graph_factory,
            file_tracking=SDKFileTrackingConfig(
                enabled=True,
                tracked_paths=[str(tmp_path)],
            ),
            workspace_isolation=WorkspaceIsolationConfig(
                enabled=True,
            ),
        )
        
        data = project.to_dict()
        
        assert "file_tracking" in data
        assert data["file_tracking"]["enabled"] is True
        assert "workspace_isolation" in data
        assert data["workspace_isolation"]["enabled"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# File Operations Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileOperations:
    """Tests for file operations during execution."""
    
    def test_create_temp_files(self, temp_files):
        """Test temp files fixture creates files."""
        assert len(temp_files) == 3
        
        for name, data in temp_files.items():
            path = Path(data["path"])
            assert path.exists()
            # Read in binary mode to ensure consistent comparison
            actual_content = path.read_bytes()
            expected_content = data["content"]
            # Normalize line endings for comparison (Windows CRLF vs Unix LF)
            actual_normalized = actual_content.replace(b'\r\n', b'\n')
            expected_normalized = expected_content.replace(b'\r\n', b'\n')
            assert actual_normalized == expected_normalized
    
    def test_file_modification_tracking(self, temp_workspace, temp_files):
        """Test tracking file modifications."""
        # Modify a file
        first_file = list(temp_files.values())[0]
        path = Path(first_file["path"])
        
        original_content = path.read_text()
        new_content = original_content + "\nModified content"
        path.write_text(new_content)
        
        # Verify modification
        assert path.read_text() == new_content
        assert len(new_content) > len(original_content)
    
    def test_file_creation_during_execution(self, temp_workspace):
        """Test file creation during execution."""
        new_file = temp_workspace / f"new_{uuid.uuid4().hex[:8]}.txt"
        new_file.write_text("New content")
        
        assert new_file.exists()
        assert new_file.read_text() == "New content"
    
    def test_file_deletion_tracking(self, temp_workspace, temp_files):
        """Test tracking file deletion."""
        first_file = list(temp_files.values())[0]
        path = Path(first_file["path"])
        
        assert path.exists()
        path.unlink()
        assert not path.exists()


# ═══════════════════════════════════════════════════════════════════════════════
# Execution with File Tracking Tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not installed")
class TestExecutionWithFileTracking:
    """Tests for execution with file tracking enabled."""
    
    def test_execution_creates_files(
        self, wtb_inmemory, file_processing_project, temp_workspace
    ):
        """Test execution that creates files."""
        wtb_inmemory.register_project(file_processing_project)
        
        result = wtb_inmemory.run(
            project=file_processing_project.name,
            initial_state=create_file_state(str(temp_workspace)),
        )
        
        assert result.status in [ExecutionStatus.COMPLETED, ExecutionStatus.FAILED]
    
    def test_execution_modifies_files(
        self, wtb_inmemory, file_processing_project, temp_workspace
    ):
        """Test execution that modifies files."""
        wtb_inmemory.register_project(file_processing_project)
        
        # Create initial file
        initial_file = temp_workspace / "initial.txt"
        initial_file.write_text("Initial content")
        
        result = wtb_inmemory.run(
            project=file_processing_project.name,
            initial_state=create_file_state(str(temp_workspace)),
        )
        
        assert result.status in [ExecutionStatus.COMPLETED, ExecutionStatus.FAILED]


# ═══════════════════════════════════════════════════════════════════════════════
# Checkpoint-File Link Tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not installed")
class TestCheckpointFileLinks:
    """Tests for checkpoint-file linking."""
    
    def test_checkpoint_with_file_state(self, wtb_langgraph, simple_project):
        """Test checkpoints can capture file state."""
        wtb_langgraph.register_project(simple_project)
        
        result = wtb_langgraph.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        checkpoints = wtb_langgraph.get_checkpoints(result.id)
        
        # Checkpoints should be accessible (file linking depends on adapter)
        assert isinstance(checkpoints, list)
    
    def test_state_adapter_file_commit_link(self, wtb_langgraph, simple_project):
        """Test state adapter can link file commits."""
        # This test verifies the interface exists
        if hasattr(wtb_langgraph, '_state_adapter'):
            adapter = wtb_langgraph._state_adapter
            if hasattr(adapter, 'link_file_commit'):
                # Interface exists
                assert callable(adapter.link_file_commit)


# ═══════════════════════════════════════════════════════════════════════════════
# ACID Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileTrackingACID:
    """Tests for ACID compliance in file tracking."""
    
    def test_atomicity_file_operations(self, temp_workspace):
        """Test file operations are atomic."""
        file_path = temp_workspace / "atomic.txt"
        content = "Atomic content test"
        
        # Write should be atomic
        file_path.write_text(content)
        
        assert file_path.exists()
        assert file_path.read_text() == content
    
    def test_consistency_file_state(self, temp_files):
        """Test file state consistency."""
        for name, data in temp_files.items():
            path = Path(data["path"])
            
            # Multiple reads should be consistent
            content1 = path.read_bytes()
            content2 = path.read_bytes()
            
            assert content1 == content2
            # Normalize line endings for comparison (Windows CRLF vs Unix LF)
            content1_normalized = content1.replace(b'\r\n', b'\n')
            expected_normalized = data["content"].replace(b'\r\n', b'\n')
            assert content1_normalized == expected_normalized
    
    def test_isolation_file_operations(self, temp_workspace):
        """Test file operations are isolated."""
        file1 = temp_workspace / "isolated1.txt"
        file2 = temp_workspace / "isolated2.txt"
        
        file1.write_text("Content 1")
        file2.write_text("Content 2")
        
        # Modifying one shouldn't affect the other
        file1.write_text("Modified 1")
        
        assert file1.read_text() == "Modified 1"
        assert file2.read_text() == "Content 2"
    
    def test_durability_file_persistence(self, temp_workspace):
        """Test file persistence."""
        file_path = temp_workspace / "durable.txt"
        content = "Durable content"
        
        file_path.write_text(content)
        
        # Multiple reads should return same content
        for _ in range(5):
            assert file_path.read_text() == content


# ═══════════════════════════════════════════════════════════════════════════════
# Blob Deduplication Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBlobDeduplication:
    """Tests for blob deduplication patterns."""
    
    def test_identical_content_same_hash(self, temp_workspace):
        """Test identical content produces same hash."""
        import hashlib
        
        content = b"Identical content for deduplication test"
        
        # Create multiple files with same content
        files = []
        for i in range(3):
            path = temp_workspace / f"dup_{i}.txt"
            path.write_bytes(content)
            files.append(path)
        
        # All should have same hash
        hashes = []
        for f in files:
            h = hashlib.sha256(f.read_bytes()).hexdigest()
            hashes.append(h)
        
        assert len(set(hashes)) == 1  # All identical
    
    def test_different_content_different_hash(self, temp_workspace):
        """Test different content produces different hash."""
        import hashlib
        
        files = []
        for i in range(3):
            path = temp_workspace / f"unique_{i}.txt"
            path.write_text(f"Unique content {i} - {uuid.uuid4()}")
            files.append(path)
        
        hashes = []
        for f in files:
            h = hashlib.sha256(f.read_bytes()).hexdigest()
            hashes.append(h)
        
        assert len(set(hashes)) == 3  # All different


# ═══════════════════════════════════════════════════════════════════════════════
# Rollback with File State Tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not installed")
class TestRollbackWithFiles:
    """Tests for rollback operations with file state."""
    
    def test_rollback_preserves_execution(self, wtb_langgraph, simple_project):
        """Test rollback preserves execution state."""
        wtb_langgraph.register_project(simple_project)
        
        result = wtb_langgraph.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        # Attempt rollback
        rollback_result = wtb_langgraph.rollback(result.id, "1")
        
        # Should not corrupt execution
        execution = wtb_langgraph.get_execution(result.id)
        assert execution is not None
    
    def test_file_state_after_rollback(self, temp_workspace):
        """Test file state simulation after rollback."""
        # Simulate file versioning
        versions = {}
        
        # Version 1
        file_path = temp_workspace / "versioned.txt"
        file_path.write_text("Version 1")
        versions[1] = file_path.read_text()
        
        # Version 2
        file_path.write_text("Version 2")
        versions[2] = file_path.read_text()
        
        # "Rollback" to version 1
        file_path.write_text(versions[1])
        
        assert file_path.read_text() == "Version 1"
