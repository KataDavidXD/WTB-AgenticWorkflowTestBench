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

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING
from datetime import datetime, timezone
import logging

# REUSE domain models - don't duplicate!
from wtb.domain.models.workflow import Execution, ExecutionState, ExecutionStatus
from wtb.domain.models.checkpoint import Checkpoint, CheckpointId
from wtb.domain.models.batch_test import BatchTest, BatchTestResult

from .workflow_project import WorkflowProject

if TYPE_CHECKING:
    from wtb.domain.interfaces import (
        IExecutionController,
        IBatchTestRunner,
    )
    from wtb.application.services import ProjectService, VariantService
    from wtb.application.services.batch_execution_coordinator import BatchExecutionCoordinator

logger = logging.getLogger(__name__)


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
    error: Optional[str] = None


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
    execution: Optional[Execution] = None
    files_restored: int = 0
    error: Optional[str] = None


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
    execution: Optional[Execution] = None
    error: Optional[str] = None


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
        self._config: Dict[str, Any] = {}
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
        connection_string: Optional[str] = None,
    ) -> "WTBTestBenchBuilder":
        """Configure for production (LangGraph checkpointers)."""
        self._mode = "production"
        self._config["checkpointer"] = checkpointer
        self._config["connection_string"] = connection_string
        return self
    
    def with_ray(
        self,
        data_dir: str = "data",
        grpc_env_url: Optional[str] = None,
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
            bench._batch_runner = self._batch_runner
        if self._execution_controller is not None:
            bench._execution_controller = self._execution_controller
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
        batch_runner: Optional["IBatchTestRunner"] = None,
    ):
        """
        Initialize WTBTestBench with Application Services.
        
        Args:
            project_service: Application service for project management
            variant_service: Application service for variant management
            execution_controller: Domain interface for execution lifecycle
            batch_runner: Optional batch test runner
        """
        self._project_service = project_service
        self._variant_service = variant_service
        self._exec_ctrl = execution_controller
        self._batch_runner = batch_runner
        self._closed = False
        
        # SDK-level caches
        self._project_cache: Dict[str, WorkflowProject] = {}
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Resource Lifecycle
    # ═══════════════════════════════════════════════════════════════════════════

    def close(self) -> None:
        """Release all resources held by this bench.

        Closes (in order): batch runner thread pool / Ray actors,
        execution controller's state adapter (checkpointer),
        and the underlying UoW session.  Safe to call multiple times.
        """
        if self._closed:
            return
        self._closed = True

        if self._batch_runner is not None:
            _shutdown = getattr(self._batch_runner, "shutdown", None)
            if callable(_shutdown):
                try:
                    _shutdown()
                except Exception:
                    pass

        inner = getattr(self._exec_ctrl, "_inner", self._exec_ctrl)
        adapter = getattr(inner, "_state_adapter", None)
        if adapter is not None:
            _close = getattr(adapter, "close", None)
            if callable(_close):
                try:
                    _close()
                except Exception:
                    pass

        uow = getattr(inner, "_uow", None)
        if uow is not None:
            try:
                uow.__exit__(None, None, None)
            except Exception:
                pass

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
    
    def list_projects(self) -> List[str]:
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
    
    def run(
        self,
        project: str,
        initial_state: Dict[str, Any],
        variant_config: Optional[Dict[str, str]] = None,
        workflow_variant: Optional[str] = None,
        breakpoints: Optional[List[str]] = None,
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
        if variant_config:
            run_initial_state.setdefault("_variant_config", dict(variant_config))

        # Build graph
        graph = proj.build_graph(
            variant_config=variant_config,
            workflow_variant=workflow_variant,
        )
        
        # Create and run execution via ExecutionController
        execution = self._exec_ctrl.create_execution(
            workflow=workflow,
            initial_state=run_initial_state,
            breakpoints=breakpoints or [],
        )
        
        return self._exec_ctrl.run(execution.id, graph=graph)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Execution Control - Delegates to IExecutionController
    # ═══════════════════════════════════════════════════════════════════════════
    
    def pause(self, execution_id: str) -> Execution:
        """Pause execution. Returns domain Execution."""
        return self._exec_ctrl.pause(execution_id)
    
    def resume(self, execution_id: str, modified_state: Optional[Dict[str, Any]] = None) -> Execution:
        """Resume execution. Returns domain Execution."""
        execution = self.get_execution(execution_id)
        graph = self._resolve_graph_for_execution(execution_id, execution=execution)
        self._prepare_controller_graph(graph, execution=execution)
        return self._exec_ctrl.resume(execution_id, modified_state)
    
    def stop(self, execution_id: str) -> Execution:
        """Stop execution. Returns domain Execution."""
        return self._exec_ctrl.stop(execution_id)
    
    def rollback(self, execution_id: str, checkpoint_id: str) -> RollbackResult:
        """
        Rollback to a checkpoint. Returns RollbackResult (operation DTO).
        
        Args:
            execution_id: Execution to rollback
            checkpoint_id: Checkpoint ID (UUID string)
        """
        try:
            self._exec_ctrl.rollback(execution_id, checkpoint_id)
            return RollbackResult(execution_id=execution_id, to_checkpoint_id=checkpoint_id, success=True)
        except Exception as e:
            return RollbackResult(execution_id=execution_id, to_checkpoint_id=checkpoint_id, success=False, error=str(e))
    
    def rollback_to_node(self, execution_id: str, node_id: str) -> RollbackResult:
        """Rollback to after a specific node completed."""
        if not self._exec_ctrl.supports_time_travel():
            return RollbackResult(execution_id=execution_id, to_checkpoint_id="", success=False, error="Time-travel not supported")
        
        try:
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
    
    def fork(self, execution_id: str, checkpoint_id: str, new_initial_state: Optional[Dict[str, Any]] = None) -> ForkResult:
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
            forked_execution = self._exec_ctrl.fork(execution_id, checkpoint_id, new_initial_state)
            
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
        if not hasattr(self, '_batch_coordinator') or self._batch_coordinator is None:
            self._batch_coordinator = self._create_batch_coordinator()
        return self._batch_coordinator
    
    def _resolve_graph_for_result(self, graph: Optional[Any] = None) -> Optional[Any]:
        """Resolve a compiled LangGraph graph from the project cache if not provided."""
        if graph is not None:
            return graph
        for proj in self._project_cache.values():
            if callable(getattr(proj, "graph_factory", None)):
                try:
                    return proj.graph_factory()
                except Exception:
                    continue
        return None

    def _resolve_graph_for_execution(
        self,
        execution_id: str,
        execution: Optional[Execution] = None,
    ) -> Optional[Any]:
        """Resolve the registered project graph for an existing execution."""
        if execution is None:
            try:
                execution = self.get_execution(execution_id)
            except Exception:
                return None

        state_vars = {}
        if execution.state and execution.state.workflow_variables:
            state_vars = execution.state.workflow_variables
        variant_config = state_vars.get("_variant_config")

        for proj in self._project_cache.values():
            if proj.id == execution.workflow_id:
                return proj.build_graph(variant_config=variant_config)

        if len(self._project_cache) == 1:
            proj = next(iter(self._project_cache.values()))
            return proj.build_graph(variant_config=variant_config)

        return None

    def _prepare_controller_graph(
        self,
        graph: Optional[Any],
        execution: Optional[Execution] = None,
    ) -> None:
        """Prime the underlying controller's state adapter before resume."""
        if graph is None:
            return

        controller = getattr(self._exec_ctrl, "_inner", self._exec_ctrl)
        state_adapter = self._state_adapter_for_execution(
            execution,
            fallback=getattr(controller, "_state_adapter", None),
        )
        if state_adapter is not None:
            setattr(controller, "_state_adapter", state_adapter)
        set_graph = getattr(state_adapter, "set_workflow_graph", None)
        if callable(set_graph):
            set_graph(graph, force_recompile=True)

    def _state_adapter_for_execution(
        self,
        execution: Optional[Execution],
        fallback: Optional[Any],
    ) -> Optional[Any]:
        """Use actor-local checkpoint storage when execution metadata requires it."""
        if execution is None:
            return fallback

        metadata = execution.metadata or {}
        if not isinstance(metadata, dict) or not metadata.get("checkpoint_db_path"):
            return fallback

        try:
            from wtb.application.services.external_storage import (
                resolve_execution_storage_paths,
            )
            from wtb.infrastructure.adapters.langgraph_state_adapter import (
                LANGGRAPH_AVAILABLE,
                LangGraphConfig,
                LangGraphStateAdapter,
            )
        except Exception:
            return fallback

        if not LANGGRAPH_AVAILABLE:
            return fallback

        paths = resolve_execution_storage_paths(
            metadata,
            fallback_actor_id=metadata.get("actor_id"),
        )
        checkpoint_db_path = str(paths.checkpoint_db_path)
        existing_connection = getattr(
            getattr(fallback, "_config", None),
            "connection_string",
            None,
        )
        if existing_connection and str(existing_connection) == checkpoint_db_path:
            return fallback

        return LangGraphStateAdapter(LangGraphConfig.for_development(checkpoint_db_path))
    
    def rollback_batch_result(
        self,
        result: BatchTestResult,
        checkpoint_id: Optional[str] = None,
        graph: Optional[Any] = None,
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
        
        resolved_graph = self._resolve_graph_for_result(graph)
        
        try:
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
        checkpoint_id: Optional[str] = None,
        new_state: Optional[Dict[str, Any]] = None,
        graph: Optional[Any] = None,
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
        
        resolved_graph = self._resolve_graph_for_result(graph)
        
        try:
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
        history: List[Dict[str, Any]],
    ) -> List[Checkpoint]:
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
    ) -> List[Checkpoint]:
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

        try:
            coordinator = self.get_batch_coordinator()
            graph = self._resolve_graph_for_result()
            history = coordinator.get_checkpoints(result.execution_id, graph=graph)
            if history:
                return self._checkpoint_dicts_to_domain(result.execution_id, history)
        except Exception:
            pass

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
        variant_matrix: List[Dict[str, str]],
        test_cases: List[Dict[str, Any]],
    ) -> BatchTest:
        """
        Run batch tests. Returns domain BatchTest with results.
        """
        if not self._batch_runner:
            return self._run_batch_sequential(project, variant_matrix, test_cases)
        
        # Get workflow via ProjectService
        workflow = self._project_service.get_workflow_by_name(project)
        if not workflow:
            raise ValueError(f"Project '{project}' not found")
        
        # Extract graph_factory reference so Ray actors can recreate the graph.
        # When the script is the entry point, __module__ is "__main__" which
        # is not importable in a Ray actor process.  Resolve to the real
        # module name via __spec__ when possible.
        proj = self._project_cache.get(project)
        gf_module: Optional[str] = None
        gf_name: Optional[str] = None
        gf_pickled: Optional[bytes] = None
        if proj and callable(getattr(proj, "graph_factory", None)):
            gf = proj.graph_factory
            gf_module = getattr(gf, "__module__", None)
            gf_name = getattr(gf, "__qualname__", None) or getattr(gf, "__name__", None)
            if gf_module == "__main__":
                import sys
                main_spec = getattr(sys.modules.get("__main__"), "__spec__", None)
                if main_spec and main_spec.name:
                    gf_module = main_spec.name
                else:
                    try:
                        import cloudpickle
                        gf_pickled = cloudpickle.dumps(gf)
                    except Exception:
                        pass
        
        # Create domain BatchTest with workflow cache for batch runner
        batch_test = BatchTest(workflow_id=workflow.id, _workflow=workflow)
        if gf_pickled is not None:
            batch_test.metadata["_graph_factory_pickled"] = gf_pickled
        
        from wtb.domain.models.batch_test import VariantCombination
        for i, variant_config in enumerate(variant_matrix):
            batch_test.variant_combinations.append(
                VariantCombination(
                    name=f"variant_{i}",
                    variants=variant_config,
                    graph_factory_module=gf_module,
                    graph_factory_name=gf_name,
                )
            )
        
        if not test_cases:
            result = self._batch_runner.run_batch_test(batch_test)
            self._expire_session()
            return result
        
        all_results = []
        for test_case in test_cases:
            case_batch = BatchTest(workflow_id=batch_test.workflow_id, _workflow=workflow)
            case_batch.variant_combinations = batch_test.variant_combinations
            case_batch.initial_state = test_case
            all_results.append(self._batch_runner.run_batch_test(case_batch))
        
        if len(all_results) == 1:
            return all_results[0]
        
        # Aggregate: return first batch with all results combined
        combined = all_results[0]
        for extra in all_results[1:]:
            combined.results.extend(extra.results)

        self._expire_session()
        return combined
    
    def _run_batch_sequential(
        self,
        project: str,
        variant_matrix: List[Dict[str, str]],
        test_cases: List[Dict[str, Any]],
    ) -> BatchTest:
        """Fallback sequential batch execution.
        
        Pre-resolves the workflow once, then builds the graph per variant
        inline and passes it explicitly to the controller.  Result fields
        are populated to match the ThreadPoolBatchTestRunner output schema.
        """
        import time as _time
        
        if project not in self._project_cache:
            raise KeyError(f"Project '{project}' not found")
        proj = self._project_cache[project]
        
        workflow = self._project_service.get_workflow_by_name(project)
        if not workflow:
            raise ValueError(f"Project '{project}' not found")
        
        batch_test = BatchTest(workflow_id=workflow.id)
        
        from wtb.domain.models.batch_test import VariantCombination
        for i, variant_config in enumerate(variant_matrix):
            batch_test.variant_combinations.append(
                VariantCombination(name=f"variant_{i}", variants=variant_config)
            )
        
        batch_test.start()
        
        for i, variant_config in enumerate(variant_matrix):
            variant_name = f"variant_{i}"
            start_ms = _time.time()
            
            try:
                graph = proj.build_graph(variant_config=variant_config)
                
                for test_case in test_cases:
                    execution = self._exec_ctrl.create_execution(
                        workflow=workflow,
                        initial_state=test_case,
                    )
                    execution = self._exec_ctrl.run(execution.id, graph=graph)
                    
                    duration_ms = int((_time.time() - start_ms) * 1000)
                    metrics = {}
                    if execution.state:
                        wv = getattr(execution.state, "workflow_variables", {}) or {}
                        metrics = {
                            k: v for k, v in wv.items()
                            if isinstance(v, (int, float))
                        }
                    
                    result = BatchTestResult(
                        combination_name=variant_name,
                        execution_id=execution.id,
                        success=execution.status == ExecutionStatus.COMPLETED,
                        error_message=execution.error_message,
                        duration_ms=duration_ms,
                        metrics=metrics,
                        overall_score=metrics.get("overall_score", 0.0),
                        last_checkpoint_id=getattr(execution, "checkpoint_id", None),
                        checkpoint_count=(
                            len(execution.state.execution_path)
                            if execution.state else 0
                        ),
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
                )
                batch_test.add_result(result)
        
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
    
    def get_checkpoints(self, execution_id: str) -> List[Checkpoint]:
        """Get checkpoints. Returns domain Checkpoint list."""
        if not self._exec_ctrl.supports_time_travel():
            return []
        
        history = self._exec_ctrl.get_checkpoint_history(execution_id)
        return self._checkpoint_dicts_to_domain(execution_id, history)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Capability Checks
    # ═══════════════════════════════════════════════════════════════════════════
    
    def supports_time_travel(self) -> bool:
        return self._exec_ctrl.supports_time_travel()
    
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
    
    def with_langgraph(self, checkpointer_type: str = "memory", connection_string: Optional[str] = None) -> "ExecutionControllerBuilder":
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
