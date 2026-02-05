"""
Ray Batch Test Runner.

Distributed implementation of IBatchTestRunner using Ray ActorPool
for parallel variant execution across a cluster.

Refactored (v1.7):
- Uses ExecutionControllerFactory pattern for ACID-compliant isolated execution
- Each actor creates ExecutionController via factory (per v1.7 pattern)
- Fixed str ID references (session_id, checkpoint_id) per v1.6

Design Principles:
- SOLID: Single responsibility (Actor executes, Runner orchestrates)
- ACID: Results saved atomically, isolated actor state, events via outbox
- DIP: Depends on interfaces (IBatchTestRunner, IUnitOfWork)

Architecture:
- RayBatchTestRunner: Orchestrator that manages ActorPool and result aggregation
- VariantExecutionActor: Ray Actor that executes single variants with isolated state
- RayEventBridge: Integrates with WTB EventBus and Audit (transaction-consistent)
- ObjectRef tracking for cancellation support

ACID Compliance (v1.7):
- Atomicity: Each actor execution is atomic (UoW transaction)
- Consistency: Unified execution via ExecutionController
- Isolation: Each actor has isolated UoW + StateAdapter
- Durability: Results persisted via UoW.commit()

Usage:
    from wtb.application.services.ray_batch_runner import RayBatchTestRunner
    from wtb.config import RayConfig
    
    runner = RayBatchTestRunner(
        config=RayConfig.for_local_development(),
        agentgit_db_url="data/agentgit.db",
        wtb_db_url="sqlite:///data/wtb.db",
    )
    
    result = runner.run_batch_test(batch_test)
"""

from typing import Dict, Any, List, Optional, Callable, Tuple
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
import logging
import time
import uuid
import copy

from wtb.domain.models.batch_test import (
    BatchTest,
    BatchTestResult,
    BatchTestStatus,
    VariantCombination,
)
from wtb.domain.models.workflow import (
    TestWorkflow,
    Execution,
    ExecutionState,
    ExecutionStatus,
)
from wtb.domain.interfaces.batch_runner import (
    IBatchTestRunner,
    BatchRunnerStatus,
    BatchRunnerProgress,
    BatchRunnerError,
    BatchRunnerExecutionError,
)
from wtb.domain.interfaces.state_adapter import IStateAdapter
from wtb.domain.interfaces.unit_of_work import IUnitOfWork

# Workspace isolation imports (2026-01-16)
from wtb.domain.models.workspace import (
    Workspace,
    WorkspaceConfig,
    WorkspaceStrategy,
)
from wtb.infrastructure.workspace.manager import WorkspaceManager

# REUSE RayConfig from wtb.config - no duplication
from wtb.config import RayConfig

# Type hint imports (avoid circular imports)
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from wtb.infrastructure.events.ray_event_bridge import RayEventBridge
    from wtb.domain.interfaces.file_tracking import IFileTrackingService
    from wtb.application.services.batch_execution_coordinator import BatchExecutionCoordinator

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Variant Execution Result (Value Object)
# ═══════════════════════════════════════════════════════════════


@dataclass
class VariantExecutionResult:
    """
    Value object for variant execution result.
    
    Serializable across Ray workers.
    """
    execution_id: str
    combination_name: str
    combination_variants: Dict[str, str]
    success: bool
    duration_ms: int
    metrics: Dict[str, float] = field(default_factory=dict)
    error: Optional[str] = None
    checkpoint_count: int = 0
    node_count: int = 0
    
    def to_batch_test_result(self) -> BatchTestResult:
        """Convert to BatchTestResult domain model."""
        return BatchTestResult(
            combination_name=self.combination_name,
            execution_id=self.execution_id,
            success=self.success,
            duration_ms=self.duration_ms,
            metrics=self.metrics,
            overall_score=self.metrics.get("overall_score", 0.0),
            error_message=self.error,
        )


# ═══════════════════════════════════════════════════════════════
# Variant Execution Actor
# ═══════════════════════════════════════════════════════════════

# Check if Ray is available
RAY_AVAILABLE = False
ray = None

try:
    import ray as _ray
    ray = _ray
    RAY_AVAILABLE = True
except ImportError:
    pass


def _create_variant_execution_actor_class():
    """
    Create VariantExecutionActor class dynamically.
    
    This avoids import errors when Ray is not installed.
    """
    if not RAY_AVAILABLE:
        return None
    
    @ray.remote
    class VariantExecutionActor:
        """
        Ray Actor for executing workflow variants.
        
        Design Decisions:
        - Actor (vs Task): Reuses database connections across executions
        - Lazy initialization: Heavy deps initialized on first use
        - Isolated state: Each actor has independent UoW/StateAdapter
        
        SOLID Compliance:
        - SRP: Only responsible for executing single variant
        - DIP: Depends on factories for creating dependencies
        
        ACID Compliance:
        - Atomic: Results saved in single transaction
        - Isolated: Actor state isolated from other actors
        """
        
        def __init__(
            self,
            agentgit_db_url: str,
            wtb_db_url: str,
            actor_id: str,
            filetracker_config: Optional[Dict[str, Any]] = None,
            workspace_config: Optional[Dict[str, Any]] = None,
        ):
            """
            Initialize actor with database URLs and optional FileTracker/Workspace config.
            
            Args:
                agentgit_db_url: AgentGit database path
                wtb_db_url: WTB database URL
                actor_id: Unique identifier for this actor
                filetracker_config: Optional FileTrackingConfig.to_dict() for file tracking
                workspace_config: Optional WorkspaceConfig.to_dict() for workspace isolation
            """
            self._agentgit_db_url = agentgit_db_url
            self._wtb_db_url = wtb_db_url
            self._actor_id = actor_id
            self._filetracker_config = filetracker_config
            self._workspace_config = workspace_config
            
            # Lazy-initialized dependencies
            self._uow = None
            self._state_adapter = None
            self._execution_controller = None
            self._file_tracking_service = None
            self._workspace_manager: Optional[WorkspaceManager] = None
            self._initialized = False
            
            # Metrics tracking
            self._executions_run = 0
            self._executions_failed = 0
            self._files_tracked = 0
            
            logger.info(f"VariantExecutionActor {actor_id} created")
        
        def _ensure_initialized(self):
            """
            Lazy initialization of heavy dependencies.
            
            Called on first execution to avoid startup overhead.
            Initializes: StateAdapter, UoW factory, FileTracking service
            
            STATE ADAPTER PRIORITY (per Section 20.9 of ARCHITECTURE.md):
            1. LangGraphStateAdapter (PRIMARY) - production-proven, robust
            2. InMemoryStateAdapter - testing fallback
            3. AgentGitStateAdapter - DEFERRED (not used)
            """
            if self._initialized:
                return
            
            try:
                # Import dependencies inside actor to avoid serialization issues
                from wtb.infrastructure.database import UnitOfWorkFactory
                from wtb.infrastructure.adapters import InMemoryStateAdapter
                
                # PRIMARY: Try LangGraph adapter (per architecture decision 2026-01-15)
                try:
                    from wtb.infrastructure.adapters.langgraph_state_adapter import (
                        LangGraphStateAdapter,
                        LangGraphConfig,
                        CheckpointerType,
                    )
                    # Use SQLite checkpointer for Ray actors (per-actor isolation)
                    # thread_id will be set per execution for isolation
                    config = LangGraphConfig(
                        checkpointer_type=CheckpointerType.SQLITE,
                        connection_string=self._wtb_db_url.replace("sqlite:///", "") 
                            if self._wtb_db_url.startswith("sqlite:///")
                            else f"{self._wtb_db_url}_checkpoints.db",
                    )
                    self._state_adapter = LangGraphStateAdapter(config)
                    logger.info(f"Actor {self._actor_id}: Using LangGraphStateAdapter (PRIMARY)")
                except Exception as e:
                    # FALLBACK: InMemory for testing or when LangGraph unavailable
                    logger.warning(
                        f"Actor {self._actor_id}: LangGraph not available, "
                        f"using InMemory fallback: {e}"
                    )
                    self._state_adapter = InMemoryStateAdapter()
                
                # Create UoW factory for each execution
                self._uow_factory = lambda: UnitOfWorkFactory.create(
                    mode="sqlalchemy" if "://" in self._wtb_db_url else "inmemory",
                    db_url=self._wtb_db_url,
                )
                
                # Initialize FileTracking service if configured
                if self._filetracker_config and self._filetracker_config.get("enabled"):
                    try:
                        from wtb.infrastructure.file_tracking import RayFileTrackerService
                        self._file_tracking_service = RayFileTrackerService(
                            self._filetracker_config
                        )
                        logger.info(f"Actor {self._actor_id}: FileTracking enabled")
                    except Exception as e:
                        logger.warning(
                            f"Actor {self._actor_id}: FileTracking init failed: {e}"
                        )
                        self._file_tracking_service = None
                
                # Initialize WorkspaceManager if configured (2026-01-16)
                if self._workspace_config and self._workspace_config.get("enabled"):
                    try:
                        from pathlib import Path
                        ws_config = WorkspaceConfig(
                            enabled=self._workspace_config.get("enabled", True),
                            strategy=WorkspaceStrategy(
                                self._workspace_config.get("strategy", "workspace")
                            ),
                            base_dir=Path(self._workspace_config["base_dir"]) 
                                if self._workspace_config.get("base_dir") else None,
                            cleanup_on_complete=self._workspace_config.get("cleanup_on_complete", True),
                            preserve_on_failure=self._workspace_config.get("preserve_on_failure", True),
                            use_hard_links=self._workspace_config.get("use_hard_links", True),
                        )
                        self._workspace_manager = WorkspaceManager(
                            config=ws_config,
                            session_id=self._actor_id,
                        )
                        logger.info(f"Actor {self._actor_id}: WorkspaceManager enabled")
                    except Exception as e:
                        logger.warning(
                            f"Actor {self._actor_id}: WorkspaceManager init failed: {e}"
                        )
                        self._workspace_manager = None
                
                self._initialized = True
                logger.info(f"Actor {self._actor_id}: Initialized successfully")
                
            except Exception as e:
                logger.error(f"Actor {self._actor_id}: Initialization failed: {e}")
                raise
        
        def execute_variant(
            self,
            workflow_dict: Dict[str, Any],
            combination: Dict[str, Any],
            initial_state: Dict[str, Any],
            batch_test_id: str,
            workspace_data: Optional[Dict[str, Any]] = None,
        ) -> Dict[str, Any]:
            """
            Execute a single variant combination with optional workspace isolation.
            
            Args:
                workflow_dict: Serialized workflow definition
                combination: VariantCombination as dict (name, variants, metadata)
                initial_state: Initial state for execution
                batch_test_id: Parent batch test ID for tracking
                workspace_data: Optional workspace data dict for isolation (2026-01-16)
                
            Returns:
                VariantExecutionResult as dict (serializable)
                
            Workspace Isolation (2026-01-16):
            - If workspace_data is provided, activates workspace before execution
            - Files written go to isolated workspace directory
            - Workspace deactivated after execution (success or failure)
            """
            start_time = time.time()
            execution_id = str(uuid.uuid4())
            combination_name = combination.get("name", "unknown")
            variants = combination.get("variants", {})
            workspace: Optional[Workspace] = None
            
            logger.info(
                f"Actor {self._actor_id}: Starting execution {execution_id} "
                f"for variant '{combination_name}'"
            )
            
            try:
                self._ensure_initialized()
                
                # Load workspace if provided (2026-01-16)
                if workspace_data:
                    try:
                        from pathlib import Path
                        workspace = Workspace.from_dict(workspace_data)
                        workspace.activate()
                        logger.info(
                            f"Actor {self._actor_id}: Workspace {workspace.workspace_id} "
                            f"activated at {workspace.root_path}"
                        )
                    except Exception as ws_error:
                        logger.warning(
                            f"Actor {self._actor_id}: Failed to activate workspace: {ws_error}"
                        )
                        workspace = None
                
                # Execute workflow with variant applied
                result = self._run_workflow_execution(
                    workflow_dict=workflow_dict,
                    variants=variants,
                    initial_state=initial_state,
                    execution_id=execution_id,
                    batch_test_id=batch_test_id,
                    workspace=workspace,  # Pass workspace for output file writing
                )
                
                duration_ms = int((time.time() - start_time) * 1000)
                self._executions_run += 1
                
                return {
                    "execution_id": execution_id,
                    "combination_name": combination_name,
                    "combination_variants": variants,
                    "success": True,
                    "duration_ms": duration_ms,
                    "metrics": result.get("metrics", {}),
                    "error": None,
                    "checkpoint_count": result.get("checkpoint_count", 0),
                    "node_count": result.get("node_count", 0),
                    "workspace_id": workspace.workspace_id if workspace else None,
                }
                
            except Exception as e:
                duration_ms = int((time.time() - start_time) * 1000)
                self._executions_run += 1
                self._executions_failed += 1
                
                error_msg = str(e)
                logger.error(
                    f"Actor {self._actor_id}: Execution {execution_id} failed: {error_msg}"
                )
                
                return {
                    "execution_id": execution_id,
                    "combination_name": combination_name,
                    "combination_variants": variants,
                    "success": False,
                    "duration_ms": duration_ms,
                    "metrics": {},
                    "error": error_msg,
                    "checkpoint_count": 0,
                    "node_count": 0,
                    "workspace_id": workspace.workspace_id if workspace else None,
                }
                
            finally:
                # Always deactivate workspace (2026-01-16)
                if workspace and workspace.is_active:
                    try:
                        workspace.deactivate()
                        logger.info(
                            f"Actor {self._actor_id}: Workspace {workspace.workspace_id} deactivated"
                        )
                    except Exception as ws_error:
                        logger.warning(
                            f"Actor {self._actor_id}: Failed to deactivate workspace: {ws_error}"
                        )
        
        def _run_workflow_execution(
            self,
            workflow_dict: Dict[str, Any],
            variants: Dict[str, str],
            initial_state: Dict[str, Any],
            execution_id: str,
            batch_test_id: str,
            workspace: Optional[Workspace] = None,
        ) -> Dict[str, Any]:
            """
            Run workflow execution with variants applied.
            
            Refactored (v1.7): Uses ExecutionController pattern for ACID compliance.
            
            Args:
                workflow_dict: Serialized workflow definition
                variants: Node variant mapping
                initial_state: Initial workflow state
                execution_id: Unique execution ID
                batch_test_id: Parent batch test ID
                workspace: Optional workspace for output file isolation
            
            Returns:
                Dict with metrics, checkpoint_count, node_count, files_tracked, file_commit_id
                
            ACID Compliance:
            - Atomicity: Execution within UoW transaction
            - Consistency: Via ExecutionController
            - Isolation: Each call creates new UoW
            - Durability: UoW.commit() persists results
            """
            from wtb.application.services.execution_controller import (
                ExecutionController,
                DefaultNodeExecutor,
            )
            
            # Create isolated UoW for this execution (ACID: Isolation)
            uow = self._uow_factory()
            
            with uow:
                # Reconstruct workflow from dict
                workflow = self._reconstruct_workflow(workflow_dict, variants)
                
                # Store workflow if not exists
                existing = uow.workflows.get(workflow.id)
                if not existing:
                    uow.workflows.add(workflow)
                
                # v1.7: Create execution controller with factory pattern
                controller = ExecutionController(
                    execution_repository=uow.executions,
                    workflow_repository=uow.workflows,
                    state_adapter=self._state_adapter,
                    node_executor=DefaultNodeExecutor(),
                    unit_of_work=uow,  # v1.7: Pass UoW for transaction management
                )
                
                # v1.7: Add variant info to initial state (like ThreadPoolBatchTestRunner)
                variant_state = initial_state.copy()
                variant_state["_variant_config"] = variants
                variant_state["_batch_test_id"] = batch_test_id
                variant_state["_actor_id"] = self._actor_id
                
                # Create execution via controller (ACID: Atomicity)
                execution = controller.create_execution(
                    workflow=workflow,
                    initial_state=variant_state,
                )
                
                # Update execution ID to match expected value
                execution.id = execution_id
                execution.metadata = {
                    "batch_test_id": batch_test_id,
                    "variants": variants,
                    "actor_id": self._actor_id,
                }
                uow.executions.update(execution)
                uow.commit()
                
                # Run execution (ACID: via controller with UoW)
                try:
                    execution = controller.run(execution.id)
                except Exception as e:
                    logger.warning(f"Execution {execution_id} encountered error: {e}")
                    # Re-fetch to get latest state
                    execution = uow.executions.get(execution.id)
                    if execution and execution.status != ExecutionStatus.FAILED:
                        execution.fail(str(e))
                        uow.executions.update(execution)
                        uow.commit()
                    raise
                
                # Write _output_files from state to workspace (2026-01-17)
                # This bridges state data to actual files that FileTracker can track
                output_file_paths: List[str] = []
                if workspace and execution.state:
                    # Check workflow_variables for _output_files
                    output_files_data = execution.state.workflow_variables.get("_output_files", {})
                    if output_files_data and isinstance(output_files_data, dict):
                        try:
                            written_paths = workspace.write_output_files(output_files_data)
                            output_file_paths = [str(p) for p in written_paths]
                            logger.info(
                                f"Actor {self._actor_id}: Wrote {len(written_paths)} output files "
                                f"to workspace {workspace.workspace_id}"
                            )
                        except Exception as write_err:
                            logger.warning(
                                f"Actor {self._actor_id}: Failed to write output files: {write_err}"
                            )
                    
                    # Also check node_results for _output_files
                    for node_id, result in (execution.state.node_results or {}).items():
                        if isinstance(result, dict) and "_output_files" in result:
                            node_output_files = result["_output_files"]
                            if isinstance(node_output_files, dict):
                                try:
                                    written_paths = workspace.write_output_files(
                                        node_output_files, 
                                        subdirectory=node_id
                                    )
                                    output_file_paths.extend([str(p) for p in written_paths])
                                    logger.debug(
                                        f"Actor {self._actor_id}: Wrote {len(written_paths)} output files "
                                        f"for node {node_id}"
                                    )
                                except Exception as write_err:
                                    logger.warning(
                                        f"Actor {self._actor_id}: Failed to write output files "
                                        f"for node {node_id}: {write_err}"
                                    )
                
                # Track output files if FileTracker is configured
                files_tracked = 0
                file_commit_id = None
                if self._file_tracking_service:
                    try:
                        output_files = self._collect_output_files(
                            execution, 
                            workspace=workspace,
                            additional_paths=output_file_paths,
                        )
                        if output_files:
                            # v1.6: checkpoint_id is now str (was agentgit_checkpoint_id: int)
                            cp_id = execution.checkpoint_id or ""
                            tracking_result = self._file_tracking_service.track_and_link(
                                checkpoint_id=cp_id,
                                file_paths=output_files,
                                message=f"Execution {execution_id} outputs",
                            )
                            files_tracked = tracking_result.files_tracked
                            file_commit_id = tracking_result.commit_id
                            self._files_tracked += files_tracked
                            logger.info(
                                f"Actor {self._actor_id}: Tracked {files_tracked} files "
                                f"for execution {execution_id}"
                            )
                    except Exception as e:
                        logger.warning(f"File tracking failed: {e}")
                
                # Calculate metrics from execution results
                metrics = self._calculate_metrics(execution)
                
                # v1.6: checkpoint_id is now str, count checkpoints from history
                checkpoint_count = 0
                if hasattr(self._state_adapter, 'get_checkpoint_history'):
                    try:
                        history = self._state_adapter.get_checkpoint_history()
                        checkpoint_count = len(history) if history else 0
                    except Exception:
                        checkpoint_count = 1 if execution.checkpoint_id else 0
                
                return {
                    "metrics": metrics,
                    "checkpoint_count": checkpoint_count,
                    "node_count": len(execution.state.execution_path),
                    "files_tracked": files_tracked,
                    "file_commit_id": file_commit_id,
                }
        
        def _reconstruct_workflow(
            self,
            workflow_dict: Dict[str, Any],
            variants: Dict[str, str],
        ) -> TestWorkflow:
            """
            Reconstruct TestWorkflow from dict and apply variants.
            """
            # Deep copy to avoid mutation
            modified = copy.deepcopy(workflow_dict)
            
            # Apply variants to nodes
            if "nodes" in modified:
                for node_id, variant_id in variants.items():
                    if node_id in modified["nodes"]:
                        modified["nodes"][node_id]["variant_id"] = variant_id
            
            # Create workflow from dict
            return TestWorkflow.from_dict(modified)
        
        def _collect_output_files(
            self, 
            execution: Execution,
            workspace: Optional[Workspace] = None,
            additional_paths: Optional[List[str]] = None,
        ) -> List[str]:
            """
            Collect output files from execution for tracking.
            
            Examines:
            1. Workspace output directory (if workspace provided)
            2. Additional paths (files written from _output_files state)
            3. Node results for file paths
            4. Workflow variables for existing file paths
            
            Filters using FileTracker config patterns.
            
            Args:
                execution: Completed execution
                workspace: Optional workspace to collect output files from
                additional_paths: Additional file paths already written
                
            Returns:
                List of file paths to track
            """
            import os
            import fnmatch
            
            output_files: List[str] = []
            
            # Get tracking patterns from config
            auto_patterns = self._filetracker_config.get(
                "auto_track_patterns", ["*.csv", "*.pkl", "*.json", "*.parquet"]
            ) if self._filetracker_config else []
            excluded_patterns = self._filetracker_config.get(
                "excluded_patterns", ["*.tmp", "*.log"]
            ) if self._filetracker_config else []
            
            # 1. Collect from workspace output directory (2026-01-17)
            if workspace:
                try:
                    workspace_files = workspace.collect_output_file_paths()
                    output_files.extend([str(f) for f in workspace_files])
                except Exception as e:
                    logger.warning(f"Failed to collect workspace output files: {e}")
            
            # 2. Add additional paths (from _output_files state writing)
            if additional_paths:
                output_files.extend(additional_paths)
            
            # 3. Collect files from node results (legacy support)
            node_results = execution.state.node_results or {}
            for node_id, result in node_results.items():
                if isinstance(result, dict):
                    # Look for output_files in result (file paths, not content)
                    if "output_files" in result:
                        files = result["output_files"]
                        # Only add if it's a list of paths (strings), not dict (content)
                        if isinstance(files, list):
                            for f in files:
                                if isinstance(f, str) and os.path.isfile(f):
                                    output_files.append(f)
                        elif isinstance(files, str) and os.path.isfile(files):
                            output_files.append(files)
                    
                    # Look for common output keys
                    for key in ["output_path", "model_path", "data_path", "file_path"]:
                        if key in result and isinstance(result[key], str):
                            output_files.append(result[key])
            
            # 4. Collect files from workflow variables
            variables = execution.state.workflow_variables or {}
            for key, value in variables.items():
                if key != "_output_files" and isinstance(value, str) and os.path.isfile(value):
                    output_files.append(value)
            
            # Filter files: must exist, match patterns, not excluded
            filtered_files = []
            for path in output_files:
                if not os.path.isfile(path):
                    continue
                
                filename = os.path.basename(path)
                
                # Check excluded patterns
                excluded = any(
                    fnmatch.fnmatch(filename, pat) or fnmatch.fnmatch(path, pat)
                    for pat in excluded_patterns
                )
                if excluded:
                    continue
                
                # Check auto-track patterns (if any specified)
                if auto_patterns:
                    matched = any(
                        fnmatch.fnmatch(filename, pat)
                        for pat in auto_patterns
                    )
                    if not matched:
                        continue
                
                filtered_files.append(path)
            
            return list(set(filtered_files))  # Remove duplicates
        
        def _calculate_metrics(self, execution: Execution) -> Dict[str, float]:
            """
            Calculate evaluation metrics from execution results.
            """
            metrics = {
                "overall_score": 0.0,
                "latency_ms": 0.0,
                "node_count": float(len(execution.state.execution_path)),
            }
            
            # Calculate success rate based on node results
            node_results = execution.state.node_results or {}
            if node_results:
                successful_nodes = sum(
                    1 for r in node_results.values()
                    if isinstance(r, dict) and r.get("success", True)
                )
                metrics["success_rate"] = successful_nodes / len(node_results)
            
            # Overall score based on completion status
            if execution.status == ExecutionStatus.COMPLETED:
                metrics["overall_score"] = 0.8 + (metrics.get("success_rate", 0.5) * 0.2)
            elif execution.status == ExecutionStatus.FAILED:
                metrics["overall_score"] = 0.2
            else:
                metrics["overall_score"] = 0.5
            
            return metrics
        
        def health_check(self) -> Dict[str, Any]:
            """
            Health check for actor monitoring.
            """
            return {
                "actor_id": self._actor_id,
                "initialized": self._initialized,
                "executions_run": self._executions_run,
                "executions_failed": self._executions_failed,
                "files_tracked": self._files_tracked,
                "file_tracking_enabled": self._file_tracking_service is not None,
                "healthy": True,
            }
        
        def get_stats(self) -> Dict[str, Any]:
            """Get actor statistics."""
            return {
                "actor_id": self._actor_id,
                "executions_run": self._executions_run,
                "executions_failed": self._executions_failed,
                "files_tracked": self._files_tracked,
                "file_tracking_enabled": self._file_tracking_service is not None,
                "failure_rate": (
                    self._executions_failed / self._executions_run
                    if self._executions_run > 0 else 0.0
                ),
            }
    
    return VariantExecutionActor


# Create actor class if Ray is available
VariantExecutionActor = _create_variant_execution_actor_class()


# ═══════════════════════════════════════════════════════════════
# Running Test State
# ═══════════════════════════════════════════════════════════════


@dataclass
class _RayRunningTest:
    """Internal state for a running Ray batch test."""
    batch_test_id: str
    started_at: datetime
    total_variants: int
    pending_refs: List[Any] = field(default_factory=list)  # Ray ObjectRefs
    completed_results: List[Dict[str, Any]] = field(default_factory=list)
    completed: int = 0
    failed: int = 0
    cancelled: bool = False


# ═══════════════════════════════════════════════════════════════
# Ray Batch Test Runner
# ═══════════════════════════════════════════════════════════════


class RayBatchTestRunner(IBatchTestRunner):
    """
    Ray-based batch test runner for distributed execution.
    
    Design Principles:
    - SRP: Orchestrates batch tests, delegates execution to actors
    - OCP: Can extend with new actor types via factory
    - DIP: Depends on abstractions (IBatchTestRunner interface)
    
    Key Design Decisions:
    - ActorPool: Database connection reuse across executions
    - ObjectRef tracking: Enables cancellation and progress monitoring
    - Backpressure: ray.wait() with batch processing prevents OOM
    - Fault tolerance: Retries via actor restart
    
    ACID Compliance:
    - Atomic: Each variant result saved atomically
    - Consistent: Batch test state transitions are valid
    - Isolated: Actors have isolated state
    - Durable: Results persisted to database
    
    Usage:
        if RayBatchTestRunner.is_available():
            runner = RayBatchTestRunner(
                config=RayConfig.for_local_development(),
                agentgit_db_url="data/agentgit.db",
                wtb_db_url="sqlite:///data/wtb.db",
            )
            result = runner.run_batch_test(batch_test)
    """
    
    def __init__(
        self,
        config: RayConfig,
        agentgit_db_url: str,
        wtb_db_url: str,
        workflow_loader: Optional[Callable[[str, IUnitOfWork], TestWorkflow]] = None,
        filetracker_config: Optional[Dict[str, Any]] = None,
        workspace_config: Optional[Dict[str, Any]] = None,
        event_bridge: Optional["RayEventBridge"] = None,
        enable_audit: bool = True,
    ):
        """
        Initialize Ray runner with optional FileTracker, Workspace, and event bridge integration.
        
        Args:
            config: Ray configuration
            agentgit_db_url: AgentGit database URL/path
            wtb_db_url: WTB database URL
            workflow_loader: Optional custom workflow loader function
            filetracker_config: Optional FileTrackingConfig.to_dict() for file tracking
            workspace_config: Optional WorkspaceConfig dict for workspace isolation (2026-01-16)
            event_bridge: Optional RayEventBridge for event publishing (ACID compliant)
            enable_audit: Whether to create audit trails per batch test
        """
        if not RAY_AVAILABLE:
            raise BatchRunnerError(
                "Ray is not installed. Install with: pip install ray"
            )
        
        self._config = config
        self._agentgit_db_url = agentgit_db_url
        self._wtb_db_url = wtb_db_url
        self._workflow_loader = workflow_loader
        self._filetracker_config = filetracker_config
        self._workspace_config = workspace_config
        
        # Actor pool management
        self._actors: List[Any] = []  # Ray actor handles
        self._actor_pool = None
        
        # Running tests tracking
        self._running_tests: Dict[str, _RayRunningTest] = {}
        
        # Ray state
        self._ray_initialized = False
        
        # Workspace manager (2026-01-16)
        self._workspace_manager: Optional[WorkspaceManager] = None
        self._workspace_enabled = (
            workspace_config is not None and
            workspace_config.get("enabled", False)
        )
        
        # Track file tracking usage
        self._file_tracking_enabled = (
            filetracker_config is not None and 
            filetracker_config.get("enabled", False)
        )
        
        # Event bridge integration (2026-01-15)
        self._event_bridge = event_bridge
        self._enable_audit = enable_audit
        
        # Initialize event bridge if not provided
        if self._event_bridge is None:
            try:
                from wtb.infrastructure.events import (
                    get_wtb_event_bus,
                    RayEventBridge,
                    WTBAuditTrail,
                )
                from wtb.infrastructure.database import UnitOfWorkFactory
                
                # Create event bridge with outbox pattern
                self._event_bridge = RayEventBridge(
                    event_bus=get_wtb_event_bus(),
                    uow_factory=lambda: UnitOfWorkFactory.create(
                        mode="sqlalchemy" if "://" in self._wtb_db_url else "inmemory",
                        db_url=self._wtb_db_url,
                    ),
                    use_outbox=True,
                    audit_trail_factory=lambda: WTBAuditTrail() if enable_audit else None,
                )
                logger.info("RayBatchTestRunner: Event bridge initialized with outbox pattern")
            except Exception as e:
                logger.warning(f"RayBatchTestRunner: Event bridge init failed, running without events: {e}")
                self._event_bridge = None
        
        # Initialize WorkspaceManager if enabled (2026-01-16)
        if self._workspace_enabled:
            try:
                from pathlib import Path
                ws_config = WorkspaceConfig(
                    enabled=True,
                    strategy=WorkspaceStrategy(
                        self._workspace_config.get("strategy", "workspace")
                    ),
                    base_dir=Path(self._workspace_config["base_dir"]) 
                        if self._workspace_config.get("base_dir") else None,
                    cleanup_on_complete=self._workspace_config.get("cleanup_on_complete", True),
                    preserve_on_failure=self._workspace_config.get("preserve_on_failure", True),
                    use_hard_links=self._workspace_config.get("use_hard_links", True),
                )
                self._workspace_manager = WorkspaceManager(
                    config=ws_config,
                    event_bus=self._event_bridge.event_bus if self._event_bridge else None,
                    session_id=f"ray-batch-runner-{uuid.uuid4().hex[:8]}",
                )
                logger.info("RayBatchTestRunner: WorkspaceManager initialized")
            except Exception as e:
                logger.warning(f"RayBatchTestRunner: WorkspaceManager init failed: {e}")
                self._workspace_manager = None
                self._workspace_enabled = False
    
    @staticmethod
    def is_available() -> bool:
        """Check if Ray is available."""
        return RAY_AVAILABLE
    
    def _ensure_ray_initialized(self):
        """Initialize Ray if not already done."""
        if self._ray_initialized:
            return
        
        if not ray.is_initialized():
            init_kwargs = {}
            
            # Set address (ignore errors for "auto")
            if self._config.ray_address != "auto":
                init_kwargs["address"] = self._config.ray_address
            
            if self._config.runtime_env:
                init_kwargs["runtime_env"] = self._config.runtime_env
            
            if self._config.object_store_memory_gb:
                init_kwargs["object_store_memory"] = int(
                    self._config.object_store_memory_gb * 1024 * 1024 * 1024
                )
            
            try:
                ray.init(**init_kwargs)
                logger.info(f"Ray initialized with config: {init_kwargs}")
            except Exception as e:
                logger.warning(f"Ray init with args failed, trying local: {e}")
                ray.init()
        
        self._ray_initialized = True
    
    def _create_actor_pool(self, num_workers: int):
        """
        Create actor pool with specified number of workers.
        
        Args:
            num_workers: Number of actors to create
        """
        if self._actor_pool is not None:
            # Pool already exists
            return
        
        self._ensure_ray_initialized()
        
        file_tracking_status = "enabled" if self._file_tracking_enabled else "disabled"
        workspace_status = "enabled" if self._workspace_enabled else "disabled"
        logger.info(
            f"Creating actor pool with {num_workers} workers "
            f"(FileTracking: {file_tracking_status}, Workspace: {workspace_status})"
        )
        
        # Create actors with resource allocation and FileTracker/Workspace config
        self._actors = []
        for i in range(num_workers):
            actor = VariantExecutionActor.options(
                num_cpus=self._config.num_cpus_per_task,
                memory=int(self._config.memory_per_task_gb * 1024 * 1024 * 1024),
                max_restarts=self._config.max_retries,
            ).remote(
                agentgit_db_url=self._agentgit_db_url,
                wtb_db_url=self._wtb_db_url,
                actor_id=f"actor_{i}",
                filetracker_config=self._filetracker_config,
                workspace_config=self._workspace_config,  # Pass Workspace config (2026-01-16)
            )
            self._actors.append(actor)
        
        # Create ActorPool from actors
        self._actor_pool = ray.util.ActorPool(self._actors)
        
        logger.info(f"Actor pool created with {len(self._actors)} actors")
    
    def _load_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """
        Load workflow and serialize for Ray transmission.
        
        Args:
            workflow_id: Workflow ID to load
            
        Returns:
            Workflow as serializable dict
        """
        # Import here to avoid circular imports
        from wtb.infrastructure.database import UnitOfWorkFactory
        
        # Create UoW to load workflow
        uow = UnitOfWorkFactory.create(
            mode="sqlalchemy" if "://" in self._wtb_db_url else "inmemory",
            db_url=self._wtb_db_url,
        )
        
        with uow:
            if self._workflow_loader:
                workflow = self._workflow_loader(workflow_id, uow)
            else:
                workflow = uow.workflows.get(workflow_id)
            
            if workflow is None:
                # Create a default workflow for testing
                workflow = TestWorkflow(
                    id=workflow_id,
                    name=f"Workflow {workflow_id}",
                    description="Auto-generated workflow",
                    definition={
                        "nodes": {
                            "start": {"type": "start"},
                            "end": {"type": "end"},
                        },
                        "edges": [
                            {"source": "start", "target": "end"},
                        ],
                    },
                )
                logger.warning(
                    f"Workflow {workflow_id} not found, using default workflow"
                )
            
            return workflow.to_dict()
    
    def run_batch_test(self, batch_test: BatchTest) -> BatchTest:
        """
        Execute batch test with Ray parallelism.
        
        Flow:
        1. Initialize Ray and actor pool
        2. Emit batch test started event (via outbox for ACID)
        3. Put workflow and initial_state in object store
        4. Submit variants to actors with backpressure
        5. Collect results as they complete via ray.wait()
        6. Emit variant completed/failed events
        7. Build comparison matrix and finalize
        8. Emit batch test completed/failed event
        
        Args:
            batch_test: BatchTest to execute
            
        Returns:
            BatchTest with results populated
            
        Raises:
            BatchRunnerError: If execution fails
        """
        if not RAY_AVAILABLE:
            raise BatchRunnerError("Ray is not available")
        
        if not batch_test.variant_combinations:
            raise BatchRunnerError("No variant combinations to execute")
        
        # Start batch test
        batch_test.start()
        
        # Track running state
        running_test = _RayRunningTest(
            batch_test_id=batch_test.id,
            started_at=datetime.now(),
            total_variants=len(batch_test.variant_combinations),
        )
        self._running_tests[batch_test.id] = running_test
        
        try:
            # Determine parallelism
            num_workers = min(
                batch_test.parallel_count,
                len(batch_test.variant_combinations),
                self._config.max_pending_tasks,
            )
            
            # Create actor pool
            self._create_actor_pool(num_workers)
            
            # Emit batch test started event
            if self._event_bridge:
                self._event_bridge.on_batch_test_started(
                    batch_test_id=batch_test.id,
                    workflow_id=batch_test.workflow_id,
                    workflow_name=batch_test.name,
                    variant_count=len(batch_test.variant_combinations),
                    parallel_workers=num_workers,
                    max_pending_tasks=self._config.max_pending_tasks,
                    file_tracking_enabled=self._file_tracking_enabled,
                    config_snapshot={
                        "ray_address": self._config.ray_address,
                        "num_cpus_per_task": self._config.num_cpus_per_task,
                        "memory_per_task_gb": self._config.memory_per_task_gb,
                        "max_retries": self._config.max_retries,
                    },
                )
                
                # Emit actor pool created event
                self._event_bridge.on_actor_pool_created(
                    batch_test_id=batch_test.id,
                    num_actors=num_workers,
                    cpus_per_actor=self._config.num_cpus_per_task,
                    memory_per_actor_gb=self._config.memory_per_task_gb,
                    actor_ids=[f"actor_{i}" for i in range(num_workers)],
                )
            
            # Load and serialize workflow
            workflow_dict = self._load_workflow(batch_test.workflow_id)
            
            # Put immutable data in object store (zero-copy sharing)
            workflow_ref = ray.put(workflow_dict)
            initial_state_ref = ray.put(batch_test.initial_state)
            batch_test_id_ref = ray.put(batch_test.id)
            
            logger.info(
                f"Starting batch test {batch_test.id} with "
                f"{len(batch_test.variant_combinations)} variants"
            )
            
            # Submit all variants with backpressure
            pending_refs = []
            ref_to_combo: Dict[Any, VariantCombination] = {}
            ref_to_workspace: Dict[Any, Optional[Workspace]] = {}  # Track workspaces (2026-01-16)
            
            for combo in batch_test.variant_combinations:
                if running_test.cancelled:
                    break
                
                # Get next available actor from pool
                # Note: We use submit instead of map for better control
                combo_dict = combo.to_dict()
                execution_id = str(uuid.uuid4())
                
                # Create workspace for this variant if enabled (2026-01-16)
                workspace: Optional[Workspace] = None
                workspace_data: Optional[Dict[str, Any]] = None
                if self._workspace_manager:
                    try:
                        # Get source paths from filetracker config
                        source_paths = []
                        if self._filetracker_config and self._filetracker_config.get("tracked_paths"):
                            from pathlib import Path
                            source_paths = [
                                Path(p) for p in self._filetracker_config["tracked_paths"]
                            ]
                        
                        workspace = self._workspace_manager.create_workspace(
                            batch_id=batch_test.id,
                            variant_name=combo.name,
                            execution_id=execution_id,
                            source_paths=source_paths,
                        )
                        workspace_data = workspace.to_dict()
                        logger.debug(
                            f"Created workspace {workspace.workspace_id} for variant {combo.name}"
                        )
                    except Exception as ws_error:
                        logger.warning(f"Failed to create workspace for {combo.name}: {ws_error}")
                        workspace = None
                        workspace_data = None
                
                # Submit to pool
                actor = self._get_available_actor()
                ref = actor.execute_variant.remote(
                    workflow_dict=ray.get(workflow_ref),
                    combination=combo_dict,
                    initial_state=ray.get(initial_state_ref),
                    batch_test_id=ray.get(batch_test_id_ref),
                    workspace_data=workspace_data,  # Pass workspace (2026-01-16)
                )
                
                pending_refs.append(ref)
                ref_to_combo[ref] = combo
                ref_to_workspace[ref] = workspace  # Track workspace for cleanup
                running_test.pending_refs.append(ref)
                
                # Backpressure: Wait if too many pending
                if len(pending_refs) >= self._config.max_pending_tasks:
                    self._process_completed_refs(
                        pending_refs,
                        ref_to_combo,
                        batch_test,
                        running_test,
                        timeout=self._config.task_timeout_seconds,
                    )
            
            # Wait for remaining results
            while pending_refs and not running_test.cancelled:
                self._process_completed_refs(
                    pending_refs,
                    ref_to_combo,
                    batch_test,
                    running_test,
                    timeout=self._config.task_timeout_seconds,
                )
            
            # Finalize batch test
            if running_test.cancelled:
                batch_test.cancel()
                
                # Emit cancelled event
                if self._event_bridge:
                    self._event_bridge.on_batch_test_cancelled(
                        batch_test_id=batch_test.id,
                        workflow_id=batch_test.workflow_id,
                        reason="User cancelled",
                        cancelled_by="user",
                        variants_completed=running_test.completed,
                        variants_cancelled=len(running_test.pending_refs),
                    )
            elif running_test.failed == len(batch_test.variant_combinations):
                batch_test.fail("All variants failed")
                
                # Emit failed event
                if self._event_bridge:
                    self._event_bridge.on_batch_test_failed(
                        batch_test_id=batch_test.id,
                        workflow_id=batch_test.workflow_id,
                        error_type="AllVariantsFailed",
                        error_message="All variants failed",
                        variants_succeeded=running_test.completed,
                        variants_failed=running_test.failed,
                    )
            else:
                batch_test.complete()
                batch_test.build_comparison_matrix()
                
                # Calculate total files tracked
                total_files_tracked = sum(
                    r.get("files_tracked", 0) for r in running_test.completed_results
                )
                
                # Get best combination
                best_combo = None
                best_score = 0.0
                for r in running_test.completed_results:
                    score = r.get("metrics", {}).get("overall_score", 0.0)
                    if score > best_score:
                        best_score = score
                        best_combo = r.get("combination_name")
                
                # Emit completed event
                if self._event_bridge:
                    self._event_bridge.on_batch_test_completed(
                        batch_test_id=batch_test.id,
                        workflow_id=batch_test.workflow_id,
                        variants_succeeded=running_test.completed,
                        variants_failed=running_test.failed,
                        best_combination_name=best_combo,
                        best_overall_score=best_score,
                        total_files_tracked=total_files_tracked,
                        has_comparison_matrix=True,
                    )
            
            logger.info(
                f"Batch test {batch_test.id} completed: "
                f"{running_test.completed} succeeded, {running_test.failed} failed"
            )
            
            return batch_test
            
        except Exception as e:
            logger.error(f"Ray batch test {batch_test.id} failed: {e}")
            batch_test.fail(str(e))
            
            # Emit failed event
            if self._event_bridge:
                self._event_bridge.on_batch_test_failed(
                    batch_test_id=batch_test.id,
                    workflow_id=batch_test.workflow_id,
                    error_type=type(e).__name__,
                    error_message=str(e),
                    variants_succeeded=running_test.completed,
                    variants_failed=running_test.failed,
                    variants_pending=len(running_test.pending_refs),
                )
            
            raise BatchRunnerError(f"Batch test failed: {e}") from e
            
        finally:
            # Cleanup event bridge resources
            if self._event_bridge:
                self._event_bridge.cleanup_batch(batch_test.id)
            self._running_tests.pop(batch_test.id, None)
            
            # Cleanup workspaces for this batch (2026-01-16)
            if self._workspace_manager:
                try:
                    cleaned = self._workspace_manager.cleanup_batch(
                        batch_id=batch_test.id,
                        reason="batch_complete",
                    )
                    if cleaned > 0:
                        logger.info(f"Cleaned up {cleaned} workspaces for batch {batch_test.id}")
                except Exception as ws_error:
                    logger.warning(f"Workspace cleanup failed for batch {batch_test.id}: {ws_error}")
    
    def _get_available_actor(self) -> Any:
        """Get an available actor from the pool (round-robin)."""
        if not self._actors:
            raise BatchRunnerError("No actors available")
        
        # Simple round-robin selection
        actor = self._actors[0]
        self._actors = self._actors[1:] + [self._actors[0]]
        return actor
    
    def _process_completed_refs(
        self,
        pending_refs: List[Any],
        ref_to_combo: Dict[Any, VariantCombination],
        batch_test: BatchTest,
        running_test: _RayRunningTest,
        timeout: float,
    ):
        """
        Process completed ObjectRefs and add results to batch test.
        
        Args:
            pending_refs: List of pending ObjectRefs (modified in place)
            ref_to_combo: Mapping from ObjectRef to VariantCombination
            batch_test: BatchTest to add results to
            running_test: Running test state
            timeout: Timeout for ray.wait()
        """
        if not pending_refs:
            return
        
        # Wait for at least one result
        ready_refs, remaining_refs = ray.wait(
            pending_refs,
            num_returns=min(len(pending_refs), 1),
            timeout=timeout,
        )
        
        # Update pending_refs in place
        pending_refs.clear()
        pending_refs.extend(remaining_refs)
        
        # Process completed results
        for ref in ready_refs:
            combo = ref_to_combo.get(ref)
            
            try:
                result_dict = ray.get(ref)
                
                # Convert to BatchTestResult
                # v1.8: Include rollback support fields (file_commit_id, checkpoint_count, last_checkpoint_id)
                result = BatchTestResult(
                    combination_name=result_dict.get("combination_name", "unknown"),
                    execution_id=result_dict.get("execution_id", ""),
                    success=result_dict.get("success", False),
                    duration_ms=result_dict.get("duration_ms", 0),
                    metrics=result_dict.get("metrics", {}),
                    overall_score=result_dict.get("metrics", {}).get("overall_score", 0.0),
                    error_message=result_dict.get("error"),
                    # v1.8: Rollback support fields - preserve from actor result
                    file_commit_id=result_dict.get("file_commit_id"),
                    checkpoint_count=result_dict.get("checkpoint_count", 0),
                    last_checkpoint_id=result_dict.get("last_checkpoint_id"),
                )
                
                batch_test.add_result(result)
                running_test.completed_results.append(result_dict)
                
                if result.success:
                    running_test.completed += 1
                    
                    # Emit variant completed event
                    if self._event_bridge:
                        self._event_bridge.on_variant_execution_completed(
                            execution_id=result_dict.get("execution_id", ""),
                            batch_test_id=batch_test.id,
                            actor_id=result_dict.get("actor_id", ""),
                            combination_name=result_dict.get("combination_name", ""),
                            variants=result_dict.get("combination_variants", {}),
                            duration_ms=result_dict.get("duration_ms", 0),
                            checkpoint_count=result_dict.get("checkpoint_count", 0),
                            node_count=result_dict.get("node_count", 0),
                            metrics=result_dict.get("metrics", {}),
                            overall_score=result_dict.get("metrics", {}).get("overall_score", 0.0),
                            files_tracked=result_dict.get("files_tracked", 0),
                            file_commit_id=result_dict.get("file_commit_id"),
                        )
                else:
                    running_test.failed += 1
                    
                    # Emit variant failed event
                    if self._event_bridge:
                        self._event_bridge.on_variant_execution_failed(
                            execution_id=result_dict.get("execution_id", ""),
                            batch_test_id=batch_test.id,
                            actor_id=result_dict.get("actor_id", ""),
                            combination_name=result_dict.get("combination_name", ""),
                            variants=result_dict.get("combination_variants", {}),
                            error_type="ExecutionError",
                            error_message=result_dict.get("error", "Unknown error"),
                            duration_ms=result_dict.get("duration_ms", 0),
                            nodes_completed=result_dict.get("node_count", 0),
                            checkpoints_created=result_dict.get("checkpoint_count", 0),
                        )
                    
            except ray.exceptions.RayTaskError as e:
                # Actor task raised an exception
                logger.error(f"Ray task error for {combo.name if combo else 'unknown'}: {e}")
                
                error_result = BatchTestResult(
                    combination_name=combo.name if combo else "unknown",
                    execution_id="",
                    success=False,
                    error_message=f"Ray task error: {e}",
                )
                batch_test.add_result(error_result)
                running_test.failed += 1
                
                # Emit variant failed event
                if self._event_bridge:
                    self._event_bridge.on_variant_execution_failed(
                        execution_id="",
                        batch_test_id=batch_test.id,
                        actor_id="",
                        combination_name=combo.name if combo else "unknown",
                        variants=combo.variants if combo else {},
                        error_type="RayTaskError",
                        error_message=str(e),
                    )
                
            except ray.exceptions.GetTimeoutError:
                # Task timed out
                logger.error(f"Ray task timeout for {combo.name if combo else 'unknown'}")
                
                error_result = BatchTestResult(
                    combination_name=combo.name if combo else "unknown",
                    execution_id="",
                    success=False,
                    error_message="Task timed out",
                )
                batch_test.add_result(error_result)
                running_test.failed += 1
                
                # Emit variant failed event
                if self._event_bridge:
                    self._event_bridge.on_variant_execution_failed(
                        execution_id="",
                        batch_test_id=batch_test.id,
                        actor_id="",
                        combination_name=combo.name if combo else "unknown",
                        variants=combo.variants if combo else {},
                        error_type="TimeoutError",
                        error_message="Task timed out",
                    )
            
            # Remove from pending_refs tracking
            if ref in running_test.pending_refs:
                running_test.pending_refs.remove(ref)
    
    def get_status(self, batch_test_id: str) -> BatchRunnerStatus:
        """Get status of a batch test."""
        if batch_test_id in self._running_tests:
            running = self._running_tests[batch_test_id]
            if running.cancelled:
                return BatchRunnerStatus.CANCELLING
            return BatchRunnerStatus.RUNNING
        return BatchRunnerStatus.IDLE
    
    def get_progress(self, batch_test_id: str) -> Optional[BatchRunnerProgress]:
        """Get progress for a running batch test."""
        running = self._running_tests.get(batch_test_id)
        if running is None:
            return None
        
        completed = running.completed + running.failed
        elapsed = (datetime.now() - running.started_at).total_seconds() * 1000
        
        # Estimate remaining time
        estimated_remaining = None
        if completed > 0:
            avg_time = elapsed / completed
            remaining = running.total_variants - completed
            estimated_remaining = avg_time * remaining
        
        return BatchRunnerProgress(
            batch_test_id=batch_test_id,
            total_variants=running.total_variants,
            completed_variants=running.completed,
            failed_variants=running.failed,
            in_progress_variants=len(running.pending_refs),
            elapsed_ms=elapsed,
            estimated_remaining_ms=estimated_remaining,
        )
    
    def cancel(self, batch_test_id: str) -> bool:
        """
        Cancel a running batch test.
        
        Args:
            batch_test_id: ID of the batch test to cancel
            
        Returns:
            True if cancellation was initiated
        """
        running = self._running_tests.get(batch_test_id)
        if running is None:
            return False
        
        running.cancelled = True
        
        # Cancel pending Ray tasks
        cancelled_count = 0
        for ref in running.pending_refs:
            try:
                ray.cancel(ref, force=True)
                cancelled_count += 1
            except Exception as e:
                logger.warning(f"Failed to cancel Ray task: {e}")
        
        logger.info(f"Batch test {batch_test_id} cancellation initiated")
        return True
    
    @property
    def event_bridge(self) -> Optional["RayEventBridge"]:
        """Get the event bridge for this runner."""
        return self._event_bridge
    
    def get_batch_audit_trail(self, batch_test_id: str) -> Optional[Any]:
        """
        Get the audit trail for a completed or running batch test.
        
        Args:
            batch_test_id: Batch test ID
            
        Returns:
            WTBAuditTrail or None
        """
        if self._event_bridge:
            return self._event_bridge.get_batch_audit_trail(batch_test_id)
        return None
    
    def shutdown(self) -> None:
        """
        Shutdown Ray resources.
        
        Kills all actors and cleans up state.
        """
        logger.info("Shutting down RayBatchTestRunner")
        
        # Cancel any running tests
        for batch_test_id in list(self._running_tests.keys()):
            self.cancel(batch_test_id)
        
        # Kill actors
        for actor in self._actors:
            try:
                ray.kill(actor)
            except Exception as e:
                logger.warning(f"Failed to kill actor: {e}")
        
        self._actors.clear()
        self._actor_pool = None
        self._running_tests.clear()
        
        # Note: We don't call ray.shutdown() as other code might be using Ray
        logger.info("RayBatchTestRunner shutdown complete")
    
    def get_actor_stats(self) -> List[Dict[str, Any]]:
        """
        Get statistics from all actors.
        
        Returns:
            List of actor statistics dicts
        """
        if not self._actors:
            return []
        
        stats = []
        for actor in self._actors:
            try:
                stat_ref = actor.get_stats.remote()
                stat = ray.get(stat_ref, timeout=5.0)
                stats.append(stat)
            except Exception as e:
                logger.warning(f"Failed to get actor stats: {e}")
        
        return stats
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Rollback Coordinator Factory (v1.8)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def create_rollback_coordinator(self) -> "BatchExecutionCoordinator":
        """
        Create BatchExecutionCoordinator reusing this runner's configuration.
        
        v1.8: Factory method for creating coordinator that shares configuration
        with the Ray batch runner for consistent rollback/fork operations.
        
        Usage:
            runner = RayBatchTestRunner(config, ...)
            result = runner.run_batch_test(batch_test)
            
            # After batch completes, rollback specific variants
            coordinator = runner.create_rollback_coordinator()
            coordinator.rollback(
                execution_id=result.results[0].execution_id,
                checkpoint_id=result.results[0].last_checkpoint_id,
            )
            
            # Or fork for A/B exploration
            forked = coordinator.fork(
                execution_id=result.results[0].execution_id,
                checkpoint_id=result.results[0].last_checkpoint_id,
                new_state={"temperature": 0.7},
            )
        
        Returns:
            BatchExecutionCoordinator configured with runner's dependencies
        """
        from wtb.application.services.batch_execution_coordinator import (
            BatchExecutionCoordinator,
            DefaultExecutionControllerFactory,
        )
        
        return BatchExecutionCoordinator(
            uow_factory=self._create_uow,
            controller_factory=DefaultExecutionControllerFactory(),
            state_adapter=self._create_shared_state_adapter(),
            file_tracking=self._create_file_tracking_service(),
        )
    
    def _create_shared_state_adapter(self) -> Optional["IStateAdapter"]:
        """
        Create StateAdapter with same config as actors.
        
        v1.8: Used by create_rollback_coordinator() for consistent state access.
        
        Returns:
            IStateAdapter instance or None
        """
        try:
            from wtb.infrastructure.adapters.langgraph_state_adapter import (
                LangGraphStateAdapter,
                LangGraphConfig,
                LANGGRAPH_AVAILABLE,
            )
            
            if LANGGRAPH_AVAILABLE:
                return LangGraphStateAdapter(LangGraphConfig.for_development())
            
        except ImportError:
            pass
        
        # Fallback to InMemory
        from wtb.infrastructure.adapters import InMemoryStateAdapter
        return InMemoryStateAdapter()
    
    def _create_file_tracking_service(self) -> Optional["IFileTrackingService"]:
        """
        Create FileTrackingService if configured.
        
        v1.8: Used by create_rollback_coordinator() for file restore operations.
        
        Returns:
            IFileTrackingService instance or None if not configured
        """
        if not self._file_tracking_enabled or not self._filetracker_config:
            return None
        
        try:
            from wtb.infrastructure.file_tracking import FileTrackerService
            from wtb.infrastructure.file_tracking.config import FileTrackingConfig
            
            config = FileTrackingConfig.from_dict(self._filetracker_config)
            return FileTrackerService(config)
        except Exception as e:
            logger.warning(f"Failed to create file tracking service: {e}")
            return None
    
    def _create_uow(self) -> "IUnitOfWork":
        """
        Create UnitOfWork instance.
        
        v1.8: Used by create_rollback_coordinator() for transaction management.
        
        Returns:
            IUnitOfWork instance
        """
        from wtb.infrastructure.database import UnitOfWorkFactory
        
        return UnitOfWorkFactory.create(
            mode="sqlalchemy" if "://" in self._wtb_db_url else "inmemory",
            db_url=self._wtb_db_url,
        )
