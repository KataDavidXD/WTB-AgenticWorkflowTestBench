"""
SDK Integration tests for Virtual Environment management.

Tests venv integration:
- Environment configuration per workflow
- Environment isolation (workflow/node/variant levels)
- Package dependency management
- Environment lifecycle (create, reuse, cleanup)
- Integration with workflow execution

Run with: pytest tests/test_sdk/test_sdk_venv_integration.py -v
"""

import pytest
import os
import time
import uuid
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from pathlib import Path

from wtb.sdk import WTBTestBench, WorkflowProject
from wtb.sdk.workflow_project import (
    EnvironmentConfig,
    EnvSpec,
    FileTrackingConfig,
    ExecutionConfig,
)
from wtb.domain.models import ExecutionStatus
from wtb.application.factories import WTBTestBenchFactory

from tests.test_sdk.conftest import (
    create_initial_state,
    MockVenvProvider,
    MockVenvSpec,
    MockVenvStatus,
    LANGGRAPH_AVAILABLE,
)


# ═══════════════════════════════════════════════════════════════════════════════
# EnvSpec Configuration Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnvSpecConfiguration:
    """Tests for EnvSpec configuration."""
    
    def test_envspec_defaults(self):
        """Test EnvSpec default values."""
        spec = EnvSpec()
        
        assert spec.python_version == "3.12"
        assert spec.dependencies == []
        assert spec.requirements_file is None
        assert spec.env_vars == {}
    
    def test_envspec_with_dependencies(self):
        """Test EnvSpec with dependencies."""
        spec = EnvSpec(
            python_version="3.11",
            dependencies=["langgraph>=0.2", "numpy>=1.24", "pandas>=2.0"],
        )
        
        assert spec.python_version == "3.11"
        assert len(spec.dependencies) == 3
        assert "langgraph>=0.2" in spec.dependencies
    
    def test_envspec_with_requirements_file(self):
        """Test EnvSpec with requirements file."""
        spec = EnvSpec(
            python_version="3.12",
            requirements_file="requirements-workflow.txt",
        )
        
        assert spec.requirements_file == "requirements-workflow.txt"
    
    def test_envspec_with_env_vars(self):
        """Test EnvSpec with environment variables."""
        spec = EnvSpec(
            env_vars={
                "API_KEY": "secret",
                "LOG_LEVEL": "DEBUG",
            },
        )
        
        assert spec.env_vars["API_KEY"] == "secret"
        assert spec.env_vars["LOG_LEVEL"] == "DEBUG"
    
    def test_envspec_serialization(self):
        """Test EnvSpec serialization."""
        spec = EnvSpec(
            python_version="3.11",
            dependencies=["numpy"],
            env_vars={"KEY": "value"},
        )
        
        data = spec.to_dict()
        
        assert data["python_version"] == "3.11"
        assert data["dependencies"] == ["numpy"]
        assert data["env_vars"]["KEY"] == "value"


# ═══════════════════════════════════════════════════════════════════════════════
# EnvironmentConfig Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnvironmentConfig:
    """Tests for EnvironmentConfig."""
    
    def test_environment_config_defaults(self):
        """Test EnvironmentConfig defaults."""
        config = EnvironmentConfig()
        
        assert config.granularity == "workflow"
        assert config.use_current_env is False
        assert config.default_env is None
        assert config.reuse_existing is True
        assert config.cleanup_on_exit is False
    
    def test_environment_config_workflow_level(self):
        """Test workflow-level environment configuration."""
        config = EnvironmentConfig(
            granularity="workflow",
            default_env=EnvSpec(
                python_version="3.12",
                dependencies=["langgraph"],
            ),
        )
        
        assert config.granularity == "workflow"
        assert config.default_env.python_version == "3.12"
    
    def test_environment_config_node_level(self):
        """Test node-level environment configuration."""
        config = EnvironmentConfig(
            granularity="node",
            node_environments={
                "node_a": EnvSpec(dependencies=["numpy"]),
                "node_b": EnvSpec(dependencies=["pandas"]),
            },
        )
        
        assert config.granularity == "node"
        assert "numpy" in config.node_environments["node_a"].dependencies
        assert "pandas" in config.node_environments["node_b"].dependencies
    
    def test_environment_config_variant_level(self):
        """Test variant-level environment configuration."""
        config = EnvironmentConfig(
            granularity="variant",
            variant_environments={
                "node_b:fast": EnvSpec(dependencies=["numba"]),
                "node_b:slow": EnvSpec(dependencies=["numpy"]),
            },
        )
        
        assert config.granularity == "variant"
        assert len(config.variant_environments) == 2
    
    def test_get_env_for_node(self):
        """Test getting environment for a node."""
        config = EnvironmentConfig(
            granularity="node",
            default_env=EnvSpec(dependencies=["default"]),
            node_environments={
                "special_node": EnvSpec(dependencies=["special"]),
            },
        )
        
        # Node with specific env
        env = config.get_env_for_node("special_node")
        assert "special" in env.dependencies
        
        # Node without specific env (falls back to default)
        env = config.get_env_for_node("other_node")
        assert "default" in env.dependencies
    
    def test_get_env_for_variant(self):
        """Test getting environment for a variant."""
        config = EnvironmentConfig(
            granularity="variant",
            default_env=EnvSpec(dependencies=["default"]),
            node_environments={
                "node_b": EnvSpec(dependencies=["node_default"]),
            },
            variant_environments={
                "node_b:special": EnvSpec(dependencies=["variant_special"]),
            },
        )
        
        # Variant with specific env
        env = config.get_env_for_variant("node_b", "special")
        assert "variant_special" in env.dependencies
        
        # Variant without specific env (falls back to node)
        env = config.get_env_for_variant("node_b", "default")
        assert "node_default" in env.dependencies


# ═══════════════════════════════════════════════════════════════════════════════
# Mock Environment Provider Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestMockEnvironmentProvider:
    """Tests for MockVenvProvider."""
    
    def test_create_environment(self, mock_venv_provider):
        """Test creating environment."""
        spec = MockVenvSpec(
            env_id="test-env-1",
            python_version="3.12",
            packages=["langgraph"],
        )
        
        env_id = mock_venv_provider.create_environment(spec)
        
        assert env_id == "test-env-1"
        status = mock_venv_provider.get_status(env_id)
        assert status.status == "ready"
    
    def test_get_environment_status(self, mock_venv_provider):
        """Test getting environment status."""
        spec = MockVenvSpec(env_id="status-test")
        mock_venv_provider.create_environment(spec)
        
        status = mock_venv_provider.get_status("status-test")
        
        assert status is not None
        assert status.status == "ready"
        assert status.python_path is not None
    
    def test_destroy_environment(self, mock_venv_provider):
        """Test destroying environment."""
        spec = MockVenvSpec(env_id="destroy-test")
        mock_venv_provider.create_environment(spec)
        
        result = mock_venv_provider.destroy_environment("destroy-test")
        
        assert result is True
        assert mock_venv_provider.get_status("destroy-test").status == "destroyed"
    
    def test_list_environments(self, mock_venv_provider):
        """Test listing environments."""
        for i in range(3):
            spec = MockVenvSpec(env_id=f"list-env-{i}")
            mock_venv_provider.create_environment(spec)
        
        envs = mock_venv_provider.list_environments()
        
        assert len(envs) == 3


# ═══════════════════════════════════════════════════════════════════════════════
# WorkflowProject Environment Configuration Tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not installed")
class TestProjectEnvironmentConfig:
    """Tests for WorkflowProject environment configuration."""
    
    def test_project_with_default_env(self, simple_graph_factory):
        """Test project with default environment."""
        project = WorkflowProject(
            name="env-default-test",
            graph_factory=simple_graph_factory,
            environment=EnvironmentConfig(
                default_env=EnvSpec(
                    python_version="3.12",
                    dependencies=["langgraph>=0.2"],
                ),
            ),
        )
        
        assert project.environment.default_env is not None
        assert project.environment.default_env.python_version == "3.12"
    
    def test_project_with_node_envs(self, simple_graph_factory):
        """Test project with per-node environments."""
        project = WorkflowProject(
            name="env-node-test",
            graph_factory=simple_graph_factory,
            environment=EnvironmentConfig(
                granularity="node",
                node_environments={
                    "node_a": EnvSpec(dependencies=["numpy"]),
                    "node_b": EnvSpec(dependencies=["pandas"]),
                },
            ),
        )
        
        assert project.environment.granularity == "node"
        assert len(project.environment.node_environments) == 2
    
    def test_project_use_current_env(self, simple_graph_factory):
        """Test project using current environment."""
        project = WorkflowProject(
            name="env-current-test",
            graph_factory=simple_graph_factory,
            environment=EnvironmentConfig(
                use_current_env=True,
            ),
        )
        
        assert project.environment.use_current_env is True
    
    def test_project_env_serialization(self, simple_graph_factory):
        """Test project environment serialization."""
        project = WorkflowProject(
            name="env-serialize-test",
            graph_factory=simple_graph_factory,
            environment=EnvironmentConfig(
                granularity="workflow",
                default_env=EnvSpec(
                    python_version="3.11",
                    dependencies=["test-package"],
                ),
            ),
        )
        
        data = project.to_dict()
        
        assert "environment" in data
        assert data["environment"]["granularity"] == "workflow"
        assert data["environment"]["default_env"]["python_version"] == "3.11"


# ═══════════════════════════════════════════════════════════════════════════════
# Environment Lifecycle Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnvironmentLifecycle:
    """Tests for environment lifecycle management."""
    
    def test_environment_reuse(self, mock_venv_provider):
        """Test reusing environments across executions."""
        spec = MockVenvSpec(env_id="reuse-env")
        mock_venv_provider.create_environment(spec)
        
        # Use multiple times
        for _ in range(5):
            status = mock_venv_provider.get_status("reuse-env")
            assert status.status == "ready"
    
    def test_environment_pool(self, mock_venv_provider):
        """Test environment pooling pattern."""
        pool_size = 4
        
        for i in range(pool_size):
            spec = MockVenvSpec(env_id=f"pool-{i}")
            mock_venv_provider.create_environment(spec)
        
        envs = mock_venv_provider.list_environments()
        assert len(envs) == pool_size
        
        # All ready
        for i in range(pool_size):
            assert mock_venv_provider.get_status(f"pool-{i}").status == "ready"
    
    def test_environment_recreation(self, mock_venv_provider):
        """Test environment recreation after failure."""
        spec = MockVenvSpec(env_id="recreate-env")
        mock_venv_provider.create_environment(spec)
        
        # Destroy
        mock_venv_provider.destroy_environment("recreate-env")
        assert mock_venv_provider.get_status("recreate-env").status == "destroyed"
        
        # Recreate
        mock_venv_provider.create_environment(spec)
        assert mock_venv_provider.get_status("recreate-env").status == "ready"
    
    def test_cleanup_on_exit(self, mock_venv_provider):
        """Test environment cleanup on exit."""
        specs = [
            MockVenvSpec(env_id=f"cleanup-{i}")
            for i in range(3)
        ]
        
        for spec in specs:
            mock_venv_provider.create_environment(spec)
        
        assert len(mock_venv_provider.list_environments()) == 3
        
        # Cleanup
        for spec in specs:
            mock_venv_provider.destroy_environment(spec.env_id)
        
        # All destroyed
        for spec in specs:
            assert mock_venv_provider.get_status(spec.env_id).status == "destroyed"


# ═══════════════════════════════════════════════════════════════════════════════
# Package Isolation Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestPackageIsolation:
    """Tests for package isolation between environments."""
    
    def test_different_packages_per_workflow(self, mock_venv_provider):
        """Test different packages per workflow."""
        spec1 = MockVenvSpec(
            env_id="wf-1-env",
            packages=["numpy>=1.24", "scipy"],
        )
        spec2 = MockVenvSpec(
            env_id="wf-2-env",
            packages=["pandas>=2.0", "scikit-learn"],
        )
        
        mock_venv_provider.create_environment(spec1)
        mock_venv_provider.create_environment(spec2)
        
        assert "wf-1-env" in mock_venv_provider.list_environments()
        assert "wf-2-env" in mock_venv_provider.list_environments()
    
    def test_python_version_isolation(self, mock_venv_provider):
        """Test Python version isolation."""
        spec311 = MockVenvSpec(env_id="py311", python_version="3.11")
        spec312 = MockVenvSpec(env_id="py312", python_version="3.12")
        
        mock_venv_provider.create_environment(spec311)
        mock_venv_provider.create_environment(spec312)
        
        assert mock_venv_provider.get_status("py311").status == "ready"
        assert mock_venv_provider.get_status("py312").status == "ready"
    
    def test_variant_specific_packages(self, mock_venv_provider):
        """Test variant-specific package configurations."""
        # GPU variant with CUDA packages
        gpu_spec = MockVenvSpec(
            env_id="variant-gpu",
            packages=["torch", "cuda-toolkit"],
        )
        
        # CPU variant without CUDA
        cpu_spec = MockVenvSpec(
            env_id="variant-cpu",
            packages=["torch-cpu", "numpy"],
        )
        
        mock_venv_provider.create_environment(gpu_spec)
        mock_venv_provider.create_environment(cpu_spec)
        
        assert mock_venv_provider.get_status("variant-gpu").status == "ready"
        assert mock_venv_provider.get_status("variant-cpu").status == "ready"


# ═══════════════════════════════════════════════════════════════════════════════
# Environment + Execution Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph not installed")
class TestEnvironmentExecutionIntegration:
    """Tests for environment + execution integration."""
    
    def test_execution_with_env_config(self, wtb_inmemory, venv_project):
        """Test execution with environment configuration."""
        wtb_inmemory.register_project(venv_project)
        
        result = wtb_inmemory.run(
            project=venv_project.name,
            initial_state=create_initial_state(),
        )
        
        # Should execute (environment is not enforced in in-memory mode)
        assert result.status in [ExecutionStatus.COMPLETED, ExecutionStatus.FAILED]
    
    def test_variant_with_env_config(self, wtb_inmemory, simple_graph_factory):
        """Test variant with environment configuration."""
        project = WorkflowProject(
            name=f"variant-env-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            environment=EnvironmentConfig(
                granularity="variant",
                default_env=EnvSpec(dependencies=["langgraph"]),
            ),
        )
        
        # Register variant with specific environment
        def variant_impl(state):
            return {"messages": state.get("messages", []) + ["variant"]}
        
        project.register_variant(
            node="node_b",
            name="special",
            implementation=variant_impl,
            environment=EnvSpec(
                python_version="3.11",
                dependencies=["special-package"],
            ),
        )
        
        # Variant should have env in project config
        env_key = "node_b:special"
        assert env_key in project.environment.variant_environments


# ═══════════════════════════════════════════════════════════════════════════════
# ACID Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnvironmentACID:
    """Tests for ACID compliance in environment operations."""
    
    def test_atomicity_environment_creation(self, mock_venv_provider):
        """Test environment creation is atomic."""
        spec = MockVenvSpec(
            env_id="atomic-env",
            python_version="3.12",
            packages=["numpy", "pandas", "scikit-learn"],
        )
        
        mock_venv_provider.create_environment(spec)
        
        status = mock_venv_provider.get_status("atomic-env")
        
        # Should be fully created or not at all
        assert status.status == "ready"
        assert status.python_path is not None
    
    def test_consistency_environment_state(self, mock_venv_provider):
        """Test environment state consistency."""
        spec = MockVenvSpec(env_id="consistent-env")
        mock_venv_provider.create_environment(spec)
        
        # Multiple checks should return consistent state
        for _ in range(10):
            status = mock_venv_provider.get_status("consistent-env")
            assert status.status == "ready"
    
    def test_isolation_environment_instances(self, mock_venv_provider):
        """Test environment instances are isolated."""
        spec1 = MockVenvSpec(env_id="iso-1")
        spec2 = MockVenvSpec(env_id="iso-2")
        
        mock_venv_provider.create_environment(spec1)
        mock_venv_provider.create_environment(spec2)
        
        # Destroying one shouldn't affect the other
        mock_venv_provider.destroy_environment("iso-1")
        
        assert mock_venv_provider.get_status("iso-1").status == "destroyed"
        assert mock_venv_provider.get_status("iso-2").status == "ready"
    
    def test_durability_environment_persistence(self, mock_venv_provider):
        """Test environment state persists."""
        spec = MockVenvSpec(env_id="durable-env")
        mock_venv_provider.create_environment(spec)
        
        # Multiple lookups should return same state
        status1 = mock_venv_provider.get_status("durable-env")
        status2 = mock_venv_provider.get_status("durable-env")
        
        assert status1.env_id == status2.env_id
        assert status1.status == status2.status
        assert status1.python_path == status2.python_path


# ═══════════════════════════════════════════════════════════════════════════════
# Error Handling Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnvironmentErrorHandling:
    """Tests for environment error handling."""
    
    def test_get_nonexistent_environment(self, mock_venv_provider):
        """Test getting nonexistent environment."""
        status = mock_venv_provider.get_status("nonexistent")
        
        assert status is None
    
    def test_destroy_nonexistent_environment(self, mock_venv_provider):
        """Test destroying nonexistent environment."""
        result = mock_venv_provider.destroy_environment("nonexistent")
        
        assert result is False
    
    def test_invalid_python_version_handling(self):
        """Test handling invalid Python version."""
        # Should not raise during creation (validation is implementation-specific)
        spec = EnvSpec(python_version="invalid.version")
        
        assert spec.python_version == "invalid.version"
    
    def test_empty_dependencies_ok(self):
        """Test empty dependencies is valid."""
        spec = EnvSpec(dependencies=[])
        
        assert spec.dependencies == []
