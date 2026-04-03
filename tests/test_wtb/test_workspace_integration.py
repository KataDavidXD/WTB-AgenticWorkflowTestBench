"""
Integration Tests for Workspace Isolation System.

Tests end-to-end workspace isolation scenarios including:
- Parallel variant execution isolation
- File tracking integration
- Event bus integration
- Rollback with workspace restore

Design Reference: docs/issues/WORKSPACE_ISOLATION_DESIGN.md
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any
from unittest.mock import MagicMock, patch

import pytest

from wtb.domain.models.workspace import (
    Workspace,
    WorkspaceConfig,
    WorkspaceStrategy,
    compute_venv_spec_hash,
)
from wtb.domain.events.workspace_events import (
    WorkspaceCreatedEvent,
    WorkspaceActivatedEvent,
    WorkspaceDeactivatedEvent,
    WorkspaceCleanedUpEvent,
    ForkRequestedEvent,
    ForkCompletedEvent,
)
from wtb.infrastructure.workspace.manager import (
    WorkspaceManager,
    create_file_link,
)
from wtb.infrastructure.events.wtb_event_bus import WTBEventBus


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def integration_temp_dir():
    """Create a temporary directory for integration tests."""
    temp = tempfile.mkdtemp(prefix="wtb_integration_workspace_")
    yield Path(temp)
    shutil.rmtree(temp, ignore_errors=True)


@pytest.fixture
def event_bus():
    """Create a real event bus for integration tests."""
    return WTBEventBus(max_history=1000)


@pytest.fixture
def workspace_manager_integration(integration_temp_dir, event_bus):
    """Create a workspace manager with real event bus."""
    config = WorkspaceConfig(
        enabled=True,
        strategy=WorkspaceStrategy.WORKSPACE,
        base_dir=integration_temp_dir / "workspaces",
        cleanup_on_complete=True,
        preserve_on_failure=True,
        use_hard_links=True,
        orphan_cleanup_grace_hours=0.001,
    )
    
    manager = WorkspaceManager(
        config=config,
        event_bus=event_bus,
        session_id="integration-test-session",
    )
    
    yield manager
    
    # Cleanup
    for ws_id in list(manager._workspaces.keys()):
        try:
            manager.cleanup_workspace(ws_id)
        except Exception:
            pass


@pytest.fixture
def sample_project(integration_temp_dir):
    """Create a sample project structure for testing."""
    project_dir = integration_temp_dir / "sample_project"
    project_dir.mkdir(parents=True)
    
    # Create data files
    data_dir = project_dir / "data"
    data_dir.mkdir()
    (data_dir / "input.csv").write_text("id,value\n1,100\n2,200\n3,300\n")
    (data_dir / "config.json").write_text('{"model": "bm25", "k": 10}')
    
    # Create model files
    models_dir = project_dir / "models"
    models_dir.mkdir()
    (models_dir / "embeddings.pkl").write_bytes(b"fake embedding data " * 100)
    
    # Create output directory
    output_dir = project_dir / "output"
    output_dir.mkdir()
    
    return project_dir


# ═══════════════════════════════════════════════════════════════════════════════
# Parallel Execution Isolation Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestParallelExecutionIsolation:
    """Tests for parallel variant execution isolation."""
    
    def test_parallel_variants_isolated_file_writes(
        self,
        workspace_manager_integration,
        sample_project,
    ):
        """Test that parallel variants write to isolated directories."""
        manager = workspace_manager_integration
        
        # Create workspaces for 3 variants
        variants = ["bm25", "dense", "hybrid"]
        workspaces = []
        
        for variant in variants:
            ws = manager.create_workspace(
                batch_id="batch-parallel-001",
                variant_name=variant,
                execution_id=f"exec-{variant}",
                source_paths=[sample_project / "data"],
            )
            workspaces.append(ws)
        
        # Simulate parallel writes (each variant writes to its output)
        def write_output(workspace: Workspace, variant_name: str):
            output_file = workspace.output_dir / "results.json"
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_text(json.dumps({
                "variant": variant_name,
                "score": 0.85,
                "workspace_id": workspace.workspace_id,
            }))
        
        # Execute in parallel
        threads = []
        for ws, variant in zip(workspaces, variants):
            t = threading.Thread(target=write_output, args=(ws, variant))
            threads.append(t)
            t.start()
        
        for t in threads:
            t.join()
        
        # Verify each workspace has its own output
        for ws, variant in zip(workspaces, variants):
            output_file = ws.output_dir / "results.json"
            assert output_file.exists()
            
            data = json.loads(output_file.read_text())
            assert data["variant"] == variant
            assert data["workspace_id"] == ws.workspace_id
        
        # Verify no cross-contamination
        all_results = []
        for ws in workspaces:
            output_file = ws.output_dir / "results.json"
            all_results.append(json.loads(output_file.read_text()))
        
        # Each result should have unique variant
        variants_in_results = [r["variant"] for r in all_results]
        assert len(set(variants_in_results)) == 3
    
    def test_parallel_variants_share_input_efficiently(
        self,
        workspace_manager_integration,
        sample_project,
    ):
        """Test that input files are shared efficiently via hard links."""
        manager = workspace_manager_integration
        
        # Create multiple workspaces from same source
        workspaces = []
        for i in range(5):
            ws = manager.create_workspace(
                batch_id="batch-shared-input",
                variant_name=f"variant_{i}",
                execution_id=f"exec-{i}",
                source_paths=[sample_project / "data" / "input.csv"],
            )
            workspaces.append(ws)
        
        # All should have the input file
        for ws in workspaces:
            input_file = ws.input_dir / "input.csv"
            assert input_file.exists()
            
            # Content should be identical
            content = input_file.read_text()
            assert "id,value" in content
        
        # On same partition, hard links should share inode (no extra space)
        # We can't easily verify this in a portable way, but we can check
        # the files exist and are readable


class TestEventBusIntegration:
    """Tests for event bus integration."""
    
    def test_workspace_lifecycle_events(
        self,
        workspace_manager_integration,
        event_bus,
        sample_project,
    ):
        """Test complete workspace lifecycle events."""
        manager = workspace_manager_integration
        
        # Subscribe to events
        received_events = []
        
        def capture_event(event):
            received_events.append(event)
        
        event_bus.subscribe(WorkspaceCreatedEvent, capture_event)
        event_bus.subscribe(WorkspaceCleanedUpEvent, capture_event)
        
        # Create workspace
        ws = manager.create_workspace(
            batch_id="batch-events",
            variant_name="test",
            execution_id="exec-events",
            source_paths=[sample_project / "data"],
        )
        
        # Verify creation event
        create_events = [e for e in received_events if isinstance(e, WorkspaceCreatedEvent)]
        assert len(create_events) == 1
        assert create_events[0].workspace_id == ws.workspace_id
        assert create_events[0].batch_test_id == "batch-events"
        
        # Cleanup workspace
        manager.cleanup_workspace(ws.workspace_id, reason="test_complete")
        
        # Verify cleanup event
        cleanup_events = [e for e in received_events if isinstance(e, WorkspaceCleanedUpEvent)]
        assert len(cleanup_events) == 1
        assert cleanup_events[0].workspace_id == ws.workspace_id
        assert cleanup_events[0].reason == "test_complete"
    
    def test_event_history_queryable(
        self,
        workspace_manager_integration,
        event_bus,
    ):
        """Test that events can be queried from history."""
        manager = workspace_manager_integration
        
        # Create multiple workspaces
        for i in range(3):
            manager.create_workspace(
                batch_id="batch-history",
                variant_name=f"v{i}",
                execution_id=f"exec-{i}",
            )
        
        # Query history
        history = event_bus.get_history(
            event_type=WorkspaceCreatedEvent,
            limit=10,
        )
        
        assert len(history) >= 3
        
        # Most recent first
        for event in history:
            assert isinstance(event, WorkspaceCreatedEvent)


class TestWorkspaceActivation:
    """Tests for workspace activation (CWD management)."""
    
    def test_workspace_activation_changes_cwd(
        self,
        workspace_manager_integration,
    ):
        """Test that activation changes working directory."""
        manager = workspace_manager_integration
        original_cwd = Path.cwd()
        
        ws = manager.create_workspace(
            batch_id="batch-activation",
            variant_name="test",
            execution_id="exec-activation",
        )
        
        try:
            # Activate
            ws.activate()
            assert Path.cwd() == ws.root_path
            assert ws.is_active
            
            # Write file relative to CWD
            (Path("test_output.txt")).write_text("test")
            assert (ws.root_path / "test_output.txt").exists()
            
            # Deactivate
            ws.deactivate()
            assert Path.cwd() == original_cwd
            assert not ws.is_active
            
        finally:
            os.chdir(original_cwd)
    
    def test_nested_activation_not_allowed(
        self,
        workspace_manager_integration,
    ):
        """Test that nested activation is handled gracefully."""
        manager = workspace_manager_integration
        original_cwd = Path.cwd()
        
        ws = manager.create_workspace(
            batch_id="batch-nested",
            variant_name="test",
            execution_id="exec-nested",
        )
        
        try:
            ws.activate()
            
            # Second activation should be no-op
            ws.activate()
            assert ws.is_active
            
            ws.deactivate()
            assert not ws.is_active
            
            # Second deactivation should be no-op
            ws.deactivate()
            assert not ws.is_active
            
        finally:
            os.chdir(original_cwd)


class TestBranchWorkspace:
    """Tests for fork/branch workspace creation."""
    
    def test_branch_workspace_inherits_source(
        self,
        workspace_manager_integration,
        sample_project,
    ):
        """Test that branch workspace inherits source files."""
        manager = workspace_manager_integration
        
        # Create source workspace
        source_ws = manager.create_workspace(
            batch_id="batch-branch",
            variant_name="original",
            execution_id="exec-original",
            source_paths=[sample_project / "data"],
        )
        
        # Write some output to source
        (source_ws.output_dir / "checkpoint_data.json").write_text('{"step": 5}')
        
        # Create branch
        branch_ws = manager.create_branch_workspace(
            source_workspace_id=source_ws.workspace_id,
            fork_checkpoint_id=5,
            new_execution_id="exec-branch",
            file_commit_id="commit-abc123",
        )
        
        # Branch should have its own identity
        assert branch_ws.workspace_id != source_ws.workspace_id
        assert branch_ws.execution_id == "exec-branch"
        
        # Branch should have source files
        assert (branch_ws.input_dir / "data" / "input.csv").exists()
        
        # Branch should have commit reference
        assert branch_ws.source_snapshot_commit_id == "commit-abc123"
    
    def test_branch_independent_of_source(
        self,
        workspace_manager_integration,
        sample_project,
    ):
        """Test that branch modifications don't affect source."""
        manager = workspace_manager_integration
        
        # Create source workspace
        source_ws = manager.create_workspace(
            batch_id="batch-independent",
            variant_name="source",
            execution_id="exec-source",
            source_paths=[sample_project / "data"],
        )
        
        # Create branch
        branch_ws = manager.create_branch_workspace(
            source_workspace_id=source_ws.workspace_id,
            fork_checkpoint_id=1,
            new_execution_id="exec-branch",
        )
        
        # Modify branch output
        (branch_ws.output_dir / "branch_result.txt").write_text("branch data")
        
        # Source should not have branch's output
        assert not (source_ws.output_dir / "branch_result.txt").exists()


class TestBatchCleanup:
    """Tests for batch-level cleanup."""
    
    def test_batch_cleanup_removes_all_workspaces(
        self,
        workspace_manager_integration,
    ):
        """Test that batch cleanup removes all related workspaces."""
        manager = workspace_manager_integration
        
        # Create multiple workspaces in batch
        workspace_ids = []
        for i in range(5):
            ws = manager.create_workspace(
                batch_id="batch-cleanup-all",
                variant_name=f"v{i}",
                execution_id=f"exec-{i}",
            )
            workspace_ids.append(ws.workspace_id)
        
        # Verify all exist
        assert len(manager.list_workspaces(batch_id="batch-cleanup-all")) == 5
        
        # Cleanup batch
        count = manager.cleanup_batch("batch-cleanup-all")
        
        assert count == 5
        assert len(manager.list_workspaces(batch_id="batch-cleanup-all")) == 0
        
        # All workspace directories should be removed
        for ws_id in workspace_ids:
            assert manager.get_workspace(ws_id) is None
    
    def test_batch_cleanup_preserves_other_batches(
        self,
        workspace_manager_integration,
    ):
        """Test that batch cleanup doesn't affect other batches."""
        manager = workspace_manager_integration
        
        # Create workspaces in two batches
        for i in range(3):
            manager.create_workspace(
                batch_id="batch-A",
                variant_name=f"v{i}",
                execution_id=f"exec-A-{i}",
            )
        
        for i in range(3):
            manager.create_workspace(
                batch_id="batch-B",
                variant_name=f"v{i}",
                execution_id=f"exec-B-{i}",
            )
        
        # Cleanup batch A
        manager.cleanup_batch("batch-A")
        
        # Batch B should be unaffected
        assert len(manager.list_workspaces(batch_id="batch-A")) == 0
        assert len(manager.list_workspaces(batch_id="batch-B")) == 3


class TestVenvSpecHashIntegration:
    """Tests for venv spec hash in workspace context."""
    
    def test_workspace_venv_spec_tracking(
        self,
        workspace_manager_integration,
    ):
        """Test that workspace tracks venv spec hash."""
        manager = workspace_manager_integration
        
        ws = manager.create_workspace(
            batch_id="batch-venv",
            variant_name="test",
            execution_id="exec-venv",
        )
        
        # Set venv spec hash
        venv_hash = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain", "faiss-cpu"],
        )
        ws.venv_spec_hash = venv_hash
        ws.save_metadata()
        
        # Load from metadata
        loaded = Workspace.load_from_path(ws.root_path)
        assert loaded is not None
        assert loaded.venv_spec_hash == venv_hash
    
    def test_venv_hash_invalidation_detection(self):
        """Test detecting venv spec changes."""
        original_hash = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain==0.1.0"],
        )
        
        # Upgrade dependency
        new_hash = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain==0.2.0"],
        )
        
        # Hashes should differ
        assert original_hash != new_hash
        
        # Same spec should match
        same_hash = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain==0.1.0"],
        )
        assert original_hash == same_hash


class TestStatistics:
    """Tests for workspace manager statistics."""
    
    def test_stats_accurate(
        self,
        workspace_manager_integration,
        sample_project,
    ):
        """Test that statistics are accurate."""
        manager = workspace_manager_integration
        
        # Create workspaces
        for i in range(3):
            manager.create_workspace(
                batch_id="batch-stats",
                variant_name=f"v{i}",
                execution_id=f"exec-{i}",
                source_paths=[sample_project / "data"],
            )
        
        stats = manager.get_stats()
        
        assert stats["active_workspaces"] == 3
        assert stats["active_batches"] == 1
        assert stats["total_size_bytes"] >= 0
        assert "session_id" in stats


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
