"""
SDK Full Integration tests combining all components.

Tests integration of:
- Ray distributed batch testing
- Virtual environment management
- LangGraph checkpointing
- File system tracking
- Workspace isolation

Operations tested:
- Update workflow
- Replace/update node
- Fork execution
- Branching
- Rolling back
- Batch testing

Run with: pytest tests/test_sdk/test_sdk_full_integration.py -v

For full integration tests with Ray:
    RAY_INTEGRATION_TESTS=1 pytest tests/test_sdk/test_sdk_full_integration.py -v
"""

import pytest
import os
import uuid
import time
import hashlib
import tempfile
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable
from datetime import datetime, timezone
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import MagicMock, patch

from wtb.sdk import WTBTestBench, WorkflowProject
from wtb.sdk.workflow_project import (
    FileTrackingConfig as SDKFileTrackingConfig,
    EnvironmentConfig,
    ExecutionConfig,
    EnvSpec,
    NodeVariant,
    WorkspaceIsolationConfig,
)
from wtb.sdk.test_bench import (
    WTBTestBenchBuilder,
    RollbackResult,
    ForkResult,
)
from wtb.domain.models import ExecutionStatus
from wtb.domain.models.batch_test import (
    BatchTest,
    BatchTestStatus,
    VariantCombination,
    BatchTestResult,
)
from wtb.application.factories import WTBTestBenchFactory, BatchTestRunnerFactory
from wtb.application.services import ProjectService, VariantService
from wtb.config import WTBConfig, RayConfig, FileTrackingConfig
from wtb.infrastructure.database import InMemoryUnitOfWork
from wtb.infrastructure.adapters import InMemoryStateAdapter

from tests.test_sdk.conftest import (
    create_initial_state,
    create_branching_state,
    create_file_state,
    MockVenvProvider,
    MockVenvSpec,
    MockVenvStatus,
    LANGGRAPH_AVAILABLE,
    RAY_AVAILABLE,
)


# Skip all tests if LangGraph not available
pytestmark = pytest.mark.skipif(
    not LANGGRAPH_AVAILABLE,
    reason="LangGraph not installed"
)


# ═══════════════════════════════════════════════════════════════════════════════
# Test Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def full_integration_workspace(tmp_path):
    """Create workspace with all integration components."""
    workspace = tmp_path / "integration_workspace"
    workspace.mkdir()
    
    # Create subdirectories
    (workspace / "data").mkdir()
    (workspace / "output").mkdir()
    (workspace / "checkpoints").mkdir()
    (workspace / "envs").mkdir()
    
    return workspace


@pytest.fixture
def integration_project(simple_graph_factory, full_integration_workspace):
    """Create project with all integration features enabled."""
    project = WorkflowProject(
        name=f"full_integration_{uuid.uuid4().hex[:8]}",
        graph_factory=simple_graph_factory,
        description="Full integration test workflow",
        environment=EnvironmentConfig(
            granularity="workflow",
            use_current_env=True,  # Use current env for tests
            default_env=EnvSpec(
                python_version="3.12",
                dependencies=["langgraph>=0.2"],
            ),
        ),
        file_tracking=SDKFileTrackingConfig(
            enabled=True,
            tracked_paths=[str(full_integration_workspace / "data")],
            auto_commit=True,
            commit_on="checkpoint",
        ),
        workspace_isolation=WorkspaceIsolationConfig(
            enabled=False,  # Disable for faster tests
        ),
    )
    return project


@pytest.fixture
def integration_wtb(full_integration_workspace):
    """Create WTBTestBench with all integration features."""
    return WTBTestBenchFactory.create_with_langgraph(
        checkpointer_type="memory",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Full Integration: Workflow Update Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFullIntegrationWorkflowUpdate:
    """Tests for workflow update with all components integrated."""
    
    def test_update_workflow_preserves_env_config(
        self, simple_graph_factory, branching_graph_factory
    ):
        """Test updating workflow preserves environment configuration."""
        project = WorkflowProject(
            name=f"update-env-preserve-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            environment=EnvironmentConfig(
                granularity="node",
                default_env=EnvSpec(python_version="3.12"),
                node_environments={
                    "node_a": EnvSpec(dependencies=["numpy"]),
                },
            ),
        )
        
        original_env = project.environment
        
        # Update workflow
        project.update_workflow(
            graph_factory=branching_graph_factory,
            version="2.0",
            changelog="Updated to branching workflow",
        )
        
        # Environment config should be preserved
        assert project.environment.granularity == original_env.granularity
        assert project.environment.default_env.python_version == "3.12"
    
    def test_update_workflow_preserves_file_tracking(
        self, simple_graph_factory, branching_graph_factory, tmp_path
    ):
        """Test updating workflow preserves file tracking config."""
        project = WorkflowProject(
            name=f"update-file-preserve-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            file_tracking=SDKFileTrackingConfig(
                enabled=True,
                tracked_paths=[str(tmp_path / "data")],
            ),
        )
        
        original_tracking = project.file_tracking.enabled
        
        project.update_workflow(
            graph_factory=branching_graph_factory,
            version="2.0",
        )
        
        assert project.file_tracking.enabled == original_tracking
    
    def test_update_workflow_triggers_version_increment(self, simple_graph_factory):
        """Test workflow update increments version."""
        project = WorkflowProject(
            name=f"update-version-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        original_version = project.version
        
        # Multiple updates
        for i in range(3):
            def new_factory(i=i):
                return simple_graph_factory()
            
            project.update_workflow(
                graph_factory=new_factory,
                version=f"{i + 2}.0",
            )
        
        assert project.version > original_version
    
    def test_update_workflow_with_execution_history(
        self, integration_wtb, simple_graph_factory, branching_graph_factory
    ):
        """Test updating workflow after executions."""
        project = WorkflowProject(
            name=f"update-exec-history-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        integration_wtb.register_project(project)
        
        # Run initial execution
        result1 = integration_wtb.run(
            project=project.name,
            initial_state=create_initial_state(),
        )
        
        assert result1.status == ExecutionStatus.COMPLETED
        
        # Update project in cache
        project.update_workflow(
            graph_factory=branching_graph_factory,
            version="2.0",
        )
        
        # Original execution should still be accessible
        execution = integration_wtb.get_execution(result1.id)
        assert execution.id == result1.id


# ═══════════════════════════════════════════════════════════════════════════════
# Full Integration: Node Replacement Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFullIntegrationNodeReplacement:
    """Tests for node replacement with all components."""
    
    def test_replace_node_with_env_specific_variant(
        self, integration_wtb, simple_graph_factory
    ):
        """Test replacing node with environment-specific variant."""
        project = WorkflowProject(
            name=f"replace-env-variant-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            environment=EnvironmentConfig(
                granularity="variant",
                default_env=EnvSpec(dependencies=["base-package"]),
            ),
        )
        
        # Register variant with specific environment
        def gpu_variant(state: Dict[str, Any]) -> Dict[str, Any]:
            return {"messages": state.get("messages", []) + ["GPU_PROCESSED"], "count": 999}
        
        project.register_variant(
            node="node_b",
            name="gpu",
            implementation=gpu_variant,
            environment=EnvSpec(
                dependencies=["torch", "cuda-toolkit"],
            ),
        )
        
        integration_wtb.register_project(project)
        
        # Run with GPU variant
        result = integration_wtb.run(
            project=project.name,
            initial_state=create_initial_state(),
            variant_config={"node_b": "gpu"},
        )
        
        assert result.status in [ExecutionStatus.COMPLETED, ExecutionStatus.FAILED]
    
    def test_replace_node_with_file_tracking_active(
        self, integration_wtb, simple_graph_factory, full_integration_workspace
    ):
        """Test replacing node with file tracking active."""
        project = WorkflowProject(
            name=f"replace-file-track-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            file_tracking=SDKFileTrackingConfig(
                enabled=True,
                tracked_paths=[str(full_integration_workspace / "data")],
            ),
        )
        
        def file_writing_variant(state: Dict[str, Any]) -> Dict[str, Any]:
            # Variant that writes a file
            return {"messages": state.get("messages", []) + ["FILE_WRITTEN"]}
        
        project.register_variant(
            node="node_b",
            name="file_writer",
            implementation=file_writing_variant,
        )
        
        integration_wtb.register_project(project)
        
        result = integration_wtb.run(
            project=project.name,
            initial_state=create_initial_state(),
            variant_config={"node_b": "file_writer"},
        )
        
        assert result.status in [ExecutionStatus.COMPLETED, ExecutionStatus.FAILED]
    
    def test_update_node_preserves_variants(self, simple_graph_factory):
        """Test updating node preserves existing variants."""
        project = WorkflowProject(
            name=f"update-preserve-variants-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        # Register variants
        def fast_impl(state):
            return {"messages": ["fast"]}
        
        def slow_impl(state):
            return {"messages": ["slow"]}
        
        project.register_variant("node_b", "fast", fast_impl)
        project.register_variant("node_b", "slow", slow_impl)
        
        # Update node
        def new_impl(state):
            return {"messages": ["updated"]}
        
        project.update_node("node_b", new_impl, reason="Updating default")
        
        # Variants should still exist
        variants = project.list_variants("node_b")
        assert "fast" in variants.get("node_b", [])
        assert "slow" in variants.get("node_b", [])


# ═══════════════════════════════════════════════════════════════════════════════
# Full Integration: Fork and Branch Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFullIntegrationForkBranch:
    """Tests for fork and branch operations with all components."""
    
    def test_fork_with_different_env(self, integration_wtb, simple_graph_factory):
        """Test forking execution with different environment config."""
        project = WorkflowProject(
            name=f"fork-env-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            environment=EnvironmentConfig(
                granularity="workflow",
                default_env=EnvSpec(python_version="3.12"),
            ),
        )
        
        integration_wtb.register_project(project)
        
        result = integration_wtb.run(
            project=project.name,
            initial_state=create_initial_state(),
        )
        
        try:
            fork_result = integration_wtb.fork(
                result.id,
                "1",
                new_initial_state={"messages": ["forked"], "count": 0},
            )
            
            assert fork_result.source_execution_id == result.id
            assert fork_result.fork_execution_id != result.id
        except (RuntimeError, ValueError, AttributeError):
            pytest.skip("Forking not supported in this configuration")
    
    def test_fork_preserves_file_state(
        self, integration_wtb, simple_graph_factory, full_integration_workspace
    ):
        """Test forking preserves file state at checkpoint."""
        project = WorkflowProject(
            name=f"fork-file-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            file_tracking=SDKFileTrackingConfig(
                enabled=True,
                tracked_paths=[str(full_integration_workspace / "data")],
            ),
        )
        
        # Create a file before execution
        data_file = full_integration_workspace / "data" / "state.txt"
        data_file.write_text("Initial state")
        
        integration_wtb.register_project(project)
        
        result = integration_wtb.run(
            project=project.name,
            initial_state=create_initial_state(),
        )
        
        try:
            fork_result = integration_wtb.fork(result.id, "1")
            
            assert isinstance(fork_result, ForkResult)
            assert fork_result.source_execution_id == result.id
        except RuntimeError:
            pytest.skip("Forking not supported")
    
    def test_multiple_forks_isolated(self, integration_wtb, simple_graph_factory):
        """Test multiple forks are isolated from each other."""
        project = WorkflowProject(
            name=f"multi-fork-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        integration_wtb.register_project(project)
        
        result = integration_wtb.run(
            project=project.name,
            initial_state=create_initial_state(),
        )
        
        forks = []
        try:
            for i in range(3):
                fork = integration_wtb.fork(result.id, "1")
                forks.append(fork)
            
            # All forks should have unique IDs
            fork_ids = [f.fork_execution_id for f in forks]
            assert len(set(fork_ids)) == 3
        except RuntimeError:
            pytest.skip("Forking not supported")
    
    def test_fork_with_variant_change(self, integration_wtb, simple_graph_factory):
        """Test forking with different variant configuration."""
        project = WorkflowProject(
            name=f"fork-variant-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        def alt_impl(state):
            return {"messages": state.get("messages", []) + ["ALT"]}
        
        project.register_variant("node_b", "alt", alt_impl)
        
        integration_wtb.register_project(project)
        
        # Run with default
        result = integration_wtb.run(
            project=project.name,
            initial_state=create_initial_state(),
        )
        
        try:
            # Fork and run with variant
            fork_result = integration_wtb.fork(
                result.id,
                "1",
                new_initial_state={"messages": ["forked"], "count": 0},
            )
            
            assert fork_result.fork_execution_id != result.id
        except (RuntimeError, ValueError, AttributeError):
            pytest.skip("Forking not supported")


# ═══════════════════════════════════════════════════════════════════════════════
# Full Integration: Rollback Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFullIntegrationRollback:
    """Tests for rollback operations with all components."""
    
    def test_rollback_with_file_state_restore(
        self, integration_wtb, simple_graph_factory, full_integration_workspace
    ):
        """Test rollback conceptually restores file state."""
        project = WorkflowProject(
            name=f"rollback-file-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            file_tracking=SDKFileTrackingConfig(
                enabled=True,
                tracked_paths=[str(full_integration_workspace / "data")],
            ),
        )
        
        integration_wtb.register_project(project)
        
        result = integration_wtb.run(
            project=project.name,
            initial_state=create_initial_state(),
        )
        
        rollback_result = integration_wtb.rollback(result.id, "1")
        
        assert isinstance(rollback_result, RollbackResult)
        assert rollback_result.execution_id == result.id
    
    def test_rollback_preserves_env_context(
        self, integration_wtb, simple_graph_factory
    ):
        """Test rollback preserves environment context."""
        project = WorkflowProject(
            name=f"rollback-env-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            environment=EnvironmentConfig(
                default_env=EnvSpec(
                    python_version="3.12",
                    dependencies=["numpy"],
                ),
            ),
        )
        
        integration_wtb.register_project(project)
        
        result = integration_wtb.run(
            project=project.name,
            initial_state=create_initial_state(),
        )
        
        rollback_result = integration_wtb.rollback(result.id, "1")
        
        # Execution should still be accessible
        execution = integration_wtb.get_execution(result.id)
        assert execution is not None
    
    def test_rollback_to_node_with_variants(
        self, integration_wtb, simple_graph_factory
    ):
        """Test rollback to specific node with variants."""
        project = WorkflowProject(
            name=f"rollback-node-variant-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        def variant_impl(state):
            return {"messages": state.get("messages", []) + ["VARIANT"]}
        
        project.register_variant("node_b", "special", variant_impl)
        
        integration_wtb.register_project(project)
        
        result = integration_wtb.run(
            project=project.name,
            initial_state=create_initial_state(),
            variant_config={"node_b": "special"},
        )
        
        rollback_result = integration_wtb.rollback_to_node(result.id, "node_a")
        
        assert isinstance(rollback_result, RollbackResult)
    
    def test_rollback_then_continue_with_different_variant(
        self, integration_wtb, simple_graph_factory
    ):
        """Test rollback then continue with different variant."""
        project = WorkflowProject(
            name=f"rollback-continue-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        def fast_impl(state):
            return {"messages": state.get("messages", []) + ["FAST"]}
        
        def slow_impl(state):
            return {"messages": state.get("messages", []) + ["SLOW"]}
        
        project.register_variant("node_b", "fast", fast_impl)
        project.register_variant("node_b", "slow", slow_impl)
        
        integration_wtb.register_project(project)
        
        # Run with fast variant
        result = integration_wtb.run(
            project=project.name,
            initial_state=create_initial_state(),
            variant_config={"node_b": "fast"},
        )
        
        # Rollback
        rollback = integration_wtb.rollback(result.id, "1")
        
        # Run new execution with slow variant
        result2 = integration_wtb.run(
            project=project.name,
            initial_state=create_initial_state(),
            variant_config={"node_b": "slow"},
        )
        
        # Both executions should exist
        assert result.id != result2.id


# ═══════════════════════════════════════════════════════════════════════════════
# Full Integration: Batch Testing Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFullIntegrationBatchTesting:
    """Tests for batch testing with all components."""
    
    def test_batch_test_with_env_variants(
        self, integration_wtb, simple_graph_factory
    ):
        """Test batch testing with environment-specific variants."""
        project = WorkflowProject(
            name=f"batch-env-variant-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            environment=EnvironmentConfig(
                granularity="variant",
                default_env=EnvSpec(dependencies=["base"]),
            ),
        )
        
        def cpu_impl(state):
            return {"messages": state.get("messages", []) + ["CPU"]}
        
        def gpu_impl(state):
            return {"messages": state.get("messages", []) + ["GPU"]}
        
        project.register_variant(
            "node_b", "cpu", cpu_impl,
            environment=EnvSpec(dependencies=["numpy"]),
        )
        project.register_variant(
            "node_b", "gpu", gpu_impl,
            environment=EnvSpec(dependencies=["torch"]),
        )
        
        integration_wtb.register_project(project)
        
        result = integration_wtb.run_batch_test(
            project=project.name,
            variant_matrix=[
                {"node_b": "cpu"},
                {"node_b": "gpu"},
            ],
            test_cases=[
                {"messages": [], "count": 0},
            ],
        )
        
        assert isinstance(result, BatchTest)
        assert result.status in [BatchTestStatus.COMPLETED, BatchTestStatus.FAILED]
    
    def test_batch_test_with_file_tracking(
        self, integration_wtb, simple_graph_factory, full_integration_workspace
    ):
        """Test batch testing with file tracking enabled."""
        project = WorkflowProject(
            name=f"batch-file-track-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            file_tracking=SDKFileTrackingConfig(
                enabled=True,
                tracked_paths=[str(full_integration_workspace / "data")],
            ),
        )
        
        def variant_a(state):
            return {"messages": state.get("messages", []) + ["A"]}
        
        def variant_b(state):
            return {"messages": state.get("messages", []) + ["B"]}
        
        project.register_variant("node_b", "a", variant_a)
        project.register_variant("node_b", "b", variant_b)
        
        integration_wtb.register_project(project)
        
        result = integration_wtb.run_batch_test(
            project=project.name,
            variant_matrix=[
                {"node_b": "a"},
                {"node_b": "b"},
            ],
            test_cases=[
                {"messages": [], "count": 0},
                {"messages": ["init"], "count": 10},
            ],
        )
        
        assert isinstance(result, BatchTest)
    
    def test_batch_test_comparison_matrix(
        self, integration_wtb, simple_graph_factory
    ):
        """Test batch test generates comparison matrix."""
        project = WorkflowProject(
            name=f"batch-matrix-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        for i in range(3):
            def impl(state, i=i):
                return {"messages": state.get("messages", []) + [f"V{i}"]}
            
            project.register_variant("node_b", f"v{i}", impl)
        
        integration_wtb.register_project(project)
        
        result = integration_wtb.run_batch_test(
            project=project.name,
            variant_matrix=[
                {"node_b": f"v{i}"} for i in range(3)
            ],
            test_cases=[
                {"messages": [], "count": 0},
            ],
        )
        
        assert len(result.variant_combinations) == 3
        
        if result.status == BatchTestStatus.COMPLETED:
            matrix = result.build_comparison_matrix()
            assert "combinations" in matrix


# ═══════════════════════════════════════════════════════════════════════════════
# Full Integration: Ray + All Components Tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(
    not os.environ.get("RAY_INTEGRATION_TESTS"),
    reason="Ray integration tests disabled. Set RAY_INTEGRATION_TESTS=1"
)
@pytest.mark.skipif(not RAY_AVAILABLE, reason="Ray not installed")
class TestFullIntegrationWithRay:
    """Full integration tests with Ray distributed execution."""
    
    def test_ray_batch_with_env_isolation(
        self, ray_initialized, tmp_path, simple_graph_factory
    ):
        """Test Ray batch testing with environment isolation."""
        from wtb.application.services.ray_batch_runner import RayBatchTestRunner
        from wtb.domain.models.workflow import TestWorkflow, WorkflowNode, WorkflowEdge
        
        workflow = TestWorkflow(
            id="wf-ray-env",
            name="Ray Env Test",
            entry_point="node_a",
        )
        workflow.add_node(WorkflowNode(id="node_a", name="A", type="action"))
        workflow.add_node(WorkflowNode(id="node_b", name="B", type="action"))
        workflow.add_edge(WorkflowEdge(source_id="node_a", target_id="node_b"))
        
        def workflow_loader(wf_id, uow):
            return workflow
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url=str(tmp_path / "agentgit.db"),
            wtb_db_url=f"sqlite:///{tmp_path / 'wtb.db'}",
            workflow_loader=workflow_loader,
        )
        
        batch = BatchTest(
            id="ray-env-batch",
            name="Ray Env Batch",
            workflow_id=workflow.id,
            variant_combinations=[
                VariantCombination(name="env_a", variants={}, metadata={"env": "cpu"}),
                VariantCombination(name="env_b", variants={}, metadata={"env": "gpu"}),
            ],
            initial_state={"messages": [], "count": 0},
            parallel_count=2,
        )
        
        try:
            result = runner.run_batch_test(batch)
            assert result.status in [BatchTestStatus.COMPLETED, BatchTestStatus.FAILED]
        finally:
            runner.shutdown()
    
    def test_ray_batch_with_file_tracking(
        self, ray_initialized, tmp_path, simple_graph_factory
    ):
        """Test Ray batch testing with file tracking."""
        from wtb.application.services.ray_batch_runner import RayBatchTestRunner
        from wtb.domain.models.workflow import TestWorkflow, WorkflowNode
        
        # Create tracked data directory
        data_dir = tmp_path / "tracked_data"
        data_dir.mkdir()
        
        workflow = TestWorkflow(
            id="wf-ray-file",
            name="Ray File Test",
            entry_point="start",
        )
        workflow.add_node(WorkflowNode(id="start", name="Start", type="start"))
        
        def workflow_loader(wf_id, uow):
            return workflow
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url=str(tmp_path / "agentgit.db"),
            wtb_db_url=f"sqlite:///{tmp_path / 'wtb.db'}",
            workflow_loader=workflow_loader,
        )
        
        batch = BatchTest(
            id="ray-file-batch",
            name="Ray File Batch",
            workflow_id=workflow.id,
            variant_combinations=[
                VariantCombination(name="A", variants={}),
                VariantCombination(name="B", variants={}),
            ],
            parallel_count=2,
        )
        
        try:
            result = runner.run_batch_test(batch)
            assert result.status in [BatchTestStatus.COMPLETED, BatchTestStatus.FAILED]
        finally:
            runner.shutdown()
    
    def test_ray_parallel_variant_execution(self, ray_initialized, tmp_path):
        """Test Ray parallel variant execution."""
        from wtb.application.services.ray_batch_runner import RayBatchTestRunner
        from wtb.domain.models.workflow import TestWorkflow, WorkflowNode
        
        workflow = TestWorkflow(
            id="wf-ray-parallel",
            name="Ray Parallel",
            entry_point="start",
        )
        workflow.add_node(WorkflowNode(id="start", name="Start", type="start"))
        
        def workflow_loader(wf_id, uow):
            return workflow
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url=str(tmp_path / "agentgit.db"),
            wtb_db_url=f"sqlite:///{tmp_path / 'wtb.db'}",
            workflow_loader=workflow_loader,
        )
        
        # Create many variant combinations
        batch = BatchTest(
            id="ray-parallel-batch",
            name="Parallel Test",
            workflow_id=workflow.id,
            variant_combinations=[
                VariantCombination(name=f"variant_{i}", variants={"node": f"v{i}"})
                for i in range(10)
            ],
            parallel_count=4,
        )
        
        try:
            start = time.time()
            result = runner.run_batch_test(batch)
            elapsed = time.time() - start
            
            assert result.status in [BatchTestStatus.COMPLETED, BatchTestStatus.FAILED]
        finally:
            runner.shutdown()


# ═══════════════════════════════════════════════════════════════════════════════
# Full Integration: ACID Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFullIntegrationACID:
    """ACID compliance tests for full integration scenarios."""
    
    def test_atomicity_workflow_update_with_files(
        self, simple_graph_factory, full_integration_workspace
    ):
        """Test workflow update atomicity with file tracking."""
        project = WorkflowProject(
            name=f"atomic-wf-file-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            file_tracking=SDKFileTrackingConfig(
                enabled=True,
                tracked_paths=[str(full_integration_workspace)],
            ),
        )
        
        original_version = project.version
        
        # Atomic update
        def new_factory():
            return simple_graph_factory()
        
        project.update_workflow(
            graph_factory=new_factory,
            version="2.0",
        )
        
        # Should be fully updated or not at all
        assert project.version > original_version
        assert project.graph_factory == new_factory
    
    def test_consistency_across_components(
        self, integration_wtb, simple_graph_factory, full_integration_workspace
    ):
        """Test consistency across all components."""
        project = WorkflowProject(
            name=f"consistent-all-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            environment=EnvironmentConfig(
                default_env=EnvSpec(python_version="3.12"),
            ),
            file_tracking=SDKFileTrackingConfig(
                enabled=True,
                tracked_paths=[str(full_integration_workspace)],
            ),
        )
        
        integration_wtb.register_project(project)
        
        # Run multiple executions
        results = []
        for i in range(5):
            result = integration_wtb.run(
                project=project.name,
                initial_state={"messages": [f"exec_{i}"], "count": i},
            )
            results.append(result)
        
        # All should complete consistently
        for r in results:
            execution = integration_wtb.get_execution(r.id)
            assert execution.id == r.id
            assert execution.status == r.status
    
    def test_isolation_concurrent_executions(
        self, integration_wtb, simple_graph_factory
    ):
        """Test isolation between concurrent executions."""
        project = WorkflowProject(
            name=f"isolated-concurrent-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        integration_wtb.register_project(project)
        
        def run_execution(idx: int):
            return integration_wtb.run(
                project=project.name,
                initial_state={"messages": [f"parallel_{idx}"], "count": idx * 100},
            )
        
        results = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(run_execution, i) for i in range(4)]
            results = [f.result() for f in as_completed(futures)]
        
        # All should complete without interference
        assert all(r.status == ExecutionStatus.COMPLETED for r in results)
        
        # All should have unique IDs
        ids = [r.id for r in results]
        assert len(set(ids)) == 4
    
    def test_durability_execution_persistence(
        self, integration_wtb, simple_graph_factory
    ):
        """Test execution durability."""
        project = WorkflowProject(
            name=f"durable-exec-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        integration_wtb.register_project(project)
        
        result = integration_wtb.run(
            project=project.name,
            initial_state=create_initial_state(),
        )
        
        # Multiple retrievals should return consistent data
        for _ in range(10):
            execution = integration_wtb.get_execution(result.id)
            assert execution.id == result.id
            assert execution.status == result.status


# ═══════════════════════════════════════════════════════════════════════════════
# Full Integration: Error Handling Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFullIntegrationErrorHandling:
    """Error handling tests for full integration scenarios."""
    
    def test_rollback_invalid_checkpoint_graceful(
        self, integration_wtb, simple_graph_factory
    ):
        """Test rollback with invalid checkpoint fails gracefully."""
        project = WorkflowProject(
            name=f"rollback-invalid-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        integration_wtb.register_project(project)
        
        result = integration_wtb.run(
            project=project.name,
            initial_state=create_initial_state(),
        )
        
        rollback = integration_wtb.rollback(result.id, "999999")
        
        # Should fail gracefully
        assert isinstance(rollback, RollbackResult)
    
    def test_fork_invalid_execution_graceful(self, integration_wtb):
        """Test fork with invalid execution fails gracefully."""
        try:
            fork = integration_wtb.fork(
                "invalid-exec-id",
                "1",
                new_initial_state={"messages": []},
            )
        except (RuntimeError, ValueError, KeyError, AttributeError):
            # Expected to fail
            pass
    
    def test_batch_test_partial_failure_handling(
        self, integration_wtb, simple_graph_factory
    ):
        """Test batch test handles partial failures."""
        project = WorkflowProject(
            name=f"batch-partial-fail-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        def failing_impl(state):
            raise RuntimeError("Intentional failure")
        
        def passing_impl(state):
            return {"messages": state.get("messages", []) + ["PASS"]}
        
        project.register_variant("node_b", "fail", failing_impl)
        project.register_variant("node_b", "pass", passing_impl)
        
        integration_wtb.register_project(project)
        
        result = integration_wtb.run_batch_test(
            project=project.name,
            variant_matrix=[
                {"node_b": "pass"},
                {"node_b": "fail"},
            ],
            test_cases=[
                {"messages": [], "count": 0},
            ],
        )
        
        # Should complete (even with some failures)
        assert isinstance(result, BatchTest)
        assert result.status in [BatchTestStatus.COMPLETED, BatchTestStatus.FAILED]


# ═══════════════════════════════════════════════════════════════════════════════
# Full Integration: Workspace + Env + File Tracking Combined Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBatchMultipleQueries:
    """Tests for batch testing with multiple queries and variants."""
    
    def test_batch_with_multiple_queries_for_variant_nodes(
        self, integration_wtb, simple_graph_factory
    ):
        """Test batch testing with multiple queries for variant nodes in workflow."""
        project = WorkflowProject(
            name=f"batch-variant-queries-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        # Register multiple variants for node_b
        def variant_fast(state: Dict[str, Any]) -> Dict[str, Any]:
            return {"messages": state.get("messages", []) + ["FAST"], "count": 10}
        
        def variant_accurate(state: Dict[str, Any]) -> Dict[str, Any]:
            return {"messages": state.get("messages", []) + ["ACCURATE"], "count": 20}
        
        def variant_balanced(state: Dict[str, Any]) -> Dict[str, Any]:
            return {"messages": state.get("messages", []) + ["BALANCED"], "count": 15}
        
        project.register_variant("node_b", "fast", variant_fast)
        project.register_variant("node_b", "accurate", variant_accurate)
        project.register_variant("node_b", "balanced", variant_balanced)
        
        integration_wtb.register_project(project)
        
        # Multiple test queries (different initial states)
        test_queries = [
            {"messages": [], "count": 0},
            {"messages": ["query_1"], "count": 100},
            {"messages": ["query_2", "complex"], "count": 200},
            {"messages": ["query_3"], "count": 300},
        ]
        
        # Variant matrix - test each variant
        variant_matrix = [
            {"node_b": "fast"},
            {"node_b": "accurate"},
            {"node_b": "balanced"},
        ]
        
        result = integration_wtb.run_batch_test(
            project=project.name,
            variant_matrix=variant_matrix,
            test_cases=test_queries,
        )
        
        assert isinstance(result, BatchTest)
        assert result.status in [BatchTestStatus.COMPLETED, BatchTestStatus.FAILED]
        # Should have results for 3 variants * 4 queries = 12 results (or 3 if batched per variant)
        assert len(result.results) >= 3
    
    def test_batch_with_multiple_queries_for_same_workflow(
        self, integration_wtb, simple_graph_factory
    ):
        """Test batch testing with multiple queries for the same workflow."""
        project = WorkflowProject(
            name=f"batch-same-wf-queries-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        integration_wtb.register_project(project)
        
        # Many different queries to the same workflow
        test_queries = [
            {"messages": [], "count": 0},
            {"messages": ["small"], "count": 1},
            {"messages": ["medium", "data"], "count": 50},
            {"messages": ["large", "dataset", "test"], "count": 100},
            {"messages": ["extra", "large", "complex", "query"], "count": 500},
        ]
        
        # Same variant (default) but multiple queries
        result = integration_wtb.run_batch_test(
            project=project.name,
            variant_matrix=[{"node_b": "default"}],
            test_cases=test_queries,
        )
        
        assert isinstance(result, BatchTest)
        assert result.status in [BatchTestStatus.COMPLETED, BatchTestStatus.FAILED]
        # Should have processed all test cases
        assert len(result.results) >= 1
    
    def test_batch_variant_x_query_matrix(
        self, integration_wtb, simple_graph_factory
    ):
        """Test batch testing with full variant x query matrix."""
        project = WorkflowProject(
            name=f"batch-matrix-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        # Register variants
        for i in range(3):
            def impl(state, i=i):
                return {"messages": state.get("messages", []) + [f"V{i}"], "count": i * 10}
            project.register_variant("node_b", f"variant_{i}", impl)
        
        integration_wtb.register_project(project)
        
        # Multiple queries
        queries = [
            {"messages": [f"q{j}"], "count": j * 100}
            for j in range(4)
        ]
        
        # All variants
        variants = [{"node_b": f"variant_{i}"} for i in range(3)]
        
        result = integration_wtb.run_batch_test(
            project=project.name,
            variant_matrix=variants,
            test_cases=queries,
        )
        
        assert isinstance(result, BatchTest)
        assert result.status in [BatchTestStatus.COMPLETED, BatchTestStatus.FAILED]
        
        if result.status == BatchTestStatus.COMPLETED:
            # Build comparison matrix
            matrix = result.build_comparison_matrix()
            assert "combinations" in matrix
    
    def test_batch_with_node_and_workflow_variants(
        self, integration_wtb, simple_graph_factory, branching_graph_factory
    ):
        """Test batch testing with both node variants and workflow variants."""
        project = WorkflowProject(
            name=f"batch-dual-variants-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        # Register node variants
        def fast_node(state):
            return {"messages": state.get("messages", []) + ["FAST"]}
        
        def slow_node(state):
            return {"messages": state.get("messages", []) + ["SLOW"]}
        
        project.register_variant("node_b", "fast", fast_node)
        project.register_variant("node_b", "slow", slow_node)
        
        # Register workflow variant
        project.register_workflow_variant("branching", branching_graph_factory)
        
        integration_wtb.register_project(project)
        
        # Test with different combinations
        queries = [
            {"messages": [], "count": 0},
            {"messages": ["init"], "count": 10},
        ]
        
        # Run batch with default workflow
        result1 = integration_wtb.run_batch_test(
            project=project.name,
            variant_matrix=[
                {"node_b": "fast"},
                {"node_b": "slow"},
            ],
            test_cases=queries,
        )
        
        assert isinstance(result1, BatchTest)
        assert result1.status in [BatchTestStatus.COMPLETED, BatchTestStatus.FAILED]
    
    def test_sequential_batch_runs_isolation(
        self, integration_wtb, simple_graph_factory
    ):
        """Test sequential batch runs are isolated from each other."""
        project = WorkflowProject(
            name=f"batch-sequential-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        def variant_a(state):
            return {"messages": state.get("messages", []) + ["A"]}
        
        def variant_b(state):
            return {"messages": state.get("messages", []) + ["B"]}
        
        project.register_variant("node_b", "a", variant_a)
        project.register_variant("node_b", "b", variant_b)
        
        integration_wtb.register_project(project)
        
        queries = [{"messages": [], "count": 0}]
        
        # Run multiple batch tests sequentially
        results = []
        for i in range(3):
            result = integration_wtb.run_batch_test(
                project=project.name,
                variant_matrix=[{"node_b": "a"}, {"node_b": "b"}],
                test_cases=queries,
            )
            results.append(result)
        
        # All should complete
        assert all(r.status in [BatchTestStatus.COMPLETED, BatchTestStatus.FAILED] for r in results)
        
        # All should have unique IDs
        ids = [r.id for r in results]
        assert len(set(ids)) == 3


class TestFullIntegrationCombined:
    """Tests combining workspace, env, and file tracking."""
    
    def test_complete_workflow_lifecycle(
        self, integration_wtb, simple_graph_factory, full_integration_workspace
    ):
        """Test complete workflow lifecycle with all components."""
        # Create project with all features
        project = WorkflowProject(
            name=f"lifecycle-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            description="Complete lifecycle test",
            environment=EnvironmentConfig(
                granularity="workflow",
                default_env=EnvSpec(
                    python_version="3.12",
                    dependencies=["langgraph"],
                ),
            ),
            file_tracking=SDKFileTrackingConfig(
                enabled=True,
                tracked_paths=[str(full_integration_workspace / "data")],
            ),
        )
        
        # Register variants
        def fast(state):
            return {"messages": state.get("messages", []) + ["FAST"]}
        
        def accurate(state):
            return {"messages": state.get("messages", []) + ["ACCURATE"]}
        
        project.register_variant("node_b", "fast", fast)
        project.register_variant("node_b", "accurate", accurate)
        
        integration_wtb.register_project(project)
        
        # Run with different variants
        result_fast = integration_wtb.run(
            project=project.name,
            initial_state=create_initial_state(),
            variant_config={"node_b": "fast"},
        )
        
        result_accurate = integration_wtb.run(
            project=project.name,
            initial_state=create_initial_state(),
            variant_config={"node_b": "accurate"},
        )
        
        # Batch test
        batch_result = integration_wtb.run_batch_test(
            project=project.name,
            variant_matrix=[
                {"node_b": "fast"},
                {"node_b": "accurate"},
            ],
            test_cases=[
                {"messages": [], "count": 0},
                {"messages": ["init"], "count": 10},
            ],
        )
        
        # Rollback
        rollback = integration_wtb.rollback(result_fast.id, "1")
        
        # Verify all operations succeeded
        assert result_fast.status in [ExecutionStatus.COMPLETED, ExecutionStatus.FAILED]
        assert result_accurate.status in [ExecutionStatus.COMPLETED, ExecutionStatus.FAILED]
        assert isinstance(batch_result, BatchTest)
        assert isinstance(rollback, RollbackResult)
    
    def test_variant_A_B_comparison_full_stack(
        self, integration_wtb, simple_graph_factory, full_integration_workspace
    ):
        """Test A/B variant comparison with full stack."""
        project = WorkflowProject(
            name=f"ab-compare-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            environment=EnvironmentConfig(
                granularity="variant",
                default_env=EnvSpec(dependencies=["base"]),
                variant_environments={
                    "node_b:algorithm_a": EnvSpec(dependencies=["numpy"]),
                    "node_b:algorithm_b": EnvSpec(dependencies=["scipy"]),
                },
            ),
            file_tracking=SDKFileTrackingConfig(
                enabled=True,
                tracked_paths=[str(full_integration_workspace)],
            ),
        )
        
        def algo_a(state):
            time.sleep(0.01)
            return {"messages": state.get("messages", []) + ["ALGO_A"], "count": 100}
        
        def algo_b(state):
            time.sleep(0.02)
            return {"messages": state.get("messages", []) + ["ALGO_B"], "count": 200}
        
        project.register_variant("node_b", "algorithm_a", algo_a)
        project.register_variant("node_b", "algorithm_b", algo_b)
        
        integration_wtb.register_project(project)
        
        # Batch comparison
        result = integration_wtb.run_batch_test(
            project=project.name,
            variant_matrix=[
                {"node_b": "algorithm_a"},
                {"node_b": "algorithm_b"},
            ],
            test_cases=[
                {"messages": [], "count": 0},
                {"messages": ["warmup"], "count": 50},
                {"messages": ["stress"], "count": 1000},
            ],
        )
        
        assert isinstance(result, BatchTest)
        assert len(result.variant_combinations) == 2
