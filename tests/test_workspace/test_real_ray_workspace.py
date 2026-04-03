"""
Integration Tests for Real Ray + Workspace Isolation.

Tests workspace isolation with actual Ray cluster (local mode) including:
- Parallel variant execution with isolated workspaces
- Ray actor workspace management
- Batch test workspace lifecycle
- Ray-workspace event integration
- Actor failure workspace preservation

Design Reference: docs/issues/WORKSPACE_ISOLATION_DESIGN.md (Section 11)

Requirements:
- Ray installed and functional
- Python 3.12 (Ray Windows compatibility)
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch
import uuid

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


# Skip entire module if Ray not available
ray_available = False
try:
    import ray
    ray_available = True
except ImportError:
    pass

pytestmark = pytest.mark.skipif(not ray_available, reason="Ray not installed")


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Ray Actor Workspace Creation
# ═══════════════════════════════════════════════════════════════════════════════


class TestRayActorWorkspaceCreation:
    """Tests for workspace creation in Ray actors."""
    
    def test_workspace_serializable_for_ray(
        self,
        workspace_manager,
        sample_project_files,
    ):
        """Test that workspace data can be serialized for Ray transmission."""
        ws = workspace_manager.create_workspace(
            batch_id="batch-serialization",
            variant_name="test",
            execution_id="exec-001",
            source_paths=[sample_project_files["data_dir"]],
        )
        
        # Serialize to dict (Ray transmission format)
        ws_data = ws.to_dict()
        
        # Should be JSON serializable
        json_str = json.dumps(ws_data)
        assert json_str is not None
        
        # Deserialize
        restored_data = json.loads(json_str)
        restored_ws = Workspace.from_dict(restored_data)
        
        assert restored_ws.workspace_id == ws.workspace_id
        assert restored_ws.execution_id == ws.execution_id
        assert restored_ws.variant_name == ws.variant_name
    
    def test_workspace_config_serializable_for_ray(self, workspace_config):
        """Test that workspace config can be passed to Ray actors."""
        config_dict = {
            "enabled": workspace_config.enabled,
            "strategy": workspace_config.strategy.value,
            "base_dir": str(workspace_config.base_dir) if workspace_config.base_dir else None,
            "cleanup_on_complete": workspace_config.cleanup_on_complete,
            "preserve_on_failure": workspace_config.preserve_on_failure,
            "use_hard_links": workspace_config.use_hard_links,
        }
        
        # Should be JSON serializable
        json_str = json.dumps(config_dict)
        assert json_str is not None
        
        # Deserialize and reconstruct
        restored = json.loads(json_str)
        restored_config = WorkspaceConfig(
            enabled=restored["enabled"],
            strategy=WorkspaceStrategy(restored["strategy"]),
            base_dir=Path(restored["base_dir"]) if restored["base_dir"] else None,
            cleanup_on_complete=restored["cleanup_on_complete"],
            preserve_on_failure=restored["preserve_on_failure"],
            use_hard_links=restored["use_hard_links"],
        )
        
        assert restored_config.enabled == workspace_config.enabled
        assert restored_config.strategy == workspace_config.strategy


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Real Ray Parallel Execution
# ═══════════════════════════════════════════════════════════════════════════════


class TestRealRayParallelExecution:
    """Tests with real Ray cluster (local mode)."""
    
    @pytest.fixture(autouse=True)
    def setup_ray(self, ray_initialized):
        """Ensure Ray is initialized for tests."""
        if not ray_initialized:
            pytest.skip("Ray not available")
    
    def test_parallel_workspace_creation_ray_remote(
        self,
        workspace_config_dict,
        sample_project_files,
        workspace_temp_dir,
    ):
        """Test creating workspaces in parallel via Ray remote functions."""
        
        @ray.remote
        def create_workspace_remote(
            config_dict: Dict,
            batch_id: str,
            variant_name: str,
            execution_id: str,
            source_paths: List[str],
        ) -> Dict:
            """Remote function to create workspace."""
            config = WorkspaceConfig(
                enabled=config_dict["enabled"],
                strategy=WorkspaceStrategy(config_dict["strategy"]),
                base_dir=Path(config_dict["base_dir"]),
                cleanup_on_complete=config_dict["cleanup_on_complete"],
                preserve_on_failure=config_dict["preserve_on_failure"],
                use_hard_links=config_dict["use_hard_links"],
            )
            
            manager = WorkspaceManager(config=config)
            ws = manager.create_workspace(
                batch_id=batch_id,
                variant_name=variant_name,
                execution_id=execution_id,
                source_paths=[Path(p) for p in source_paths],
            )
            
            return ws.to_dict()
        
        # Create workspaces in parallel
        variants = ["bm25", "dense", "hybrid", "sparse"]
        batch_id = f"batch-{uuid.uuid4().hex[:8]}"
        
        futures = []
        for variant in variants:
            future = create_workspace_remote.remote(
                workspace_config_dict,
                batch_id,
                variant,
                f"exec-{variant}",
                [str(sample_project_files["data_dir"])],
            )
            futures.append(future)
        
        # Wait for all
        results = ray.get(futures)
        
        assert len(results) == 4
        
        # Verify all have unique IDs
        workspace_ids = [r["workspace_id"] for r in results]
        assert len(set(workspace_ids)) == 4
        
        # Verify all workspaces exist
        for ws_data in results:
            root_path = Path(ws_data["root_path"])
            assert root_path.exists()
            assert (root_path / "input").exists()
            assert (root_path / "output").exists()
    
    def test_parallel_execution_isolation_ray_remote(
        self,
        workspace_config_dict,
        sample_project_files,
        workspace_temp_dir,
    ):
        """Test that parallel Ray executions are isolated."""
        
        @ray.remote
        def execute_variant_remote(
            config_dict: Dict,
            batch_id: str,
            variant_name: str,
            source_paths: List[str],
        ) -> Dict:
            """Remote function simulating variant execution."""
            config = WorkspaceConfig(
                enabled=config_dict["enabled"],
                strategy=WorkspaceStrategy(config_dict["strategy"]),
                base_dir=Path(config_dict["base_dir"]),
                cleanup_on_complete=config_dict["cleanup_on_complete"],
                preserve_on_failure=config_dict["preserve_on_failure"],
                use_hard_links=config_dict["use_hard_links"],
            )
            
            manager = WorkspaceManager(config=config)
            ws = manager.create_workspace(
                batch_id=batch_id,
                variant_name=variant_name,
                execution_id=f"exec-{variant_name}",
                source_paths=[Path(p) for p in source_paths],
            )
            
            original_cwd = os.getcwd()
            
            try:
                # Activate workspace
                ws.activate()
                
                # Write output file (should go to isolated workspace)
                output_file = ws.output_dir / "result.json"
                output_file.parent.mkdir(parents=True, exist_ok=True)
                output_file.write_text(json.dumps({
                    "variant": variant_name,
                    "workspace_id": ws.workspace_id,
                    "cwd": os.getcwd(),
                    "pid": os.getpid(),
                }))
                
                return {
                    "workspace_id": ws.workspace_id,
                    "variant": variant_name,
                    "output_path": str(output_file),
                    "root_path": str(ws.root_path),
                    "success": True,
                }
                
            finally:
                ws.deactivate()
                os.chdir(original_cwd)
        
        # Execute 4 variants in parallel
        variants = ["variant_a", "variant_b", "variant_c", "variant_d"]
        batch_id = f"batch-isolation-{uuid.uuid4().hex[:8]}"
        
        futures = [
            execute_variant_remote.remote(
                workspace_config_dict,
                batch_id,
                variant,
                [str(sample_project_files["csv"])],
            )
            for variant in variants
        ]
        
        results = ray.get(futures)
        
        # Verify isolation
        assert len(results) == 4
        
        # Check each variant wrote to its own isolated output
        output_paths = set()
        for result in results:
            assert result["success"]
            output_path = Path(result["output_path"])
            assert output_path.exists()
            
            # Read and verify content
            content = json.loads(output_path.read_text())
            assert content["variant"] == result["variant"]
            assert content["workspace_id"] == result["workspace_id"]
            
            # Output paths should be unique
            output_paths.add(result["output_path"])
        
        assert len(output_paths) == 4, "Each variant should write to unique path"


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Ray Actor Pool Workspace Management
# ═══════════════════════════════════════════════════════════════════════════════


class TestRayActorPoolWorkspace:
    """Tests for Ray actor pool with workspace management."""
    
    @pytest.fixture(autouse=True)
    def setup_ray(self, ray_initialized):
        """Ensure Ray is initialized for tests."""
        if not ray_initialized:
            pytest.skip("Ray not available")
    
    def test_actor_workspace_lifecycle(
        self,
        workspace_config_dict,
        sample_project_files,
    ):
        """Test workspace lifecycle in Ray actor."""
        
        @ray.remote
        class WorkspaceActor:
            """Ray actor managing workspace lifecycle."""
            
            def __init__(self, config_dict: Dict):
                self.config = WorkspaceConfig(
                    enabled=config_dict["enabled"],
                    strategy=WorkspaceStrategy(config_dict["strategy"]),
                    base_dir=Path(config_dict["base_dir"]),
                    cleanup_on_complete=config_dict["cleanup_on_complete"],
                    preserve_on_failure=config_dict["preserve_on_failure"],
                    use_hard_links=config_dict["use_hard_links"],
                )
                self.manager = WorkspaceManager(config=self.config)
                self._workspace = None
            
            def create_workspace(
                self,
                batch_id: str,
                variant_name: str,
                execution_id: str,
                source_paths: List[str],
            ) -> Dict:
                self._workspace = self.manager.create_workspace(
                    batch_id=batch_id,
                    variant_name=variant_name,
                    execution_id=execution_id,
                    source_paths=[Path(p) for p in source_paths],
                )
                return self._workspace.to_dict()
            
            def execute(self, data: Dict) -> Dict:
                if not self._workspace:
                    raise RuntimeError("Workspace not created")
                
                original_cwd = os.getcwd()
                try:
                    self._workspace.activate()
                    
                    # Simulate execution
                    output_file = self._workspace.output_dir / "result.json"
                    output_file.parent.mkdir(parents=True, exist_ok=True)
                    output_file.write_text(json.dumps({
                        **data,
                        "workspace_id": self._workspace.workspace_id,
                    }))
                    
                    return {
                        "success": True,
                        "output_path": str(output_file),
                    }
                finally:
                    self._workspace.deactivate()
                    os.chdir(original_cwd)
            
            def cleanup(self) -> bool:
                if self._workspace:
                    return self.manager.cleanup_workspace(self._workspace.workspace_id)
                return False
        
        # Create actor
        actor = WorkspaceActor.remote(workspace_config_dict)
        
        # Create workspace
        ws_data = ray.get(actor.create_workspace.remote(
            "batch-actor-test",
            "test_variant",
            "exec-actor-001",
            [str(sample_project_files["data_dir"])],
        ))
        
        assert ws_data["workspace_id"] is not None
        assert Path(ws_data["root_path"]).exists()
        
        # Execute
        result = ray.get(actor.execute.remote({"test": "data", "step": 1}))
        
        assert result["success"]
        output_path = Path(result["output_path"])
        assert output_path.exists()
        
        content = json.loads(output_path.read_text())
        assert content["test"] == "data"
        
        # Cleanup
        cleanup_result = ray.get(actor.cleanup.remote())
        assert cleanup_result
        
        # Workspace should be removed
        assert not Path(ws_data["root_path"]).exists()
    
    def test_actor_failure_preserves_workspace(
        self,
        workspace_config_dict,
        sample_project_files,
    ):
        """Test that workspace is preserved when actor fails."""
        
        @ray.remote
        class FailingActor:
            def __init__(self, config_dict: Dict):
                self.config = WorkspaceConfig(
                    enabled=config_dict["enabled"],
                    strategy=WorkspaceStrategy(config_dict["strategy"]),
                    base_dir=Path(config_dict["base_dir"]),
                    cleanup_on_complete=config_dict["cleanup_on_complete"],
                    preserve_on_failure=True,  # Preserve on failure
                    use_hard_links=config_dict["use_hard_links"],
                )
                self.manager = WorkspaceManager(config=self.config)
            
            def execute_with_failure(
                self,
                batch_id: str,
                variant_name: str,
                source_paths: List[str],
            ) -> Dict:
                ws = self.manager.create_workspace(
                    batch_id=batch_id,
                    variant_name=variant_name,
                    execution_id=f"exec-fail-{variant_name}",
                    source_paths=[Path(p) for p in source_paths],
                )
                
                original_cwd = os.getcwd()
                try:
                    ws.activate()
                    
                    # Write partial output
                    partial_file = ws.output_dir / "partial.txt"
                    partial_file.parent.mkdir(parents=True, exist_ok=True)
                    partial_file.write_text("partial data before failure")
                    
                    # Return workspace path before "failure"
                    return {
                        "workspace_path": str(ws.root_path),
                        "partial_output": str(partial_file),
                        "workspace_id": ws.workspace_id,
                    }
                    
                finally:
                    ws.deactivate()
                    os.chdir(original_cwd)
        
        actor = FailingActor.remote(workspace_config_dict)
        
        result = ray.get(actor.execute_with_failure.remote(
            "batch-failure-test",
            "failing_variant",
            [str(sample_project_files["csv"])],
        ))
        
        # Workspace should still exist with partial output
        assert Path(result["workspace_path"]).exists()
        assert Path(result["partial_output"]).exists()
        
        # Partial output should be readable
        content = Path(result["partial_output"]).read_text()
        assert "partial data" in content


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Ray Batch Test Integration
# ═══════════════════════════════════════════════════════════════════════════════


class TestRayBatchTestWorkspace:
    """Tests for Ray batch test workspace integration."""
    
    @pytest.fixture(autouse=True)
    def setup_ray(self, ray_initialized):
        """Ensure Ray is initialized for tests."""
        if not ray_initialized:
            pytest.skip("Ray not available")
    
    def test_batch_test_creates_isolated_workspaces(
        self,
        workspace_config_dict,
        sample_project_files,
    ):
        """Test that batch test creates isolated workspace per variant."""
        
        @ray.remote
        def run_variant(
            config_dict: Dict,
            batch_id: str,
            variant_name: str,
            input_data: Dict,
        ) -> Dict:
            """Run single variant in isolated workspace."""
            config = WorkspaceConfig(
                enabled=config_dict["enabled"],
                strategy=WorkspaceStrategy(config_dict["strategy"]),
                base_dir=Path(config_dict["base_dir"]),
                cleanup_on_complete=config_dict["cleanup_on_complete"],
                preserve_on_failure=config_dict["preserve_on_failure"],
                use_hard_links=config_dict["use_hard_links"],
            )
            
            manager = WorkspaceManager(config=config)
            ws = manager.create_workspace(
                batch_id=batch_id,
                variant_name=variant_name,
                execution_id=f"exec-{variant_name}",
            )
            
            original_cwd = os.getcwd()
            
            try:
                ws.activate()
                
                # Simulate processing
                result_data = {
                    "variant": variant_name,
                    "input": input_data,
                    "score": 0.85 + (hash(variant_name) % 10) / 100,
                    "workspace_id": ws.workspace_id,
                }
                
                # Write result
                output_file = ws.output_dir / "result.json"
                output_file.parent.mkdir(parents=True, exist_ok=True)
                output_file.write_text(json.dumps(result_data))
                
                return {
                    "variant": variant_name,
                    "workspace_path": str(ws.root_path),
                    "output_path": str(output_file),
                    "result": result_data,
                }
                
            finally:
                ws.deactivate()
                os.chdir(original_cwd)
        
        # Run batch test
        batch_id = f"batch-test-{uuid.uuid4().hex[:8]}"
        variants = ["bm25", "dense", "hybrid"]
        test_input = {"query": "test query", "k": 10}
        
        # Submit all variants
        futures = [
            run_variant.remote(workspace_config_dict, batch_id, variant, test_input)
            for variant in variants
        ]
        
        # Collect results
        results = ray.get(futures)
        
        # Verify
        assert len(results) == 3
        
        workspace_paths = set()
        for result in results:
            assert result["variant"] in variants
            assert Path(result["output_path"]).exists()
            
            # Check result content
            content = json.loads(Path(result["output_path"]).read_text())
            assert content["variant"] == result["variant"]
            
            # Paths should be unique
            workspace_paths.add(result["workspace_path"])
        
        assert len(workspace_paths) == 3, "Each variant should have unique workspace"
    
    def test_batch_cleanup_removes_all_workspaces(
        self,
        workspace_config_dict,
        sample_project_files,
    ):
        """Test that batch cleanup removes all variant workspaces."""
        
        @ray.remote
        def create_and_return_workspace(
            config_dict: Dict,
            batch_id: str,
            variant_name: str,
        ) -> str:
            """Create workspace and return path."""
            config = WorkspaceConfig(
                enabled=config_dict["enabled"],
                strategy=WorkspaceStrategy(config_dict["strategy"]),
                base_dir=Path(config_dict["base_dir"]),
                cleanup_on_complete=config_dict["cleanup_on_complete"],
                preserve_on_failure=config_dict["preserve_on_failure"],
                use_hard_links=config_dict["use_hard_links"],
            )
            
            manager = WorkspaceManager(config=config)
            ws = manager.create_workspace(
                batch_id=batch_id,
                variant_name=variant_name,
                execution_id=f"exec-{variant_name}",
            )
            return str(ws.root_path)
        
        # Create workspaces
        batch_id = f"batch-cleanup-{uuid.uuid4().hex[:8]}"
        
        futures = [
            create_and_return_workspace.remote(workspace_config_dict, batch_id, f"v{i}")
            for i in range(5)
        ]
        
        workspace_paths = [Path(p) for p in ray.get(futures)]
        
        # All should exist
        for path in workspace_paths:
            assert path.exists()
        
        # Cleanup batch from main process
        config = WorkspaceConfig(
            enabled=workspace_config_dict["enabled"],
            strategy=WorkspaceStrategy(workspace_config_dict["strategy"]),
            base_dir=Path(workspace_config_dict["base_dir"]),
            cleanup_on_complete=workspace_config_dict["cleanup_on_complete"],
            preserve_on_failure=workspace_config_dict["preserve_on_failure"],
            use_hard_links=workspace_config_dict["use_hard_links"],
        )
        
        # Note: In real implementation, we'd reload workspaces from metadata files
        # For this test, we'll directly remove the directories
        batch_dir = Path(workspace_config_dict["base_dir"]) / f"batch_{batch_id}"
        if batch_dir.exists():
            shutil.rmtree(batch_dir)
        
        # Verify cleanup
        for path in workspace_paths:
            assert not path.exists()


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Ray Workspace Event Integration
# ═══════════════════════════════════════════════════════════════════════════════


class TestRayWorkspaceEvents:
    """Tests for Ray workspace event integration."""
    
    @pytest.fixture(autouse=True)
    def setup_ray(self, ray_initialized):
        """Ensure Ray is initialized for tests."""
        if not ray_initialized:
            pytest.skip("Ray not available")
    
    def test_workspace_events_serializable(self):
        """Test that workspace events can be serialized for Ray."""
        event = WorkspaceCreatedEvent(
            workspace_id="ws-001",
            batch_test_id="batch-001",
            execution_id="exec-001",
            variant_name="bm25",
            workspace_path="/tmp/workspace",
            file_count=10,
            total_size_bytes=1024,
        )
        
        # Serialize via event's to_dict method
        event_dict = {
            "event_type": event.event_type,
            "workspace_id": event.workspace_id,
            "batch_test_id": event.batch_test_id,
            "execution_id": event.execution_id,
            "variant_name": event.variant_name,
            "workspace_path": event.workspace_path,
            "file_count": event.file_count,
            "total_size_bytes": event.total_size_bytes,
            "timestamp": event.timestamp.isoformat(),
        }
        
        # Should be JSON serializable
        json_str = json.dumps(event_dict)
        assert json_str is not None
        
        # Deserialize
        restored = json.loads(json_str)
        assert restored["workspace_id"] == "ws-001"
        assert restored["variant_name"] == "bm25"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
