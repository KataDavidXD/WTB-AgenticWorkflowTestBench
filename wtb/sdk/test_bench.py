"""
WTBTestBench - Main Entry Point for WTB SDK.

Refactored (v1.6): 
- All IDs are strings (session_id, checkpoint_id)
- Removed broken branch() method
- fork() delegates to ExecutionController.fork()
- Removed state_adapter dependency from SDK (layer separation)

Architecture:
    WTBTestBench (SDK Facade)
        │
        ├── ProjectService        - Project management (Application)
        ├── VariantService        - Variant management (Application)
        ├── IExecutionController  - Execution lifecycle (Application)
        └── IBatchTestRunner      - Batch test execution (Application)
        
    Application Services manage:
        └── IUnitOfWork           - Transaction boundaries (Infrastructure)

Domain Models (REUSED, not duplicated):
    - Execution            - wtb.domain.models.workflow
    - ExecutionState       - wtb.domain.models.workflow
    - Checkpoint           - wtb.domain.models.checkpoint
    - BatchTest            - wtb.domain.models.batch_test
"""

from __future__ import annotations

import base64
import copy
import logging
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from typing import TYPE_CHECKING, Any

from wtb.domain.models.batch_test import (
    BatchTest,
    BatchTestResult,
    BatchTestStatus,
    normalize_finite_metrics,
)
from wtb.domain.models.checkpoint import Checkpoint, CheckpointId

# REUSE domain models - don't duplicate!
from wtb.domain.models.workflow import Execution, ExecutionState, ExecutionStatus

from .workflow_project import WorkflowProject


def _serialized_adapter_access(method):
    """Serialize operations that may observe or mutate the shared adapter."""
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lifecycle_lock:
            return method(self, *args, **kwargs)

    return locked


if TYPE_CHECKING:
    from wtb.application.services import ProjectService, VariantService
    from wtb.application.services.batch_execution_coordinator import (
        BatchExecutionCoordinator,
    )
    from wtb.domain.interfaces import (
        IBatchTestRunner,
        IExecutionController,
    )

logger = logging.getLogger(__name__)

_EXECUTION_STATE_ADAPTER_BACKENDS = {
    "langgraph_sqlite",
    "node_sqlite",
}


# ═══════════════════════════════════════════════════════════════════════════════
# SDK-Specific Operation Results (NOT domain entities - just operation outcomes)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class RollbackResult:
    """Result of a rollback operation (SDK operation DTO)."""
    execution_id: str
    to_checkpoint_id: str
    nodes_reverted: int = 0
    success: bool = True
    error: str | None = None


@dataclass
class ForkResult:
    """Result of forking an execution (SDK operation DTO)."""
    fork_execution_id: str
    source_execution_id: str
    source_checkpoint_id: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class BatchRollbackResult:
    """
    Result of rolling back a batch test result (SDK operation DTO).
    
    Usage:
        result = wtb.rollback_batch_result(batch.results[0])
        if result.success:
            print(f"Rolled back to {result.checkpoint_id}")
    """
    execution_id: str
    checkpoint_id: str
    success: bool
    execution: Execution | None = None
    files_restored: int = 0
    error: str | None = None


@dataclass
class BatchForkResult:
    """
    Result of forking a batch test result (SDK operation DTO).
    
    Usage:
        fork = wtb.fork_batch_result(batch.results[0])
        # Run the forked execution
        execution = wtb.resume(fork.fork_execution_id)
    """
    source_execution_id: str
    fork_execution_id: str
    checkpoint_id: str
    execution: Execution | None = None
    error: str | None = None


# ═══════════════════════════════════════════════════════════════════════════════
# WTBTestBenchBuilder - Thin wrapper delegating to Application Factory
# ═══════════════════════════════════════════════════════════════════════════════

class WTBTestBenchBuilder:
    """
    Builder for creating WTBTestBench.
    
    DESIGN: Thin wrapper that delegates to WTBTestBenchFactory.
    Infrastructure wiring belongs in Application layer, not SDK.
    
    Usage:
        wtb = WTBTestBenchBuilder().for_testing().build()
        wtb = WTBTestBenchBuilder().for_development("data").build()
        wtb = WTBTestBenchBuilder().for_production(checkpointer="postgres").build()
    """
    
    def __init__(self):
        self._mode = "testing"
        self._config: dict[str, Any] = {}
        # Custom dependency overrides (for testing/advanced use)
        self._project_service = None
        self._variant_service = None
        self._execution_controller = None
        self._batch_runner = None
    
    def for_testing(self) -> "WTBTestBenchBuilder":
        """Configure for in-memory testing."""
        self._mode = "testing"
        return self
    
    def for_development(self, data_dir: str = "data") -> "WTBTestBenchBuilder":
        """Configure for SQLite persistence."""
        self._mode = "development"
        self._config["data_dir"] = data_dir
        return self
    
    def for_production(
        self,
        checkpointer: str = "postgres",
        connection_string: str | None = None,
    ) -> "WTBTestBenchBuilder":
        """Configure for production (LangGraph checkpointers)."""
        self._mode = "production"
        self._config["checkpointer"] = checkpointer
        self._config["connection_string"] = connection_string
        return self
    
    def with_ray(
        self,
        data_dir: str = "data",
        grpc_env_url: str | None = None,
    ) -> "WTBTestBenchBuilder":
        """
        Configure for development mode with Ray batch runner.
        
        Requires ``ray`` to be installed and ``ray.init()``
        to have been called before invoking batch operations.
        
        Args:
            data_dir: Directory for SQLite persistence
            grpc_env_url: Optional gRPC URL for UV venv manager Docker
                          service (e.g. ``localhost:50051``). When set,
                          each Ray actor provisions an isolated venv.
        """
        self._mode = "development"
        self._config["data_dir"] = data_dir
        self._config["enable_ray"] = True
        if grpc_env_url:
            self._config["grpc_env_url"] = grpc_env_url
        return self
    
    # Dependency override methods (for advanced testing scenarios)
    def with_project_service(self, service: "ProjectService") -> "WTBTestBenchBuilder":
        """Override the project service (for testing)."""
        self._project_service = service
        return self
    
    def with_variant_service(self, service: "VariantService") -> "WTBTestBenchBuilder":
        """Override the variant service (for testing)."""
        self._variant_service = service
        return self
    
    def with_execution_controller(self, controller: "IExecutionController") -> "WTBTestBenchBuilder":
        """Override the execution controller (for testing)."""
        self._execution_controller = controller
        return self
    
    def with_batch_runner(self, runner: "IBatchTestRunner") -> "WTBTestBenchBuilder":
        """Override the batch runner (for testing)."""
        self._batch_runner = runner
        return self
    
    def build(self) -> "WTBTestBench":
        """Build WTBTestBench - delegates to Application Factory."""
        from wtb.application.factories import WTBTestBenchFactory
        
        # Full manual wiring: all four deps provided
        if self._has_all_custom_dependencies():
            return WTBTestBench(
                project_service=self._project_service,
                variant_service=self._variant_service,
                execution_controller=self._execution_controller,
                batch_runner=self._batch_runner,
            )
        
        # Delegate to Application Factory (proper composition root)
        if self._mode == "testing":
            bench = WTBTestBenchFactory.create_for_testing()
        elif self._mode == "development":
            bench = WTBTestBenchFactory.create_for_development(
                data_dir=self._config.get("data_dir", "data"),
                enable_ray=self._config.get("enable_ray", False),
                grpc_env_url=self._config.get("grpc_env_url"),
            )
        elif self._mode == "production":
            bench = WTBTestBenchFactory.create_with_langgraph(
                checkpointer_type=self._config.get("checkpointer", "postgres"),
                connection_string=self._config.get("connection_string"),
            )
        else:
            bench = WTBTestBenchFactory.create_for_testing()
        
        # Apply partial overrides from builder
        if self._batch_runner is not None:
            bench._replace_batch_runner(
                self._batch_runner,
                owns_batch_runner=False,
            )
        if self._execution_controller is not None:
            bench._replace_execution_controller(
                self._execution_controller,
                owns_execution_resources=False,
            )
        return bench
    
    def _has_all_custom_dependencies(self) -> bool:
        """Check if all four dependencies are custom-provided."""
        return all([
            self._project_service is not None,
            self._variant_service is not None,
            self._execution_controller is not None,
            self._batch_runner is not None,
        ])


# ═══════════════════════════════════════════════════════════════════════════════
# WTBTestBench - Main SDK Entry Point (Facade Pattern)
# ═══════════════════════════════════════════════════════════════════════════════

class WTBTestBench:
    """
    Main entry point for WTB SDK - Facade that delegates to Application Services.
    
    Refactored (v1.6):
    - Removed state_adapter dependency (layer separation)
    - Removed broken branch() method
    - fork() delegates to ExecutionController.fork()
    - All IDs are strings (UUIDs)
    
    Usage:
        wtb = WTBTestBench.create()
        wtb.register_project(project)
        execution = wtb.run(project="my_workflow", initial_state={"key": "value"})
        print(execution.status)  # ExecutionStatus.COMPLETED
    """
    
    def __init__(
        self,
        project_service: "ProjectService",
        variant_service: "VariantService",
        execution_controller: "IExecutionController",
        batch_runner: "IBatchTestRunner" | None = None,
        *,
        owns_batch_runner: bool = False,
        owns_execution_resources: bool = False,
    ):
        """
        Initialize WTBTestBench with Application Services.
        
        Args:
            project_service: Application service for project management
            variant_service: Application service for variant management
            execution_controller: Domain interface for execution lifecycle
            batch_runner: Optional batch test runner
            owns_batch_runner: Whether this bench must shut down the runner
            owns_execution_resources: Whether this bench owns the controller's
                state adapter, file tracker, and UoW
        """
        self._project_service = project_service
        self._variant_service = variant_service
        self._exec_ctrl = execution_controller
        self._batch_runner = batch_runner
        self._owns_batch_runner = bool(owns_batch_runner)
        self._owns_execution_resources = bool(owns_execution_resources)
        self._lifecycle_lock = threading.RLock()
        self._closed = False
        
        # SDK-level caches
        self._project_cache: dict[str, WorkflowProject] = {}
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Resource Lifecycle
    # ═══════════════════════════════════════════════════════════════════════════

    def close(self) -> None:
        """Release all resources held by this bench.

        Closes (in order): batch runner thread pool / Ray actors,
        execution controller's state adapter (checkpointer),
        and the underlying UoW session.  Safe to call multiple times.

        Every owned close is attempted. Failures are propagated and leave the
        bench open so cleanup can be retried; multiple failures are grouped.
        """
        with self._lifecycle_lock:
            if self._closed:
                return

            close_errors = []

            if self._owns_batch_runner and self._batch_runner is not None:
                shutdown = getattr(self._batch_runner, "shutdown", None)
                if callable(shutdown):
                    try:
                        shutdown()
                    except Exception as error:
                        close_errors.append(error)

            coordinator = getattr(self, "_batch_coordinator", None)
            if coordinator is not None:
                close_coordinator = getattr(coordinator, "close", None)
                if callable(close_coordinator):
                    try:
                        close_coordinator()
                    except Exception as error:
                        close_errors.append(error)
                    else:
                        self._batch_coordinator = None
                else:
                    self._batch_coordinator = None

            if self._owns_execution_resources:
                inner = getattr(self._exec_ctrl, "_inner", self._exec_ctrl)
                adapter = getattr(inner, "_state_adapter", None)
                file_tracking = getattr(inner, "_file_tracking", None)
                uow = getattr(inner, "_uow", None)
                seen_resources = set()

                for resource in (adapter, file_tracking):
                    if resource is None or id(resource) in seen_resources:
                        continue
                    seen_resources.add(id(resource))
                    close_resource = getattr(resource, "close", None)
                    if callable(close_resource):
                        try:
                            close_resource()
                        except Exception as error:
                            close_errors.append(error)

                if uow is not None and id(uow) not in seen_resources:
                    try:
                        uow.__exit__(None, None, None)
                    except Exception as error:
                        close_errors.append(error)
                    dispose_uow = getattr(uow, "dispose", None)
                    if callable(dispose_uow):
                        try:
                            dispose_uow()
                        except Exception as error:
                            close_errors.append(error)

            if close_errors:
                if len(close_errors) == 1:
                    raise close_errors[0]
                raise ExceptionGroup(
                    "Failed to close WTBTestBench resources",
                    close_errors,
                )

            self._closed = True

    def _replace_batch_runner(
        self,
        batch_runner: "IBatchTestRunner" | None,
        *,
        owns_batch_runner: bool,
    ) -> None:
        """Replace the runner without leaking an owned previous instance."""
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("WTBTestBench is closed")
            if batch_runner is self._batch_runner:
                self._owns_batch_runner = bool(owns_batch_runner)
                return

            previous = self._batch_runner
            if self._owns_batch_runner and previous is not None:
                shutdown = getattr(previous, "shutdown", None)
                if callable(shutdown):
                    shutdown()

            self._batch_runner = batch_runner
            self._owns_batch_runner = bool(owns_batch_runner)

    def _replace_execution_controller(
        self,
        execution_controller: "IExecutionController",
        *,
        owns_execution_resources: bool,
    ) -> None:
        """Replace the controller and release resources owned by the bench."""
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("WTBTestBench is closed")
            if execution_controller is self._exec_ctrl:
                self._owns_execution_resources = bool(owns_execution_resources)
                return

            if self._owns_execution_resources:
                inner = getattr(self._exec_ctrl, "_inner", self._exec_ctrl)
                adapter = getattr(inner, "_state_adapter", None)
                file_tracking = getattr(inner, "_file_tracking", None)
                uow = getattr(inner, "_uow", None)
                seen_resources = set()
                for resource in (adapter, file_tracking):
                    if resource is None or id(resource) in seen_resources:
                        continue
                    seen_resources.add(id(resource))
                    close_resource = getattr(resource, "close", None)
                    if callable(close_resource):
                        try:
                            close_resource()
                        except Exception:
                            pass
                if uow is not None and id(uow) not in seen_resources:
                    try:
                        uow.__exit__(None, None, None)
                    except Exception:
                        pass
                    dispose_uow = getattr(uow, "dispose", None)
                    if callable(dispose_uow):
                        try:
                            dispose_uow()
                        except Exception:
                            pass

            self._exec_ctrl = execution_controller
            self._owns_execution_resources = bool(owns_execution_resources)

    def __enter__(self) -> "WTBTestBench":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    @classmethod
    def create(cls, mode: str = "testing", **kwargs) -> "WTBTestBench":
        """
        Factory method for quick WTBTestBench creation.
        
        Delegates to WTBTestBenchFactory in the Application layer.
        
        Args:
            mode: "testing", "development", or "production"
            **kwargs:
                - data_dir: Directory for database files (default: "data")
                - enable_file_tracking: Enable file tracking for rollback
                - enable_ray: Use Ray-based batch runner (requires ray installed
                  and ``ray.init()`` called beforehand)
                - grpc_env_url: Optional gRPC URL for UV venv manager Docker
                  service (e.g. ``localhost:50051``). When set alongside
                  ``enable_ray=True``, each Ray actor gets an isolated venv.
                - checkpointer: "sqlite" or "postgres" (for production)
                - connection_string: Database connection string (for production)
        """
        from wtb.application.factories import WTBTestBenchFactory
        
        if mode == "testing":
            return WTBTestBenchFactory.create_for_testing()
        elif mode == "development":
            return WTBTestBenchFactory.create_for_development(
                data_dir=kwargs.get("data_dir", "data"),
                enable_file_tracking=kwargs.get("enable_file_tracking", False),
                enable_ray=kwargs.get("enable_ray", False),
                grpc_env_url=kwargs.get("grpc_env_url"),
            )
        elif mode == "production":
            return WTBTestBenchFactory.create_with_langgraph(
                checkpointer_type=kwargs.get("checkpointer", "postgres"),
                connection_string=kwargs.get("connection_string"),
                enable_file_tracking=kwargs.get("enable_file_tracking", False),
            )
        else:
            return WTBTestBenchFactory.create_for_testing()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Project Management - Delegates to ProjectService
    # ═══════════════════════════════════════════════════════════════════════════
    
    def register_project(self, project: WorkflowProject) -> None:
        """Register a workflow project."""
        workflow = self._project_to_workflow(project)
        self._project_service.register_workflow(workflow)
        self._project_cache[project.name] = project
    
    def get_project(self, name: str) -> WorkflowProject:
        """Get a registered project by name."""
        if name in self._project_cache:
            return self._project_cache[name]
        raise KeyError(f"Project '{name}' not found in cache")
    
    def list_projects(self) -> list[str]:
        """List all registered project names."""
        return list(self._project_cache.keys())
    
    def unregister_project(self, name: str) -> bool:
        """Unregister a project."""
        result = self._project_service.unregister_workflow(name)
        if result:
            self._project_cache.pop(name, None)
        return result
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Execution - Returns domain Execution directly
    # ═══════════════════════════════════════════════════════════════════════════
    
    @_serialized_adapter_access
    def run(
        self,
        project: str,
        initial_state: dict[str, Any],
        variant_config: dict[str, str] | None = None,
        workflow_variant: str | None = None,
        breakpoints: list[str] | None = None,
    ) -> Execution:
        """
        Run a workflow execution. Returns domain Execution object directly.
        
        Returns:
            Execution - domain model with status, state, timing, etc.
        """
        if project not in self._project_cache:
            raise KeyError(f"Project '{project}' not found")
        
        proj = self._project_cache[project]
        
        # Get workflow via ProjectService
        workflow = self._project_service.get_workflow_by_name(project)
        if not workflow:
            raise ValueError(f"Workflow for project '{project}' not found")
        
        run_initial_state = dict(initial_state)
        execution_metadata: dict[str, Any] = {}
        if variant_config:
            run_initial_state.setdefault(
                "_variant_config",
                copy.deepcopy(variant_config),
            )
            execution_metadata["_variant_config"] = copy.deepcopy(variant_config)
        if workflow_variant is not None:
            run_initial_state["_workflow_variant"] = workflow_variant
            execution_metadata["_workflow_variant"] = workflow_variant

        # Build graph
        graph = proj.build_graph(
            variant_config=variant_config,
            workflow_variant=workflow_variant,
        )
        
        # Create and run execution via ExecutionController
        create_kwargs: dict[str, Any] = {
            "workflow": workflow,
            "initial_state": run_initial_state,
            "breakpoints": breakpoints or [],
        }
        if execution_metadata:
            create_kwargs["metadata"] = execution_metadata
        execution = self._exec_ctrl.create_execution(**create_kwargs)

        try:
            return self._exec_ctrl.run(execution.id, graph=graph)
        except BaseException as error:
            # The execution is already durable at this point.  Surface its ID
            # so callers can account for an orphaned physical attempt even if
            # the controller fails while running or persisting the result.
            error.wtb_execution_id = str(execution.id)
            raise
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Execution Control - Delegates to IExecutionController
    # ═══════════════════════════════════════════════════════════════════════════
    
    @_serialized_adapter_access
    def pause(self, execution_id: str) -> Execution:
        """Pause execution. Returns domain Execution."""
        execution = self.get_execution(execution_id)
        metadata = getattr(execution, "metadata", None)
        if isinstance(metadata, dict) and metadata.get("checkpoint_db_path"):
            graph = self._require_graph_for_execution(
                execution_id,
                execution=execution,
            )
            with self._prepare_controller_graph(graph, execution=execution):
                return self._exec_ctrl.pause(execution_id)
        return self._exec_ctrl.pause(execution_id)
    
    @_serialized_adapter_access
    def resume(self, execution_id: str, modified_state: dict[str, Any] | None = None) -> Execution:
        """Resume execution. Returns domain Execution."""
        execution = self.get_execution(execution_id)
        graph = self._require_graph_for_execution(execution_id, execution=execution)
        with self._prepare_controller_graph(graph, execution=execution):
            return self._exec_ctrl.resume(execution_id, modified_state)
    
    @_serialized_adapter_access
    def stop(self, execution_id: str) -> Execution:
        """Stop execution. Returns domain Execution."""
        return self._exec_ctrl.stop(execution_id)
    
    @_serialized_adapter_access
    def rollback(self, execution_id: str, checkpoint_id: str) -> RollbackResult:
        """
        Rollback to a checkpoint. Returns RollbackResult (operation DTO).
        
        Args:
            execution_id: Execution to rollback
            checkpoint_id: Checkpoint ID (UUID string)
        """
        try:
            execution = self.get_execution(execution_id)
            graph = self._require_graph_for_execution(
                execution_id,
                execution=execution,
            )
            with self._prepare_controller_graph(graph, execution=execution):
                self._exec_ctrl.rollback(execution_id, checkpoint_id)
            return RollbackResult(execution_id=execution_id, to_checkpoint_id=checkpoint_id, success=True)
        except Exception as e:
            return RollbackResult(execution_id=execution_id, to_checkpoint_id=checkpoint_id, success=False, error=str(e))
    
    @_serialized_adapter_access
    def rollback_to_node(self, execution_id: str, node_id: str) -> RollbackResult:
        """Rollback to after a specific node completed."""
        if not self._exec_ctrl.supports_time_travel():
            return RollbackResult(execution_id=execution_id, to_checkpoint_id="", success=False, error="Time-travel not supported")
        
        try:
            execution = self.get_execution(execution_id)
            graph = self._require_graph_for_execution(
                execution_id,
                execution=execution,
            )
            with self._prepare_controller_graph(graph, execution=execution):
                execution = self._exec_ctrl.rollback_to_node(execution_id, node_id)
            return RollbackResult(
                execution_id=execution_id,
                to_checkpoint_id=execution.checkpoint_id or "",
                success=True,
            )
        except Exception as e:
            return RollbackResult(execution_id=execution_id, to_checkpoint_id="", success=False, error=str(e))
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Forking - Delegates to ExecutionController.fork()
    # ═══════════════════════════════════════════════════════════════════════════
    
    @_serialized_adapter_access
    def fork(self, execution_id: str, checkpoint_id: str, new_initial_state: dict[str, Any] | None = None) -> ForkResult:
        """
        Fork an execution to create a new independent execution.
        
        Creates a new Execution record starting from the checkpoint state.
        Delegates to ExecutionController.fork() for ACID compliance.
        
        Args:
            execution_id: Source execution ID
            checkpoint_id: Checkpoint ID to fork from (UUID string)
            new_initial_state: Optional state to merge with checkpoint state
            
        Returns:
            ForkResult with fork details including the new execution ID
        """
        try:
            execution = self.get_execution(execution_id)
            graph = self._require_graph_for_execution(
                execution_id,
                execution=execution,
            )
            with self._prepare_controller_graph(graph, execution=execution):
                forked_execution = self._exec_ctrl.fork(
                    execution_id,
                    checkpoint_id,
                    new_initial_state,
                )
            
            return ForkResult(
                fork_execution_id=forked_execution.id,
                source_execution_id=execution_id,
                source_checkpoint_id=checkpoint_id,
            )
        except Exception as e:
            logger.error(f"Fork failed: {e}")
            raise
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Batch Rollback/Fork - NEW (v1.8)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_batch_coordinator(self) -> "BatchExecutionCoordinator":
        """
        Get or create BatchExecutionCoordinator for rollback/fork operations.
        
        Lazily initializes coordinator on first call.
        Reuses same coordinator instance for efficiency (StateAdapter reuse).
        
        Design (SOLID):
        - SRP: Coordinator handles batch operations, SDK provides convenient access
        - DIP: Uses interfaces, not concrete implementations
        
        Usage:
            batch = wtb.run_batch_test(...)
            coordinator = wtb.get_batch_coordinator()
            coordinator.rollback(batch.results[0].execution_id, checkpoint_id)
        
        Returns:
            BatchExecutionCoordinator instance
        """
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("WTBTestBench is closed")
            if not hasattr(self, '_batch_coordinator') or self._batch_coordinator is None:
                self._batch_coordinator = self._create_batch_coordinator()
            return self._batch_coordinator

    def _resolve_graph_for_execution(
        self,
        execution_id: str,
        execution: Execution | None = None,
    ) -> Any | None:
        """Resolve the registered project graph for an existing execution."""
        if execution is None:
            try:
                execution = self.get_execution(execution_id)
            except Exception:
                return None

        metadata = (
            getattr(execution, "metadata", None)
            if isinstance(getattr(execution, "metadata", None), dict)
            else {}
        )
        state_vars = {}
        if execution.state and execution.state.workflow_variables:
            state_vars = execution.state.workflow_variables
        variant_config = metadata.get(
            "_variant_config",
            state_vars.get("_variant_config"),
        )
        workflow_variant = metadata.get(
            "_workflow_variant",
            state_vars.get("_workflow_variant"),
        )

        matching_projects = {
            id(proj): proj
            for cache_name, proj in self._project_cache.items()
            if execution.workflow_id
            in (proj.id, getattr(proj, "name", cache_name), cache_name)
        }
        if len(matching_projects) == 1:
            proj = next(iter(matching_projects.values()))
            build_kwargs = {"variant_config": variant_config}
            if workflow_variant is not None:
                build_kwargs["workflow_variant"] = workflow_variant
            return proj.build_graph(**build_kwargs)

        return None

    def _require_graph_for_execution(
        self,
        execution_id: str,
        execution: Execution | None = None,
    ) -> Any | None:
        """Resolve one execution-owned graph or reject the control operation.

        Node-executor checkpoints do not have a LangGraph graph contract. A
        persisted ``node_sqlite`` execution therefore remains controllable
        without a registered SDK project, provided its exact state adapter is
        rebound by :meth:`_prepare_controller_graph`.
        """
        graph = self._resolve_graph_for_execution(
            execution_id,
            execution=execution,
        )
        if graph is None:
            if self._execution_state_adapter_backend(execution) == "node_sqlite":
                return None
            workflow_id = getattr(execution, "workflow_id", None)
            raise ValueError(
                "No registered project matches "
                f"workflow_id '{workflow_id}' for execution '{execution_id}'"
            )
        return graph

    @contextmanager
    def _prepare_controller_graph(
        self,
        graph: Any | None,
        execution: Execution | None = None,
    ) -> Iterator[None]:
        """Temporarily bind an execution-specific graph and state adapter."""
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("WTBTestBench is closed")

            controller = getattr(self._exec_ctrl, "_inner", self._exec_ctrl)
            original_adapter = getattr(controller, "_state_adapter", None)
            state_adapter = self._state_adapter_for_execution(
                execution,
                fallback=original_adapter,
            )
            temporary_adapter = (
                state_adapter is not None and state_adapter is not original_adapter
            )
            if temporary_adapter:
                setattr(controller, "_state_adapter", state_adapter)

            try:
                set_graph = (
                    getattr(state_adapter, "set_workflow_graph", None)
                    if graph is not None
                    else None
                )
                if callable(set_graph):
                    set_graph(graph, force_recompile=True)
                yield
            finally:
                if temporary_adapter:
                    setattr(controller, "_state_adapter", original_adapter)
                    close_adapter = getattr(state_adapter, "close", None)
                    if callable(close_adapter):
                        try:
                            close_adapter()
                        except Exception as close_error:
                            logger.warning(
                                "Could not close temporary state adapter: %s",
                                close_error,
                            )

    @staticmethod
    def _execution_state_adapter_backend(
        execution: Execution | None,
    ) -> str | None:
        """Return the persisted adapter backend for actor-local execution state."""
        if execution is None:
            return None

        metadata = getattr(execution, "metadata", None)
        if not isinstance(metadata, dict):
            return None
        if not metadata.get("checkpoint_db_path"):
            return None

        backend = metadata.get("state_adapter_backend")
        if not backend:
            raise RuntimeError(
                "Missing state_adapter_backend for execution-specific "
                f"checkpoint storage on execution '{execution.id}'"
            )
        if backend not in _EXECUTION_STATE_ADAPTER_BACKENDS:
            raise RuntimeError(
                "Unsupported execution-specific state adapter backend "
                f"'{backend}' for execution '{execution.id}'"
            )
        return backend

    @staticmethod
    def _normalized_state_adapter_path(value: Any) -> str | None:
        """Normalize adapter paths before deciding whether reuse is safe."""
        if value is None:
            return None
        try:
            path_value = os.fspath(value)
        except TypeError:
            path_value = str(value)
        if not path_value:
            return None
        return os.path.normcase(os.path.abspath(os.path.expanduser(path_value)))

    @staticmethod
    def _state_adapter_identity(adapter: Any | None) -> tuple[str | None, Any]:
        """Return the advertised backend and storage path of an adapter."""
        if adapter is None:
            return None, None

        backend = getattr(adapter, "state_adapter_backend", None)
        if (
            not isinstance(backend, str)
            or backend not in _EXECUTION_STATE_ADAPTER_BACKENDS
        ):
            backend = None

        storage_path = getattr(adapter, "storage_path", None)
        if storage_path is None:
            storage_path = getattr(
                getattr(adapter, "_config", None),
                "connection_string",
                None,
            )
        return backend, storage_path

    def _state_adapter_for_execution(
        self,
        execution: Execution | None,
        fallback: Any | None,
    ) -> Any | None:
        """Use actor-local checkpoint storage when execution metadata requires it."""
        backend = self._execution_state_adapter_backend(execution)
        if backend is None or execution is None:
            return fallback

        checkpoint_db_path = self._normalized_state_adapter_path(
            execution.metadata["checkpoint_db_path"]
        )
        if checkpoint_db_path is None:
            raise RuntimeError(
                "Could not resolve execution-specific state adapter storage "
                f"for execution '{execution.id}'"
            )

        fallback_backend, fallback_path = self._state_adapter_identity(fallback)
        if (
            fallback_backend == backend
            and self._normalized_state_adapter_path(fallback_path)
            == checkpoint_db_path
        ):
            return fallback

        if backend == "node_sqlite":
            try:
                from wtb.infrastructure.adapters.sqlite_state_adapter import (
                    SqliteStateAdapter,
                )

                return SqliteStateAdapter(storage_path=checkpoint_db_path)
            except Exception as error:
                raise RuntimeError(
                    "Could not create execution-specific node_sqlite state adapter "
                    f"for execution '{execution.id}' at '{checkpoint_db_path}': "
                    f"{error}"
                ) from error

        try:
            from wtb.infrastructure.adapters.langgraph_state_adapter import (
                LANGGRAPH_AVAILABLE,
                LangGraphConfig,
                LangGraphStateAdapter,
            )
        except Exception as error:
            raise RuntimeError(
                "Could not import execution-specific langgraph_sqlite state "
                f"adapter for execution '{execution.id}': {error}"
            ) from error

        if not LANGGRAPH_AVAILABLE:
            raise RuntimeError(
                "Could not create execution-specific langgraph_sqlite state "
                f"adapter for execution '{execution.id}': LangGraph is unavailable"
            )

        try:
            return LangGraphStateAdapter(
                LangGraphConfig.for_development(checkpoint_db_path)
            )
        except Exception as error:
            raise RuntimeError(
                "Could not create execution-specific langgraph_sqlite state "
                f"adapter for execution '{execution.id}' at "
                f"'{checkpoint_db_path}': {error}"
            ) from error
    
    def rollback_batch_result(
        self,
        result: BatchTestResult,
        checkpoint_id: str | None = None,
        graph: Any | None = None,
    ) -> BatchRollbackResult:
        """
        Convenience: Rollback a batch test result to a checkpoint.
        
        Transaction Flow (ACID):
        1. [UoW] Restore state via ExecutionController
        2. [UoW] Emit outbox event for audit
        3. [UoW] Commit transaction
        4. [Post-commit] Restore files (best-effort, retryable)
        
        Args:
            result: BatchTestResult from run_batch_test()
            checkpoint_id: Checkpoint ID to rollback to.
                          Defaults to result.last_checkpoint_id if not provided.
            graph: Optional compiled LangGraph. Auto-resolved from registered
                   projects when not provided.
            
        Returns:
            BatchRollbackResult with execution and file restore status
            
        Raises:
            ValueError: If result has no execution_id or no checkpoint available
            
        Example:
            # Rollback to last checkpoint (most common)
            wtb.rollback_batch_result(batch.results[0])
            
            # Rollback to specific checkpoint
            wtb.rollback_batch_result(batch.results[0], checkpoint_id="abc-123")
        """
        if not result.execution_id:
            raise ValueError("BatchTestResult has no execution_id")
        
        cp_id = checkpoint_id or result.last_checkpoint_id
        if not cp_id:
            raise ValueError(
                "No checkpoint_id provided and result has no last_checkpoint_id. "
                "Use get_batch_result_checkpoints() to list available checkpoints."
            )
        
        try:
            resolved_graph = (
                graph
                if graph is not None
                else self._require_graph_for_execution(result.execution_id)
            )
            coordinator = self.get_batch_coordinator()
            execution = coordinator.rollback(result.execution_id, cp_id, graph=resolved_graph)
            
            return BatchRollbackResult(
                execution_id=result.execution_id,
                checkpoint_id=cp_id,
                success=True,
                execution=execution,
            )
        except Exception as e:
            logger.error(f"Batch rollback failed: {e}")
            return BatchRollbackResult(
                execution_id=result.execution_id,
                checkpoint_id=cp_id,
                success=False,
                error=str(e),
            )
    
    def fork_batch_result(
        self,
        result: BatchTestResult,
        checkpoint_id: str | None = None,
        new_state: dict[str, Any] | None = None,
        graph: Any | None = None,
    ) -> BatchForkResult:
        """
        Convenience: Fork a batch test result from a checkpoint.
        
        Creates a new execution starting from the checkpoint state.
        Original execution is unchanged (non-destructive operation).
        
        Transaction Flow (ACID):
        1. [UoW] Create new execution from checkpoint state
        2. [UoW] Emit EXECUTION_FORKED outbox event
        3. [UoW] Commit transaction
        
        Args:
            result: BatchTestResult from run_batch_test()
            checkpoint_id: Checkpoint ID to fork from.
                          Defaults to result.last_checkpoint_id if not provided.
            new_state: Optional state to merge with checkpoint state
            graph: Optional compiled LangGraph. Auto-resolved from registered
                   projects when not provided.
            
        Returns:
            BatchForkResult with new execution details
            
        Example:
            # Fork from last checkpoint
            fork = wtb.fork_batch_result(batch.results[0])
            
            # Fork from specific checkpoint with modified state
            fork = wtb.fork_batch_result(
                batch.results[0], 
                checkpoint_id="abc-123",
                new_state={"temperature": 0.5}
            )
        """
        if not result.execution_id:
            raise ValueError("BatchTestResult has no execution_id")
        
        cp_id = checkpoint_id or result.last_checkpoint_id
        if not cp_id:
            raise ValueError(
                "No checkpoint_id provided and result has no last_checkpoint_id. "
                "Use get_batch_result_checkpoints() to list available checkpoints."
            )
        
        try:
            resolved_graph = (
                graph
                if graph is not None
                else self._require_graph_for_execution(result.execution_id)
            )
            coordinator = self.get_batch_coordinator()
            forked = coordinator.fork(result.execution_id, cp_id, new_state, graph=resolved_graph)
            
            return BatchForkResult(
                source_execution_id=result.execution_id,
                fork_execution_id=forked.id,
                checkpoint_id=cp_id,
                execution=forked,
            )
        except Exception as e:
            logger.error(f"Batch fork failed: {e}")
            return BatchForkResult(
                source_execution_id=result.execution_id,
                fork_execution_id="",
                checkpoint_id=cp_id,
                error=str(e),
            )
    
    def _checkpoint_dicts_to_domain(
        self,
        execution_id: str,
        history: list[dict[str, Any]],
    ) -> list[Checkpoint]:
        """Convert raw checkpoint dicts to domain Checkpoint objects."""
        checkpoints = []
        for cp in history:
            writes = cp.get("writes") or {}
            source = cp.get("source", "")
            
            if not writes and source and source not in ("input", "__start__", ""):
                writes = {source: {}}
            
            checkpoints.append(Checkpoint(
                id=CheckpointId(str(cp.get("checkpoint_id", cp.get("id", "")))),
                execution_id=execution_id,
                step=cp.get("step", 0),
                node_writes=writes,
                next_nodes=cp.get("next", []),
                state_values=cp.get("values", {}),
                created_at=cp.get("created_at") or datetime.now(timezone.utc),
            ))
        return checkpoints

    def get_batch_result_checkpoints(
        self,
        result: BatchTestResult,
    ) -> list[Checkpoint]:
        """
        Get checkpoints for a batch test result.

        Uses the BatchExecutionCoordinator to resolve execution-specific
        storage (actor-local checkpoint DBs) before falling back to the
        bench's own state adapter.
        
        Args:
            result: BatchTestResult from run_batch_test()
            
        Returns:
            List of Checkpoint objects
            
        Example:
            checkpoints = wtb.get_batch_result_checkpoints(batch.results[0])
            for cp in checkpoints:
                print(f"Checkpoint {cp.id} at step {cp.step}")
        """
        if not result.execution_id:
            return []

        coordinator = self.get_batch_coordinator()
        graph = self._resolve_graph_for_execution(result.execution_id)
        history = coordinator.get_checkpoints(result.execution_id, graph=graph)
        if history:
            return self._checkpoint_dicts_to_domain(result.execution_id, history)

        return self.get_checkpoints(result.execution_id)
    
    def _create_batch_coordinator(self) -> "BatchExecutionCoordinator":
        """
        Create BatchExecutionCoordinator with current configuration.
        
        Design (DIP + Layer Separation):
        - Prefers using batch_runner's factory if available (shares config)
        - Falls back to Application layer factory (NOT infrastructure)
        - SDK never directly instantiates infrastructure components
        """
        # Prefer batch_runner's factory (shares configuration)
        if self._batch_runner and hasattr(self._batch_runner, 'create_rollback_coordinator'):
            return self._batch_runner.create_rollback_coordinator()
        
        # Fallback: delegate to Application layer factory (proper layer separation)
        logger.warning(
            "Creating BatchExecutionCoordinator without batch_runner - "
            "using Application factory with default configuration"
        )
        
        from wtb.application.factories import BatchCoordinatorFactory
        return BatchCoordinatorFactory.create_default()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Variant Management - Delegates to VariantService
    # ═══════════════════════════════════════════════════════════════════════════
    
    def register_variant(
        self,
        project: str,
        node: str,
        name: str,
        implementation: Callable,
        description: str = "",
    ) -> None:
        """Register a node variant."""
        self._variant_service.register_variant(
            workflow_name=project,
            node_id=node,
            variant_name=name,
            implementation=implementation,
            description=description,
        )
        
        # Update SDK cache
        if project in self._project_cache:
            self._project_cache[project].register_variant(node=node, name=name, implementation=implementation, description=description)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Batch Testing - Returns domain BatchTest directly
    # ═══════════════════════════════════════════════════════════════════════════
    
    def run_batch_test(
        self,
        project: str,
        variant_matrix: list[dict[str, str]],
        test_cases: list[dict[str, Any]],
    ) -> BatchTest:
        """
        Run batch tests. Returns domain BatchTest with results.
        """
        proj = self._project_cache.get(project)
        isolated_variant_matrix = copy.deepcopy(variant_matrix)
        isolated_test_cases = copy.deepcopy(test_cases)
        requested_executor = getattr(
            getattr(proj, "execution", None),
            "batch_executor",
            None,
        )

        if requested_executor == "sequential":
            return self._run_batch_sequential(
                project,
                isolated_variant_matrix,
                isolated_test_cases,
            )

        if not self._batch_runner:
            if requested_executor in {"ray", "threadpool"}:
                from wtb.domain.interfaces.batch_runner import BatchRunnerError
                raise BatchRunnerError(
                    f"Project '{project}' requires batch_executor="
                    f"'{requested_executor}', but no batch runner is configured. "
                    "Recreate the test bench with the requested executor or use "
                    "ExecutionConfig(batch_executor='sequential')."
                )
            return self._run_batch_sequential(
                project,
                isolated_variant_matrix,
                isolated_test_cases,
            )

        runner_executor = None
        from wtb.application.services.batch_test_runner import ThreadPoolBatchTestRunner
        if isinstance(self._batch_runner, ThreadPoolBatchTestRunner):
            runner_executor = "threadpool"
        elif any(
            runner_type.__module__ == "wtb.application.services.ray_batch_runner"
            and runner_type.__name__ == "RayBatchTestRunner"
            for runner_type in type(self._batch_runner).__mro__
        ):
            # Avoid importing Ray merely to identify an already-created runner.
            runner_executor = "ray"

        if (
            runner_executor is not None
            and requested_executor in {"ray", "threadpool"}
            and requested_executor != runner_executor
        ):
            from wtb.domain.interfaces.batch_runner import BatchRunnerError
            raise BatchRunnerError(
                f"Project '{project}' requires batch_executor='{requested_executor}', "
                f"but the configured batch runner is '{runner_executor}'. "
                "Recreate the test bench with matching enable_ray or update ExecutionConfig."
            )
        
        # Get workflow via ProjectService
        workflow = self._project_service.get_workflow_by_name(project)
        if not workflow:
            raise ValueError(f"Project '{project}' not found")
        
        # Extract graph_factory reference so Ray actors can recreate the graph.
        # When the script is the entry point, __module__ is "__main__" which
        # is not importable in a Ray actor process.  Resolve to the real
        # module name via __spec__ when possible.
        gf_module: str | None = None
        gf_name: str | None = None
        gf_pickled: str | None = None
        if proj and callable(getattr(proj, "graph_factory", None)):
            gf = proj.graph_factory
            gf_module = getattr(gf, "__module__", None)
            gf_name = getattr(gf, "__qualname__", None) or getattr(gf, "__name__", None)
            if gf_module == "__main__":
                import sys
                main_spec = getattr(sys.modules.get("__main__"), "__spec__", None)
                if main_spec and main_spec.name:
                    gf_module = main_spec.name
            # An importable module on the driver may not be installed on a
            # remote Ray worker.  Always carry a cloudpickle fallback so the
            # actor can still reconstruct the registered graph factory.
            try:
                import sys

                import cloudpickle

                factory_module = sys.modules.get(getattr(gf, "__module__", ""))
                pickle_by_value = bool(
                    factory_module is not None
                    and getattr(gf, "__module__", None) != "__main__"
                    and hasattr(cloudpickle, "register_pickle_by_value")
                )
                if pickle_by_value:
                    cloudpickle.register_pickle_by_value(factory_module)
                try:
                    gf_pickled = base64.b64encode(
                        cloudpickle.dumps(gf)
                    ).decode("ascii")
                finally:
                    if pickle_by_value:
                        cloudpickle.unregister_pickle_by_value(factory_module)
            except Exception:
                pass
        
        # Create domain BatchTest with workflow cache for batch runner
        batch_test = BatchTest(workflow_id=workflow.id, _workflow=workflow)
        if gf_pickled is not None:
            batch_test.metadata["_graph_factory_pickled"] = gf_pickled
        
        from wtb.domain.models.batch_test import VariantCombination
        for i, variant_config in enumerate(isolated_variant_matrix):
            isolated_variant_config = copy.deepcopy(variant_config)
            combo_metadata: dict[str, Any] = {}
            runtime_graph: Any | None = None
            graph: Any | None = None
            registered_variant_selected = bool(
                proj
                and any(
                    proj.get_variant(node_id, variant_name) is not None
                    for node_id, variant_name in isolated_variant_config.items()
                )
            )
            if registered_variant_selected:
                try:
                    graph = proj.build_graph(
                        variant_config=copy.deepcopy(isolated_variant_config)
                    )
                    try:
                        import cloudpickle
                    except ImportError:
                        from ray import cloudpickle
                    serialized_graph = cloudpickle.dumps(graph)
                    # A few proxy/test graph types serialize but cannot be
                    # restored. Never publish a transport payload that the
                    # worker cannot consume.
                    cloudpickle.loads(serialized_graph)
                    combo_metadata["_graph_pickled"] = base64.b64encode(
                        serialized_graph
                    ).decode("ascii")
                except Exception as graph_error:
                    if runner_executor == "threadpool" and graph is not None:
                        # Threads share this process, so retain the real graph
                        # outside public metadata instead of forcing a broken
                        # serialization round-trip.
                        runtime_graph = graph
                    else:
                        from wtb.domain.interfaces.batch_runner import BatchRunnerError
                        raise BatchRunnerError(
                            "Registered node variants require a serializable graph "
                            f"for batch execution: {graph_error}"
                        ) from graph_error

            batch_test.variant_combinations.append(VariantCombination(
                name=f"variant_{i}",
                variants=isolated_variant_config,
                metadata=combo_metadata,
                graph_factory_module=gf_module,
                graph_factory_name=gf_name,
                _runtime_graph=runtime_graph,
            ))
        
        if not isolated_test_cases:
            result = self._batch_runner.run_batch_test(batch_test)
            result.metadata["test_case_count"] = 1
            for item in result.results:
                item.test_case_index = 0
            result.build_comparison_matrix()
            self._expire_session()
            return result
        
        all_results = []
        for case_index, test_case in enumerate(isolated_test_cases):
            case_batch = BatchTest(workflow_id=batch_test.workflow_id, _workflow=workflow)
            case_batch.metadata = dict(batch_test.metadata)
            case_batch.variant_combinations = list(batch_test.variant_combinations)
            case_batch.initial_state = copy.deepcopy(test_case)
            case_result = self._batch_runner.run_batch_test(case_batch)
            for item in case_result.results:
                item.test_case_index = case_index
            all_results.append(case_result)
        
        if len(all_results) == 1:
            all_results[0].metadata["test_case_count"] = 1
            all_results[0].build_comparison_matrix()
            self._expire_session()
            return all_results[0]

        combined_metadata = dict(batch_test.metadata)
        combined_metadata["test_case_count"] = len(isolated_test_cases)
        combined = BatchTest(
            workflow_id=batch_test.workflow_id,
            variant_combinations=list(batch_test.variant_combinations),
            metadata=combined_metadata,
            _workflow=workflow,
        )
        combined.start()
        for case_result in all_results:
            for result in case_result.results:
                combined.add_result(result)

        expected_names = {
            combination.name for combination in batch_test.variant_combinations
        }
        incomplete_cases = [
            case_result for case_result in all_results
            if (
                len(case_result.results) != len(expected_names)
                or len({item.combination_name for item in case_result.results})
                != len(case_result.results)
                or {item.combination_name for item in case_result.results}
                != expected_names
            )
        ]
        if any(
            case_result.status is BatchTestStatus.CANCELLED
            for case_result in all_results
        ):
            # Cancellation takes precedence because the requested test matrix
            # was not fully evaluated, even if earlier cases produced results.
            combined.cancel()
        elif incomplete_cases:
            combined.fail(
                "Incomplete test-case result identities: expected exactly "
                f"{sorted(expected_names)} for every case"
            )
        elif combined.results and all(
            not result.success for result in combined.results
        ):
            combined.fail("All variants failed")
        else:
            combined.complete()
        combined.build_comparison_matrix()

        self._expire_session()
        return combined
    
    @staticmethod
    def _extract_batch_result_metrics(execution: Execution) -> dict[str, float]:
        """Extract batch metrics with the same contract used by worker modes."""
        metrics: dict[str, float] = {}
        if execution.state:
            variables = (
                getattr(execution.state, "workflow_variables", {}) or {}
            )
            if "overall_score" in variables:
                metrics["overall_score"] = variables["overall_score"]
            if "accuracy" in variables:
                metrics["accuracy"] = variables["accuracy"]
            if "latency_ms" in variables:
                metrics["latency_ms"] = variables["latency_ms"]
            if "_metrics" in variables and isinstance(variables["_metrics"], dict):
                metrics.update(variables["_metrics"])

        if "overall_score" not in metrics:
            metrics["overall_score"] = (
                1.0 if execution.status == ExecutionStatus.COMPLETED else 0.0
            )
        return normalize_finite_metrics(metrics)

    @_serialized_adapter_access
    def _run_batch_sequential(
        self,
        project: str,
        variant_matrix: list[dict[str, str]],
        test_cases: list[dict[str, Any]],
    ) -> BatchTest:
        """Fallback sequential batch execution.
        
        Pre-resolves the workflow once, then builds the graph per variant
        inline and passes it explicitly to the controller.  Result fields
        are populated to match the ThreadPoolBatchTestRunner output schema.
        """
        import time as _time

        from wtb.domain.interfaces.batch_runner import BatchRunnerError

        if not variant_matrix:
            raise BatchRunnerError("No variant combinations to execute")
        isolated_variant_matrix = copy.deepcopy(variant_matrix)
        cases = copy.deepcopy(test_cases or [{}])

        
        if project not in self._project_cache:
            raise KeyError(f"Project '{project}' not found")
        proj = self._project_cache[project]
        
        workflow = self._project_service.get_workflow_by_name(project)
        if not workflow:
            raise ValueError(f"Project '{project}' not found")
        
        batch_test = BatchTest(
            workflow_id=workflow.id,
            metadata={"test_case_count": len(cases)},
        )
        
        from wtb.domain.models.batch_test import VariantCombination
        for i, variant_config in enumerate(isolated_variant_matrix):
            batch_test.variant_combinations.append(
                VariantCombination(
                    name=f"variant_{i}",
                    variants=copy.deepcopy(variant_config),
                )
            )
        
        batch_test.start()
        
        for i, variant_config in enumerate(isolated_variant_matrix):
            variant_name = f"variant_{i}"
            for case_index, test_case in enumerate(cases):
                start_ms = _time.time()
                try:
                    graph = proj.build_graph(
                        variant_config=copy.deepcopy(variant_config)
                    )
                    execution = self._exec_ctrl.create_execution(
                        workflow=workflow,
                        initial_state=copy.deepcopy(test_case),
                    )
                    execution = self._exec_ctrl.run(execution.id, graph=graph)
                    
                    duration_ms = int((_time.time() - start_ms) * 1000)
                    metrics = self._extract_batch_result_metrics(execution)
                    
                    result = BatchTestResult(
                        combination_name=variant_name,
                        execution_id=execution.id,
                        success=execution.status == ExecutionStatus.COMPLETED,
                        error_message=execution.error_message,
                        duration_ms=duration_ms,
                        metrics=metrics,
                        overall_score=metrics["overall_score"],
                        last_checkpoint_id=getattr(execution, "checkpoint_id", None),
                        checkpoint_count=(
                            len(execution.state.execution_path)
                            if execution.state else 0
                        ),
                        test_case_index=case_index,
                    )
                    batch_test.add_result(result)
                except Exception as e:
                    duration_ms = int((_time.time() - start_ms) * 1000)
                    result = BatchTestResult(
                        combination_name=variant_name,
                        execution_id="",
                        success=False,
                        error_message=str(e),
                        duration_ms=duration_ms,
                        test_case_index=case_index,
                    )
                    batch_test.add_result(result)
        
        if batch_test.results and all(
            not result.success for result in batch_test.results
        ):
            batch_test.fail("All variants failed")
        else:
            batch_test.complete()
        batch_test.build_comparison_matrix()
        return batch_test
    
    def _expire_session(self) -> None:
        """Expire the bench's ORM session cache so subsequent reads see
        rows committed by isolated batch-worker sessions."""
        inner = getattr(self._exec_ctrl, "_inner", self._exec_ctrl)
        uow = getattr(inner, "_uow", None)
        session = getattr(uow, "_session", None)
        if session is not None:
            try:
                session.expire_all()
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════════════════════════
    # State Inspection - Returns domain models
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_state(self, execution_id: str) -> ExecutionState:
        """Get current state. Returns domain ExecutionState."""
        return self._exec_ctrl.get_state(execution_id)
    
    def get_execution(self, execution_id: str) -> Execution:
        """Get execution. Returns domain Execution."""
        return self._exec_ctrl.get_status(execution_id)
    
    @_serialized_adapter_access
    def get_checkpoints(self, execution_id: str) -> list[Checkpoint]:
        """Get checkpoints. Returns domain Checkpoint list."""
        if not self._exec_ctrl.supports_time_travel():
            return []

        execution = self.get_execution(execution_id)
        metadata = getattr(execution, "metadata", None)
        if isinstance(metadata, dict) and metadata.get("checkpoint_db_path"):
            graph = self._require_graph_for_execution(
                execution_id,
                execution=execution,
            )
            with self._prepare_controller_graph(graph, execution=execution):
                history = self._exec_ctrl.get_checkpoint_history(execution_id)
        else:
            history = self._exec_ctrl.get_checkpoint_history(execution_id)
        return self._checkpoint_dicts_to_domain(execution_id, history)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Capability Checks
    # ═══════════════════════════════════════════════════════════════════════════
    
    @_serialized_adapter_access
    def supports_time_travel(self) -> bool:
        return self._exec_ctrl.supports_time_travel()
    
    @_serialized_adapter_access
    def supports_streaming(self) -> bool:
        return self._exec_ctrl.supports_streaming()
    
    def supports_forking(self) -> bool:
        """Check if forking is supported (always True in v1.6)."""
        return hasattr(self._exec_ctrl, 'fork')
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Internal Conversion Methods (v1.7: Delegates to Application Service)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _project_to_workflow(self, project: WorkflowProject):
        """
        Convert SDK WorkflowProject to domain TestWorkflow.
        
        v1.7: Delegates to WorkflowConversionService (layer separation).
        The conversion logic now lives in Application layer, not SDK.
        """
        from wtb.application.services.project_service import WorkflowConversionService
        
        converter = WorkflowConversionService()
        return converter.convert_from_project(project)


# ═══════════════════════════════════════════════════════════════════════════════
# Backward Compatibility - Deprecated
# ═══════════════════════════════════════════════════════════════════════════════

class ExecutionControllerBuilder:
    """DEPRECATED: Use WTBTestBenchBuilder instead."""
    
    def __init__(self):
        import warnings
        warnings.warn("ExecutionControllerBuilder is deprecated. Use WTBTestBenchBuilder.", DeprecationWarning, stacklevel=2)
        self._mode = "inmemory"
        self._config = {}
    
    def with_inmemory(self) -> "ExecutionControllerBuilder":
        self._mode = "inmemory"
        return self
    
    def with_sqlite(self, db_path: str = "data/wtb.db") -> "ExecutionControllerBuilder":
        self._mode = "sqlite"
        self._config["db_path"] = db_path
        return self
    
    def with_langgraph(self, checkpointer_type: str = "memory", connection_string: str | None = None) -> "ExecutionControllerBuilder":
        self._mode = "langgraph"
        self._config["checkpointer_type"] = checkpointer_type
        self._config["connection_string"] = connection_string
        return self
    
    def with_state_adapter(self, adapter) -> "ExecutionControllerBuilder":
        return self
    
    def with_event_bus(self, event_bus) -> "ExecutionControllerBuilder":
        return self
    
    def with_node_executor(self, executor) -> "ExecutionControllerBuilder":
        return self
    
    def build(self):
        from wtb.application.factories import ExecutionControllerFactory
        if self._mode == "inmemory":
            return ExecutionControllerFactory.create_for_testing()
        elif self._mode == "sqlite":
            return ExecutionControllerFactory.create_for_development()
        else:
            return ExecutionControllerFactory.create_for_testing()
