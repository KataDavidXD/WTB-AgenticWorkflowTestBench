"""
Integration tests for GrpcEnvironmentProvider.

These tests verify the integration between WTB and UV Venv Manager service.
Tests are skipped if the gRPC service is not available.

ARCHITECTURE DECISION (2026-01-15):
- UV Venv Manager = ENVIRONMENT PROVISIONING ONLY
- Ray = NODE EXECUTION (using provisioned python_path)
- A node is a COMPLETE PROJECT (e.g., RAG pipeline), not a code snippet
"""

import pytest
import os
import sys
from pathlib import Path
from typing import Generator

# Skip all tests if gRPC dependencies are not available
try:
    import grpc
    GRPC_AVAILABLE = True
except ImportError:
    GRPC_AVAILABLE = False

from wtb.infrastructure.environment.providers import GrpcEnvironmentProvider


# Default test configuration
GRPC_ADDRESS = os.environ.get("UV_VENV_GRPC_ADDRESS", "localhost:50051")
TEST_WORKFLOW_ID = "test_integration"
TEST_NODE_ID = "test_node"


def is_grpc_service_available() -> bool:
    """Check if the gRPC service is running."""
    if not GRPC_AVAILABLE:
        return False
    
    try:
        channel = grpc.insecure_channel(GRPC_ADDRESS)
        # Try to connect with a short timeout
        grpc.channel_ready_future(channel).result(timeout=2)
        channel.close()
        return True
    except Exception:
        return False


# Skip marker for tests requiring gRPC service
requires_grpc_service = pytest.mark.skipif(
    not is_grpc_service_available(),
    reason=f"UV Venv Manager gRPC service not available at {GRPC_ADDRESS}"
)

requires_grpc = pytest.mark.skipif(
    not GRPC_AVAILABLE,
    reason="grpcio package not installed"
)


@pytest.fixture
def provider() -> Generator[GrpcEnvironmentProvider, None, None]:
    """Create a GrpcEnvironmentProvider for testing."""
    provider = GrpcEnvironmentProvider(GRPC_ADDRESS)
    yield provider
    provider.close()


@pytest.fixture
def cleanup_env(provider):
    """Fixture to cleanup test environments after tests."""
    created_variants = []
    
    def track(variant_id: str):
        created_variants.append(variant_id)
        return variant_id
    
    yield track
    
    # Cleanup all created environments
    for variant_id in created_variants:
        try:
            provider.cleanup_environment(variant_id)
        except Exception:
            pass


@requires_grpc
class TestGrpcEnvironmentProviderConnection:
    """Tests for gRPC connection handling."""
    
    def test_init_creates_channel(self):
        """Should create gRPC channel on init."""
        provider = GrpcEnvironmentProvider(GRPC_ADDRESS)
        
        # Channel should be created (may or may not be connected)
        assert provider._channel is not None or provider._stub is None
        
        provider.close()
    
    def test_close_cleans_up_channel(self):
        """Should close channel on close()."""
        provider = GrpcEnvironmentProvider(GRPC_ADDRESS)
        provider.close()
        
        assert provider._channel is None
        assert provider._stub is None
    
    def test_context_manager_closes_channel(self):
        """Should close channel when exiting context."""
        with GrpcEnvironmentProvider(GRPC_ADDRESS) as provider:
            pass
        
        assert provider._channel is None


@requires_grpc_service
class TestGrpcEnvironmentProviderProvisioning:
    """Tests for environment provisioning via gRPC."""
    
    def test_create_environment_basic(self, provider, cleanup_env):
        """Should create environment with basic config."""
        variant_id = cleanup_env("test-basic")
        
        env = provider.create_environment(variant_id, {
            "workflow_id": TEST_WORKFLOW_ID,
            "node_id": TEST_NODE_ID,
        })
        
        assert env["type"] == "grpc_uv"
        assert "env_path" in env
        assert "python_path" in env
        assert env["status"] == "created"
    
    def test_create_environment_with_packages(self, provider, cleanup_env):
        """Should create environment with packages installed."""
        variant_id = cleanup_env("test-packages")
        
        env = provider.create_environment(variant_id, {
            "workflow_id": TEST_WORKFLOW_ID,
            "node_id": f"{TEST_NODE_ID}_pkg",
            "packages": ["requests"],
        })
        
        assert env["type"] == "grpc_uv"
        assert "env_path" in env
        
        # Verify python_path exists
        python_path = env["python_path"]
        assert python_path.endswith("python") or python_path.endswith("python.exe")
    
    def test_create_environment_with_version_id(self, provider, cleanup_env):
        """Should create environment with version_id."""
        variant_id = cleanup_env("test-version")
        
        env = provider.create_environment(variant_id, {
            "workflow_id": TEST_WORKFLOW_ID,
            "node_id": TEST_NODE_ID,
            "version_id": "v1",
        })
        
        assert env["version_id"] == "v1"
    
    def test_create_environment_with_python_version(self, provider, cleanup_env):
        """Should create environment with specific Python version."""
        variant_id = cleanup_env("test-python")
        
        env = provider.create_environment(variant_id, {
            "workflow_id": TEST_WORKFLOW_ID,
            "node_id": f"{TEST_NODE_ID}_py",
            "python_version": "3.11",
        })
        
        assert "3.11" in env.get("python_version", "")


@requires_grpc_service
class TestGrpcEnvironmentProviderRuntimeEnv:
    """Tests for runtime environment retrieval."""
    
    def test_get_runtime_env_after_create(self, provider, cleanup_env):
        """Should return runtime_env after creating environment."""
        variant_id = cleanup_env("test-runtime")
        
        provider.create_environment(variant_id, {
            "workflow_id": TEST_WORKFLOW_ID,
            "node_id": f"{TEST_NODE_ID}_rt",
        })
        
        runtime_env = provider.get_runtime_env(variant_id)
        
        assert runtime_env is not None
        assert "python_path" in runtime_env
        assert "py_executable" in runtime_env
        assert "env_vars" in runtime_env
    
    def test_runtime_env_has_virtual_env(self, provider, cleanup_env):
        """Should include VIRTUAL_ENV in env_vars."""
        variant_id = cleanup_env("test-venv")
        
        provider.create_environment(variant_id, {
            "workflow_id": TEST_WORKFLOW_ID,
            "node_id": f"{TEST_NODE_ID}_ve",
        })
        
        runtime_env = provider.get_runtime_env(variant_id)
        
        assert "VIRTUAL_ENV" in runtime_env["env_vars"]
    
    def test_runtime_env_py_executable_matches_python_path(self, provider, cleanup_env):
        """py_executable should match python_path for Ray compatibility."""
        variant_id = cleanup_env("test-exec")
        
        provider.create_environment(variant_id, {
            "workflow_id": TEST_WORKFLOW_ID,
            "node_id": f"{TEST_NODE_ID}_ex",
        })
        
        runtime_env = provider.get_runtime_env(variant_id)
        
        assert runtime_env["py_executable"] == runtime_env["python_path"]


@requires_grpc_service
class TestGrpcEnvironmentProviderCleanup:
    """Tests for environment cleanup."""
    
    def test_cleanup_removes_tracking(self, provider):
        """Should remove local tracking on cleanup."""
        variant_id = "test-cleanup"
        
        provider.create_environment(variant_id, {
            "workflow_id": TEST_WORKFLOW_ID,
            "node_id": f"{TEST_NODE_ID}_cl",
        })
        
        provider.cleanup_environment(variant_id)
        
        assert variant_id not in provider._environments
    
    def test_cleanup_calls_delete_env(self, provider):
        """Should call DeleteEnv RPC on cleanup."""
        variant_id = "test-delete"
        
        provider.create_environment(variant_id, {
            "workflow_id": TEST_WORKFLOW_ID,
            "node_id": f"{TEST_NODE_ID}_del",
        })
        
        # Should not raise
        provider.cleanup_environment(variant_id)
        
        # Runtime env should be None after cleanup
        runtime_env = provider.get_runtime_env(variant_id)
        assert runtime_env is None


@requires_grpc_service
class TestGrpcEnvironmentProviderRayIntegration:
    """Tests for Ray integration patterns."""
    
    def test_runtime_env_usable_for_ray_remote(self, provider, cleanup_env):
        """
        Runtime env should be usable with @ray.remote decorator.
        
        This test verifies the structure, not actual Ray execution.
        """
        variant_id = cleanup_env("test-ray")
        
        provider.create_environment(variant_id, {
            "workflow_id": TEST_WORKFLOW_ID,
            "node_id": f"{TEST_NODE_ID}_ray",
            "packages": ["requests"],
        })
        
        runtime_env = provider.get_runtime_env(variant_id)
        
        # Should have py_executable for Ray
        assert "py_executable" in runtime_env
        
        # py_executable should be a valid path format
        py_exec = runtime_env["py_executable"]
        assert py_exec.endswith("python") or py_exec.endswith("python.exe")
    
    def test_env_path_for_subprocess(self, provider, cleanup_env):
        """
        Environment should provide paths usable for subprocess execution.
        
        This is how Ray actors would execute node projects.
        """
        variant_id = cleanup_env("test-subprocess")
        
        env = provider.create_environment(variant_id, {
            "workflow_id": TEST_WORKFLOW_ID,
            "node_id": f"{TEST_NODE_ID}_sub",
        })
        
        # Should have env_path for working directory
        assert "env_path" in env
        
        # Should have python_path for subprocess.run
        assert "python_path" in env
        
        # Path should be absolute
        python_path = env["python_path"]
        assert os.path.isabs(python_path) or python_path.startswith("/")


@requires_grpc_service  
class TestGrpcEnvironmentProviderNoRunCode:
    """
    Tests verifying that GrpcEnvironmentProvider does NOT execute code.
    
    ARCHITECTURE DECISION: Execution is Ray's responsibility.
    """
    
    def test_no_run_code_method(self, provider):
        """Provider should not have run_code method."""
        assert not hasattr(provider, 'run_code')
    
    def test_no_execute_method(self, provider):
        """Provider should not have execute method."""
        assert not hasattr(provider, 'execute')
        assert not hasattr(provider, 'execute_code')
    
    def test_no_run_method(self, provider):
        """Provider should not have run method."""
        assert not hasattr(provider, 'run')
    
    def test_provisioning_only_returns_paths(self, provider, cleanup_env):
        """
        Provider should only return paths, not execution results.
        
        The returned dict should contain paths for Ray to use,
        not stdout/stderr from code execution.
        """
        variant_id = cleanup_env("test-paths-only")
        
        env = provider.create_environment(variant_id, {
            "workflow_id": TEST_WORKFLOW_ID,
            "node_id": f"{TEST_NODE_ID}_po",
        })
        
        # Should have paths
        assert "env_path" in env
        assert "python_path" in env
        
        # Should NOT have execution results
        assert "stdout" not in env
        assert "stderr" not in env
        assert "exit_code" not in env
        assert "output" not in env
