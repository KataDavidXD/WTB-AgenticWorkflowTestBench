"""
WorkflowProject - Configuration object for workflow submission to WTB.

This module defines the core configuration objects for submitting LangGraph
workflows to the Workflow Test Bench (WTB) system.

Architecture (2026-01-16):
    - WorkflowProject: Main configuration container
    - Configuration classes for file tracking, environment, execution
    - Variant management for A/B testing

Design Principles:
    - Immutable configuration where possible
    - Sensible defaults for quick start
    - Full configurability for advanced users
    - Type-safe with dataclasses

Author: Senior Architect
Date: 2026-01-16
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, Type, TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import BaseModel


# ═══════════════════════════════════════════════════════════════════════════════
# Environment Configuration
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class EnvSpec:
    """
    Environment specification for dependency isolation.
    
    Attributes:
        python_version: Python version (e.g., "3.12")
        dependencies: List of pip dependencies
        requirements_file: Optional path to requirements.txt
        env_vars: Environment variables to set
    """
    python_version: str = "3.12"
    dependencies: List[str] = field(default_factory=list)
    requirements_file: Optional[str] = None
    env_vars: Dict[str, str] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "python_version": self.python_version,
            "dependencies": self.dependencies,
            "requirements_file": self.requirements_file,
            "env_vars": self.env_vars,
        }


@dataclass
class EnvironmentConfig:
    """
    UV Venv Manager integration configuration.
    
    Supports three granularity levels:
    - workflow: Single environment for entire workflow
    - node: Separate environment per node
    - variant: Separate environment per variant
    
    Attributes:
        granularity: Environment isolation level
        use_current_env: Skip isolation, use current environment
        default_env: Default environment specification
        node_environments: Per-node environment specs (key: node_id)
        variant_environments: Per-variant specs (key: "node_id:variant_name")
        reuse_existing: Reuse existing environments if matching
        cleanup_on_exit: Clean up environments after workflow completion
        uv_manager_url: URL for UV Venv Manager service (None = embedded)
    """
    granularity: Literal["workflow", "node", "variant"] = "workflow"
    use_current_env: bool = False
    default_env: Optional[EnvSpec] = None
    node_environments: Dict[str, EnvSpec] = field(default_factory=dict)
    variant_environments: Dict[str, EnvSpec] = field(default_factory=dict)
    reuse_existing: bool = True
    cleanup_on_exit: bool = False
    uv_manager_url: Optional[str] = None
    
    def get_env_for_node(self, node_id: str) -> Optional[EnvSpec]:
        """Get environment spec for a specific node."""
        if self.granularity == "workflow":
            return self.default_env
        return self.node_environments.get(node_id, self.default_env)
    
    def get_env_for_variant(self, node_id: str, variant_name: str) -> Optional[EnvSpec]:
        """Get environment spec for a specific variant."""
        key = f"{node_id}:{variant_name}"
        if self.granularity == "variant" and key in self.variant_environments:
            return self.variant_environments[key]
        return self.get_env_for_node(node_id)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "granularity": self.granularity,
            "use_current_env": self.use_current_env,
            "default_env": self.default_env.to_dict() if self.default_env else None,
            "node_environments": {k: v.to_dict() for k, v in self.node_environments.items()},
            "variant_environments": {k: v.to_dict() for k, v in self.variant_environments.items()},
            "reuse_existing": self.reuse_existing,
            "cleanup_on_exit": self.cleanup_on_exit,
            "uv_manager_url": self.uv_manager_url,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# File Tracking Configuration (SDK version)
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class FileTrackingConfig:
    """
    FileTracker integration configuration.
    
    Controls how WTB tracks file changes during workflow execution
    for versioning and rollback support.
    
    Attributes:
        enabled: Enable file tracking
        tracked_paths: Paths to track (relative to project root)
        ignore_patterns: Gitignore-style patterns to exclude
        auto_commit: Automatically commit changes
        commit_on: When to commit ("checkpoint", "node_complete", "manual")
        repository_path: Storage location (None = auto in project dir)
        snapshot_strategy: How to create snapshots ("full", "incremental")
        max_snapshots: Maximum number of snapshots to retain
    """
    enabled: bool = False
    tracked_paths: List[str] = field(default_factory=list)
    ignore_patterns: List[str] = field(default_factory=lambda: [
        "*.pyc", "__pycache__/", "*.log", ".git/", "*.tmp"
    ])
    auto_commit: bool = True
    commit_on: Literal["checkpoint", "node_complete", "manual"] = "checkpoint"
    repository_path: Optional[str] = None
    snapshot_strategy: Literal["full", "incremental"] = "incremental"
    max_snapshots: int = 100
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "enabled": self.enabled,
            "tracked_paths": self.tracked_paths,
            "ignore_patterns": self.ignore_patterns,
            "auto_commit": self.auto_commit,
            "commit_on": self.commit_on,
            "repository_path": self.repository_path,
            "snapshot_strategy": self.snapshot_strategy,
            "max_snapshots": self.max_snapshots,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Execution Configuration
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class NodeResourceConfig:
    """
    Per-node Ray resource allocation.
    
    Attributes:
        num_cpus: Number of CPUs to allocate
        num_gpus: Number of GPUs to allocate
        memory: Memory allocation (e.g., "2GB", "512MB")
        custom_resources: Custom Ray resources
    """
    num_cpus: float = 1.0
    num_gpus: float = 0.0
    memory: str = "1GB"
    custom_resources: Dict[str, float] = field(default_factory=dict)
    
    def _parse_memory(self, memory_str: str) -> Optional[int]:
        """
        Parse memory string to bytes.
        
        Supports: KB, MB, GB, TB
        
        Args:
            memory_str: Memory string like "2GB", "512MB"
            
        Returns:
            Memory in bytes, or None if invalid
        """
        match = re.match(r'^(\d+(?:\.\d+)?)\s*(KB|MB|GB|TB)$', memory_str.upper())
        if not match:
            return None
        
        value = float(match.group(1))
        unit = match.group(2)
        
        multipliers = {
            "KB": 1024,
            "MB": 1024 ** 2,
            "GB": 1024 ** 3,
            "TB": 1024 ** 4,
        }
        
        return int(value * multipliers[unit])
    
    def to_ray_options(self) -> Dict[str, Any]:
        """
        Convert to Ray resource options.
        
        Returns:
            Dictionary suitable for ray.remote() options
        """
        options = {
            "num_cpus": self.num_cpus,
            "num_gpus": self.num_gpus,
        }
        
        memory_bytes = self._parse_memory(self.memory)
        if memory_bytes:
            options["memory"] = memory_bytes
        
        # Add custom resources directly
        options.update(self.custom_resources)
        
        return options
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "num_cpus": self.num_cpus,
            "num_gpus": self.num_gpus,
            "memory": self.memory,
            "custom_resources": self.custom_resources,
        }


@dataclass
class RayConfig:
    """
    Ray cluster configuration (Workflow-level).
    
    Attributes:
        address: Ray cluster address ("auto", "local", "ray://host:port")
        max_concurrent_workflows: Maximum concurrent workflow executions
        object_store_memory: Object store memory in bytes
        max_retries: Maximum retries for failed tasks
        retry_delay: Delay between retries in seconds
    """
    address: str = "auto"
    max_concurrent_workflows: int = 10
    object_store_memory: Optional[int] = None
    max_retries: int = 3
    retry_delay: float = 1.0
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "address": self.address,
            "max_concurrent_workflows": self.max_concurrent_workflows,
            "object_store_memory": self.object_store_memory,
            "max_retries": self.max_retries,
            "retry_delay": self.retry_delay,
        }


@dataclass
class ExecutionConfig:
    """
    Execution configuration for workflow runs.
    
    Attributes:
        batch_executor: Executor type ("ray", "threadpool", "sequential")
        ray_config: Ray cluster configuration
        node_resources: Per-node resource allocation
        default_node_resources: Default resources for unconfigured nodes
        checkpoint_strategy: When to checkpoint ("per_node", "per_step", "manual")
        checkpoint_storage: Storage backend ("memory", "sqlite", "postgres")
        default_timeout: Default timeout in seconds
    """
    batch_executor: Literal["ray", "threadpool", "sequential"] = "ray"
    ray_config: RayConfig = field(default_factory=RayConfig)
    node_resources: Dict[str, NodeResourceConfig] = field(default_factory=dict)
    default_node_resources: NodeResourceConfig = field(default_factory=NodeResourceConfig)
    checkpoint_strategy: Literal["per_node", "per_step", "manual"] = "per_node"
    checkpoint_storage: Literal["memory", "sqlite", "postgres"] = "sqlite"
    default_timeout: int = 300
    
    def get_node_resources(self, node_id: str) -> NodeResourceConfig:
        """Get resource config for a node."""
        return self.node_resources.get(node_id, self.default_node_resources)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "batch_executor": self.batch_executor,
            "ray_config": self.ray_config.to_dict(),
            "node_resources": {k: v.to_dict() for k, v in self.node_resources.items()},
            "default_node_resources": self.default_node_resources.to_dict(),
            "checkpoint_strategy": self.checkpoint_strategy,
            "checkpoint_storage": self.checkpoint_storage,
            "default_timeout": self.default_timeout,
        }


@dataclass
class WorkspaceIsolationConfig:
    """
    Workspace isolation configuration.
    
    Controls how workspace files are isolated between executions.
    
    Attributes:
        enabled: Enable workspace isolation
        mode: Isolation mode ("copy", "symlink", "overlay")
        base_path: Base path for isolated workspaces
        cleanup_on_complete: Clean up workspace after completion
    """
    enabled: bool = False
    mode: Literal["copy", "symlink", "overlay"] = "copy"
    base_path: Optional[str] = None
    cleanup_on_complete: bool = True
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "base_path": self.base_path,
            "cleanup_on_complete": self.cleanup_on_complete,
        }


@dataclass
class PauseStrategyConfig:
    """
    Pause strategy configuration for breakpoint behavior.
    
    Attributes:
        mode: Pause mode ("before_node", "after_node", "on_condition")
        timeout: Pause timeout in seconds (None = indefinite)
        auto_resume: Automatically resume after timeout
    """
    mode: Literal["before_node", "after_node", "on_condition"] = "before_node"
    timeout: Optional[int] = None
    auto_resume: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "mode": self.mode,
            "timeout": self.timeout,
            "auto_resume": self.auto_resume,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Variant Types
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class NodeVariant:
    """
    Node-level variant for A/B testing components.
    
    Represents an alternative implementation of a single node
    in the workflow graph.
    
    Attributes:
        node_id: Target node ID
        name: Variant name (unique within node)
        implementation: Variant implementation callable
        description: Human-readable description
        environment: Optional variant-specific environment
        resources: Optional variant-specific resources
        created_at: Creation timestamp
    """
    node_id: str
    name: str
    implementation: Callable
    description: str = ""
    environment: Optional[EnvSpec] = None
    resources: Optional[NodeResourceConfig] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary (excluding callable)."""
        return {
            "node_id": self.node_id,
            "name": self.name,
            "description": self.description,
            "environment": self.environment.to_dict() if self.environment else None,
            "resources": self.resources.to_dict() if self.resources else None,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class WorkflowVariant:
    """
    Workflow-level variant for architecture comparison.
    
    Represents an alternative graph structure for the entire workflow.
    
    Attributes:
        name: Variant name (unique within project)
        graph_factory: Factory function that creates the variant graph
        description: Human-readable description
        state_schema: Optional different state schema
        created_at: Creation timestamp
    """
    name: str
    graph_factory: Callable
    description: str = ""
    state_schema: Optional[Type["BaseModel"]] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary (excluding callables)."""
        return {
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at.isoformat(),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# WorkflowProject - Main Configuration Object
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class WorkflowProject:
    """
    Workflow Project configuration for WTB submission.
    
    Container for all configuration needed to submit a LangGraph workflow
    to the Workflow Test Bench for testing, debugging, and batch execution.
    
    Attributes:
        name: Unique project name
        graph_factory: Factory function that creates the LangGraph
        description: Human-readable description
        state_schema: Pydantic model for state validation
        id: Unique project ID (auto-generated)
        version: Project version number
        file_tracking: FileTracker configuration
        environment: Environment isolation configuration
        execution: Execution/resource configuration
        workspace_isolation: Workspace isolation configuration
        pause_strategy: Pause/breakpoint configuration
        created_at: Creation timestamp
        updated_at: Last update timestamp
        
    Usage:
        project = WorkflowProject(
            name="rag_pipeline",
            graph_factory=create_rag_graph,
            description="RAG Pipeline for Q&A",
        )
        project.register_variant(node="retriever", name="bm25", implementation=bm25_func)
        wtb.register_project(project)
    """
    
    name: str
    graph_factory: Callable
    description: str = ""
    state_schema: Optional[Type["BaseModel"]] = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    version: int = 1
    file_tracking: FileTrackingConfig = field(default_factory=FileTrackingConfig)
    environment: EnvironmentConfig = field(default_factory=EnvironmentConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    workspace_isolation: WorkspaceIsolationConfig = field(default_factory=WorkspaceIsolationConfig)
    pause_strategy: PauseStrategyConfig = field(default_factory=PauseStrategyConfig)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    # Private variant storage
    _node_variants: Dict[str, Dict[str, NodeVariant]] = field(default_factory=dict, repr=False)
    _workflow_variants: Dict[str, WorkflowVariant] = field(default_factory=dict, repr=False)
    _graph_cache: Dict[Tuple[Any, ...], Any] = field(default_factory=dict, repr=False)

    def _graph_cache_key(
        self,
        variant_config: Optional[Dict[str, str]],
        workflow_variant: Optional[str],
    ) -> Tuple[Any, ...]:
        """Build a stable cache key for repeated graph construction."""
        normalized_variant_config = tuple(sorted((variant_config or {}).items()))
        return (
            workflow_variant or "",
            normalized_variant_config,
        )

    def _clear_graph_cache(self) -> None:
        """Invalidate memoized graphs after project structure changes."""
        self._graph_cache.clear()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Node-Level Variant Management
    # ═══════════════════════════════════════════════════════════════════════════
    
    def register_variant(
        self,
        node: str,
        name: str,
        implementation: Callable,
        description: str = "",
        environment: Optional[EnvSpec] = None,
        resources: Optional[NodeResourceConfig] = None,
    ) -> None:
        """
        Register a node-level variant for A/B testing.
        
        Args:
            node: Target node ID
            name: Variant name (unique within node)
            implementation: Variant implementation callable
            description: Human-readable description
            environment: Optional variant-specific environment
            resources: Optional variant-specific resources
            
        Raises:
            ValueError: If variant with same name already exists for node
        """
        if node not in self._node_variants:
            self._node_variants[node] = {}
        
        if name in self._node_variants[node]:
            raise ValueError(f"Variant '{name}' already exists for node '{node}'")
        
        variant = NodeVariant(
            node_id=node,
            name=name,
            implementation=implementation,
            description=description,
            environment=environment,
            resources=resources,
        )
        self._node_variants[node][name] = variant
        
        # Auto-register environment to project's variant_environments
        if environment is not None:
            env_key = f"{node}:{name}"
            self.environment.variant_environments[env_key] = environment
        
        self._clear_graph_cache()
        self.updated_at = datetime.now(timezone.utc)
    
    def list_variants(self, node: Optional[str] = None) -> Dict[str, List[str]]:
        """
        List all registered node variants.
        
        Args:
            node: Optional node ID to filter by
            
        Returns:
            Dictionary mapping node IDs to list of variant names
        """
        if node is not None:
            if node in self._node_variants:
                return {node: list(self._node_variants[node].keys())}
            return {}
        return {n: list(variants.keys()) for n, variants in self._node_variants.items()}
    
    def get_variant(self, node: str, name: str) -> Optional[NodeVariant]:
        """
        Get a specific node variant.
        
        Args:
            node: Node ID
            name: Variant name
            
        Returns:
            NodeVariant if found, None otherwise
        """
        return self._node_variants.get(node, {}).get(name)
    
    def get_variant_implementation(self, node: str, name: str) -> Optional[Callable]:
        """
        Get the implementation callable for a variant.
        
        Args:
            node: Node ID
            name: Variant name
            
        Returns:
            Implementation callable if found, None otherwise
        """
        variant = self.get_variant(node, name)
        return variant.implementation if variant else None
    
    def remove_variant(self, node: str, name: str) -> bool:
        """
        Remove a registered variant.
        
        Args:
            node: Node ID
            name: Variant name
            
        Returns:
            True if removed, False if not found
        """
        if node in self._node_variants and name in self._node_variants[node]:
            del self._node_variants[node][name]
            if not self._node_variants[node]:
                del self._node_variants[node]
            self._clear_graph_cache()
            self.updated_at = datetime.now(timezone.utc)
            return True
        return False
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Workflow-Level Variant Management
    # ═══════════════════════════════════════════════════════════════════════════
    
    def register_workflow_variant(
        self,
        name: str,
        graph_factory: Callable,
        description: str = "",
        state_schema: Optional[Type["BaseModel"]] = None,
    ) -> None:
        """
        Register a workflow-level variant for architecture comparison.
        
        Args:
            name: Variant name (unique within project)
            graph_factory: Factory function for variant graph
            description: Human-readable description
            state_schema: Optional different state schema
            
        Raises:
            ValueError: If variant with same name already exists
        """
        if name in self._workflow_variants:
            raise ValueError(f"Workflow variant '{name}' already exists")
        
        variant = WorkflowVariant(
            name=name,
            graph_factory=graph_factory,
            description=description,
            state_schema=state_schema,
        )
        self._workflow_variants[name] = variant
        self._clear_graph_cache()
        self.updated_at = datetime.now(timezone.utc)
    
    def list_workflow_variants(self) -> List[str]:
        """
        List all registered workflow variants.
        
        Returns:
            List of variant names
        """
        return list(self._workflow_variants.keys())
    
    def get_workflow_variant(self, name: str) -> Optional[WorkflowVariant]:
        """
        Get a specific workflow variant.
        
        Args:
            name: Variant name
            
        Returns:
            WorkflowVariant if found, None otherwise
        """
        return self._workflow_variants.get(name)
    
    def remove_workflow_variant(self, name: str) -> bool:
        """
        Remove a registered workflow variant.
        
        Args:
            name: Variant name
            
        Returns:
            True if removed, False if not found
        """
        if name in self._workflow_variants:
            del self._workflow_variants[name]
            self._clear_graph_cache()
            self.updated_at = datetime.now(timezone.utc)
            return True
        return False
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Node Updates (Permanent)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def update_node(
        self,
        node: str,
        new_implementation: Callable,
        reason: str = "",
    ) -> None:
        """
        Permanently update a node implementation.
        
        This creates a new version of the node, preserving history.
        Use variants for temporary A/B testing.
        
        Args:
            node: Node ID to update
            new_implementation: New implementation callable
            reason: Reason for the update
        """
        # For now, register as "updated" variant to track updates
        # In a full implementation, this would update the graph_factory
        # Avoid duplicate "updated" variants
        variant_name = f"updated_{self.version + 1}"
        try:
            self.register_variant(
                node=node,
                name=variant_name,
                implementation=new_implementation,
                description=f"Updated: {reason}",
            )
        except ValueError:
            # Variant already exists, that's fine
            pass
        self.version += 1
        self._clear_graph_cache()
        self.updated_at = datetime.now(timezone.utc)
    
    def update_workflow(
        self,
        graph_factory: Callable,
        version: str = "",
        changelog: str = "",
    ) -> None:
        """
        Update the main workflow graph structure.
        
        Creates a new major version of the workflow.
        
        Args:
            graph_factory: New factory function for the graph
            version: Version string (informational)
            changelog: Description of changes
        """
        self.graph_factory = graph_factory
        self.version += 1
        self._clear_graph_cache()
        self.updated_at = datetime.now(timezone.utc)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Graph Building
    # ═══════════════════════════════════════════════════════════════════════════
    
    def build_graph(
        self,
        variant_config: Optional[Dict[str, str]] = None,
        workflow_variant: Optional[str] = None,
    ) -> Any:
        """
        Build the workflow graph with optional variant configuration.
        
        ARCHITECTURE NOTE (2026-01-17):
        - This method does NOT accept a checkpointer parameter
        - Checkpointer wiring is the responsibility of the Application layer
        - LangGraphStateAdapter.set_workflow_graph() handles recompilation with checkpointer
        - This follows Single Responsibility Principle (SRP) and Dependency Inversion (DIP)
        
        Args:
            variant_config: Node variant configuration {node_id: variant_name}
            workflow_variant: Workflow variant to use (overrides main graph)
            
        Returns:
            LangGraph - may be compiled or uncompiled depending on factory.
            If compiled without checkpointer, LangGraphStateAdapter will recompile.
        """
        cache_key = self._graph_cache_key(
            variant_config=variant_config,
            workflow_variant=workflow_variant,
        )
        cached_graph = self._graph_cache.get(cache_key)
        if cached_graph is not None:
            return cached_graph

        # Select graph factory
        if workflow_variant:
            wf_variant = self.get_workflow_variant(workflow_variant)
            if not wf_variant:
                raise ValueError(f"Unknown workflow variant: {workflow_variant}")
            factory = wf_variant.graph_factory
        else:
            factory = self.graph_factory
        
        # Build graph - DO NOT pass checkpointer (Application layer responsibility)
        # Factory may return compiled or uncompiled graph
        graph = factory()
        
        # Apply node variants if configured
        if variant_config:
            graph = self._apply_node_variants(graph, variant_config)
        
        self._graph_cache[cache_key] = graph
        return graph
    
    def _apply_node_variants(
        self,
        graph: Any,
        variant_config: Dict[str, str],
    ) -> Any:
        """
        Apply node variants to a graph.
        
        This is a placeholder - actual implementation depends on
        LangGraph's node replacement API.
        """
        # TODO: Implement node replacement using LangGraph's API
        # For now, return graph as-is
        return graph
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Serialization
    # ═══════════════════════════════════════════════════════════════════════════
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize project to dictionary.
        
        Note: Callables (graph_factory, implementations) are not serialized.
        
        Returns:
            Dictionary representation
        """
        return {
            "name": self.name,
            "id": self.id,
            "version": self.version,
            "description": self.description,
            "file_tracking": self.file_tracking.to_dict(),
            "environment": self.environment.to_dict(),
            "execution": self.execution.to_dict(),
            "workspace_isolation": self.workspace_isolation.to_dict(),
            "pause_strategy": self.pause_strategy.to_dict(),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "node_variants": {
                node: {v.name: v.to_dict() for v in variants.values()}
                for node, variants in self._node_variants.items()
            },
            "workflow_variants": [v.to_dict() for v in self._workflow_variants.values()],
        }
    
    @classmethod
    def from_yaml(cls, path: str) -> "WorkflowProject":
        """
        Load project from YAML configuration file.
        
        Args:
            path: Path to YAML file
            
        Returns:
            WorkflowProject instance
            
        Note:
            This requires the module paths to be importable.
        """
        import importlib
        import yaml
        
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        
        # Import graph factory via centralized loader
        from wtb.application.services.graph_loader import load_graph_factory
        module_name = data["workflow"]["module"]
        factory_name = data["workflow"]["factory"]
        graph_factory = load_graph_factory(module_name, factory_name)
        
        # Build project
        project = cls(
            name=data["name"],
            graph_factory=graph_factory,
            description=data.get("description", ""),
        )
        
        return project
