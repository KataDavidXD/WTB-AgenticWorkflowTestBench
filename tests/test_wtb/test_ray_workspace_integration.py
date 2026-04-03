"""
Integration Tests for Ray Batch Runner + Workspace Isolation.

Tests the integration between RayBatchTestRunner and WorkspaceManager
for parallel variant execution with file system isolation.

Design Reference: docs/issues/WORKSPACE_ISOLATION_DESIGN.md (Section 13.5)
"""

import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List
from unittest.mock import MagicMock, patch

import pytest

from wtb.domain.models.workspace import (
    Workspace,
    WorkspaceConfig,
    WorkspaceStrategy,
)
from wtb.domain.events.workspace_events import (
    WorkspaceCreatedEvent,
    WorkspaceCleanedUpEvent,
)
from wtb.infrastructure.workspace.manager import (
    WorkspaceManager,
    create_file_link,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def ray_test_temp_dir():
    """Create a temporary directory for Ray workspace tests."""
    temp = tempfile.mkdtemp(prefix="wtb_ray_workspace_")
    yield Path(temp)
    shutil.rmtree(temp, ignore_errors=True)


@pytest.fixture
def workspace_config_dict(ray_test_temp_dir):
    """Create a workspace config dict for testing."""
    return {
        "enabled": True,
        "strategy": "workspace",
        "base_dir": str(ray_test_temp_dir / "workspaces"),
        "cleanup_on_complete": True,
        "preserve_on_failure": True,
        "use_hard_links": True,
    }


@pytest.fixture
def filetracker_config_dict(ray_test_temp_dir):
    """Create a filetracker config dict for testing."""
    return {
        "enabled": True,
        "tracked_paths": [str(ray_test_temp_dir / "source")],
    }


@pytest.fixture
def sample_source_files(ray_test_temp_dir):
    """Create sample source files for testing."""
    source_dir = ray_test_temp_dir / "source"
    source_dir.mkdir(parents=True)
    
    # Create test files
    (source_dir / "data.csv").write_text("id,value\n1,100\n2,200\n")
    (source_dir / "config.json").write_text('{"model": "test"}')
    
    return source_dir


# ═══════════════════════════════════════════════════════════════════════════════
# Unit Tests for Workspace Integration Components
# ═══════════════════════════════════════════════════════════════════════════════


class TestWorkspaceConfigSerialization:
    """Tests for workspace config serialization for Ray transmission."""
    
    def test_workspace_config_to_dict(self, ray_test_temp_dir):
        """Test that workspace config can be serialized for Ray."""
        config = WorkspaceConfig(
            enabled=True,
            strategy=WorkspaceStrategy.WORKSPACE,
            base_dir=ray_test_temp_dir,
            cleanup_on_complete=True,
            preserve_on_failure=True,
            use_hard_links=True,
        )
        
        # Serialize to dict (for Ray transmission)
        config_dict = {
            "enabled": config.enabled,
            "strategy": config.strategy.value,
            "base_dir": str(config.base_dir) if config.base_dir else None,
            "cleanup_on_complete": config.cleanup_on_complete,
            "preserve_on_failure": config.preserve_on_failure,
            "use_hard_links": config.use_hard_links,
        }
        
        assert config_dict["enabled"] is True
        assert config_dict["strategy"] == "workspace"
        assert config_dict["base_dir"] == str(ray_test_temp_dir)
    
    def test_workspace_data_round_trip(self, ray_test_temp_dir):
        """Test workspace serialization round trip."""
        workspace = Workspace(
            workspace_id="ws-test-001",
            batch_test_id="batch-001",
            variant_name="bm25",
            execution_id="exec-001",
            root_path=ray_test_temp_dir / "workspace",
            strategy=WorkspaceStrategy.WORKSPACE,
        )
        
        # Serialize
        data = workspace.to_dict()
        
        # Deserialize
        restored = Workspace.from_dict(data)
        
        assert restored.workspace_id == workspace.workspace_id
        assert restored.batch_test_id == workspace.batch_test_id
        assert restored.variant_name == workspace.variant_name
        assert restored.execution_id == workspace.execution_id
        assert restored.root_path == workspace.root_path


class TestWorkspaceManagerForRay:
    """Tests for WorkspaceManager used by Ray actors."""
    
    def test_create_workspace_for_variant(
        self, 
        ray_test_temp_dir, 
        sample_source_files,
    ):
        """Test creating workspace for a variant execution."""
        config = WorkspaceConfig(
            enabled=True,
            base_dir=ray_test_temp_dir / "workspaces",
        )
        manager = WorkspaceManager(config=config)
        
        workspace = manager.create_workspace(
            batch_id="batch-001",
            variant_name="bm25",
            execution_id="exec-001",
            source_paths=[sample_source_files],
        )
        
        assert workspace.workspace_id is not None
        assert workspace.batch_test_id == "batch-001"
        assert workspace.variant_name == "bm25"
        assert workspace.root_path.exists()
        assert workspace.input_dir.exists()
        assert workspace.output_dir.exists()
    
    def test_multiple_variants_isolated(
        self,
        ray_test_temp_dir,
        sample_source_files,
    ):
        """Test that multiple variant workspaces are isolated."""
        config = WorkspaceConfig(
            enabled=True,
            base_dir=ray_test_temp_dir / "workspaces",
        )
        manager = WorkspaceManager(config=config)
        
        # Create workspaces for 3 variants
        variants = ["bm25", "dense", "hybrid"]
        workspaces = []
        
        for variant in variants:
            ws = manager.create_workspace(
                batch_id="batch-001",
                variant_name=variant,
                execution_id=f"exec-{variant}",
                source_paths=[sample_source_files / "data.csv"],
            )
            workspaces.append(ws)
        
        # Each should have unique workspace_id and root_path
        workspace_ids = [ws.workspace_id for ws in workspaces]
        root_paths = [ws.root_path for ws in workspaces]
        
        assert len(set(workspace_ids)) == 3, "Workspace IDs must be unique"
        assert len(set(root_paths)) == 3, "Root paths must be unique"
        
        # Write to each workspace's output
        for ws in workspaces:
            output_file = ws.output_dir / "result.json"
            output_file.write_text(json.dumps({"workspace": ws.workspace_id}))
        
        # Verify isolation - each file should contain its own workspace_id
        for ws in workspaces:
            output_file = ws.output_dir / "result.json"
            data = json.loads(output_file.read_text())
            assert data["workspace"] == ws.workspace_id
    
    def test_batch_cleanup(
        self,
        ray_test_temp_dir,
        sample_source_files,
    ):
        """Test batch cleanup removes all variant workspaces."""
        config = WorkspaceConfig(
            enabled=True,
            base_dir=ray_test_temp_dir / "workspaces",
        )
        manager = WorkspaceManager(config=config)
        
        # Create workspaces
        for i in range(5):
            manager.create_workspace(
                batch_id="batch-cleanup",
                variant_name=f"v{i}",
                execution_id=f"exec-{i}",
            )
        
        assert len(manager.list_workspaces(batch_id="batch-cleanup")) == 5
        
        # Cleanup batch
        cleaned = manager.cleanup_batch("batch-cleanup")
        
        assert cleaned == 5
        assert len(manager.list_workspaces(batch_id="batch-cleanup")) == 0


class TestWorkspaceActivation:
    """Tests for workspace activation during execution."""
    
    def test_workspace_activation_context(
        self,
        ray_test_temp_dir,
    ):
        """Test workspace activation changes CWD correctly."""
        workspace_root = ray_test_temp_dir / "workspace"
        workspace_root.mkdir(parents=True)
        (workspace_root / "input").mkdir()
        (workspace_root / "output").mkdir()
        
        workspace = Workspace(
            workspace_id="ws-activation-test",
            batch_test_id="batch-001",
            variant_name="test",
            execution_id="exec-001",
            root_path=workspace_root,
        )
        
        original_cwd = Path.cwd()
        
        try:
            # Activate
            workspace.activate()
            assert workspace.is_active
            assert Path.cwd() == workspace_root
            
            # Write file relative to CWD (should go to workspace)
            Path("test_output.txt").write_text("test data")
            assert (workspace_root / "test_output.txt").exists()
            
            # Deactivate
            workspace.deactivate()
            assert not workspace.is_active
            assert Path.cwd() == original_cwd
            
        finally:
            os.chdir(original_cwd)
    
    def test_workspace_activation_idempotent(
        self,
        ray_test_temp_dir,
    ):
        """Test that double activation/deactivation is safe."""
        workspace_root = ray_test_temp_dir / "workspace"
        workspace_root.mkdir(parents=True)
        
        workspace = Workspace(
            workspace_id="ws-idempotent",
            batch_test_id="batch-001",
            variant_name="test",
            execution_id="exec-001",
            root_path=workspace_root,
        )
        
        original_cwd = Path.cwd()
        
        try:
            # Double activate
            workspace.activate()
            workspace.activate()  # Should be no-op
            assert workspace.is_active
            
            # Double deactivate
            workspace.deactivate()
            workspace.deactivate()  # Should be no-op
            assert not workspace.is_active
            
        finally:
            os.chdir(original_cwd)


class TestWorkspaceForVariantExecution:
    """Tests simulating variant execution with workspace isolation."""
    
    def test_simulate_variant_execution_isolation(
        self,
        ray_test_temp_dir,
        sample_source_files,
    ):
        """
        Simulate what happens during variant execution:
        1. Create workspace
        2. Activate workspace
        3. Execute (write output files)
        4. Deactivate workspace
        """
        config = WorkspaceConfig(
            enabled=True,
            base_dir=ray_test_temp_dir / "workspaces",
        )
        manager = WorkspaceManager(config=config)
        
        original_cwd = Path.cwd()
        
        try:
            # Simulate 3 parallel variant executions
            results = []
            
            for variant in ["bm25", "dense", "hybrid"]:
                # 1. Create workspace
                workspace = manager.create_workspace(
                    batch_id="batch-sim",
                    variant_name=variant,
                    execution_id=f"exec-{variant}",
                    source_paths=[sample_source_files / "data.csv"],
                )
                
                # 2. Activate
                workspace.activate()
                
                try:
                    # 3. Execute - read input, write output
                    input_file = workspace.input_dir / "data.csv"
                    assert input_file.exists()
                    
                    # Process and write result
                    output_file = workspace.output_dir / "result.json"
                    output_file.parent.mkdir(parents=True, exist_ok=True)
                    output_file.write_text(json.dumps({
                        "variant": variant,
                        "processed": True,
                        "workspace_id": workspace.workspace_id,
                    }))
                    
                    results.append({
                        "variant": variant,
                        "workspace_id": workspace.workspace_id,
                        "output_path": str(output_file),
                    })
                    
                finally:
                    # 4. Deactivate
                    workspace.deactivate()
            
            # Verify all results are isolated
            assert len(results) == 3
            
            for result in results:
                output_path = Path(result["output_path"])
                assert output_path.exists()
                
                data = json.loads(output_path.read_text())
                assert data["variant"] == result["variant"]
                assert data["workspace_id"] == result["workspace_id"]
            
        finally:
            os.chdir(original_cwd)
    
    def test_workspace_survives_execution_failure(
        self,
        ray_test_temp_dir,
    ):
        """Test that workspace is preserved on execution failure."""
        config = WorkspaceConfig(
            enabled=True,
            base_dir=ray_test_temp_dir / "workspaces",
            preserve_on_failure=True,
        )
        manager = WorkspaceManager(config=config)
        
        workspace = manager.create_workspace(
            batch_id="batch-fail",
            variant_name="failing",
            execution_id="exec-fail",
        )
        
        original_cwd = Path.cwd()
        
        try:
            workspace.activate()
            
            # Write some partial output
            (workspace.output_dir / "partial.txt").write_text("partial data")
            
            # Simulate failure
            try:
                raise RuntimeError("Simulated execution failure")
            except RuntimeError:
                pass
            
        finally:
            workspace.deactivate()
            os.chdir(original_cwd)
        
        # Workspace should still exist with partial output
        assert workspace.root_path.exists()
        assert (workspace.output_dir / "partial.txt").exists()


# ═══════════════════════════════════════════════════════════════════════════════
# Mock Ray Tests (No actual Ray cluster required)
# ═══════════════════════════════════════════════════════════════════════════════


class TestRayWorkspaceIntegrationMock:
    """
    Tests for Ray + Workspace integration using mocks.
    
    These tests verify the integration logic without requiring
    an actual Ray cluster.
    """
    
    def test_workspace_data_passable_to_actor(
        self,
        ray_test_temp_dir,
    ):
        """Test that workspace data can be passed to Ray actors."""
        # Create workspace
        workspace_root = ray_test_temp_dir / "workspace"
        workspace_root.mkdir(parents=True)
        
        workspace = Workspace(
            workspace_id="ws-ray-001",
            batch_test_id="batch-001",
            variant_name="test",
            execution_id="exec-001",
            root_path=workspace_root,
        )
        
        # Serialize for Ray transmission
        workspace_data = workspace.to_dict()
        
        # Verify it's JSON serializable (Ray requirement)
        json_str = json.dumps(workspace_data)
        assert json_str is not None
        
        # Deserialize (simulating actor receiving data)
        restored_data = json.loads(json_str)
        restored_workspace = Workspace.from_dict(restored_data)
        
        assert restored_workspace.workspace_id == workspace.workspace_id
        assert restored_workspace.root_path == workspace.root_path


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
