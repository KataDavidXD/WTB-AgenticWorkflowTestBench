"""
Integration tests for UV Venv Manager + VenvCache + Workspace.

Tests REAL infrastructure:
- gRPC connection to uv_venv_manager Docker service (localhost:50051)
- GrpcEnvironmentProvider full lifecycle (create, status, cleanup, delete)
- VenvSpec hashing, VenvCacheManager lifecycle
- Workspace python_path resolution

Requires: Docker container 'uv_venv_manager' running on localhost:50051
"""

import os
import sys
import uuid
import tempfile
import pytest
from pathlib import Path
from typing import Optional

GRPC_ADDRESS = "localhost:50051"


def _grpc_available() -> bool:
    """Check if gRPC and uv_venv_manager service are reachable."""
    try:
        import grpc
        from wtb.infrastructure.environment.uv_manager.grpc_generated import (
            env_manager_pb2 as pb2,
            env_manager_pb2_grpc as pb2_grpc,
        )
        ch = grpc.insecure_channel(GRPC_ADDRESS)
        stub = pb2_grpc.EnvManagerServiceStub(ch)
        stub.GetEnvStatus(
            pb2.GetEnvStatusRequest(workflow_id="health", node_id="check"),
            timeout=5,
        )
        ch.close()
        return True
    except Exception:
        return False


requires_grpc = pytest.mark.skipif(
    not _grpc_available(),
    reason="uv_venv_manager gRPC service not available on localhost:50051",
)


# ═══════════════════════════════════════════════════════════════
# VenvSpec Hashing (uses real VenvSpec from venv_cache.py)
# ═══════════════════════════════════════════════════════════════


class TestVenvSpecHashing:

    def test_same_spec_produces_same_hash(self):
        from wtb.infrastructure.environment.venv_cache import VenvSpec
        s1 = VenvSpec(python_version="3.12", packages=["numpy", "pandas"])
        s2 = VenvSpec(python_version="3.12", packages=["numpy", "pandas"])
        assert s1.compute_hash() == s2.compute_hash()

    def test_different_packages_produce_different_hash(self):
        from wtb.infrastructure.environment.venv_cache import VenvSpec
        s1 = VenvSpec(python_version="3.12", packages=["numpy"])
        s2 = VenvSpec(python_version="3.12", packages=["pandas"])
        assert s1.compute_hash() != s2.compute_hash()

    def test_different_python_version_produces_different_hash(self):
        from wtb.infrastructure.environment.venv_cache import VenvSpec
        s1 = VenvSpec(python_version="3.11", packages=["numpy"])
        s2 = VenvSpec(python_version="3.12", packages=["numpy"])
        assert s1.compute_hash() != s2.compute_hash()

    def test_package_order_does_not_affect_hash(self):
        from wtb.infrastructure.environment.venv_cache import VenvSpec
        s1 = VenvSpec(python_version="3.12", packages=["numpy", "pandas"])
        s2 = VenvSpec(python_version="3.12", packages=["pandas", "numpy"])
        assert s1.compute_hash() == s2.compute_hash()

    def test_hash_is_16_char_hex(self):
        from wtb.infrastructure.environment.venv_cache import VenvSpec
        spec = VenvSpec(python_version="3.12", packages=["flask"])
        h = spec.compute_hash()
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_requirements_content_affects_hash(self):
        from wtb.infrastructure.environment.venv_cache import VenvSpec
        s1 = VenvSpec(python_version="3.12", packages=[], requirements_content="numpy==1.0")
        s2 = VenvSpec(python_version="3.12", packages=[], requirements_content="numpy==2.0")
        assert s1.compute_hash() != s2.compute_hash()

    def test_lock_file_content_affects_hash(self):
        from wtb.infrastructure.environment.venv_cache import VenvSpec
        s1 = VenvSpec(python_version="3.12", packages=[], lock_file_content="lock_v1")
        s2 = VenvSpec(python_version="3.12", packages=[], lock_file_content="lock_v2")
        assert s1.compute_hash() != s2.compute_hash()

    def test_roundtrip_to_dict_and_back(self):
        from wtb.infrastructure.environment.venv_cache import VenvSpec
        original = VenvSpec(python_version="3.11", packages=["requests", "flask"])
        d = original.to_dict()
        restored = VenvSpec.from_dict(d)
        assert restored.compute_hash() == original.compute_hash()
        assert restored.python_version == original.python_version
        assert sorted(restored.packages) == sorted(original.packages)


# ═══════════════════════════════════════════════════════════════
# VenvCacheManager (real SQLite index on disk)
# ═══════════════════════════════════════════════════════════════


class TestVenvCacheManager:

    def test_empty_cache_has_zero_entries(self, tmp_path):
        from wtb.infrastructure.environment.venv_cache import VenvCacheManager, VenvCacheConfig
        config = VenvCacheConfig(cache_dir=tmp_path / "cache")
        mgr = VenvCacheManager(config)
        stats = mgr.get_stats()
        assert stats.total_entries == 0

    def test_cache_miss_returns_none(self, tmp_path):
        from wtb.infrastructure.environment.venv_cache import VenvCacheManager, VenvCacheConfig
        config = VenvCacheConfig(cache_dir=tmp_path / "cache")
        mgr = VenvCacheManager(config)
        result = mgr.get("nonexistent_hash")
        assert result is None
        stats = mgr.get_stats()
        assert stats.misses >= 1

    def test_put_and_get_cache_entry(self, tmp_path):
        from wtb.infrastructure.environment.venv_cache import VenvCacheManager, VenvCacheConfig, VenvSpec
        config = VenvCacheConfig(cache_dir=tmp_path / "cache")
        mgr = VenvCacheManager(config)

        venv_dir = tmp_path / "source_venv"
        venv_dir.mkdir()
        (venv_dir / "pyvenv.cfg").write_text("home = /usr/bin", encoding="utf-8")

        spec = VenvSpec(python_version="3.12", packages=["numpy"])
        mgr.put(spec, venv_dir, copy_to_cache=True)

        entry = mgr.get(spec.compute_hash())
        assert entry is not None

    def test_index_persists_across_instances(self, tmp_path):
        from wtb.infrastructure.environment.venv_cache import VenvCacheManager, VenvCacheConfig, VenvSpec
        config = VenvCacheConfig(cache_dir=tmp_path / "cache")

        venv_dir = tmp_path / "source_venv"
        venv_dir.mkdir()
        (venv_dir / "pyvenv.cfg").write_text("home = /usr/bin", encoding="utf-8")

        spec = VenvSpec(python_version="3.12", packages=["flask"])
        mgr1 = VenvCacheManager(config)
        mgr1.put(spec, venv_dir, copy_to_cache=True)

        mgr2 = VenvCacheManager(config)
        entry = mgr2.get(spec.compute_hash())
        assert entry is not None

    def test_cache_stats_structure(self, tmp_path):
        from wtb.infrastructure.environment.venv_cache import VenvCacheManager, VenvCacheConfig
        config = VenvCacheConfig(cache_dir=tmp_path / "cache")
        mgr = VenvCacheManager(config)
        stats = mgr.get_stats()
        assert hasattr(stats, "hits")
        assert hasattr(stats, "misses")
        assert hasattr(stats, "total_entries")


# ═══════════════════════════════════════════════════════════════
# Workspace Python Path Resolution
# ═══════════════════════════════════════════════════════════════


class TestWorkspacePythonPath:

    def _make_workspace(self, base_path):
        from wtb.domain.models.workspace import Workspace
        root = Path(base_path) / "ws_root"
        root.mkdir(parents=True, exist_ok=True)
        return Workspace(
            workspace_id=f"ws-{uuid.uuid4().hex[:8]}",
            batch_test_id=f"bt-{uuid.uuid4().hex[:8]}",
            variant_name="test-variant",
            execution_id=f"exec-{uuid.uuid4().hex[:8]}",
            root_path=root,
        )

    def test_workspace_venv_dir_is_dot_venv(self, tmp_path):
        ws = self._make_workspace(tmp_path)
        assert ws.venv_dir.name == ".venv"

    def test_python_path_windows_or_unix(self, tmp_path):
        ws = self._make_workspace(tmp_path)
        pp = str(ws.python_path)
        if sys.platform == "win32":
            assert pp.endswith("python.exe")
            assert "Scripts" in pp
        else:
            assert pp.endswith("python")
            assert "bin" in pp

    def test_workspace_metadata_roundtrip(self, tmp_path):
        ws = self._make_workspace(tmp_path)
        d = ws.to_dict()
        assert "workspace_id" in d
        assert d["workspace_id"] == ws.workspace_id


# ═══════════════════════════════════════════════════════════════
# REAL gRPC Environment Provider Tests
# ═══════════════════════════════════════════════════════════════


@requires_grpc
class TestGrpcEnvironmentProviderReal:

    @pytest.fixture
    def provider(self):
        from wtb.infrastructure.environment.providers import GrpcEnvironmentProvider
        p = GrpcEnvironmentProvider(grpc_address=GRPC_ADDRESS, timeout_seconds=60.0)
        yield p
        p.close()

    def test_grpc_channel_connects(self, provider):
        assert provider._stub is not None
        assert provider._channel is not None

    def test_create_environment_returns_env_info(self, provider):
        variant_id = f"test-{uuid.uuid4().hex[:8]}"
        env = provider.create_environment(variant_id, {
            "workflow_id": "test_integration",
            "node_id": f"node_{variant_id}",
            "packages": [],
            "python_version": "3.11",
        })
        assert env["type"] == "grpc_uv"
        assert "env_path" in env
        assert "python_path" in env

        provider.cleanup_environment(variant_id)

    def test_get_env_status_after_create(self, provider):
        from wtb.infrastructure.environment.uv_manager.grpc_generated import (
            env_manager_pb2 as pb2,
        )
        variant_id = f"test-status-{uuid.uuid4().hex[:8]}"
        provider.create_environment(variant_id, {
            "workflow_id": "test_integration",
            "node_id": f"node_{variant_id}",
            "packages": [],
            "python_version": "3.11",
        })

        resp = provider._stub.GetEnvStatus(
            pb2.GetEnvStatusRequest(
                workflow_id="test_integration",
                node_id=f"node_{variant_id}",
            ),
            timeout=15,
        )
        assert resp.status in ("READY", "EXISTS", "ACTIVE", "CREATED")
        assert len(resp.env_path) > 0

        provider.cleanup_environment(variant_id)

    def test_create_with_packages(self, provider):
        variant_id = f"test-pkg-{uuid.uuid4().hex[:8]}"
        env = provider.create_environment(variant_id, {
            "workflow_id": "test_pkg",
            "node_id": f"node_{variant_id}",
            "packages": ["requests"],
            "python_version": "3.11",
        })
        assert env["type"] == "grpc_uv"
        assert len(env.get("env_path", "")) > 0

        provider.cleanup_environment(variant_id)

    def test_get_runtime_env_after_create(self, provider):
        variant_id = f"test-rt-{uuid.uuid4().hex[:8]}"
        provider.create_environment(variant_id, {
            "workflow_id": "test_runtime",
            "node_id": f"node_{variant_id}",
            "packages": [],
            "python_version": "3.11",
        })

        rt = provider.get_runtime_env(variant_id)
        assert rt is not None
        assert "env_path" in rt
        assert "python_path" in rt
        assert rt["type"] == "grpc_uv"

        provider.cleanup_environment(variant_id)

    def test_cleanup_removes_tracking(self, provider):
        variant_id = f"test-clean-{uuid.uuid4().hex[:8]}"
        provider.create_environment(variant_id, {
            "workflow_id": "test_cleanup",
            "node_id": f"node_{variant_id}",
            "packages": [],
            "python_version": "3.11",
        })
        provider.cleanup_environment(variant_id)

        rt = provider.get_runtime_env(variant_id)
        assert rt is None

    def test_list_environments_includes_created(self, provider):
        variant_id = f"test-list-{uuid.uuid4().hex[:8]}"
        provider.create_environment(variant_id, {
            "workflow_id": "test_list",
            "node_id": f"node_{variant_id}",
            "packages": [],
            "python_version": "3.11",
        })

        envs = provider.list_environments()
        assert variant_id in envs

        provider.cleanup_environment(variant_id)

    def test_add_deps_to_environment(self, provider):
        """Test adding dependencies via gRPC AddDeps."""
        from wtb.infrastructure.environment.uv_manager.grpc_generated import (
            env_manager_pb2 as pb2,
        )
        variant_id = f"test-deps-{uuid.uuid4().hex[:8]}"
        provider.create_environment(variant_id, {
            "workflow_id": "test_deps",
            "node_id": f"node_{variant_id}",
            "packages": [],
            "python_version": "3.11",
        })

        resp = provider._stub.AddDeps(
            pb2.DepsRequest(
                workflow_id="test_deps",
                node_id=f"node_{variant_id}",
                packages=["six"],
            ),
            timeout=60,
        )
        assert resp.exit_code == 0

        provider.cleanup_environment(variant_id)

    def test_list_deps_for_environment(self, provider):
        """Test listing dependencies from an existing environment."""
        from wtb.infrastructure.environment.uv_manager.grpc_generated import (
            env_manager_pb2 as pb2,
        )
        variant_id = f"test-ld-{uuid.uuid4().hex[:8]}"
        provider.create_environment(variant_id, {
            "workflow_id": "test_listdeps",
            "node_id": f"node_{variant_id}",
            "packages": ["six"],
            "python_version": "3.11",
        })

        resp = provider._stub.ListDeps(
            pb2.ListDepsRequest(
                workflow_id="test_listdeps",
                node_id=f"node_{variant_id}",
            ),
            timeout=30,
        )
        deps = list(resp.dependencies)
        dep_names = [d if isinstance(d, str) else getattr(d, 'name', str(d)) for d in deps]
        assert any("six" in n for n in dep_names)

        provider.cleanup_environment(variant_id)

    def test_delete_env_via_grpc(self, provider):
        from wtb.infrastructure.environment.uv_manager.grpc_generated import (
            env_manager_pb2 as pb2,
        )
        variant_id = f"test-del-{uuid.uuid4().hex[:8]}"
        provider.create_environment(variant_id, {
            "workflow_id": "test_delete",
            "node_id": f"node_{variant_id}",
            "packages": [],
            "python_version": "3.11",
        })

        resp = provider._stub.DeleteEnv(
            pb2.DeleteEnvRequest(
                workflow_id="test_delete",
                node_id=f"node_{variant_id}",
            ),
            timeout=30,
        )
        assert resp.status in ("DELETED", "deleted", "OK")

    def test_context_manager_closes_channel(self):
        from wtb.infrastructure.environment.providers import GrpcEnvironmentProvider
        with GrpcEnvironmentProvider(grpc_address=GRPC_ADDRESS) as p:
            assert p._stub is not None
        assert p._stub is None


# ═══════════════════════════════════════════════════════════════
# InProcess and Ray Provider
# ═══════════════════════════════════════════════════════════════


class TestInProcessProvider:

    def test_create_returns_inprocess_type(self):
        from wtb.infrastructure.environment.providers import InProcessEnvironmentProvider
        provider = InProcessEnvironmentProvider()
        env = provider.create_environment("v1", {"pip": ["numpy"]})
        assert env["type"] == "inprocess"

    def test_cleanup_removes_environment(self):
        from wtb.infrastructure.environment.providers import InProcessEnvironmentProvider
        provider = InProcessEnvironmentProvider()
        provider.create_environment("v1", {})
        provider.cleanup_environment("v1")
        assert provider.get_runtime_env("v1") is None

    def test_get_runtime_env_returns_none(self):
        from wtb.infrastructure.environment.providers import InProcessEnvironmentProvider
        provider = InProcessEnvironmentProvider()
        provider.create_environment("v1", {})
        assert provider.get_runtime_env("v1") is None


class TestRayEnvironmentProvider:

    def test_create_merges_base_and_config(self):
        from wtb.infrastructure.environment.providers import RayEnvironmentProvider
        provider = RayEnvironmentProvider(base_env={"pip": ["numpy"]})
        env = provider.create_environment("v1", {"pip": ["pandas"]})
        pip_packages = env.get("pip", [])
        assert "numpy" in pip_packages
        assert "pandas" in pip_packages

    def test_get_runtime_env_returns_dict(self):
        from wtb.infrastructure.environment.providers import RayEnvironmentProvider
        provider = RayEnvironmentProvider()
        provider.create_environment("v1", {"pip": ["requests"]})
        rt = provider.get_runtime_env("v1")
        assert isinstance(rt, dict)
        assert "pip" in rt

    def test_cleanup_removes_runtime_env(self):
        from wtb.infrastructure.environment.providers import RayEnvironmentProvider
        provider = RayEnvironmentProvider()
        provider.create_environment("v1", {})
        provider.cleanup_environment("v1")
        assert provider.get_runtime_env("v1") is None

    def test_list_environments(self):
        from wtb.infrastructure.environment.providers import RayEnvironmentProvider
        provider = RayEnvironmentProvider()
        provider.create_environment("v1", {})
        provider.create_environment("v2", {"pip": ["flask"]})
        envs = provider.list_environments()
        assert "v1" in envs
        assert "v2" in envs
