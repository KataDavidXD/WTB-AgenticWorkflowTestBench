"""
ThreadPool Batch Test Runner.

Local multithreaded implementation of IBatchTestRunner for development
and single-node execution.

Refactored (v1.7):
- Uses ExecutionControllerFactory for ACID-compliant isolated execution
- Each thread gets its own ExecutionController + UoW (Isolation)
- Removed placeholder _run_workflow_nodes() - actual execution via controller

SOLID Compliance:
- SRP: Orchestrates batch tests only, delegates execution to controller
- OCP: New execution strategies via factory
- DIP: Depends on factory callables, not concrete implementations

ACID Compliance:
- Atomicity: Each variant execution is atomic (UoW transaction)
- Consistency: Unified execution path via ExecutionController
- Isolation: Each thread has isolated UoW + StateAdapter
- Durability: Results persisted via UoW.commit()

Usage:
    from wtb.application.services.batch_test_runner import ThreadPoolBatchTestRunner
    from wtb.application.factories import ExecutionControllerFactory
    
    runner = ThreadPoolBatchTestRunner(
        controller_factory=ExecutionControllerFactory.get_factory_callable(config),
        max_workers=4,
    )
    
    result = runner.run_batch_test(batch_test)
"""

import base64
from concurrent.futures import ThreadPoolExecutor, Future, as_completed
from threading import Lock
from typing import Dict, Any, List, Optional, Callable, TYPE_CHECKING
from datetime import datetime
from dataclasses import dataclass
import time
import uuid
import logging

from wtb.domain.models.batch_test import (
    BatchTest,
    BatchTestResult,
    BatchTestStatus,
    VariantCombination,
)
from wtb.domain.models.workflow import Execution, ExecutionStatus, TestWorkflow
from wtb.domain.interfaces.batch_runner import (
    IBatchTestRunner,
    BatchRunnerStatus,
    BatchRunnerProgress,
    BatchRunnerError,
    BatchRunnerExecutionError,
)
from wtb.domain.interfaces.state_adapter import IStateAdapter
from wtb.domain.interfaces.unit_of_work import IUnitOfWork

if TYPE_CHECKING:
    from wtb.application.factories import ManagedController

logger = logging.getLogger(__name__)


@dataclass
class _RunningTest:
    """Internal state for a running batch test."""
    batch_test_id: str
    futures: List[Future]
    started_at: datetime
    completed: int = 0
    failed: int = 0
    cancelled: bool = False


class ThreadPoolBatchTestRunner(IBatchTestRunner):
    """
    ThreadPool-based batch test runner for local execution.
    
    Refactored (v1.7):
    - Uses ExecutionControllerFactory for ACID-compliant isolated execution
    - Each thread gets its own ExecutionController + UoW (Isolation)
    - Actual execution via ExecutionController (replaced placeholder)
    
    Key Design Decisions:
    - ThreadPoolExecutor for I/O-bound operations (DB, LLM calls)
    - Application-level isolation: each thread gets own controller + UoW
    - Progress tracking via internal state
    - Graceful cancellation support
    
    SOLID Compliance:
    - SRP: Orchestrates batch tests only
    - OCP: New execution strategies via factory
    - DIP: Depends on factory callable abstraction
    
    ACID Compliance:
    - Atomicity: Each variant in its own transaction
    - Consistency: Unified execution via ExecutionController
    - Isolation: Each thread has isolated dependencies
    - Durability: Results persisted via UoW.commit()
    
    Thread Safety:
    - Uses Lock for internal state updates
    - Each worker thread has isolated dependencies
    
    Usage (v1.7):
        from wtb.application.factories import ExecutionControllerFactory
        
        runner = ThreadPoolBatchTestRunner(
            controller_factory=ExecutionControllerFactory.get_factory_callable(config),
            max_workers=4,
        )
        
        batch_test = BatchTest(...)
        result = runner.run_batch_test(batch_test)
        
    Legacy Usage (backward compatible):
        runner = ThreadPoolBatchTestRunner(
            uow_factory=lambda: InMemoryUnitOfWork(),
            state_adapter_factory=lambda: InMemoryStateAdapter(),
            max_workers=4,
        )
    """
    
    def __init__(
        self,
        controller_factory: Optional[Callable[[], "ManagedController"]] = None,
        uow_factory: Optional[Callable[[], IUnitOfWork]] = None,
        state_adapter_factory: Optional[Callable[[], IStateAdapter]] = None,
        max_workers: int = 4,
        execution_timeout_seconds: float = 300.0,
        config: Optional[Any] = None,
    ):
        """
        Initialize the runner.
        
        Args:
            controller_factory: Factory to create isolated ExecutionController (v1.7 preferred)
            uow_factory: Legacy factory to create isolated UnitOfWork per thread
            state_adapter_factory: Legacy factory to create isolated StateAdapter per thread
            max_workers: Maximum concurrent workers
            execution_timeout_seconds: Timeout for each variant execution
            config: Optional WTBConfig for creating coordinators.
            
        Note:
            If controller_factory is provided, uow_factory and state_adapter_factory
            are ignored. The controller_factory approach is preferred for ACID compliance.
        """
        self._controller_factory = controller_factory
        self._uow_factory = uow_factory
        self._state_adapter_factory = state_adapter_factory
        self._max_workers = max_workers
        self._execution_timeout = execution_timeout_seconds
        self._config = config
        
        self._executor: Optional[ThreadPoolExecutor] = None
        self._running_tests: Dict[str, _RunningTest] = {}
        self._lock = Lock()
        self._shutdown = False
    
    # ═══════════════════════════════════════════════════════════════
    # IBatchTestRunner Implementation
    # ═══════════════════════════════════════════════════════════════
    
    def run_batch_test(self, batch_test: BatchTest) -> BatchTest:
        """
        Execute batch test with ThreadPool parallelism.
        
        Flow:
        1. Create ThreadPoolExecutor if needed
        2. Submit all variants as separate tasks
        3. Wait for completion with progress tracking
        4. Aggregate results and build comparison matrix
        """
        if self._shutdown:
            raise BatchRunnerError("Runner has been shut down")
        
        # Validate batch test
        if not batch_test.variant_combinations:
            raise BatchRunnerError("No variant combinations to execute")
        
        # Resolve workflow locally (not on self) for thread safety when
        # concurrent run_batch_test calls share the same runner instance.
        batch_workflow = getattr(batch_test, '_workflow', None)
        
        batch_test.start()
        
        # Ensure executor exists
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=self._max_workers)
        
        # Track running state
        running_test = _RunningTest(
            batch_test_id=batch_test.id,
            futures=[],
            started_at=datetime.now(),
        )
        
        with self._lock:
            self._running_tests[batch_test.id] = running_test
        
        try:
            # Submit all variants
            futures_to_combo: Dict[Future, VariantCombination] = {}
            
            for combo in batch_test.variant_combinations:
                runtime_graph = getattr(combo, "_runtime_graph", None)
                future = self._executor.submit(
                    self._execute_variant,
                    batch_test.workflow_id,
                    combo,
                    batch_test.initial_state.copy(),
                    runtime_graph,
                    batch_workflow,
                )
                futures_to_combo[future] = combo
                running_test.futures.append(future)
            
            # Process results as they complete
            for future in as_completed(futures_to_combo.keys()):
                combo = futures_to_combo[future]
                
                # Check for cancellation under lock for visibility
                with self._lock:
                    cancelled = running_test.cancelled
                if cancelled:
                    break
                
                try:
                    result = future.result(timeout=self._execution_timeout)
                    batch_test.add_result(result)
                    
                    with self._lock:
                        if result.success:
                            running_test.completed += 1
                        else:
                            running_test.failed += 1
                            
                except Exception as e:
                    logger.error(f"Variant {combo.name} failed: {e}")
                    
                    error_result = BatchTestResult(
                        combination_name=combo.name,
                        execution_id="",
                        success=False,
                        error_message=str(e),
                    )
                    batch_test.add_result(error_result)
                    
                    with self._lock:
                        running_test.failed += 1
            
            # Finalize
            if running_test.cancelled:
                batch_test.cancel()
            elif running_test.failed == len(batch_test.variant_combinations):
                batch_test.fail("All variants failed")
            else:
                batch_test.complete()
                batch_test.build_comparison_matrix()
            
            return batch_test
            
        finally:
            with self._lock:
                self._running_tests.pop(batch_test.id, None)
    
    def _execute_variant(
        self,
        workflow_id: str,
        combo: VariantCombination,
        initial_state: Dict[str, Any],
        workflow_graph: Optional[Any] = None,
        batch_workflow: Optional[Any] = None,
    ) -> BatchTestResult:
        """
        Execute a single variant combination.
        
        Refactored (v1.7): Uses ExecutionController for actual execution.
        Creates isolated dependencies for thread safety and ACID compliance.
        
        Args:
            workflow_id: Workflow to execute
            combo: Variant combination to apply
            initial_state: Initial state for execution
            workflow_graph: Optional pre-built LangGraph workflow
            
        Returns:
            BatchTestResult with execution outcome
            
        ACID Compliance:
        - Atomicity: Entire execution in single transaction
        - Consistency: Uses ExecutionController for unified execution path
        - Isolation: Each call creates new UoW + controller
        - Durability: Results persisted via UoW.commit()
        """
        start_time = time.time()
        execution_id = str(uuid.uuid4())
        
        try:
            if workflow_graph is None:
                graph_pickled = (combo.metadata or {}).get("_graph_pickled")
                if graph_pickled:
                    try:
                        try:
                            import cloudpickle
                        except ImportError:
                            from ray import cloudpickle
                        graph_payload = (
                            base64.b64decode(graph_pickled.encode("ascii"), validate=True)
                            if isinstance(graph_pickled, str)
                            else graph_pickled
                        )
                        workflow_graph = cloudpickle.loads(graph_payload)
                    except Exception as graph_error:
                        raise BatchRunnerExecutionError(
                            f"Failed to load serialized variant graph: {graph_error}",
                            batch_test_id="",
                            failed_variant=combo.name,
                        ) from graph_error

            # v1.7: Use controller factory if available (preferred)
            if self._controller_factory is not None:
                return self._execute_with_controller_factory(
                    workflow_id, combo, initial_state, workflow_graph, start_time,
                    batch_workflow,
                )
            
            # Legacy path: use uow_factory + state_adapter_factory
            return self._execute_with_legacy_factories(
                workflow_id, combo, initial_state, start_time, execution_id,
                workflow_graph, batch_workflow,
            )
                
        except Exception as e:
            logger.error(f"Execution failed for {combo.name}: {e}")
            duration_ms = int((time.time() - start_time) * 1000)
            
            return BatchTestResult(
                combination_name=combo.name,
                execution_id=execution_id,
                success=False,
                duration_ms=duration_ms,
                error_message=str(e),
            )
    
    def _execute_with_controller_factory(
        self,
        workflow_id: str,
        combo: VariantCombination,
        initial_state: Dict[str, Any],
        workflow_graph: Optional[Any],
        start_time: float,
        batch_workflow: Optional[Any] = None,
    ) -> BatchTestResult:
        """
        Execute variant using v1.7 controller factory pattern.
        
        v1.9: Imports graph from graph_factory if workflow_graph is None
        (mirrors Ray actor pattern for LangGraph execution with checkpoints).
        
        ACID Compliance: Each execution gets isolated controller + UoW.
        """
        # Import graph from factory if not provided (mirror Ray pattern)
        if workflow_graph is None:
            graph_factory_module = getattr(combo, 'graph_factory_module', None)
            graph_factory_name = getattr(combo, 'graph_factory_name', None)
            if not graph_factory_module:
                graph_factory_module = (combo.metadata or {}).get("graph_factory_module")
            if not graph_factory_name:
                graph_factory_name = (combo.metadata or {}).get("graph_factory_name")
            if graph_factory_module and graph_factory_name:
                try:
                    from wtb.application.services.graph_loader import load_graph_factory
                    factory_fn = load_graph_factory(graph_factory_module, graph_factory_name)
                    workflow_graph = factory_fn()
                    logger.info(
                        f"ThreadPool: Imported graph from "
                        f"{graph_factory_module}.{graph_factory_name} for {combo.name}"
                    )
                except Exception as e:
                    logger.warning(
                        f"ThreadPool: Failed to import graph factory "
                        f"{graph_factory_module}.{graph_factory_name}: {e}"
                    )
        
        with self._controller_factory() as managed:
            controller = managed.controller
            uow = managed.uow
            
            # Use workflow passed as argument, else lookup from UoW
            workflow = batch_workflow
            if workflow is None:
                workflow = uow.workflows.get(workflow_id)
            if workflow is None:
                raise BatchRunnerExecutionError(
                    f"Workflow {workflow_id} not found",
                    batch_test_id="",
                    failed_variant=combo.name,
                )
            
            if not uow.workflows.exists(workflow.id):
                try:
                    uow.workflows.add(workflow)
                except Exception:
                    uow.rollback()
            
            variant_state = initial_state.copy()
            variant_state["_variant_config"] = combo.variants
            variant_state["_variant_name"] = combo.name
            
            execution = controller.create_execution(
                workflow=workflow,
                initial_state=variant_state,
            )
            
            execution = controller.run(execution.id, graph=workflow_graph)
            
            result_metrics = self._extract_metrics(execution)
            duration_ms = int((time.time() - start_time) * 1000)
            
            success = execution.status == ExecutionStatus.COMPLETED
            error_msg = execution.error_message if not success else None
            
            return BatchTestResult(
                combination_name=combo.name,
                execution_id=execution.id,
                success=success,
                duration_ms=duration_ms,
                metrics=result_metrics,
                overall_score=result_metrics.get("overall_score", 0.0),
                error_message=error_msg,
                last_checkpoint_id=execution.checkpoint_id,
                checkpoint_count=len(execution.state.execution_path) if execution.state else 0,
            )
    
    def _execute_with_legacy_factories(
        self,
        workflow_id: str,
        combo: VariantCombination,
        initial_state: Dict[str, Any],
        start_time: float,
        execution_id: str,
        workflow_graph: Optional[Any] = None,
        batch_workflow: Optional[Any] = None,
    ) -> BatchTestResult:
        """
        Execute variant using legacy uow_factory + state_adapter_factory.
        
        Maintains backward compatibility with existing code.
        """
        if self._uow_factory is None or self._state_adapter_factory is None:
            raise BatchRunnerError(
                "Either controller_factory or (uow_factory + state_adapter_factory) required"
            )
        
        # Create isolated dependencies
        uow = self._uow_factory()
        state_adapter = self._state_adapter_factory()
        
        with uow:
            # Import here to avoid circular imports
            from .execution_controller import ExecutionController, DefaultNodeExecutor
            
            workflow = batch_workflow
            if workflow is None:
                workflow = uow.workflows.get(workflow_id)
            if workflow is None:
                raise BatchRunnerExecutionError(
                    f"Workflow {workflow_id} not found",
                    batch_test_id="",
                    failed_variant=combo.name,
                )
            if not uow.workflows.exists(workflow.id):
                try:
                    uow.workflows.add(workflow)
                except Exception:
                    uow.rollback()
            
            # Create controller for this execution
            controller = ExecutionController(
                execution_repository=uow.executions,
                workflow_repository=uow.workflows,
                state_adapter=state_adapter,
                node_executor=DefaultNodeExecutor(),
                unit_of_work=uow,
            )
            
            # Apply variants to initial state
            variant_state = initial_state.copy()
            variant_state["_variant_config"] = combo.variants
            variant_state["_variant_name"] = combo.name
            
            # Create and run execution
            execution = controller.create_execution(
                workflow=workflow,
                initial_state=variant_state,
            )
            
            execution = controller.run(execution.id, graph=workflow_graph)
            
            # Calculate metrics
            result_metrics = self._extract_metrics(execution)
            duration_ms = int((time.time() - start_time) * 1000)
            
            success = execution.status == ExecutionStatus.COMPLETED
            error_msg = execution.error_message if not success else None
            
            return BatchTestResult(
                combination_name=combo.name,
                execution_id=execution.id,
                success=success,
                duration_ms=duration_ms,
                metrics=result_metrics,
                overall_score=result_metrics.get("overall_score", 0.0),
                error_message=error_msg,
            )
    
    def _extract_metrics(self, execution: Execution) -> Dict[str, float]:
        """
        Extract metrics from completed execution.
        
        Args:
            execution: Completed execution
            
        Returns:
            Dictionary of metrics
        """
        metrics: Dict[str, float] = {}
        
        # Extract from execution state
        if execution.state and execution.state.workflow_variables:
            vars = execution.state.workflow_variables
            
            # Common metric patterns
            if "overall_score" in vars:
                metrics["overall_score"] = float(vars["overall_score"])
            if "accuracy" in vars:
                metrics["accuracy"] = float(vars["accuracy"])
            if "latency_ms" in vars:
                metrics["latency_ms"] = float(vars["latency_ms"])
            if "_metrics" in vars and isinstance(vars["_metrics"], dict):
                metrics.update(vars["_metrics"])
        
        # Default overall score if not present
        if "overall_score" not in metrics:
            metrics["overall_score"] = 1.0 if execution.status == ExecutionStatus.COMPLETED else 0.0
        
        return metrics
    
    
    def get_status(self, batch_test_id: str) -> BatchRunnerStatus:
        """Get status of a batch test."""
        with self._lock:
            if batch_test_id in self._running_tests:
                running = self._running_tests[batch_test_id]
                if running.cancelled:
                    return BatchRunnerStatus.CANCELLING
                return BatchRunnerStatus.RUNNING
        return BatchRunnerStatus.IDLE
    
    def get_progress(self, batch_test_id: str) -> Optional[BatchRunnerProgress]:
        """Get progress for a running batch test."""
        with self._lock:
            running = self._running_tests.get(batch_test_id)
            if running is None:
                return None
            
            total = len(running.futures)
            completed = running.completed + running.failed
            elapsed = (datetime.now() - running.started_at).total_seconds() * 1000
            
            # Estimate remaining time
            estimated_remaining = None
            if completed > 0:
                avg_time = elapsed / completed
                remaining = total - completed
                estimated_remaining = avg_time * remaining
            
            return BatchRunnerProgress(
                batch_test_id=batch_test_id,
                total_variants=total,
                completed_variants=running.completed,
                failed_variants=running.failed,
                in_progress_variants=total - completed,
                elapsed_ms=elapsed,
                estimated_remaining_ms=estimated_remaining,
            )
    
    def cancel(self, batch_test_id: str) -> bool:
        """Cancel a running batch test."""
        with self._lock:
            running = self._running_tests.get(batch_test_id)
            if running is None:
                return False
            
            running.cancelled = True
            
            # Cancel pending futures
            for future in running.futures:
                future.cancel()
            
            return True
    
    def create_rollback_coordinator(
        self,
        config: Optional[Any] = None,
    ) -> "BatchExecutionCoordinator":
        """
        Create a BatchExecutionCoordinator that shares this runner's DB.

        Uses the stored WTBConfig so that rollback / fork operations
        connect to the same SQLite DB used during batch execution.
        """
        from wtb.application.factories import BatchCoordinatorFactory
        return BatchCoordinatorFactory.create_default(config=config or self._config)

    def shutdown(self) -> None:
        """Shutdown the executor."""
        self._shutdown = True
        
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None
        
        with self._lock:
            self._running_tests.clear()

