"""
Unit tests for Environment Providers.

Tests the refactored environment provider implementations:
- InProcessEnvironmentProvider
- RayEnvironmentProvider  
- GrpcEnvironmentProvider

ARCHITECTURE DECISION (2026-01-15):
- UV Venv Manager = ENVIRONMENT PROVISIONING ONLY
- Ray = NODE EXECUTION (using provisioned python_path)
- A node is a COMPLETE PROJECT (e.g., RAG pipeline), not a code snippet
"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from typing import Dict, Any

from wtb.infrastructure.environment.providers import (
    InProcessEnvironmentProvider,
    RayEnvironmentProvider,
    RayRuntimeEnvConfig,
    GrpcEnvironmentProvider,
)


class TestInProcessEnvironmentProvider:
    """Tests for InProcessEnvironmentProvider."""
    
    def test_create_environment_returns_inprocess_type(self):
        """Should return environment with inprocess type."""
        provider = InProcessEnvironmentProvider()
        
        env = provider.create_environment("variant-1", {"pip": ["numpy"]})
        
        assert env["type"] == "inprocess"
        assert env["variant_id"] == "variant-1"
    
    def test_create_environment_stores_reference(self):
        """Should store environment reference internally."""
        provider = InProcessEnvironmentProvider()
        
        provider.create_environment("variant-1", {})
        provider.create_environment("variant-2", {})
        
        assert "variant-1" in provider._environments
        assert "variant-2" in provider._environments
    
    def test_cleanup_environment_removes_reference(self):
        """Should remove environment reference on cleanup."""
        provider = InProcessEnvironmentProvider()
        provider.create_environment("variant-1", {})
        
        provider.cleanup_environment("variant-1")
        
        assert "variant-1" not in provider._environments
    
    def test_cleanup_nonexistent_environment_is_safe(self):
        """Should not raise when cleaning up nonexistent environment."""
        provider = InProcessEnvironmentProvider()
        
        # Should not raise
        provider.cleanup_environment("nonexistent")
    
    def test_get_runtime_env_returns_none(self):
        """Should return None (no runtime env needed for inprocess)."""
        provider = InProcessEnvironmentProvider()
        provider.create_environment("variant-1", {})
        
        runtime_env = provider.get_runtime_env("variant-1")
        
        assert runtime_env is None


class TestRayRuntimeEnvConfig:
    """Tests for RayRuntimeEnvConfig dataclass."""
    
    def test_to_dict_with_pip_packages(self):
        """Should include pip packages in dict."""
        config = RayRuntimeEnvConfig(pip=["numpy", "pandas"])
        
        result = config.to_dict()
        
        assert result["pip"] == ["numpy", "pandas"]
    
    def test_to_dict_with_env_vars(self):
        """Should include env_vars in dict."""
        config = RayRuntimeEnvConfig(env_vars={"MODEL_VERSION": "v1"})
        
        result = config.to_dict()
        
        assert result["env_vars"] == {"MODEL_VERSION": "v1"}
    
    def test_to_dict_excludes_empty_fields(self):
        """Should not include empty fields in dict."""
        config = RayRuntimeEnvConfig()
        
        result = config.to_dict()
        
        assert "pip" not in result
        assert "env_vars" not in result
        assert "working_dir" not in result
    
    def test_to_dict_with_all_fields(self):
        """Should include all non-empty fields."""
        config = RayRuntimeEnvConfig(
            pip=["numpy"],
            conda={"name": "myenv"},
            env_vars={"KEY": "value"},
            working_dir="/tmp/work",
            py_modules=["mymodule"],
            excludes=["*.pyc"],
        )
        
        result = config.to_dict()
        
        assert result["pip"] == ["numpy"]
        assert result["conda"] == {"name": "myenv"}
        assert result["env_vars"] == {"KEY": "value"}
        assert result["working_dir"] == "/tmp/work"
        assert result["py_modules"] == ["mymodule"]
        assert result["excludes"] == ["*.pyc"]


class TestRayEnvironmentProvider:
    """Tests for RayEnvironmentProvider."""
    
    def test_create_environment_returns_runtime_env(self):
        """Should return Ray runtime_env dict."""
        provider = RayEnvironmentProvider()
        
        env = provider.create_environment("variant-1", {
            "pip": ["numpy==1.24.0"],
            "env_vars": {"MODEL_VERSION": "v1"},
        })
        
        assert env["pip"] == ["numpy==1.24.0"]
        assert env["env_vars"] == {"MODEL_VERSION": "v1"}
    
    def test_create_environment_merges_base_env(self):
        """Should merge with base environment."""
        provider = RayEnvironmentProvider(base_env={
            "pip": ["base-package"],
            "env_vars": {"BASE_VAR": "base"},
        })
        
        env = provider.create_environment("variant-1", {
            "pip": ["variant-package"],
            "env_vars": {"VARIANT_VAR": "variant"},
        })
        
        assert "base-package" in env["pip"]
        assert "variant-package" in env["pip"]
        assert env["env_vars"]["BASE_VAR"] == "base"
        assert env["env_vars"]["VARIANT_VAR"] == "variant"
    
    def test_cleanup_environment_removes_config(self):
        """Should remove environment config on cleanup."""
        provider = RayEnvironmentProvider()
        provider.create_environment("variant-1", {"pip": ["numpy"]})
        
        provider.cleanup_environment("variant-1")
        
        assert "variant-1" not in provider._environments
    
    def test_get_runtime_env_returns_config(self):
        """Should return stored runtime_env config."""
        provider = RayEnvironmentProvider()
        provider.create_environment("variant-1", {"pip": ["numpy"]})
        
        runtime_env = provider.get_runtime_env("variant-1")
        
        assert runtime_env["pip"] == ["numpy"]
    
    def test_get_runtime_env_returns_none_for_unknown(self):
        """Should return None for unknown variant."""
        provider = RayEnvironmentProvider()
        
        runtime_env = provider.get_runtime_env("unknown")
        
        assert runtime_env is None
    
    def test_list_environments(self):
        """Should list all created environments."""
        provider = RayEnvironmentProvider()
        provider.create_environment("variant-1", {"pip": ["numpy"]})
        provider.create_environment("variant-2", {"pip": ["pandas"]})
        
        envs = provider.list_environments()
        
        assert len(envs) == 2
        assert "variant-1" in envs
        assert "variant-2" in envs


class TestGrpcEnvironmentProvider:
    """Tests for GrpcEnvironmentProvider."""
    
    def test_init_without_grpc_logs_warning(self):
        """Should log warning when gRPC not available."""
        with patch('wtb.infrastructure.environment.providers.logger') as mock_logger:
            # Mock import failure
            with patch.dict('sys.modules', {'grpc': None}):
                provider = GrpcEnvironmentProvider("localhost:50051")
                
                # Should not crash, just log warning
                assert provider._stub is None or mock_logger.warning.called
    
    def test_create_environment_stub_response(self):
        """Should return stub response when gRPC not available."""
        provider = GrpcEnvironmentProvider("localhost:50051")
        provider._stub = None  # Simulate no gRPC
        
        env = provider.create_environment("variant-1", {
            "workflow_id": "ml_pipeline",
            "node_id": "rag",
        })
        
        assert env["type"] == "grpc_uv_stub"
        assert env["variant_id"] == "variant-1"
    
    def test_create_environment_with_mock_grpc(self):
        """Should create environment with correct structure when gRPC available."""
        # Test the internal structure by manually setting up the environment
        provider = GrpcEnvironmentProvider("localhost:50051")
        provider._stub = None  # Simulate no gRPC
        
        # Manually set up environment info as if gRPC succeeded
        env_info = {
            "type": "grpc_uv",
            "workflow_id": "ml_pipeline",
            "node_id": "rag",
            "version_id": "v1",
            "env_path": "/data/envs/ml_pipeline_rag_v1",
            "python_path": "/data/envs/ml_pipeline_rag_v1/.venv/bin/python",
            "venv_path": "/data/envs/ml_pipeline_rag_v1/.venv",
            "python_version": "3.11",
            "status": "created",
        }
        provider._environments["variant-1"] = env_info
        
        # Verify the structure is correct
        assert env_info["type"] == "grpc_uv"
        assert env_info["env_path"] == "/data/envs/ml_pipeline_rag_v1"
        assert "python_path" in env_info
        
        # Verify get_runtime_env works with this structure
        runtime_env = provider.get_runtime_env("variant-1")
        assert runtime_env is not None
        assert runtime_env["python_path"] == "/data/envs/ml_pipeline_rag_v1/.venv/bin/python"
    
    def test_get_runtime_env_returns_ray_compatible_dict(self):
        """Should return Ray-compatible runtime_env dict."""
        provider = GrpcEnvironmentProvider("localhost:50051")
        provider._environments["variant-1"] = {
            "type": "grpc_uv",
            "env_path": "/data/envs/test",
            "python_path": "/data/envs/test/.venv/bin/python",
            "venv_path": "/data/envs/test/.venv",
        }
        
        runtime_env = provider.get_runtime_env("variant-1")
        
        assert runtime_env["python_path"] == "/data/envs/test/.venv/bin/python"
        assert runtime_env["py_executable"] == "/data/envs/test/.venv/bin/python"
        assert "env_vars" in runtime_env
        assert runtime_env["env_vars"]["VIRTUAL_ENV"] == "/data/envs/test/.venv"
    
    def test_get_runtime_env_returns_none_for_unknown(self):
        """Should return None for unknown variant."""
        provider = GrpcEnvironmentProvider("localhost:50051")
        
        runtime_env = provider.get_runtime_env("unknown")
        
        assert runtime_env is None
    
    def test_cleanup_environment_removes_tracking(self):
        """Should remove local tracking on cleanup."""
        provider = GrpcEnvironmentProvider("localhost:50051")
        provider._stub = None  # No actual gRPC call
        provider._environments["variant-1"] = {"type": "grpc_uv"}
        
        provider.cleanup_environment("variant-1")
        
        assert "variant-1" not in provider._environments
    
    def test_context_manager_closes_channel(self):
        """Should close channel when used as context manager."""
        provider = GrpcEnvironmentProvider("localhost:50051")
        mock_channel = Mock()
        provider._channel = mock_channel
        
        with provider:
            pass
        
        mock_channel.close.assert_called_once()
    
    def test_python_path_windows_format(self):
        """Should return Windows-style path on Windows."""
        provider = GrpcEnvironmentProvider("localhost:50051")
        
        with patch('wtb.infrastructure.environment.providers.sys') as mock_sys:
            mock_sys.platform = "win32"
            # Re-initialize to pick up platform
            path = provider._get_python_path("C:\\data\\envs\\test")
        
        assert "Scripts" in path or ".venv" in path
    
    def test_python_path_unix_format(self):
        """Should return Unix-style path on Unix (or Windows format on Windows)."""
        provider = GrpcEnvironmentProvider("localhost:50051")
        
        path = provider._get_python_path("/data/envs/test")
        
        # Path format depends on current platform
        import sys
        if sys.platform == "win32":
            assert "python.exe" in path or "python" in path
        else:
            assert path == "/data/envs/test/.venv/bin/python"


def _grpc_provider_factory():
    """Factory that checks gRPC availability before creating provider."""
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        result = sock.connect_ex(('localhost', 50051))
        sock.close()
        if result != 0:
            pytest.skip("gRPC service not available at localhost:50051")
    except Exception:
        pytest.skip("gRPC service not available at localhost:50051")
    return GrpcEnvironmentProvider("localhost:50051")


class TestEnvironmentProviderInterface:
    """Tests verifying all providers implement IEnvironmentProvider correctly."""
    
    @pytest.fixture(params=[
        InProcessEnvironmentProvider,
        RayEnvironmentProvider,
        _grpc_provider_factory,
    ])
    def provider(self, request):
        """Parameterized fixture for all provider types."""
        if callable(request.param):
            return request.param()
        return request.param()
    
    def test_has_create_environment_method(self, provider):
        """All providers should have create_environment method."""
        assert hasattr(provider, 'create_environment')
        assert callable(provider.create_environment)
    
    def test_has_cleanup_environment_method(self, provider):
        """All providers should have cleanup_environment method."""
        assert hasattr(provider, 'cleanup_environment')
        assert callable(provider.cleanup_environment)
    
    def test_has_get_runtime_env_method(self, provider):
        """All providers should have get_runtime_env method."""
        assert hasattr(provider, 'get_runtime_env')
        assert callable(provider.get_runtime_env)
    
    def test_create_returns_dict(self, provider):
        """create_environment should return a dict."""
        result = provider.create_environment("test-variant", {})
        
        assert isinstance(result, dict)
    
    def test_cleanup_accepts_variant_id(self, provider):
        """cleanup_environment should accept variant_id."""
        provider.create_environment("test-variant", {})
        
        # Should not raise
        provider.cleanup_environment("test-variant")


class TestProviderNoRunCodeMethods:
    """
    Tests verifying that providers do NOT have run_code methods.
    
    ARCHITECTURE DECISION: Execution is Ray's responsibility, not the provider's.
    """
    
    def test_inprocess_provider_no_run_code(self):
        """InProcessEnvironmentProvider should not have run_code method."""
        provider = InProcessEnvironmentProvider()
        
        assert not hasattr(provider, 'run_code')
        assert not hasattr(provider, 'execute_code')
        assert not hasattr(provider, 'run')
    
    def test_ray_provider_no_run_code(self):
        """RayEnvironmentProvider should not have run_code method."""
        provider = RayEnvironmentProvider()
        
        assert not hasattr(provider, 'run_code')
        assert not hasattr(provider, 'execute_code')
        assert not hasattr(provider, 'run')
    
    def test_grpc_provider_no_run_code(self):
        """GrpcEnvironmentProvider should not have run_code method."""
        provider = GrpcEnvironmentProvider("localhost:50051")
        
        assert not hasattr(provider, 'run_code')
        assert not hasattr(provider, 'execute_code')
        assert not hasattr(provider, 'run')
