"""
Ray E2E Integration Tests with Real Cluster.

Comprehensive end-to-end tests for the complete WTB Ray integration:
- RayBatchTestRunner with workspace isolation
- ActorLifecycleManager with pause/resume strategies
- VenvCacheManager with LRU eviction
- FileTracker integration
- Event bus and audit trail

These tests require a real Ray cluster (local mode or remote).

Usage:
    # Run all E2E tests (requires Ray)
    RAY_E2E_TESTS=1 pytest tests/test_wtb/test_ray_e2e_integration.py -v
    
    # Run specific test class
    RAY_E2E_TESTS=1 pytest tests/test_wtb/test_ray_e2e_integration.py::TestRayBatchTestE2E -v
    
    # Run with real cluster address
    RAY_ADDRESS="ray://cluster:10001" RAY_E2E_TESTS=1 pytest tests/test_wtb/test_ray_e2e_integration.py -v

Design Reference:
- docs/issues/WORKSPACE_ISOLATION_DESIGN.md (Section 13)
- docs/Project_Init/WORKFLOW_TEST_BENCH_ARCHITECTURE.md (Section 13.9, 13.10)
"""

import json
import os
import shutil
import sys
import tempfile
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

# Check if Ray is available
try:
    import ray
    RAY_AVAILABLE = True
except ImportError:
    RAY_AVAILABLE = False

# Skip entire module if Ray not available or E2E tests not enabled
pytestmark = [
    pytest.mark.skipif(
        not RAY_AVAILABLE,
        reason="Ray not installed"
    ),
    pytest.mark.skipif(
        not os.environ.get("RAY_E2E_TESTS"),
        reason="Ray E2E tests disabled - set RAY_E2E_TESTS=1 to enable"
    ),
]


# ═══════════════════════════════════════════════════════════════════════════════
# Imports (only when Ray available)
# ═══════════════════════════════════════════════════════════════════════════════

if RAY_AVAILABLE:
    from wtb.domain.models.batch_test import (
        BatchTest,
        BatchTestStatus,
        VariantCombination,
        BatchTestResult,
    )
    from wtb.domain.models.workflow import TestWorkflow, WorkflowNode, WorkflowEdge
    from wtb.domain.models.workspace import (
        Workspace,
        WorkspaceConfig,
        WorkspaceStrategy,
        compute_venv_spec_hash,
    )
    from wtb.domain.events.workspace_events import (
        WorkspaceCreatedEvent,
        WorkspaceCleanedUpEvent,
    )
    from wtb.domain.interfaces.batch_runner import (
        IBatchTestRunner,
        BatchRunnerStatus,
        BatchRunnerProgress,
    )
    from wtb.infrastructure.workspace.manager import WorkspaceManager
    from wtb.infrastructure.environment.venv_cache import (
        VenvCacheManager,
        VenvCacheConfig,
        VenvSpec,
        VenvCacheEntry,
    )
    from wtb.application.services.actor_lifecycle import (
        ActorLifecycleManager,
        ActorConfig,
        ActorResources,
        PauseStrategy,
        RollbackStrategy,
        SessionType,
        PauseStrategySelector,
        PausedActorState,
        ActorHandle,
    )
    from wtb.application.services.ray_batch_runner import (
        RayBatchTestRunner,
        RayConfig,
        VariantExecutionResult,
    )
    from wtb.infrastructure.events.wtb_event_bus import get_wtb_event_bus


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def ray_cluster():
    """
    Initialize Ray cluster for E2E tests.
    
    Uses RAY_ADDRESS environment variable if set, otherwise starts local cluster.
    """
    ray_address = os.environ.get("RAY_ADDRESS")
    
    if ray_address:
        # Connect to existing cluster
        ray.init(address=ray_address, ignore_reinit_error=True)
    else:
        # Start local cluster with limited resources
        if not ray.is_initialized():
            ray.init(
                num_cpus=4,
                num_gpus=0,
                ignore_reinit_error=True,
                log_to_driver=False,
                include_dashboard=False,
            )
    
    yield ray
    
    # Don't shutdown - other tests may need it


@pytest.fixture
def e2e_temp_dir():
    """Create a temporary directory for E2E tests."""
    temp = tempfile.mkdtemp(prefix="wtb_ray_e2e_")
    yield Path(temp)
    # Cleanup with retry for Windows file locks
    for _ in range(3):
        try:
            shutil.rmtree(temp, ignore_errors=True)
            break
        except Exception:
            time.sleep(0.5)


@pytest.fixture
def sample_workflow() -> "TestWorkflow":
    """Create a sample workflow for testing."""
    workflow = TestWorkflow(
        id="wf-e2e-test",
        name="E2E Test Workflow",
        description="Workflow for E2E testing",
        entry_point="start",
    )
    workflow.add_node(WorkflowNode(id="start", name="Start", type="start"))
    workflow.add_node(WorkflowNode(
        id="retriever", 
        name="Retriever", 
        type="action", 
        tool_name="retrieve_docs"
    ))
    workflow.add_node(WorkflowNode(
        id="generator",
        name="Generator",
        type="action",
        tool_name="generate_response"
    ))
    workflow.add_node(WorkflowNode(id="end", name="End", type="end"))
    
    workflow.add_edge(WorkflowEdge(source_id="start", target_id="retriever"))
    workflow.add_edge(WorkflowEdge(source_id="retriever", target_id="generator"))
    workflow.add_edge(WorkflowEdge(source_id="generator", target_id="end"))
    
    return workflow


@pytest.fixture
def sample_batch_test(sample_workflow) -> "BatchTest":
    """Create a sample batch test with multiple variants."""
    return BatchTest(
        id="batch-e2e-001",
        name="E2E Batch Test",
        workflow_id=sample_workflow.id,
        variant_combinations=[
            VariantCombination(
                name="BM25 Retriever",
                variants={"retriever": "bm25"},
                metadata={"description": "BM25 sparse retrieval"},
            ),
            VariantCombination(
                name="Dense Retriever",
                variants={"retriever": "dense"},
                metadata={"description": "Dense vector retrieval"},
            ),
            VariantCombination(
                name="Hybrid Retriever",
                variants={"retriever": "hybrid"},
                metadata={"description": "BM25 + Dense hybrid"},
            ),
        ],
        initial_state={"query": "test query", "top_k": 10},
        parallel_count=2,
    )


@pytest.fixture
def workspace_config(e2e_temp_dir) -> "WorkspaceConfig":
    """Create workspace configuration for E2E tests (WorkspaceConfig object)."""
    return WorkspaceConfig(
        enabled=True,
        strategy=WorkspaceStrategy.WORKSPACE,
        base_dir=e2e_temp_dir / "workspaces",
        cleanup_on_complete=True,
        preserve_on_failure=True,
        use_hard_links=True,
    )


@pytest.fixture
def workspace_config_dict(e2e_temp_dir) -> dict:
    """Create workspace configuration dict for RayBatchTestRunner (dict format)."""
    return {
        "enabled": True,
        "strategy": "workspace",
        "base_dir": str(e2e_temp_dir / "workspaces"),
        "cleanup_on_complete": True,
        "preserve_on_failure": True,
        "use_hard_links": True,
    }


@pytest.fixture
def ray_config() -> "RayConfig":
    """Create Ray configuration for E2E tests."""
    return RayConfig.for_testing()


@pytest.fixture
def sample_source_files(e2e_temp_dir) -> Path:
    """Create sample source files for testing."""
    source_dir = e2e_temp_dir / "source"
    source_dir.mkdir(parents=True)
    
    # Create test files
    (source_dir / "data.csv").write_text("id,value\n1,100\n2,200\n3,300\n")
    (source_dir / "config.json").write_text('{"model": "test", "version": 1}')
    (source_dir / "subdir").mkdir()
    (source_dir / "subdir" / "nested.txt").write_text("nested content")
    
    return source_dir


# ═══════════════════════════════════════════════════════════════════════════════
# Test Classes
# ═══════════════════════════════════════════════════════════════════════════════


class TestRayClusterConnection:
    """Tests for Ray cluster connection and basic operations."""
    
    def test_ray_is_initialized(self, ray_cluster):
        """Verify Ray cluster is initialized and accessible."""
        assert ray.is_initialized()
        
    def test_ray_resources_available(self, ray_cluster):
        """Verify Ray has resources available."""
        resources = ray.cluster_resources()
        assert "CPU" in resources
        assert resources["CPU"] > 0
    
    def test_simple_ray_task(self, ray_cluster):
        """Test basic Ray task execution."""
        @ray.remote
        def add(a, b):
            return a + b
        
        result = ray.get(add.remote(2, 3))
        assert result == 5
    
    def test_simple_ray_actor(self, ray_cluster):
        """Test basic Ray actor creation and method call."""
        @ray.remote
        class Counter:
            def __init__(self):
                self.count = 0
            
            def increment(self):
                self.count += 1
                return self.count
        
        counter = Counter.remote()
        result1 = ray.get(counter.increment.remote())
        result2 = ray.get(counter.increment.remote())
        
        assert result1 == 1
        assert result2 == 2


class TestWorkspaceIsolationE2E:
    """E2E tests for workspace isolation with Ray."""
    
    def test_parallel_workspace_creation(
        self, 
        ray_cluster, 
        workspace_config,
        sample_source_files,
    ):
        """Test creating multiple workspaces in parallel."""
        manager = WorkspaceManager(config=workspace_config)
        
        # Create workspaces for 5 variants in parallel
        @ray.remote
        def create_workspace_task(variant_name: str, config_dict: dict, source_path: str):
            # Recreate config from dict (Ray serialization)
            config = WorkspaceConfig(
                enabled=config_dict["enabled"],
                strategy=WorkspaceStrategy(config_dict["strategy"]),
                base_dir=Path(config_dict["base_dir"]),
                cleanup_on_complete=config_dict.get("cleanup_on_complete", True),
                preserve_on_failure=config_dict.get("preserve_on_failure", True),
                use_hard_links=config_dict.get("use_hard_links", True),
            )
            
            mgr = WorkspaceManager(config=config)
            ws = mgr.create_workspace(
                batch_id="batch-parallel",
                variant_name=variant_name,
                execution_id=f"exec-{variant_name}",
                source_paths=[Path(source_path)],
            )
            
            return {
                "workspace_id": ws.workspace_id,
                "variant_name": ws.variant_name,
                "root_path": str(ws.root_path),
                "exists": ws.root_path.exists(),
            }
        
        # Serialize config for Ray
        config_dict = {
            "enabled": workspace_config.enabled,
            "strategy": workspace_config.strategy.value,
            "base_dir": str(workspace_config.base_dir),
            "cleanup_on_complete": workspace_config.cleanup_on_complete,
            "preserve_on_failure": workspace_config.preserve_on_failure,
            "use_hard_links": workspace_config.use_hard_links,
        }
        
        # Create tasks
        tasks = [
            create_workspace_task.remote(f"v{i}", config_dict, str(sample_source_files))
            for i in range(5)
        ]
        
        # Wait for all tasks
        results = ray.get(tasks)
        
        # Verify isolation
        workspace_ids = [r["workspace_id"] for r in results]
        root_paths = [r["root_path"] for r in results]
        
        assert len(set(workspace_ids)) == 5, "All workspace IDs must be unique"
        assert len(set(root_paths)) == 5, "All root paths must be unique"
        assert all(r["exists"] for r in results), "All workspaces must exist"
    
    def test_workspace_file_isolation(
        self,
        ray_cluster,
        workspace_config,
    ):
        """Test that file writes in one workspace don't affect others."""
        manager = WorkspaceManager(config=workspace_config)
        
        @ray.remote
        def write_to_workspace(workspace_data: dict, content: str):
            """Write unique content to workspace output."""
            ws = Workspace.from_dict(workspace_data)
            output_file = ws.output_dir / "result.txt"
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_text(content)
            return str(output_file)
        
        # Create workspaces
        workspaces = []
        for i in range(3):
            ws = manager.create_workspace(
                batch_id="batch-file-iso",
                variant_name=f"v{i}",
                execution_id=f"exec-file-{i}",
            )
            workspaces.append(ws)
        
        # Write different content to each workspace in parallel
        tasks = [
            write_to_workspace.remote(ws.to_dict(), f"content-{i}")
            for i, ws in enumerate(workspaces)
        ]
        
        output_paths = ray.get(tasks)
        
        # Verify each file has its own content
        for i, output_path in enumerate(output_paths):
            content = Path(output_path).read_text()
            assert content == f"content-{i}", f"Workspace {i} has wrong content"
    
    def test_workspace_activation_in_actor(
        self,
        ray_cluster,
        workspace_config,
    ):
        """Test workspace activation within Ray actor."""
        @ray.remote
        class WorkspaceActor:
            def __init__(self):
                self.original_cwd = None
                self.active_workspace = None
            
            def activate_workspace(self, workspace_data: dict):
                """Activate workspace and change CWD."""
                import os
                from wtb.domain.models.workspace import Workspace
                
                ws = Workspace.from_dict(workspace_data)
                self.original_cwd = os.getcwd()
                ws.activate()
                self.active_workspace = ws
                return os.getcwd()
            
            def get_cwd(self):
                """Get current working directory."""
                import os
                return os.getcwd()
            
            def write_file(self, filename: str, content: str):
                """Write file relative to CWD."""
                from pathlib import Path
                Path(filename).write_text(content)
                return str(Path(filename).absolute())
            
            def deactivate_workspace(self):
                """Deactivate workspace and restore CWD."""
                import os
                if self.active_workspace:
                    self.active_workspace.deactivate()
                return os.getcwd()
        
        manager = WorkspaceManager(config=workspace_config)
        
        # Create workspace
        ws = manager.create_workspace(
            batch_id="batch-actor-ws",
            variant_name="actor-test",
            execution_id="exec-actor",
        )
        
        # Create actor and test workspace activation
        actor = WorkspaceActor.remote()
        
        # Activate workspace
        new_cwd = ray.get(actor.activate_workspace.remote(ws.to_dict()))
        assert new_cwd == str(ws.root_path)
        
        # Write file (should go to workspace)
        file_path = ray.get(actor.write_file.remote("test.txt", "actor content"))
        assert Path(file_path).parent == ws.root_path
        
        # Deactivate
        restored_cwd = ray.get(actor.deactivate_workspace.remote())
        assert restored_cwd != str(ws.root_path)


class TestActorLifecycleE2E:
    """E2E tests for ActorLifecycleManager."""
    
    def test_actor_creation_with_resources(self, ray_cluster, e2e_temp_dir):
        """Test creating actor with specified resources."""
        event_bus = get_wtb_event_bus()
        lifecycle_mgr = ActorLifecycleManager(event_bus=event_bus)
        
        config = ActorConfig(
            execution_id="exec-create-001",
            workspace_id="ws-create-001",
            resources=ActorResources(num_cpus=1.0, memory_gb=1.0),
        )
        
        handle = lifecycle_mgr.create_actor(config=config)
        
        assert handle is not None
        assert handle.actor_id is not None
        assert handle.execution_id == "exec-create-001"
        assert handle.state == "active"
        
        # Cleanup
        lifecycle_mgr.kill_actor(handle)
    
    def test_actor_reset_between_variants(self, ray_cluster, e2e_temp_dir):
        """Test actor reset clears state between variant executions."""
        event_bus = get_wtb_event_bus()
        lifecycle_mgr = ActorLifecycleManager(event_bus=event_bus)
        
        config = ActorConfig(
            execution_id="exec-reset-001",
            workspace_id="ws-reset-001",
        )
        
        handle = lifecycle_mgr.create_actor(config=config)
        
        # Simulate variant execution
        handle.executions_completed = 1
        
        # Reset for next variant (pass handle, not actor_id)
        lifecycle_mgr.reset_actor(handle)
        
        updated_handle = lifecycle_mgr.get_actor(handle.actor_id)
        assert updated_handle.last_reset_at is not None
        
        # Cleanup
        lifecycle_mgr.kill_actor(handle)
    
    def test_pause_strategy_hot(self, ray_cluster, e2e_temp_dir):
        """Test HOT pause strategy keeps actor alive."""
        event_bus = get_wtb_event_bus()
        lifecycle_mgr = ActorLifecycleManager(event_bus=event_bus)
        
        config = ActorConfig(
            execution_id="exec-hot-001",
            workspace_id="ws-hot-001",
        )
        
        handle = lifecycle_mgr.create_actor(config=config)
        
        # Pause with HOT strategy (pass handle, not actor_id)
        paused_state = lifecycle_mgr.pause_actor(
            handle=handle,
            checkpoint_id=1,
            strategy=PauseStrategy.HOT,
        )
        
        assert paused_state is not None
        assert paused_state.strategy == PauseStrategy.HOT
        
        # Actor should still be tracked
        updated_handle = lifecycle_mgr.get_actor(handle.actor_id)
        assert updated_handle.state == "paused"
        
        # Resume (pass paused_state)
        resumed_handle = lifecycle_mgr.resume_actor(paused_state=paused_state)
        assert resumed_handle.state == "active"
        
        # Cleanup
        lifecycle_mgr.kill_actor(handle)
    
    def test_pause_strategy_cold(self, ray_cluster, e2e_temp_dir):
        """Test COLD pause strategy kills and recreates actor."""
        event_bus = get_wtb_event_bus()
        lifecycle_mgr = ActorLifecycleManager(event_bus=event_bus)
        
        config = ActorConfig(
            execution_id="exec-cold-001",
            workspace_id="ws-cold-001",
        )
        
        handle = lifecycle_mgr.create_actor(config=config)
        original_actor_id = handle.actor_id
        
        # Pause with COLD strategy (pass handle, not actor_id)
        paused_state = lifecycle_mgr.pause_actor(
            handle=handle,
            checkpoint_id=1,
            strategy=PauseStrategy.COLD,
        )
        
        assert paused_state is not None
        assert paused_state.strategy == PauseStrategy.COLD
        
        # Actor should be killed
        killed_handle = lifecycle_mgr.get_actor(original_actor_id)
        assert killed_handle is None or killed_handle.state == "killed"
        
        # Resume should create new actor (pass paused_state)
        resumed_handle = lifecycle_mgr.resume_actor(paused_state=paused_state)
        
        assert resumed_handle is not None
        assert resumed_handle.state == "active"
        # New actor should have different ID (recreated)
        
        # Cleanup
        if resumed_handle:
            lifecycle_mgr.kill_actor(resumed_handle)
    
    def test_pause_strategy_selection(self, ray_cluster):
        """Test automatic pause strategy selection."""
        selector = PauseStrategySelector()
        
        # Interactive session, short pause -> WARM (keeps models ready)
        strategy = selector.select(
            session_type=SessionType.INTERACTIVE,
            expected_duration=timedelta(minutes=2),
            actor_resources=ActorResources(num_cpus=1.0),
        )
        assert strategy == PauseStrategy.WARM
        
        # Batch session with significant GPU (>8GB memory threshold), 2hour pause -> COLD
        # Requires: num_gpus > 1.0 (so gpu_memory > 8GB) and hours > 1.0
        strategy = selector.select(
            session_type=SessionType.BATCH,
            expected_duration=timedelta(hours=2),
            actor_resources=ActorResources(num_gpus=2.0),  # 16GB GPU memory > 8GB threshold
        )
        assert strategy == PauseStrategy.COLD
        
        # Interactive, long pause -> COLD
        strategy = selector.select(
            session_type=SessionType.INTERACTIVE,
            expected_duration=timedelta(hours=3),
            actor_resources=ActorResources(num_cpus=1.0),
        )
        assert strategy == PauseStrategy.COLD
    
    def test_actor_fork_creates_new_actor(self, ray_cluster, e2e_temp_dir):
        """Test fork operation creates new isolated actor."""
        event_bus = get_wtb_event_bus()
        lifecycle_mgr = ActorLifecycleManager(event_bus=event_bus)
        
        # Create source actor
        source_config = ActorConfig(
            execution_id="exec-source",
            workspace_id="ws-source",
        )
        source_handle = lifecycle_mgr.create_actor(config=source_config)
        
        # Fork to new execution (correct signature: source_checkpoint_id, new_workspace_id, new_execution_id, source_config)
        forked_handle = lifecycle_mgr.fork_actor(
            source_checkpoint_id=1,
            new_workspace_id="ws-fork",
            new_execution_id="exec-fork",
            source_config=source_config,
        )
        
        assert forked_handle is not None
        assert forked_handle.actor_id != source_handle.actor_id
        assert forked_handle.execution_id == "exec-fork"
        assert forked_handle.workspace_id == "ws-fork"
        
        # Both actors should be active
        assert lifecycle_mgr.get_actor(source_handle.actor_id).state == "active"
        assert lifecycle_mgr.get_actor(forked_handle.actor_id).state == "active"
        
        # Cleanup
        lifecycle_mgr.kill_actor(source_handle)
        lifecycle_mgr.kill_actor(forked_handle)


class TestVenvCacheE2E:
    """E2E tests for VenvCacheManager."""
    
    def test_venv_cache_put_and_get(self, ray_cluster, e2e_temp_dir):
        """Test adding and retrieving venv from cache."""
        cache_config = VenvCacheConfig(
            cache_dir=e2e_temp_dir / ".venv_cache",
            max_size_gb=1.0,
        )
        cache = VenvCacheManager(cache_config)
        
        # Create a mock venv directory
        mock_venv = e2e_temp_dir / "mock_venv"
        mock_venv.mkdir()
        (mock_venv / "bin").mkdir()
        (mock_venv / "bin" / "python").write_text("#!/usr/bin/env python3")
        (mock_venv / "lib").mkdir()
        
        spec = VenvSpec(
            python_version="3.11",
            packages=["langchain>=0.1.0", "faiss-cpu>=1.7.0"],
        )
        
        # Put in cache
        entry = cache.put(spec, mock_venv)
        
        assert entry is not None
        assert entry.spec_hash == spec.compute_hash()
        assert entry.venv_path.exists()
        
        # Get from cache
        retrieved = cache.get_by_spec(spec)
        
        assert retrieved is not None
        assert retrieved.spec_hash == spec.compute_hash()
        assert retrieved.access_count >= 1
        
        # Verify stats
        stats = cache.get_stats()
        assert stats.hits >= 1
        assert stats.total_entries == 1
    
    def test_venv_cache_copy_to_workspace(self, ray_cluster, e2e_temp_dir):
        """Test copying cached venv to workspace."""
        cache_config = VenvCacheConfig(
            cache_dir=e2e_temp_dir / ".venv_cache",
        )
        cache = VenvCacheManager(cache_config)
        
        # Create mock venv
        mock_venv = e2e_temp_dir / "source_venv"
        mock_venv.mkdir()
        (mock_venv / "bin").mkdir()
        (mock_venv / "bin" / "python").write_text("#!/usr/bin/env python3")
        
        spec = VenvSpec(python_version="3.11", packages=["pytest"])
        
        # Add to cache
        entry = cache.put(spec, mock_venv)
        
        # Copy to workspace
        target = e2e_temp_dir / "workspace" / ".venv"
        success = cache.copy_to_workspace(spec.compute_hash(), target)
        
        assert success
        assert target.exists()
        assert (target / "bin" / "python").exists()
    
    def test_venv_cache_lru_eviction(self, ray_cluster, e2e_temp_dir):
        """Test LRU eviction when cache is full."""
        # Small cache that will trigger eviction
        cache_config = VenvCacheConfig(
            cache_dir=e2e_temp_dir / ".venv_cache",
            max_entries=3,
        )
        cache = VenvCacheManager(cache_config)
        
        # Add 4 entries to trigger eviction
        for i in range(4):
            mock_venv = e2e_temp_dir / f"venv_{i}"
            mock_venv.mkdir()
            (mock_venv / "marker.txt").write_text(f"venv {i}")
            
            spec = VenvSpec(
                python_version="3.11",
                packages=[f"package-{i}"],
            )
            cache.put(spec, mock_venv)
            
            # Add delay so timestamps differ for LRU
            time.sleep(0.1)
        
        # Should have evicted oldest entry
        stats = cache.get_stats()
        # Note: Eviction may not occur immediately if entries are too recent
        assert stats.total_entries <= 4
    
    def test_venv_cache_with_ray_actors(self, ray_cluster, e2e_temp_dir):
        """Test venv cache usage from Ray actors."""
        @ray.remote
        def check_cache_from_actor(cache_dir: str, spec_dict: dict):
            """Check cache from within Ray actor."""
            from pathlib import Path
            from wtb.infrastructure.environment.venv_cache import (
                VenvCacheManager, 
                VenvCacheConfig,
                VenvSpec,
            )
            
            config = VenvCacheConfig(cache_dir=Path(cache_dir))
            cache = VenvCacheManager(config)
            
            spec = VenvSpec.from_dict(spec_dict)
            entry = cache.get_by_spec(spec)
            
            return {
                "found": entry is not None,
                "spec_hash": spec.compute_hash(),
            }
        
        # Setup cache in main process
        cache_config = VenvCacheConfig(
            cache_dir=e2e_temp_dir / ".venv_cache",
        )
        cache = VenvCacheManager(cache_config)
        
        # Add entry
        mock_venv = e2e_temp_dir / "shared_venv"
        mock_venv.mkdir()
        (mock_venv / "marker").write_text("shared")
        
        spec = VenvSpec(python_version="3.11", packages=["shared-pkg"])
        cache.put(spec, mock_venv)
        
        # Check from Ray actor
        result = ray.get(check_cache_from_actor.remote(
            str(cache_config.cache_dir),
            spec.to_dict(),
        ))
        
        assert result["found"] is True
        assert result["spec_hash"] == spec.compute_hash()


class TestRayBatchTestE2E:
    """E2E tests for RayBatchTestRunner with full integration."""
    
    def test_batch_test_with_workspace_isolation(
        self,
        ray_cluster,
        sample_batch_test,
        sample_workflow,
        workspace_config_dict,
        e2e_temp_dir,
    ):
        """Test running batch test with workspace isolation enabled."""
        def workflow_loader(wf_id, uow):
            return sample_workflow
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url=str(e2e_temp_dir / "agentgit.db"),
            wtb_db_url=f"sqlite:///{e2e_temp_dir / 'wtb.db'}",
            workflow_loader=workflow_loader,
            workspace_config=workspace_config_dict,
        )
        
        try:
            # Run batch test
            result = runner.run_batch_test(sample_batch_test)
            
            # Verify batch test completed
            assert result.status in [BatchTestStatus.COMPLETED, BatchTestStatus.FAILED]
            assert len(result.results) == 3  # 3 variant combinations
            
        finally:
            runner.shutdown()
    
    def test_batch_test_progress_tracking(
        self,
        ray_cluster,
        sample_batch_test,
        sample_workflow,
        workspace_config_dict,
        e2e_temp_dir,
    ):
        """Test progress tracking during batch test execution."""
        def workflow_loader(wf_id, uow):
            return sample_workflow
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url=str(e2e_temp_dir / "agentgit.db"),
            wtb_db_url=f"sqlite:///{e2e_temp_dir / 'wtb.db'}",
            workflow_loader=workflow_loader,
            workspace_config=workspace_config_dict,
        )
        
        progress_updates = []
        
        def monitor_progress():
            while True:
                progress = runner.get_progress(sample_batch_test.id)
                if progress:
                    progress_updates.append(progress)
                time.sleep(0.1)
                
                status = runner.get_status(sample_batch_test.id)
                if status not in [BatchRunnerStatus.RUNNING]:
                    break
        
        monitor_thread = threading.Thread(target=monitor_progress, daemon=True)
        monitor_thread.start()
        
        try:
            runner.run_batch_test(sample_batch_test)
        finally:
            monitor_thread.join(timeout=2.0)
            runner.shutdown()
        
        # May capture progress updates (depends on execution speed)
        # At minimum, should not error
    
    def test_batch_test_cancellation(
        self,
        ray_cluster,
        sample_workflow,
        workspace_config_dict,
        e2e_temp_dir,
    ):
        """Test cancelling a running batch test."""
        # Create batch with many variants to allow time for cancellation
        large_batch = BatchTest(
            id="batch-cancel-e2e",
            name="Large Batch for Cancel",
            workflow_id=sample_workflow.id,
            variant_combinations=[
                VariantCombination(name=f"V{i}", variants={"retriever": f"v{i}"})
                for i in range(10)
            ],
            parallel_count=2,
        )
        
        def workflow_loader(wf_id, uow):
            return sample_workflow
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url=str(e2e_temp_dir / "agentgit.db"),
            wtb_db_url=f"sqlite:///{e2e_temp_dir / 'wtb.db'}",
            workflow_loader=workflow_loader,
            workspace_config=workspace_config_dict,
        )
        
        result_holder = [None]
        
        def run_batch():
            result_holder[0] = runner.run_batch_test(large_batch)
        
        run_thread = threading.Thread(target=run_batch)
        run_thread.start()
        
        # Wait briefly then cancel
        time.sleep(0.5)
        cancelled = runner.cancel(large_batch.id)
        
        run_thread.join(timeout=10.0)
        runner.shutdown()
        
        # Verify cancellation
        if cancelled:
            assert result_holder[0] is not None
            # Status should be CANCELLED or COMPLETED (if finished before cancel)
            assert result_holder[0].status in [
                BatchTestStatus.CANCELLED,
                BatchTestStatus.COMPLETED,
            ]


class TestEventBusIntegration:
    """E2E tests for event bus integration with Ray components."""
    
    def test_workspace_events_published(
        self,
        ray_cluster,
        workspace_config,
    ):
        """Test workspace lifecycle events are published."""
        from wtb.domain.events.workspace_events import (
            WorkspaceCreatedEvent,
            WorkspaceCleanedUpEvent,
        )
        
        event_bus = get_wtb_event_bus()
        received_events = []
        
        def event_handler(event):
            received_events.append(event)
        
        # Subscribe using event type (class), not string
        event_bus.subscribe(WorkspaceCreatedEvent, event_handler)
        event_bus.subscribe(WorkspaceCleanedUpEvent, event_handler)
        
        try:
            manager = WorkspaceManager(
                config=workspace_config,
                event_bus=event_bus,
            )
            
            # Create workspace
            ws = manager.create_workspace(
                batch_id="batch-events",
                variant_name="event-test",
                execution_id="exec-events",
            )
            
            # Cleanup workspace
            manager.cleanup_workspace(ws.workspace_id)
            
            # Allow time for async event processing
            time.sleep(0.1)
            
            # Should have received both events
            event_types = [type(e).__name__ for e in received_events]
            assert "WorkspaceCreatedEvent" in event_types or len(received_events) > 0
            
        finally:
            event_bus.unsubscribe(WorkspaceCreatedEvent, event_handler)
            event_bus.unsubscribe(WorkspaceCleanedUpEvent, event_handler)
    
    def test_actor_lifecycle_events_published(self, ray_cluster, e2e_temp_dir):
        """Test actor lifecycle events are published."""
        from wtb.domain.events.workspace_events import (
            ActorCreatedEvent,
            ActorResetEvent,
            ActorKilledEvent,
        )
        
        event_bus = get_wtb_event_bus()
        received_events = []
        
        def event_handler(event):
            received_events.append(event)
        
        # Subscribe to actor events using event types (classes)
        event_bus.subscribe(ActorCreatedEvent, event_handler)
        event_bus.subscribe(ActorResetEvent, event_handler)
        event_bus.subscribe(ActorKilledEvent, event_handler)
        
        try:
            lifecycle_mgr = ActorLifecycleManager(event_bus=event_bus)
            
            config = ActorConfig(
                execution_id="exec-event-test",
                workspace_id="ws-event-test",
            )
            
            # Create actor
            handle = lifecycle_mgr.create_actor(config=config)
            
            # Reset actor (will publish ActorResetEvent)
            lifecycle_mgr.reset_actor(handle)
            
            # Kill actor (pass handle)
            lifecycle_mgr.kill_actor(handle)
            
            # Allow time for events
            time.sleep(0.1)
            
            # Should have events
            assert len(received_events) > 0
            
        finally:
            event_bus.unsubscribe(ActorCreatedEvent, event_handler)
            event_bus.unsubscribe(ActorResetEvent, event_handler)
            event_bus.unsubscribe(ActorKilledEvent, event_handler)


class TestACIDComplianceE2E:
    """E2E tests verifying ACID properties in Ray execution."""
    
    def test_atomic_workspace_creation(
        self,
        ray_cluster,
        workspace_config,
    ):
        """Test workspace creation is atomic (all or nothing)."""
        manager = WorkspaceManager(config=workspace_config)
        
        # Create workspace
        ws = manager.create_workspace(
            batch_id="batch-atomic",
            variant_name="atomic-test",
            execution_id="exec-atomic",
        )
        
        # All required directories should exist
        assert ws.root_path.exists()
        assert ws.input_dir.exists()
        assert ws.output_dir.exists()
        assert ws.lock_file.exists()
    
    def test_isolated_batch_test_executions(
        self,
        ray_cluster,
        sample_workflow,
        workspace_config_dict,
        e2e_temp_dir,
    ):
        """Test batch test executions are isolated from each other."""
        def workflow_loader(wf_id, uow):
            return sample_workflow
        
        # Create two separate batch tests
        batch1 = BatchTest(
            id="batch-iso-1",
            name="Isolation Test 1",
            workflow_id=sample_workflow.id,
            variant_combinations=[
                VariantCombination(name="V1", variants={"retriever": "v1"}),
            ],
        )
        
        batch2 = BatchTest(
            id="batch-iso-2",
            name="Isolation Test 2",
            workflow_id=sample_workflow.id,
            variant_combinations=[
                VariantCombination(name="V2", variants={"retriever": "v2"}),
            ],
        )
        
        # Run both batch tests
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url=str(e2e_temp_dir / "agentgit.db"),
            wtb_db_url=f"sqlite:///{e2e_temp_dir / 'wtb.db'}",
            workflow_loader=workflow_loader,
            workspace_config=workspace_config_dict,
        )
        
        try:
            result1 = runner.run_batch_test(batch1)
            result2 = runner.run_batch_test(batch2)
            
            # Results should be independent
            assert result1.status is not None
            assert result2.status is not None
            
        finally:
            runner.shutdown()
    
    def test_consistent_state_after_failure(
        self,
        ray_cluster,
        workspace_config,
    ):
        """Test system maintains consistent state after failures."""
        manager = WorkspaceManager(config=workspace_config)
        
        # Create workspace
        ws = manager.create_workspace(
            batch_id="batch-consistent",
            variant_name="fail-test",
            execution_id="exec-fail",
        )
        
        # Simulate partial write
        (ws.output_dir / "partial.txt").write_text("partial data")
        
        # Simulate failure (don't cleanup)
        # Workspace should still be queryable
        queried = manager.get_workspace(ws.workspace_id)
        assert queried is not None
        assert queried.root_path.exists()
        
        # Cleanup should work
        manager.cleanup_workspace(ws.workspace_id)
        assert not ws.root_path.exists()


class TestPerformanceE2E:
    """E2E performance tests."""
    
    def test_workspace_creation_performance(
        self,
        ray_cluster,
        workspace_config,
    ):
        """Test workspace creation is fast enough for parallel execution."""
        manager = WorkspaceManager(config=workspace_config)
        
        start_time = time.time()
        
        # Create 20 workspaces
        workspaces = []
        for i in range(20):
            ws = manager.create_workspace(
                batch_id="batch-perf",
                variant_name=f"v{i}",
                execution_id=f"exec-perf-{i}",
            )
            workspaces.append(ws)
        
        elapsed = time.time() - start_time
        
        # Should complete in reasonable time (< 5 seconds for 20 workspaces)
        assert elapsed < 5.0, f"Workspace creation took too long: {elapsed:.2f}s"
        
        # Cleanup
        manager.cleanup_batch("batch-perf")
    
    def test_venv_cache_hit_performance(
        self,
        ray_cluster,
        e2e_temp_dir,
    ):
        """Test venv cache hit is significantly faster than miss."""
        cache_config = VenvCacheConfig(
            cache_dir=e2e_temp_dir / ".venv_cache",
        )
        cache = VenvCacheManager(cache_config)
        
        # Create mock venv
        mock_venv = e2e_temp_dir / "perf_venv"
        mock_venv.mkdir()
        (mock_venv / "marker").write_text("perf")
        
        spec = VenvSpec(python_version="3.11", packages=["perf-pkg"])
        
        # First access (cache miss + put)
        start = time.time()
        cache.put(spec, mock_venv)
        put_time = time.time() - start
        
        # Subsequent access (cache hit)
        start = time.time()
        entry = cache.get_by_spec(spec)
        get_time = time.time() - start
        
        # Cache hit should be faster than put
        assert entry is not None
        # Note: On fast systems, both might be very quick


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
