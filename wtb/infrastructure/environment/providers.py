"""
Environment Provider Implementations.

Provides execution environment isolation for batch testing variants.

ARCHITECTURE DECISION (2026-01-15):
- UV Venv Manager = ENVIRONMENT PROVISIONING ONLY
- Ray = NODE EXECUTION (using provisioned python_path)
- A node is a COMPLETE PROJECT (e.g., RAG pipeline), not a code snippet

Usage:
    provider = RayEnvironmentProvider()
    env = provider.create_environment("variant-1", {"pip": ["numpy"]})
    runtime_env = provider.get_runtime_env("variant-1")
    
    # For gRPC-based UV Venv Manager:
    provider = GrpcEnvironmentProvider("localhost:50051")
    env = provider.create_environment("variant-1", {
        "workflow_id": "ml_pipeline",
        "node_id": "rag",
        "packages": ["langchain", "chromadb"],
    })
    # Returns env_path, python_path for Ray to use
"""

from typing import Dict, Any, List, Optional, TYPE_CHECKING
from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import json
import logging
import sys

from wtb.domain.interfaces.batch_runner import IEnvironmentProvider

if TYPE_CHECKING:
    from wtb.infrastructure.environment.venv_cache import VenvCacheManager, VenvSpec

logger = logging.getLogger(__name__)


class InProcessEnvironmentProvider(IEnvironmentProvider):
    """
    No-isolation environment provider.
    
    All variants run in the same process with shared dependencies.
    Use for development and testing where isolation isn't needed.
    """
    
    def __init__(self):
        self._environments: Dict[str, Dict[str, Any]] = {}
    
    def create_environment(
        self,
        variant_id: str,
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Create a no-op environment (just stores config).
        
        Args:
            variant_id: Unique identifier for the variant
            config: Environment configuration (ignored)
            
        Returns:
            Empty environment spec
        """
        env = {"variant_id": variant_id, "type": "inprocess"}
        self._environments[variant_id] = env
        return env
    
    def cleanup_environment(self, variant_id: str) -> None:
        """Remove environment reference."""
        self._environments.pop(variant_id, None)
    
    def get_runtime_env(self, variant_id: str) -> Optional[Dict[str, Any]]:
        """Return None (no runtime env needed)."""
        return None


@dataclass
class RayRuntimeEnvConfig:
    """
    Configuration for Ray runtime environment.
    
    See: https://docs.ray.io/en/latest/ray-core/handling-dependencies.html
    """
    pip: list = field(default_factory=list)
    conda: Optional[Dict[str, Any]] = None
    env_vars: Dict[str, str] = field(default_factory=dict)
    working_dir: Optional[str] = None
    py_modules: list = field(default_factory=list)
    excludes: list = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to Ray runtime_env dict."""
        env = {}
        
        if self.pip:
            env["pip"] = self.pip
        if self.conda:
            env["conda"] = self.conda
        if self.env_vars:
            env["env_vars"] = self.env_vars
        if self.working_dir:
            env["working_dir"] = self.working_dir
        if self.py_modules:
            env["py_modules"] = self.py_modules
        if self.excludes:
            env["excludes"] = self.excludes
        
        return env


class RayEnvironmentProvider(IEnvironmentProvider):
    """
    Ray runtime_env-based environment provider.
    
    Provides isolation using Ray's runtime_env feature:
    - Separate pip dependencies per variant
    - Environment variables
    - Working directory isolation
    
    Usage:
        provider = RayEnvironmentProvider()
        
        # Create environment with specific dependencies
        env = provider.create_environment("variant-1", {
            "pip": ["numpy==1.24.0"],
            "env_vars": {"MODEL_VERSION": "v1"},
        })
        
        # Get runtime_env for Ray task/actor
        runtime_env = provider.get_runtime_env("variant-1")
    """
    
    def __init__(self, base_env: Optional[Dict[str, Any]] = None):
        """
        Initialize provider.
        
        Args:
            base_env: Base runtime_env to extend for all variants
        """
        self._base_env = base_env or {}
        self._environments: Dict[str, RayRuntimeEnvConfig] = {}
    
    def create_environment(
        self,
        variant_id: str,
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Create a Ray runtime_env.
        
        Args:
            variant_id: Unique identifier for the variant
            config: Environment configuration with optional keys:
                - pip: List of pip packages
                - conda: Conda environment dict
                - env_vars: Environment variables
                - working_dir: Working directory
                - py_modules: Python modules to include
                
        Returns:
            Ray runtime_env dict
        """
        # Merge with base env
        merged_pip = self._base_env.get("pip", []) + config.get("pip", [])
        merged_env_vars = {
            **self._base_env.get("env_vars", {}),
            **config.get("env_vars", {}),
        }
        
        env_config = RayRuntimeEnvConfig(
            pip=merged_pip,
            conda=config.get("conda"),
            env_vars=merged_env_vars,
            working_dir=config.get("working_dir"),
            py_modules=config.get("py_modules", []),
            excludes=config.get("excludes", []),
        )
        
        self._environments[variant_id] = env_config
        
        return env_config.to_dict()
    
    def cleanup_environment(self, variant_id: str) -> None:
        """
        Cleanup an environment.
        
        For Ray, this just removes the config.
        Ray manages actual environment lifecycle.
        
        Args:
            variant_id: Environment to cleanup
        """
        self._environments.pop(variant_id, None)
    
    def get_runtime_env(self, variant_id: str) -> Optional[Dict[str, Any]]:
        """
        Get Ray runtime_env for a variant.
        
        Args:
            variant_id: Variant identifier
            
        Returns:
            Ray runtime_env dict or None if not found
        """
        env_config = self._environments.get(variant_id)
        if env_config is None:
            return None
        return env_config.to_dict()
    
    def list_environments(self) -> Dict[str, Dict[str, Any]]:
        """List all created environments."""
        return {
            vid: config.to_dict()
            for vid, config in self._environments.items()
        }


class GrpcEnvironmentProvider(IEnvironmentProvider):
    """
    gRPC-based environment provider calling UV Venv Manager service.
    
    RESPONSIBILITY: Provision environments for workflow nodes.
    NOT responsible for: Executing node logic (that's Ray's job).
    
    A node is a COMPLETE PROJECT (e.g., RAG pipeline), not a code snippet.
    The environment provides the Python interpreter and dependencies.
    Ray actors execute the node project using the provisioned environment.
    
    Features:
    - gRPC connection to UV Venv Manager service
    - Venv spec hash tracking for invalidation
    - Integration with VenvCacheManager for caching
    - Workspace-aware venv creation
    
    Usage:
        provider = GrpcEnvironmentProvider("localhost:50051")
        
        # Create environment with dependencies
        env = provider.create_environment("variant-1", {
            "workflow_id": "ml_pipeline",
            "node_id": "rag",
            "packages": ["langchain", "chromadb"],
            "python_version": "3.11",
        })
        
        # Get runtime env for Ray
        runtime_env = provider.get_runtime_env("variant-1")
        # Returns: {"python_path": "/path/to/.venv/bin/python", ...}
        
        # Create workspace-bound environment
        env = provider.create_workspace_environment(
            workspace_id="ws-123",
            workspace_path="/path/to/workspace",
            spec=VenvSpec(python_version="3.12", packages=["langchain"]),
        )
    """
    
    def __init__(
        self,
        grpc_address: str,
        timeout_seconds: float = 120.0,  # Environment creation can take time
        default_python: str = "3.12",
        venv_cache: Optional["VenvCacheManager"] = None,
        event_bus: Optional[Any] = None,
    ):
        """
        Initialize gRPC provider.
        
        Args:
            grpc_address: gRPC service address (host:port)
            timeout_seconds: RPC timeout (default 120s for package installation)
            default_python: Default Python version
            venv_cache: Optional venv cache for efficient reuse
            event_bus: Optional event bus for publishing events
        """
        self._grpc_address = grpc_address
        self._timeout = timeout_seconds
        self._default_python = default_python
        self._environments: Dict[str, Dict[str, Any]] = {}  # variant_id -> env_info
        self._channel = None
        self._stub = None
        self._venv_cache = venv_cache
        self._event_bus = event_bus
        
        self._init_grpc()
    
    def _init_grpc(self) -> None:
        """Initialize gRPC channel and stub."""
        try:
            import grpc
            from wtb.infrastructure.environment.uv_manager.grpc_generated import (
                env_manager_pb2_grpc as pb2_grpc,
            )
            
            self._channel = grpc.insecure_channel(self._grpc_address)
            self._stub = pb2_grpc.EnvManagerServiceStub(self._channel)
            logger.info(f"GrpcEnvironmentProvider connected to {self._grpc_address}")
        except ImportError as e:
            logger.warning(
                f"gRPC dependencies not available: {e}. "
                "Install grpcio and generate proto files."
            )
            self._channel = None
            self._stub = None
    
    def _get_python_path(self, env_path: str) -> str:
        """Get Python interpreter path for the environment.
        
        When ``env_path`` is a Docker-internal Linux path (starts with
        ``/``) we always return a Linux-style path regardless of the
        host OS, because the venv lives inside the Docker container.
        """
        is_remote_linux = env_path.startswith("/")
        if is_remote_linux:
            return f"{env_path}/.venv/bin/python"
        if sys.platform == "win32":
            return f"{env_path}\\.venv\\Scripts\\python.exe"
        return f"{env_path}/.venv/bin/python"
    
    def create_environment(
        self,
        variant_id: str,
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Provision environment via gRPC.
        
        Returns env_path which can be used by Ray to:
        - Set VIRTUAL_ENV environment variable
        - Use {env_path}/.venv/bin/python as interpreter
        - Mount as volume if using containers
        
        Args:
            variant_id: Unique identifier for this variant
            config: {
                "workflow_id": str,
                "node_id": str,
                "version_id": str,  # Optional: for node variants
                "packages": List[str],
                "python_version": str,
            }
        
        Returns:
            {
                "type": "grpc_uv",
                "env_path": "/path/to/envs/workflow_node_version",
                "python_path": "/path/to/envs/.../bin/python",
                "venv_path": "/path/to/envs/.../.venv",
                ...
            }
        """
        if self._stub is None:
            logger.warning(f"gRPC not available, returning stub response for {variant_id}")
            self._environments[variant_id] = {
                "type": "grpc_uv_stub",
                "variant_id": variant_id,
            }
            return self._environments[variant_id]
        
        try:
            from wtb.infrastructure.environment.uv_manager.grpc_generated import (
                env_manager_pb2 as pb2,
            )
            
            workflow_id = config.get("workflow_id", "default")
            node_id = config.get("node_id", variant_id)
            version_id = config.get("version_id", "")
            packages = config.get("packages", [])
            python_version = config.get("python_version", self._default_python)
            
            request = pb2.CreateEnvRequest(
                workflow_id=workflow_id,
                node_id=node_id,
                version_id=version_id,
                python_version=python_version,
                packages=packages,
            )
            
            response = self._stub.CreateEnv(request, timeout=self._timeout)
            
            env_path = response.env_path
            python_path = self._get_python_path(env_path)
            
            env_info = {
                "type": "grpc_uv",
                "workflow_id": response.workflow_id,
                "node_id": response.node_id,
                "version_id": response.version_id,
                "env_path": env_path,
                "python_path": python_path,
                "venv_path": f"{env_path}/.venv",
                "python_version": response.python_version,
                "status": response.status,
            }
            
            self._environments[variant_id] = env_info
            logger.info(f"Created gRPC environment for {variant_id}: {env_path}")
            
            return env_info
            
        except Exception as e:
            logger.error(f"Failed to create gRPC environment for {variant_id}: {e}")
            raise
    
    def cleanup_environment(self, variant_id: str) -> None:
        """
        Cleanup environment.
        
        Note: Actual cleanup is handled by service TTL.
        This just removes local tracking.
        """
        env_info = self._environments.pop(variant_id, None)
        
        if env_info and self._stub is not None:
            try:
                from wtb.infrastructure.environment.uv_manager.grpc_generated import (
                    env_manager_pb2 as pb2,
                )
                
                request = pb2.DeleteEnvRequest(
                    workflow_id=env_info.get("workflow_id", ""),
                    node_id=env_info.get("node_id", ""),
                    version_id=env_info.get("version_id", ""),
                )
                
                self._stub.DeleteEnv(request, timeout=self._timeout)
                logger.info(f"Deleted gRPC environment for {variant_id}")
                
            except Exception as e:
                logger.warning(f"Failed to delete gRPC environment for {variant_id}: {e}")
    
    def get_runtime_env(self, variant_id: str) -> Optional[Dict[str, Any]]:
        """
        Get Ray-compatible runtime environment specification.
        
        Returns a dict that can be used with Ray's runtime_env feature
        or to configure subprocess execution.
        """
        env_info = self._environments.get(variant_id)
        if not env_info:
            return None
        
        env_path = env_info.get("env_path", "")
        python_path = env_info.get("python_path", "")
        venv_path = env_info.get("venv_path", "")
        
        return {
            "type": "grpc_uv",
            "env_path": env_path,
            "python_path": python_path,
            "venv_path": venv_path,
            # For Ray runtime_env
            "env_vars": {
                "VIRTUAL_ENV": venv_path,
            },
            # Ray py_executable
            "py_executable": python_path,
        }
    
    def create_workspace_environment(
        self,
        workspace_id: str,
        workspace_path: str,
        python_version: Optional[str] = None,
        packages: Optional[List[str]] = None,
        use_cache: bool = True,
    ) -> Dict[str, Any]:
        """
        Create environment bound to a specific workspace.
        
        This method creates a venv in the workspace's .venv directory,
        using the cache if available for efficiency.
        
        Args:
            workspace_id: Workspace identifier
            workspace_path: Path to workspace root
            python_version: Python version (default: provider default)
            packages: List of packages to install
            use_cache: Whether to use venv cache
            
        Returns:
            Environment info dict with paths and spec_hash
        """
        python_ver = python_version or self._default_python
        pkg_list = packages or []
        
        # Compute spec hash for cache lookup
        spec_hash = self._compute_spec_hash(python_ver, pkg_list)
        
        workspace_venv_path = Path(workspace_path) / ".venv"
        
        # Try cache first
        if use_cache and self._venv_cache:
            if self._venv_cache.copy_to_workspace(spec_hash, workspace_venv_path):
                logger.info(f"Used cached venv for workspace {workspace_id}")
                
                env_info = self._build_env_info(
                    workspace_id=workspace_id,
                    env_path=workspace_path,
                    python_version=python_ver,
                    spec_hash=spec_hash,
                    from_cache=True,
                )
                self._environments[workspace_id] = env_info
                
                # Publish event
                self._publish_venv_reused_event(workspace_id, spec_hash)
                
                return env_info
        
        # Create new venv via gRPC
        env_info = self.create_environment(workspace_id, {
            "workflow_id": workspace_id,
            "node_id": "workspace",
            "packages": pkg_list,
            "python_version": python_ver,
        })
        
        # Copy to workspace path if created elsewhere
        if env_info.get("env_path") and env_info["env_path"] != workspace_path:
            import shutil
            source_venv = Path(env_info["env_path"]) / ".venv"
            if source_venv.exists():
                if workspace_venv_path.exists():
                    shutil.rmtree(workspace_venv_path)
                shutil.copytree(source_venv, workspace_venv_path)
        
        # Update env_info with workspace paths
        env_info.update({
            "workspace_id": workspace_id,
            "workspace_path": workspace_path,
            "venv_path": str(workspace_venv_path),
            "python_path": self._get_python_path(workspace_path),
            "spec_hash": spec_hash,
            "from_cache": False,
        })
        
        self._environments[workspace_id] = env_info
        
        # Add to cache if available
        if use_cache and self._venv_cache and workspace_venv_path.exists():
            from wtb.infrastructure.environment.venv_cache import VenvSpec
            spec = VenvSpec(
                python_version=python_ver,
                packages=pkg_list,
            )
            self._venv_cache.put(spec, workspace_venv_path, copy_to_cache=True)
        
        # Publish event
        self._publish_venv_created_event(
            workspace_id=workspace_id,
            venv_path=str(workspace_venv_path),
            python_version=python_ver,
            packages=pkg_list,
            spec_hash=spec_hash,
        )
        
        return env_info
    
    def _compute_spec_hash(
        self,
        python_version: str,
        packages: List[str],
    ) -> str:
        """Compute hash for venv specification."""
        spec_data = {
            "python_version": python_version,
            "packages": sorted(packages),
        }
        spec_json = json.dumps(spec_data, sort_keys=True)
        return hashlib.sha256(spec_json.encode()).hexdigest()[:16]
    
    def _build_env_info(
        self,
        workspace_id: str,
        env_path: str,
        python_version: str,
        spec_hash: str,
        from_cache: bool,
    ) -> Dict[str, Any]:
        """Build environment info dictionary."""
        venv_path = f"{env_path}/.venv"
        return {
            "type": "grpc_uv",
            "workspace_id": workspace_id,
            "env_path": env_path,
            "venv_path": venv_path,
            "python_path": self._get_python_path(env_path),
            "python_version": python_version,
            "spec_hash": spec_hash,
            "from_cache": from_cache,
            "status": "ready",
        }
    
    def check_venv_compatibility(
        self,
        workspace_id: str,
        expected_spec_hash: str,
    ) -> bool:
        """
        Check if workspace venv is compatible with expected spec.
        
        Used during rollback to detect venv spec changes.
        
        Args:
            workspace_id: Workspace identifier
            expected_spec_hash: Expected venv spec hash from checkpoint
            
        Returns:
            True if compatible (hashes match), False otherwise
        """
        env_info = self._environments.get(workspace_id)
        if not env_info:
            return False
        
        current_hash = env_info.get("spec_hash", "")
        return current_hash == expected_spec_hash
    
    def invalidate_environment(self, workspace_id: str) -> bool:
        """
        Invalidate environment for workspace (spec changed).
        
        Args:
            workspace_id: Workspace identifier
            
        Returns:
            True if invalidated, False if not found
        """
        env_info = self._environments.pop(workspace_id, None)
        if not env_info:
            return False
        
        # Also cleanup via gRPC if applicable
        self.cleanup_environment(workspace_id)
        
        # Publish event
        if self._event_bus:
            from wtb.domain.events.workspace_events import VenvInvalidatedEvent
            try:
                self._event_bus.publish(VenvInvalidatedEvent(
                    node_id=workspace_id,
                    old_spec_hash=env_info.get("spec_hash", ""),
                    new_spec_hash="",
                    reason="manual_invalidation",
                ))
            except Exception as e:
                logger.warning(f"Failed to publish invalidation event: {e}")
        
        return True
    
    def _publish_venv_created_event(
        self,
        workspace_id: str,
        venv_path: str,
        python_version: str,
        packages: List[str],
        spec_hash: str,
    ) -> None:
        """Publish VenvCreatedEvent."""
        if not self._event_bus:
            return
        
        try:
            from wtb.domain.events.workspace_events import VenvCreatedEvent
            self._event_bus.publish(VenvCreatedEvent(
                workspace_id=workspace_id,
                execution_id="",
                venv_path=venv_path,
                python_version=python_version,
                packages=packages,
                venv_spec_hash=spec_hash,
                creation_time_ms=0.0,
            ))
        except Exception as e:
            logger.warning(f"Failed to publish venv created event: {e}")
    
    def _publish_venv_reused_event(
        self,
        workspace_id: str,
        spec_hash: str,
    ) -> None:
        """Publish VenvReusedEvent."""
        if not self._event_bus:
            return
        
        try:
            from wtb.domain.events.workspace_events import VenvReusedEvent
            self._event_bus.publish(VenvReusedEvent(
                workspace_id=workspace_id,
                venv_path="",
                venv_spec_hash=spec_hash,
            ))
        except Exception as e:
            logger.warning(f"Failed to publish venv reused event: {e}")
    
    def list_environments(self) -> Dict[str, Dict[str, Any]]:
        """List all created environments."""
        return dict(self._environments)
    
    def get_env_status(self, variant_id: str) -> Dict[str, Any]:
        """
        Query the Docker service for the real status of a provisioned
        environment using the ``GetEnvStatus`` gRPC RPC.
        
        This is the canonical way to verify that a Docker-internal venv
        was actually created, regardless of host OS.
        
        Returns:
            {"status": str, "has_venv": bool, "has_pyproject": bool,
             "has_uv_lock": bool, "env_path": str}
        """
        env_info = self._environments.get(variant_id)
        if not env_info:
            raise ValueError(f"No environment found for variant {variant_id}")
        if self._stub is None:
            raise RuntimeError("gRPC stub not initialized")
        
        from wtb.infrastructure.environment.uv_manager.grpc_generated import (
            env_manager_pb2 as pb2,
        )
        
        request = pb2.GetEnvStatusRequest(
            workflow_id=env_info.get("workflow_id", ""),
            node_id=env_info.get("node_id", ""),
            version_id=env_info.get("version_id", ""),
        )
        response = self._stub.GetEnvStatus(request, timeout=self._timeout)
        return {
            "status": response.status,
            "has_venv": response.has_venv,
            "has_pyproject": response.has_pyproject,
            "has_uv_lock": response.has_uv_lock,
            "env_path": response.env_path,
        }
    
    def close(self) -> None:
        """Close gRPC channel."""
        if self._channel:
            self._channel.close()
            self._channel = None
            self._stub = None
    
    def __enter__(self) -> "GrpcEnvironmentProvider":
        return self
    
    def __exit__(self, *args) -> None:
        self.close()

