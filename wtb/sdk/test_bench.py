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
import uuid
import logging

# REUSE domain models - don't duplicate!
from wtb.domain.models.workflow import Execution, ExecutionState, ExecutionStatus
from wtb.domain.models.checkpoint import Checkpoint, CheckpointId
from wtb.domain.models.batch_test import BatchTest, BatchTestResult, BatchTestStatus

from .workflow_project import WorkflowProject, NodeVariant as SDKNodeVariant

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
        
        # Check if user provided ALL custom dependencies (full manual wiring)
        if self._has_all_custom_dependencies():
            return WTBTestBench(
                project_service=self._project_service,
                variant_service=self._variant_service,
                execution_controller=self._execution_controller,
                batch_runner=self._batch_runner,
            )
        
        # Delegate to Application Factory (proper composition root)
        if self._mode == "testing":
            return WTBTestBenchFactory.create_for_testing()
        elif self._mode == "development":
            return WTBTestBenchFactory.create_for_development(
                data_dir=self._config.get("data_dir", "data")
            )
        elif self._mode == "production":
            return WTBTestBenchFactory.create_with_langgraph(
                checkpointer_type=self._config.get("checkpointer", "postgres"),
                connection_string=self._config.get("connection_string"),
            )
        else:
            return WTBTestBenchFactory.create_for_testing()
    
    def _has_all_custom_dependencies(self) -> bool:
        """Check if all dependencies are custom-provided."""
        return (
            self._project_service is not None and
            self._variant_service is not None and
            self._execution_controller is not None
        )


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
        
        # SDK-level caches
        self._project_cache: Dict[str, WorkflowProject] = {}
    
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
        
        # Build graph
        graph = proj.build_graph(
            variant_config=variant_config,
            workflow_variant=workflow_variant,
        )
        
        # Create and run execution via ExecutionController
        execution = self._exec_ctrl.create_execution(
            workflow=workflow,
            initial_state=initial_state,
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
    
    def rollback_batch_result(
        self,
        result: BatchTestResult,
        checkpoint_id: Optional[str] = None,
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
        
        # Use provided checkpoint_id or default to last_checkpoint_id
        cp_id = checkpoint_id or result.last_checkpoint_id
        if not cp_id:
            raise ValueError(
                "No checkpoint_id provided and result has no last_checkpoint_id. "
                "Use get_batch_result_checkpoints() to list available checkpoints."
            )
        
        try:
            coordinator = self.get_batch_coordinator()
            execution = coordinator.rollback(result.execution_id, cp_id)
            
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
        
        # Use provided checkpoint_id or default to last_checkpoint_id
        cp_id = checkpoint_id or result.last_checkpoint_id
        if not cp_id:
            raise ValueError(
                "No checkpoint_id provided and result has no last_checkpoint_id. "
                "Use get_batch_result_checkpoints() to list available checkpoints."
            )
        
        try:
            coordinator = self.get_batch_coordinator()
            forked = coordinator.fork(result.execution_id, cp_id, new_state)
            
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
    
    def get_batch_result_checkpoints(
        self,
        result: BatchTestResult,
    ) -> List[Checkpoint]:
        """
        Get checkpoints for a batch test result.
        
        Convenience method to list available checkpoints for rollback/fork.
        
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
        
        # Create domain BatchTest
        batch_test = BatchTest(workflow_id=workflow.id)
        
        from wtb.domain.models.batch_test import VariantCombination
        for i, variant_config in enumerate(variant_matrix):
            batch_test.variant_combinations.append(
                VariantCombination(name=f"variant_{i}", variants=variant_config)
            )
        
        for test_case in test_cases:
            batch_test.initial_state = test_case
        
        # Execute via domain interface
        return self._batch_runner.run_batch_test(batch_test)
    
    def _run_batch_sequential(
        self,
        project: str,
        variant_matrix: List[Dict[str, str]],
        test_cases: List[Dict[str, Any]],
    ) -> BatchTest:
        """Fallback sequential batch execution."""
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
            
            try:
                for test_case in test_cases:
                    execution = self.run(project=project, initial_state=test_case, variant_config=variant_config)
                    
                    result = BatchTestResult(
                        combination_name=variant_name,
                        execution_id=execution.id,
                        success=execution.status == ExecutionStatus.COMPLETED,
                        error_message=execution.error_message,
                    )
                    batch_test.add_result(result)
                    
            except Exception as e:
                result = BatchTestResult(
                    combination_name=variant_name,
                    execution_id="",
                    success=False,
                    error_message=str(e),
                )
                batch_test.add_result(result)
        
        batch_test.complete()
        batch_test.build_comparison_matrix()
        return batch_test
    
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
