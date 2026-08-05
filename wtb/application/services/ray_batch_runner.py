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

from typing import Dict, Any, List, Optional, Callable
from datetime import datetime
from dataclasses import dataclass, field
import base64
import logging
import threading
import time
import uuid
import copy

from wtb.domain.models.batch_test import (
    BatchTest,
    BatchTestStatus,
    BatchTestResult,
    VariantCombination,
)
from wtb.domain.models.workflow import (
    TestWorkflow,
    WorkflowNode,
    WorkflowEdge,
    Execution,
    ExecutionStatus,
)
from wtb.domain.interfaces.batch_runner import (
    IBatchTestRunner,
    BatchRunnerStatus,
    BatchRunnerProgress,
    BatchRunnerError,
)
from wtb.domain.interfaces.state_adapter import IStateAdapter
from wtb.domain.interfaces.unit_of_work import IUnitOfWork
from wtb.application.services.external_storage import (
    ActorLocalStoragePaths,
    resolve_actor_local_storage_paths,
)

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
    from wtb.config import WTBConfig
    from wtb.domain.interfaces.batch_runner import IEnvironmentProvider

logger = logging.getLogger(__name__)

def _decode_pickled_payload(payload: Any) -> Any:
    """Decode public metadata payloads while preserving legacy bytes."""
    if isinstance(payload, str):
        return base64.b64decode(payload.encode("ascii"), validate=True)
    return payload


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
            self._storage_paths: ActorLocalStoragePaths = resolve_actor_local_storage_paths(
                actor_id
            )
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
                    config = LangGraphConfig(
                        checkpointer_type=CheckpointerType.SQLITE,
                        connection_string=str(self._storage_paths.checkpoint_db_path),
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
            graph_factory_pickled: Optional[Any] = None,
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
            # v1.8: Extract graph factory reference for LangGraph execution with checkpoints
            graph_factory_module = combination.get("graph_factory_module")
            graph_factory_name = combination.get("graph_factory_name")
            graph_pickled = (combination.get("metadata") or {}).get(
                "_graph_pickled"
            )
            workspace: Optional[Workspace] = None
            
            logger.info(
                f"Actor {self._actor_id}: Starting execution {execution_id} "
                f"for variant '{combination_name}'"
                + (f" (graph: {graph_factory_module}.{graph_factory_name})" 
                   if graph_factory_module else " (no graph factory)")
            )
            
            try:
                self._ensure_initialized()
                
                # Load workspace if provided (2026-01-16)
                if workspace_data:
                    try:
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
                # v1.8: Pass graph factory ref for LangGraph execution with checkpoints
                result = self._run_workflow_execution(
                    workflow_dict=workflow_dict,
                    variants=variants,
                    initial_state=initial_state,
                    execution_id=execution_id,
                    batch_test_id=batch_test_id,
                    workspace=workspace,  # Pass workspace for output file writing
                    graph_factory_module=graph_factory_module,
                    graph_factory_name=graph_factory_name,
                    graph_factory_pickled=graph_factory_pickled,
                    graph_pickled=graph_pickled,
                )
                
                duration_ms = int((time.time() - start_time) * 1000)
                self._executions_run += 1
                
                actual_exec_id = result.get("execution_id", execution_id)
                
                return {
                    "execution_id": actual_exec_id,
                    "combination_name": combination_name,
                    "combination_variants": variants,
                    "actor_id": self._actor_id,
                    "checkpoint_db_path": str(self._storage_paths.checkpoint_db_path),
                    "llm_cache_path": str(self._storage_paths.llm_cache_path),
                    "cache_storage_scope": self._storage_paths.cache_storage_scope,
                    "success": True,
                    "duration_ms": duration_ms,
                    "metrics": result.get("metrics", {}),
                    "error": None,
                    "checkpoint_count": result.get("checkpoint_count", 0),
                    "last_checkpoint_id": result.get("last_checkpoint_id"),
                    "node_count": result.get("node_count", 0),
                    "workspace_id": workspace.workspace_id if workspace else None,
                    "file_commit_id": result.get("file_commit_id"),
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
                    "actor_id": self._actor_id,
                    "checkpoint_db_path": str(self._storage_paths.checkpoint_db_path),
                    "llm_cache_path": str(self._storage_paths.llm_cache_path),
                    "cache_storage_scope": self._storage_paths.cache_storage_scope,
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
            graph_factory_module: Optional[str] = None,
            graph_factory_name: Optional[str] = None,
            graph_factory_pickled: Optional[Any] = None,
            graph_pickled: Optional[Any] = None,
        ) -> Dict[str, Any]:
            """
            Run workflow execution with variants applied.
            
            Refactored (v1.7): Uses ExecutionController pattern for ACID compliance.
            v1.8: Added graph factory support for LangGraph execution with checkpoints.
            
            Args:
                workflow_dict: Serialized workflow definition
                variants: Node variant mapping
                initial_state: Initial workflow state
                execution_id: Unique execution ID
                batch_test_id: Parent batch test ID
                workspace: Optional workspace for output file isolation
                graph_factory_module: Optional module containing graph factory (v1.8)
                graph_factory_name: Optional name of graph factory function (v1.8)
                graph_factory_pickled: Optional cloudpickle bytes of the factory
                    function, used as fallback when module import fails (e.g.
                    factory defined in __main__).
            
            Returns:
                Dict with metrics, checkpoint_count, last_checkpoint_id, node_count, 
                files_tracked, file_commit_id
                
            ACID Compliance:
            - Atomicity: Execution within UoW transaction
            - Consistency: Via ExecutionController
            - Isolation: Each call creates new UoW
            - Durability: UoW.commit() persists results
            
            Checkpoint Support (v1.8):
            - If graph_factory_module and graph_factory_name are provided,
              creates LangGraph graph and passes to controller.run(graph=graph)
            - This enables LangGraph native execution with automatic checkpointing
            """
            from wtb.application.services.execution_controller import (
                ExecutionController,
                DefaultNodeExecutor,
            )
            from pathlib import Path
            
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
                controller_output_dir = None
                if self._file_tracking_service and self._filetracker_config:
                    controller_output_dir = str(
                        Path(self._filetracker_config["storage_path"]) / "outputs"
                    )

                controller = ExecutionController(
                    execution_repository=uow.executions,
                    workflow_repository=uow.workflows,
                    state_adapter=self._state_adapter,
                    node_executor=DefaultNodeExecutor(),
                    unit_of_work=uow,  # v1.7: Pass UoW for transaction management
                    file_tracking_service=self._file_tracking_service,
                    output_dir=controller_output_dir,
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
                
                # Use the DB-assigned ID; store the pre-generated one as alias
                execution.metadata = {
                    **(execution.metadata or {}),
                    "batch_test_id": batch_test_id,
                    "variants": variants,
                    "actor_id": self._actor_id,
                    "requested_execution_id": execution_id,
                    "checkpoint_db_path": str(self._storage_paths.checkpoint_db_path),
                    "llm_cache_path": str(self._storage_paths.llm_cache_path),
                    "cache_storage_scope": self._storage_paths.cache_storage_scope,
                }
                # Update the execution_id variable to match the real persisted ID
                execution_id = execution.id
                uow.executions.update(execution)
                uow.commit()
                
                # v1.8: Create LangGraph graph from factory if available
                # This enables automatic checkpointing at each super-step
                langgraph_graph = None
                if graph_pickled:
                    try:
                        try:
                            import cloudpickle
                        except ImportError:
                            from ray import cloudpickle
                        langgraph_graph = cloudpickle.loads(
                            _decode_pickled_payload(graph_pickled)
                        )
                        logger.info(
                            "Actor %s: Loaded serialized registered-variant graph",
                            self._actor_id,
                        )
                    except Exception as graph_error:
                        raise RuntimeError(
                            "Failed to load serialized registered-variant graph: "
                            f"{graph_error}"
                        ) from graph_error
                elif graph_factory_module and graph_factory_name:
                    try:
                        from wtb.application.services.graph_loader import load_graph_factory
                        factory = load_graph_factory(graph_factory_module, graph_factory_name)
                        langgraph_graph = factory()
                        logger.info(
                            f"Actor {self._actor_id}: Created LangGraph graph from "
                            f"{graph_factory_module}.{graph_factory_name}"
                        )
                    except Exception as graph_err:
                        if graph_factory_pickled:
                            try:
                                import cloudpickle
                                factory = cloudpickle.loads(
                                    _decode_pickled_payload(graph_factory_pickled)
                                )
                                langgraph_graph = factory()
                                logger.info(
                                    f"Actor {self._actor_id}: Created LangGraph graph "
                                    f"from cloudpickle fallback"
                                )
                            except Exception as pkl_err:
                                logger.warning(
                                    f"Actor {self._actor_id}: cloudpickle fallback also "
                                    f"failed: {pkl_err}. No checkpoints will be created."
                                )
                        else:
                            logger.warning(
                                f"Actor {self._actor_id}: Failed to create graph from "
                                f"factory {graph_factory_module}.{graph_factory_name}: "
                                f"{graph_err}. "
                                f"Falling back to legacy execution (no checkpoints)."
                            )
                
                # Run execution (ACID: via controller with UoW)
                # v1.8: Pass graph for LangGraph execution with checkpoints
                try:
                    execution = controller.run(execution.id, graph=langgraph_graph)
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
                if self._file_tracking_service and controller_output_dir:
                    file_commit_id = self._file_tracking_service.get_commit_for_checkpoint(
                        execution.checkpoint_id or ""
                    )
                    files_tracked = 1 if file_commit_id else 0
                elif self._file_tracking_service:
                    try:
                        output_files = self._collect_output_files(
                            execution, 
                            workspace=workspace,
                            additional_paths=output_file_paths,
                        )
                        if output_files:
                            # v1.6: checkpoint_id is now str (legacy agent field was int)
                            cp_id = execution.checkpoint_id or ""
                            if not cp_id:
                                raise RuntimeError(
                                    "Execution produced output files but has no checkpoint_id "
                                    "for CAS file linking"
                                )
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
                        logger.error(f"File tracking failed: {e}")
                        raise
                
                # Calculate metrics from execution results
                metrics = self._calculate_metrics(execution)
                
                # v1.6: checkpoint_id is now str, count checkpoints from history
                # v1.8: Also extract last_checkpoint_id for rollback support
                checkpoint_count = 0
                last_checkpoint_id = execution.checkpoint_id  # Fallback to execution's checkpoint
                if hasattr(self._state_adapter, 'get_checkpoint_history'):
                    try:
                        history = self._state_adapter.get_checkpoint_history()
                        if history:
                            checkpoint_count = len(history)
                            # Get the most recent checkpoint (first in history, sorted desc)
                            last_checkpoint_id = history[0].get("checkpoint_id", last_checkpoint_id)
                    except Exception as e:
                        logger.debug(f"Failed to get checkpoint history: {e}")
                        checkpoint_count = 1 if execution.checkpoint_id else 0
                
                return {
                    "execution_id": execution_id,
                    "metrics": metrics,
                    "checkpoint_count": checkpoint_count,
                    "last_checkpoint_id": last_checkpoint_id,
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
    cancelled_variants: int = 0
    termination_confirmed: bool = False
    termination_error: Optional[str] = None
    terminal_owner: Optional[str] = None
    cancellation_in_progress: bool = False
    actor_termination_confirmed: bool = False
    cancellation_finished: threading.Event = field(
        default_factory=threading.Event,
        repr=False,
    )
    finished_event: threading.Event = field(
        default_factory=threading.Event,
        repr=False,
    )


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
        environment_provider: Optional["IEnvironmentProvider"] = None,
        owns_environment_provider: bool = False,
    ):
        """
        Initialize Ray runner with optional FileTracker, Workspace, and event bridge integration.
        
        Args:
            config: Ray configuration
            agentgit_db_url: AgentGit database URL/path
            owns_environment_provider: Close the injected provider on shutdown.
                                        Injected providers are borrowed by default.
            wtb_db_url: WTB database URL
            workflow_loader: Optional custom workflow loader function
            filetracker_config: Optional FileTrackingConfig.to_dict() for file tracking
            workspace_config: Optional WorkspaceConfig dict for workspace isolation (2026-01-16)
            event_bridge: Optional RayEventBridge for event publishing (ACID compliant)
            enable_audit: Whether to create audit trails per batch test
            environment_provider: Optional IEnvironmentProvider for UV venv provisioning
                                  via gRPC Docker service (GrpcEnvironmentProvider)
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
        # A failed actor termination makes resource cleanup unsafe. Preserve
        # handles until shutdown can retry instead of pretending they stopped.
        self._poisoned = False
        self._pending_event_cleanup_ids: set[str] = set()
        self._unsafe_batch_ids: set[str] = set()
        self._orphaned_refs: List[Any] = []

        # One runner owns one shared actor pool. Batch registration and actor
        # lifecycle operations use separate locks so cancellation can safely
        # coordinate with pool creation and task submission.
        self._running_tests: Dict[str, _RayRunningTest] = {}
        self._running_tests_lock = threading.Lock()
        self._actor_pool_lock = threading.RLock()
        self._shutdown_lock = threading.Lock()
        self._provider_close_lock = threading.Lock()
        self._provider_close_event: Optional[threading.Event] = None
        self._provider_close_error: Optional[BaseException] = None
        self._provider_closed = False
        # One total budget covers cancellation, actor termination, run
        # finalization, environment cleanup, and provider close.
        self._shutdown_timeout_seconds = 5.0
        self._shutting_down = False
        self._closed = False
        
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
                    audit_trail_factory=(lambda: WTBAuditTrail()) if enable_audit else None,
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
        
        # UV Venv environment provider (gRPC Docker service)
        self._environment_provider = environment_provider
        self._owns_environment_provider = bool(
            environment_provider is not None and owns_environment_provider
        )
        self._environment_namespace = f"ray-{uuid.uuid4().hex[:12]}"
        self._provisioned_env_ids: List[str] = []
    
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
            except Exception as init_error:
                configured_target = init_kwargs.get("address", "local")
                raise BatchRunnerError(
                    "Failed to initialize configured Ray runtime "
                    f"at {configured_target}: {init_error}"
                ) from init_error
        
        self._ray_initialized = True
    
    def _create_actor_pool(self, num_workers: int):
        """
        Create actor pool with specified number of workers.
        
        When an ``IEnvironmentProvider`` (e.g. ``GrpcEnvironmentProvider``)
        is configured, each actor provisions an isolated UV venv through
        the Docker gRPC service and receives a per-actor ``runtime_env``.
        
        Args:
            num_workers: Number of actors to create
        """
        if self._actor_pool is not None:
            if len(self._actors) == num_workers:
                return

            # Sequential batches may request different parallelism. The pool
            # is idle here because run_batch_test permits one owner.
            failed_actors = []
            resize_errors = []
            for actor in list(self._actors):
                try:
                    ray.kill(actor, no_restart=True)
                except Exception as resize_error:
                    failed_actors.append(actor)
                    resize_errors.append(f"actor termination: {resize_error}")

            self._actors = failed_actors
            self._actor_pool = None
            if failed_actors:
                self._poisoned = True
                raise BatchRunnerError(
                    "Cannot resize Ray actor pool because actor termination "
                    f"was incomplete: {resize_errors[0]}"
                )

            if self._environment_provider and self._provisioned_env_ids:
                remaining_env_ids = []
                for env_id in list(self._provisioned_env_ids):
                    try:
                        self._environment_provider.cleanup_environment(env_id)
                    except Exception as resize_error:
                        remaining_env_ids.append(env_id)
                        resize_errors.append(
                            f"environment {env_id}: {resize_error}"
                        )
                self._provisioned_env_ids = remaining_env_ids

            if resize_errors:
                self._poisoned = True
                raise BatchRunnerError(
                    "Cannot resize Ray actor pool because cleanup was "
                    f"incomplete: {resize_errors[0]}"
                )
        
        self._ensure_ray_initialized()
        
        file_tracking_status = "enabled" if self._file_tracking_enabled else "disabled"
        workspace_status = "enabled" if self._workspace_enabled else "disabled"
        env_status = type(self._environment_provider).__name__ if self._environment_provider else "none"
        logger.info(
            f"Creating actor pool with {num_workers} workers "
            f"(FileTracking: {file_tracking_status}, Workspace: {workspace_status}, "
            f"EnvProvider: {env_status})"
        )
        
        if self._actors:
            self._poisoned = True
            raise BatchRunnerError(
                "Cannot create Ray actor pool while uncommitted actor handles "
                "remain; call shutdown() to retry cleanup"
            )

        created_actors: List[Any] = []
        attempted_env_ids: List[str] = []
        try:
            for i in range(num_workers):
                logical_actor_id = f"actor_{i}"
                actor_id = f"{self._environment_namespace}-{logical_actor_id}"
                environment_id = actor_id
                ray_runtime_env: Dict[str, Any] = self._build_ray_runtime_env(
                    actor_id,
                    {},
                )

                if self._environment_provider is not None:
                    attempted_env_ids.append(environment_id)
                    env_config = {
                        "packages": [],
                        "workflow_id": f"ray-batch-{self._environment_namespace}",
                        "node_id": actor_id,
                    }
                    self._environment_provider.create_environment(
                        environment_id,
                        env_config,
                    )
                    self._provisioned_env_ids.append(environment_id)
                    raw_env = self._environment_provider.get_runtime_env(
                        environment_id
                    ) or {}
                    ray_runtime_env = self._build_ray_runtime_env(
                        actor_id,
                        raw_env,
                    )
                    logger.info(
                        f"Actor {actor_id}: UV venv provisioned via "
                        f"{type(self._environment_provider).__name__} - "
                        f"env_path={raw_env.get('env_path', 'N/A')}, "
                        f"python_path={raw_env.get('python_path', 'N/A')}"
                    )

                actor_options = {
                    "num_cpus": self._config.num_cpus_per_task,
                    "memory": int(
                        self._config.memory_per_task_gb * 1024 * 1024 * 1024
                    ),
                    "max_restarts": self._config.max_retries,
                }
                if ray_runtime_env:
                    actor_options["runtime_env"] = ray_runtime_env

                actor = VariantExecutionActor.options(**actor_options).remote(
                    agentgit_db_url=self._agentgit_db_url,
                    wtb_db_url=self._wtb_db_url,
                    actor_id=actor_id,
                    filetracker_config=self._filetracker_config,
                    workspace_config=self._workspace_config,
                )
                created_actors.append(actor)

            actor_pool = ray.util.ActorPool(created_actors)
        except Exception as creation_error:
            failed_actors = []
            rollback_errors = []
            for actor in created_actors:
                try:
                    ray.kill(actor, no_restart=True)
                except Exception as rollback_error:
                    failed_actors.append(actor)
                    rollback_errors.append(
                        f"actor termination: {rollback_error}"
                    )
            
            if self._environment_provider is not None:
                if failed_actors:
                    # A potentially-live actor may still be starting against
                    # any attempted environment. Preserve every ID until
                    # shutdown confirms actor termination.
                    for env_id in attempted_env_ids:
                        if env_id not in self._provisioned_env_ids:
                            self._provisioned_env_ids.append(env_id)
                else:
                    for env_id in attempted_env_ids:
                        try:
                            self._environment_provider.cleanup_environment(env_id)
                        except Exception as rollback_error:
                            rollback_errors.append(
                                f"environment {env_id}: {rollback_error}"
                            )
                            if env_id not in self._provisioned_env_ids:
                                self._provisioned_env_ids.append(env_id)
                        else:
                            if env_id in self._provisioned_env_ids:
                                self._provisioned_env_ids.remove(env_id)

            self._actors = failed_actors
            self._actor_pool = None
            if rollback_errors:
                self._poisoned = True
                raise BatchRunnerError(
                    "Failed to create Ray actor pool and rollback incomplete: "
                    f"{creation_error}; {rollback_errors[0]}"
                ) from creation_error

            raise BatchRunnerError(
                f"Failed to create Ray actor pool: {creation_error}"
            ) from creation_error
        
        # Commit shared state only after every actor and the ActorPool exist.
        self._actors = created_actors
        self._actor_pool = actor_pool
        logger.info("Actor pool created with %s actors", len(self._actors))
    
    @staticmethod
    def _build_ray_runtime_env(
        actor_id: str,
        raw_env: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Translate a provider's raw environment response into a valid
        Ray ``runtime_env`` dict.
        
        The ``GrpcEnvironmentProvider`` returns Docker-internal paths
        (e.g. ``/data/envs/...``) that don't exist on the host when
        the UV venv manager runs in a container.  We pass only the
        subset of keys that Ray recognises *and* whose values are
        valid on the current platform.  Metadata about the provisioned
        environment is injected via ``env_vars`` prefixed with
        ``WTB_UV_`` for traceability without breaking Ray workers.
        """
        import os as _os
        
        ray_env: Dict[str, Any] = {}
        env_vars: Dict[str, str] = dict((raw_env or {}).get("env_vars", {}) or {})
        # Provider paths may be container-local. Re-introduce VIRTUAL_ENV only
        # when Ray can select the matching host-accessible interpreter.
        env_vars.pop("VIRTUAL_ENV", None)
        metadata_vars: Dict[str, str] = {
            "WTB_UV_ENV_TYPE": raw_env.get("type", "unknown"),
            "WTB_UV_ENV_PATH": raw_env.get("env_path", ""),
            "WTB_UV_PYTHON_PATH": raw_env.get("python_path", ""),
            "WTB_UV_ACTOR_ID": actor_id,
        }

        storage_paths = resolve_actor_local_storage_paths(actor_id)
        env_vars.update(storage_paths.to_env_vars())

        for key in (
            "OPENAI_API_KEY",
            "LLM_API_KEY",
            "OPENAI_BASE_URL",
            "LLM_BASE_URL",
            "DEFAULT_LLM",
            "ALT_LLM",
            "EMBEDDING_MODEL",
            "ALT_EMBEDDING_MODEL",
            "WTB_LLM_RESPONSE_CACHE_ENABLED",
            "WTB_LLM_DEBUG",
            "DEBUG",
        ):
            value = _os.getenv(key)
            if value is not None and key not in env_vars:
                env_vars[key] = value
        
        py_exec = raw_env.get("py_executable", "")
        venv_path = raw_env.get("venv_path", "")
        
        supports_py_executable = False
        try:
            from ray.runtime_env import RuntimeEnv

            supports_py_executable = (
                "py_executable"
                in (getattr(RuntimeEnv, "known_fields", ()) or ())
            )
        except (ImportError, AttributeError):
            pass

        if (
            py_exec
            and venv_path
            and _os.path.isfile(py_exec)
            and supports_py_executable
        ):
            ray_env["py_executable"] = str(py_exec)
            env_vars["VIRTUAL_ENV"] = str(venv_path)
            logger.info(
                f"Actor {actor_id}: local venv detected, "
                f"setting VIRTUAL_ENV={venv_path}"
            )
        else:
            logger.info(
                f"Actor {actor_id}: venv is Docker-internal "
                f"({raw_env.get('env_path', 'N/A')}), "
                f"using host Python for Ray worker"
            )
        
        env_vars.update(metadata_vars)
        ray_env["env_vars"] = env_vars
        return ray_env
    
    def _load_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """
        Load workflow and serialize for Ray transmission.
        
        Args:
            workflow_id: Workflow ID to load (generates UUID if empty)
            
        Returns:
            Workflow as serializable dict
        """
        import uuid as uuid_module
        
        # Import here to avoid circular imports
        from wtb.infrastructure.database import UnitOfWorkFactory
        
        # Generate workflow_id if empty to avoid UNIQUE constraint issues
        if not workflow_id:
            workflow_id = f"auto_{uuid_module.uuid4().hex[:8]}"
        
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
                    entry_point="start",
                )
                # Add default nodes and edges
                workflow.add_node(WorkflowNode(id="start", name="Start", type="start"))
                workflow.add_node(WorkflowNode(id="end", name="End", type="end"))
                workflow.add_edge(WorkflowEdge(source_id="start", target_id="end"))
                
                # Save to DB to avoid duplicate insert by actors (SYSTEM FIX)
                # Use separate try block for add+commit as atomic unit
                try:
                    uow.workflows.add(workflow)
                except Exception as e:
                    # Might already exist (race condition) - that's OK, rollback add
                    uow.rollback()
                    logger.debug(f"Workflow {workflow_id} add failed (may exist): {e}")
                else:
                    # Only commit if add succeeded
                    uow.commit()
                    logger.info(f"Auto-created workflow {workflow_id} saved to DB")
                
                logger.warning(
                    f"Workflow {workflow_id} not found, using default workflow"
                )
            
            return workflow.to_dict()
    def _cleanup_batch_resources(
        self,
        batch_test: BatchTest,
    ) -> Optional[BatchRunnerError]:
        """Cleanup independent batch resources and retain every failed retry."""
        cleanup_errors: List[str] = []

        if self._event_bridge:
            try:
                self._event_bridge.cleanup_batch(batch_test.id)
            except Exception as event_error:
                self._poisoned = True
                self._pending_event_cleanup_ids.add(batch_test.id)
                cleanup_errors.append(f"event bridge: {event_error}")
                logger.exception(
                    "Failed to cleanup event resources for batch %s",
                    batch_test.id,
                )
            else:
                self._pending_event_cleanup_ids.discard(batch_test.id)

        is_complete = batch_test.status is BatchTestStatus.COMPLETED
        workspace_config = self._workspace_config or {}
        if is_complete:
            should_cleanup_workspace = workspace_config.get(
                "cleanup_on_complete",
                True,
            )
            workspace_reason = "batch_complete"
        else:
            should_cleanup_workspace = not workspace_config.get(
                "preserve_on_failure",
                True,
            )
            workspace_reason = "batch_failed"

        # A failed actor termination may leave code writing into a workspace.
        # Preserve it until shutdown confirms termination, regardless of the
        # configured retention policy.
        if batch_test.id in self._unsafe_batch_ids:
            logger.error(
                "Preserving workspaces for unsafe batch %s",
                batch_test.id,
            )
        elif self._workspace_manager and should_cleanup_workspace:
            try:
                cleaned = self._workspace_manager.cleanup_batch(
                    batch_id=batch_test.id,
                    reason=workspace_reason,
                )
                if cleaned > 0:
                    logger.info(
                        "Cleaned up %s workspaces for batch %s",
                        cleaned,
                        batch_test.id,
                    )
            except Exception as workspace_error:
                self._poisoned = True
                self._unsafe_batch_ids.add(batch_test.id)
                cleanup_errors.append(f"workspace: {workspace_error}")
                logger.exception(
                    "Failed to cleanup workspaces for batch %s",
                    batch_test.id,
                )
        elif self._workspace_manager:
            logger.info(
                "Preserving workspaces for batch %s by lifecycle policy",
                batch_test.id,
            )

        if cleanup_errors:
            return BatchRunnerError(
                "Ray batch resource cleanup incomplete for "
                f"{batch_test.id}: {'; '.join(cleanup_errors)}"
            )
        return None

    def _claim_terminal_owner(
        self,
        running_test: _RayRunningTest,
        owner: str,
    ) -> str:
        """Claim one immutable terminal owner and return the winner."""
        with self._running_tests_lock:
            if running_test.terminal_owner is None:
                running_test.terminal_owner = owner
            return running_test.terminal_owner

    def _is_confirmed_cancel(
        self,
        running_test: _RayRunningTest,
    ) -> bool:
        """Return whether cancellation safely owns the terminal outcome."""
        with self._running_tests_lock:
            return (
                running_test.terminal_owner == "cancel"
                and running_test.actor_termination_confirmed
                and running_test.termination_confirmed
                and running_test.termination_error is None
            )

    def _cancel_stopped_actors(
        self,
        running_test: _RayRunningTest,
    ) -> bool:
        """Return whether cancellation already terminated every actor."""
        with self._running_tests_lock:
            return (
                running_test.terminal_owner == "cancel"
                and running_test.actor_termination_confirmed
            )

    def _wait_for_cancellation_result(
        self,
        running_test: _RayRunningTest,
    ) -> tuple[bool, Optional[str]]:
        """Wait until the cancellation owner publishes its final verdict."""
        while True:
            with self._running_tests_lock:
                if not running_test.cancellation_in_progress:
                    return (
                        running_test.termination_confirmed,
                        running_test.termination_error,
                    )
            running_test.cancellation_finished.wait()

    def _reconcile_cancelled_variants(
        self,
        running_test: _RayRunningTest,
    ) -> int:
        """Recompute terminal cancellation totals after result processing stops."""
        with self._running_tests_lock:
            running_test.cancelled_variants = max(
                0,
                running_test.total_variants
                - running_test.completed
                - running_test.failed,
            )
            return running_test.cancelled_variants

    
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
        
        # This runner has one shared actor pool. Register its owner atomically
        # so concurrent batches cannot select or terminate each other's actors.
        with self._running_tests_lock:
            if self._closed:
                raise BatchRunnerError("Ray batch runner is closed")
            if self._poisoned:
                raise BatchRunnerError(
                    "Ray batch runner is poisoned after incomplete actor termination; "
                    "call shutdown() to retry cleanup before reusing it"
                )
            if self._shutting_down:
                raise BatchRunnerError("Ray batch runner is shutting down")
            if self._running_tests:
                active_id = next(iter(self._running_tests))
                raise BatchRunnerError(
                    f"Ray batch {active_id} is already running on this runner"
                )

            batch_test.start()
            running_test = _RayRunningTest(
                batch_test_id=batch_test.id,
                started_at=datetime.now(),
                total_variants=len(batch_test.variant_combinations),
            )
            self._running_tests[batch_test.id] = running_test
        
        pending_refs: List[Any] = []
        try:
            # Determine parallelism
            num_workers = min(
                batch_test.parallel_count,
                len(batch_test.variant_combinations),
                self._config.max_pending_tasks,
            )
            
            # Pool creation and cancellation share a lock. A cancellation
            # requested before creation acquires no new actors.
            with self._actor_pool_lock:
                if not running_test.cancelled:
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
                    actor_ids=[
                        f"{self._environment_namespace}-actor_{i}"
                        for i in range(num_workers)
                    ],
                )
            
            # Load and serialize workflow
            workflow_dict = self._load_workflow(batch_test.workflow_id)
            
            # Put immutable data in object store (zero-copy sharing)
            workflow_ref = ray.put(workflow_dict)
            initial_state_ref = ray.put(batch_test.initial_state)
            batch_test_id_ref = ray.put(batch_test.id)
            
            # Cloudpickle-serialized graph factory for __main__ fallback
            graph_factory_pickled = batch_test.metadata.get("_graph_factory_pickled")
            
            logger.info(
                f"Starting batch test {batch_test.id} with "
                f"{len(batch_test.variant_combinations)} variants"
            )
            
            # Submit all variants with backpressure
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
                
                # Emit variant execution started event (FLAW 8 fix)
                if self._event_bridge:
                    self._event_bridge.on_variant_execution_started(
                        execution_id=execution_id,
                        batch_test_id=batch_test.id,
                        actor_id="",
                        combination_name=combo.name,
                        variants=combo.variants,
                        queue_position=batch_test.variant_combinations.index(combo),
                        total_in_queue=len(batch_test.variant_combinations),
                    )
                
                # Cancellation cannot return between actor selection and ref
                # registration. It either prevents submission or kills the
                # actor that accepted the fully-tracked task.
                with self._actor_pool_lock:
                    if running_test.cancelled:
                        break
                    actor = self._get_available_actor()
                    ref = actor.execute_variant.remote(
                        workflow_dict=ray.get(workflow_ref),
                        combination=combo_dict,
                        initial_state=ray.get(initial_state_ref),
                        batch_test_id=ray.get(batch_test_id_ref),
                        workspace_data=workspace_data,  # Pass workspace (2026-01-16)
                        graph_factory_pickled=graph_factory_pickled,
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
            
            # Build the comparison before committing a terminal state. This
            # keeps matrix failures as a single RUNNING -> FAILED transition.
            with self._running_tests_lock:
                terminal_owner = running_test.terminal_owner

            if terminal_owner is None:
                if running_test.failed == len(batch_test.variant_combinations):
                    terminal_owner = self._claim_terminal_owner(
                        running_test,
                        "run_failed",
                    )
                else:
                    batch_test.build_comparison_matrix()
                    terminal_owner = self._claim_terminal_owner(
                        running_test,
                        "run_completed",
                    )

            # A cancellation request is not a safe terminal result until
            # actor termination and required environment cleanup complete.
            if terminal_owner == "cancel":
                termination_confirmed, termination_error = (
                    self._wait_for_cancellation_result(running_test)
                )
                if termination_error:
                    raise BatchRunnerError(termination_error)
                if not termination_confirmed:
                    raise BatchRunnerError(
                        "Ray batch cancellation ended without confirmed actor termination"
                    )

                self._reconcile_cancelled_variants(running_test)
                batch_test.cancel()
                
                # Emit cancelled event
                if self._event_bridge:
                    self._event_bridge.on_batch_test_cancelled(
                        batch_test_id=batch_test.id,
                        workflow_id=batch_test.workflow_id,
                        reason="User cancelled",
                        cancelled_by="user",
                        variants_completed=running_test.completed,
                        variants_cancelled=running_test.cancelled_variants,
                    )
            elif terminal_owner == "run_failed":
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
            elif terminal_owner == "run_completed":
                batch_test.complete()
                
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
            else:
                raise BatchRunnerError(
                    "Ray batch finalization cannot proceed while terminal "
                    f"state is owned by {terminal_owner}"
                )
            
            logger.info(
                f"Batch test {batch_test.id} completed: "
                f"{running_test.completed} succeeded, {running_test.failed} failed"
            )
            
            return batch_test
            
        except Exception as e:
            # Seal an unowned failure before a late cancel can claim a
            # contradictory outcome. If cancellation already owns the result,
            # wait for its environment-cleanup verdict before deciding.
            with self._running_tests_lock:
                terminal_owner = running_test.terminal_owner
                claimed_run_failure = terminal_owner is None
                if claimed_run_failure:
                    running_test.terminal_owner = "run_failed"
                    terminal_owner = "run_failed"
            # Unknown orchestration failures may leave submitted actor work
            # running. Terminate it before workspace cleanup, or preserve all
            # handles/resources and poison the runner if termination fails.
            if (
                running_test.pending_refs
                and batch_test.id not in self._unsafe_batch_ids
            ):
                termination_issue = self._terminate_failed_batch_work(
                    batch_test.id,
                    pending_refs,
                    running_test,
                    reason=type(e).__name__,
                )
                if termination_issue:
                    logger.error(
                        "Ray batch %s failure cleanup incomplete: %s",
                        batch_test.id,
                        termination_issue,
                    )

            if terminal_owner == "cancel":
                self._wait_for_cancellation_result(running_test)
            confirmed_cancel = self._is_confirmed_cancel(running_test)

            if confirmed_cancel:
                # Cancellation already stopped every actor and cleaned its
                # environments. Preserve it despite late result/event errors.
                logger.warning(
                    "Ignoring post-cancellation Ray batch error for %s: %s",
                    batch_test.id,
                    e,
                )
                if batch_test.status is BatchTestStatus.RUNNING:
                    self._reconcile_cancelled_variants(running_test)
                    batch_test.cancel()
                    if self._event_bridge:
                        try:
                            self._event_bridge.on_batch_test_cancelled(
                                batch_test_id=batch_test.id,
                                workflow_id=batch_test.workflow_id,
                                reason="User cancelled",
                                cancelled_by="user",
                                variants_completed=running_test.completed,
                                variants_cancelled=running_test.cancelled_variants,
                            )
                        except Exception:
                            logger.exception(
                                "Failed to publish terminal cancellation for %s",
                                batch_test.id,
                            )
                return batch_test

            if (
                terminal_owner == "run_completed"
                or (terminal_owner == "run_failed" and not claimed_run_failure)
            ):
                # The aggregate is already terminal. Surface the callback error
                # without a contradictory second state transition.
                logger.error(
                    "Ray batch %s post-terminal finalization failed: %s",
                    batch_test.id,
                    e,
                )
                raise BatchRunnerError(
                    f"Batch test {terminal_owner} finalization failed: {e}"
                ) from e

            logger.error(f"Ray batch test {batch_test.id} failed: {e}")
            if batch_test.status in {
                BatchTestStatus.PENDING,
                BatchTestStatus.RUNNING,
            }:
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
                    variants_pending=max(
                        0,
                        running_test.total_variants
                        - running_test.completed
                        - running_test.failed,
                    ),
                )
            
            raise BatchRunnerError(f"Batch test failed: {e}") from e
            
        finally:
            cleanup_error: Optional[BatchRunnerError] = None
            try:
                cleanup_error = self._cleanup_batch_resources(batch_test)
            finally:
                with self._running_tests_lock:
                    self._running_tests.pop(batch_test.id, None)
                running_test.finished_event.set()

            if cleanup_error is not None:
                raise cleanup_error
    
    def _get_available_actor(self) -> Any:
        """Get an available actor from the pool (round-robin)."""
        if not self._actors:
            raise BatchRunnerError("No actors available")
        
        # Simple round-robin selection
        actor = self._actors[0]
        self._actors = self._actors[1:] + [self._actors[0]]
        return actor
    
    def _terminate_failed_batch_work(
        self,
        batch_test_id: str,
        pending_refs: List[Any],
        running_test: _RayRunningTest,
        reason: str,
    ) -> Optional[str]:
        """Terminate submitted work after an unexpected orchestration failure."""
        if self._cancel_stopped_actors(running_test):
            # The actor pool was already terminated safely by cancel(). Late
            # event/result processing errors cannot resurrect submitted work.
            pending_refs.clear()
            running_test.pending_refs.clear()
            return None

        with self._actor_pool_lock:
            # cancel() may have won while this path waited for lifecycle lock.
            if self._cancel_stopped_actors(running_test):
                pending_refs.clear()
                running_test.pending_refs.clear()
                return None

            actors = list(self._actors)
            failed_actors = []
            termination_errors = []
            for actor in actors:
                try:
                    ray.kill(actor, no_restart=True)
                except Exception as termination_error:
                    failed_actors.append(actor)
                    termination_errors.append(str(termination_error))
                    logger.error(
                        "Failed to terminate actor after %s: %s",
                        reason,
                        termination_error,
                    )

            if pending_refs and not actors:
                termination_errors.append(
                    "no actor handles available for submitted refs"
                )

            self._actors = failed_actors
            self._actor_pool = None
            if termination_errors:
                self._poisoned = True
                self._unsafe_batch_ids.add(batch_test_id)
                self._orphaned_refs.extend(pending_refs)
                return termination_errors[0]

            self._actors.clear()
            pending_refs.clear()
            running_test.pending_refs.clear()

            cleanup_errors = []
            if self._environment_provider and self._provisioned_env_ids:
                remaining_env_ids = []
                for env_id in list(self._provisioned_env_ids):
                    try:
                        self._environment_provider.cleanup_environment(env_id)
                    except Exception as cleanup_error:
                        remaining_env_ids.append(env_id)
                        cleanup_errors.append(
                            f"environment {env_id}: {cleanup_error}"
                        )
                        logger.warning(
                            "Failed to cleanup env %s after %s: %s",
                            env_id,
                            reason,
                            cleanup_error,
                        )
                self._provisioned_env_ids = remaining_env_ids

            if cleanup_errors:
                self._poisoned = True
                return cleanup_errors[0]

            return None
    def _process_completed_refs(
        self,
        pending_refs: List[Any],
        ref_to_combo: Dict[Any, VariantCombination],
        batch_test: BatchTest,
        running_test: _RayRunningTest,
        timeout: float,
    ):
        """
        Process completed ObjectRefs and abort the actor pool if the configured
        no-progress window expires.
        
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

        if not ready_refs:
            timeout_message = (
                f"No Ray task completed within {timeout} seconds; "
                "terminating the actor pool"
            )
            logger.error(timeout_message)

            with self._actor_pool_lock:
                # Commit exactly one terminal owner while cancel cannot mutate
                # the actor pool. A late cancel observes "timeout" and cannot
                # report a contradictory successful cancellation.
                with self._running_tests_lock:
                    if running_test.terminal_owner == "cancel":
                        return
                    if running_test.terminal_owner is None:
                        running_test.terminal_owner = "timeout"
                    elif running_test.terminal_owner != "timeout":
                        raise BatchRunnerError(
                            "Ray timeout cannot replace terminal owner "
                            f"{running_test.terminal_owner}"
                        )

                failed_actors = []
                kill_errors = []
                for actor in list(self._actors):
                    try:
                        ray.kill(actor, no_restart=True)
                    except Exception as kill_error:
                        failed_actors.append(actor)
                        kill_errors.append(str(kill_error))
                        logger.error(
                            "Failed to terminate timed-out Ray actor: %s",
                            kill_error,
                        )

                self._actor_pool = None

                if failed_actors:
                    # At least one actor may still be executing. Keep every
                    # potentially-live ref and resource so shutdown can retry.
                    self._actors = failed_actors
                    self._poisoned = True
                    self._unsafe_batch_ids.add(batch_test.id)
                    self._orphaned_refs.extend(pending_refs)
                    timeout_message += (
                        f" ({len(kill_errors)} actor termination error(s); "
                        "runner poisoned)"
                    )
                    raise BatchRunnerError(timeout_message)

                self._actors.clear()
                pending_refs.clear()
                running_test.pending_refs.clear()
                ref_to_combo.clear()

                if self._environment_provider and self._provisioned_env_ids:
                    remaining_env_ids = []
                    for env_id in list(self._provisioned_env_ids):
                        try:
                            self._environment_provider.cleanup_environment(env_id)
                        except Exception as cleanup_error:
                            remaining_env_ids.append(env_id)
                            logger.warning(
                                "Failed to clean up timed-out env %s: %s",
                                env_id,
                                cleanup_error,
                            )
                    self._provisioned_env_ids = remaining_env_ids
                    if remaining_env_ids:
                        self._poisoned = True
                        timeout_message += (
                            " (environment cleanup incomplete; runner poisoned)"
                        )

            # Timeout owns the terminal outcome once committed above.

            # The batch aborts here, including combinations that backpressure
            # had not submitted yet. Keep the terminal event totals complete.
            aborted_count = max(
                0,
                running_test.total_variants - running_test.completed - running_test.failed,
            )
            running_test.failed += aborted_count

            raise BatchRunnerError(timeout_message)
        
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

            except (
                ray.exceptions.TaskCancelledError,
                ray.exceptions.RayActorError,
            ) as cancellation_error:
                if not running_test.cancelled:
                    raise
                logger.info(
                    "Ray task for %s reached cancellation terminal state: %s",
                    combo.name if combo else "unknown",
                    cancellation_error,
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
        with self._running_tests_lock:
            running = self._running_tests.get(batch_test_id)
            if running is None:
                return BatchRunnerStatus.IDLE
            if running.cancelled:
                return BatchRunnerStatus.CANCELLING
            return BatchRunnerStatus.RUNNING
    
    def get_progress(self, batch_test_id: str) -> Optional[BatchRunnerProgress]:
        """Get progress for a running batch test."""
        with self._running_tests_lock:
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
    
    @staticmethod
    def _remaining_budget(deadline: Optional[float]) -> Optional[float]:
        """Return remaining seconds for an absolute monotonic deadline."""
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    @staticmethod
    def _run_with_deadline(
        operation: Callable[[], Any],
        deadline: Optional[float],
        description: str,
    ) -> Any:
        """Run an external blocking operation within the remaining budget."""
        if deadline is None:
            return operation()

        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError(f"{description} exceeded shutdown deadline")

        completed = threading.Event()
        outcome: Dict[str, Any] = {}

        def invoke() -> None:
            try:
                outcome["value"] = operation()
            except BaseException as error:
                outcome["error"] = error
            finally:
                completed.set()

        worker = threading.Thread(
            target=invoke,
            name="wtb-bounded-cleanup",
            daemon=True,
        )
        worker.start()
        if not completed.wait(timeout=remaining):
            raise TimeoutError(f"{description} exceeded shutdown deadline")
        if "error" in outcome:
            raise outcome["error"]
        return outcome.get("value")

    def _cleanup_environment_with_deadline(
        self,
        env_id: str,
        deadline: Optional[float],
    ) -> None:
        """Cleanup one environment without exceeding an optional deadline."""
        if self._environment_provider is None:
            return

        if deadline is None:
            self._environment_provider.cleanup_environment(env_id)
            return

        remaining = self._remaining_budget(deadline)
        if remaining is None or remaining <= 0:
            raise TimeoutError(
                f"Environment cleanup for {env_id} exceeded shutdown deadline"
            )

        self._run_with_deadline(
            lambda: self._environment_provider.cleanup_environment(
                env_id,
                timeout=remaining,
            ),
            deadline,
            f"Environment cleanup for {env_id}",
        )

    def cancel(
        self,
        batch_test_id: str,
        _lock_timeout: Optional[float] = None,
    ) -> bool:
        """
        Cancel a running batch test.
        
        Args:
            batch_test_id: ID of the batch test to cancel
            _lock_timeout: Internal total shutdown budget in seconds.
            
        Returns:
            True if cancellation reached a confirmed safe terminal state;
            False if not found or termination could not be confirmed
        """
        cancel_deadline = (
            None
            if _lock_timeout is None
            else time.monotonic() + max(0.0, _lock_timeout)
        )
        wait_for_existing = False
        with self._running_tests_lock:
            running = self._running_tests.get(batch_test_id)
            if running is None:
                return False

            if running.terminal_owner not in (None, "cancel"):
                logger.info(
                    "Ray batch cancellation rejected because terminal state "
                    "is owned by %s",
                    running.terminal_owner,
                )
                return False

            if running.cancellation_in_progress:
                wait_for_existing = True
            elif running.terminal_owner == "cancel":
                return (
                    running.termination_confirmed
                    and running.termination_error is None
                )
            else:
                # A cancellation failure is terminal for this run. Recovery
                # belongs to shutdown, which can retry preserved resources.
                running.terminal_owner = "cancel"
                running.cancelled = True
                running.cancelled_variants = max(
                    0,
                    running.total_variants
                    - running.completed
                    - running.failed,
                )
                running.cancellation_in_progress = True
                running.termination_confirmed = False
                running.termination_error = None
                running.cancellation_finished.clear()

        if wait_for_existing:
            completed = running.cancellation_finished.wait(
                timeout=self._remaining_budget(cancel_deadline),
            )
            if not completed:
                return False
            with self._running_tests_lock:
                return (
                    running.termination_confirmed
                    and running.termination_error is None
                )
        
        success = False
        error_message: Optional[str] = None
        remaining = self._remaining_budget(cancel_deadline)
        acquired = (
            self._actor_pool_lock.acquire()
            if remaining is None
            else self._actor_pool_lock.acquire(
                timeout=remaining,
            )
        )
        try:
            if not acquired:
                error_message = (
                    "Ray batch actor termination lock acquisition timed out"
                )
                self._poisoned = True
                self._unsafe_batch_ids.add(batch_test_id)
                self._orphaned_refs.extend(running.pending_refs)
            else:
                # Regular/threaded Ray actors only receive a cooperative
                # cancellation flag. Kill the pool exactly once for this run.
                termination_errors = []
                if not running.actor_termination_confirmed:
                    actors = list(self._actors)
                    failed_actors = []
                    for actor in actors:
                        try:
                            self._run_with_deadline(
                                lambda actor=actor: ray.kill(
                                    actor, no_restart=True
                                ),
                                cancel_deadline,
                                "Ray actor termination during cancellation",
                            )
                        except Exception as error:
                            failed_actors.append(actor)
                            termination_errors.append(str(error))
                            logger.warning(
                                "Failed to terminate actor during cancellation: %s",
                                error,
                            )

                    if running.pending_refs and not actors:
                        termination_errors.append(
                            "no actor handles available for submitted refs"
                        )

                    self._actors = failed_actors
                    self._actor_pool = None
                    if not termination_errors:
                        running.actor_termination_confirmed = True

                if termination_errors:
                    self._poisoned = True
                    self._unsafe_batch_ids.add(batch_test_id)
                    self._orphaned_refs.extend(running.pending_refs)
                    error_message = (
                        "Ray batch actor termination was incomplete: "
                        f"{termination_errors[0]}"
                    )
                else:
                    cleanup_errors = []
                    if self._environment_provider and self._provisioned_env_ids:
                        remaining_env_ids = []
                        for env_id in list(self._provisioned_env_ids):
                            try:
                                self._cleanup_environment_with_deadline(
                                    env_id,
                                    cancel_deadline,
                                )
                            except Exception as cleanup_error:
                                remaining_env_ids.append(env_id)
                                cleanup_errors.append(str(cleanup_error))
                                logger.warning(
                                    "Failed to cleanup cancelled env %s: %s",
                                    env_id,
                                    cleanup_error,
                                )
                        self._provisioned_env_ids = remaining_env_ids

                    if cleanup_errors:
                        self._poisoned = True
                        error_message = (
                            "Ray batch cancellation environment cleanup was "
                            f"incomplete: {cleanup_errors[0]}"
                        )
                    else:
                        success = True
        finally:
            if acquired:
                self._actor_pool_lock.release()
            with self._running_tests_lock:
                running.termination_confirmed = success
                running.termination_error = error_message
                if success:
                    running.pending_refs.clear()
                running.cancellation_in_progress = False
                running.cancellation_finished.set()

        if success:
            logger.info("Batch test %s cancellation confirmed", batch_test_id)
        else:
            logger.error(
                "Ray batch %s cancellation was not confirmed: %s",
                batch_test_id,
                error_message,
            )
        return success
    
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
        """Serialize shutdown so shared resources close exactly once."""
        with self._shutdown_lock:
            self._shutdown_locked()

    def _close_owned_environment_provider(
        self,
        deadline: Optional[float],
    ) -> None:
        """Close an owned provider once, reusing an in-flight close on retry."""
        if not self._environment_provider or not self._owns_environment_provider:
            return
        close = getattr(self._environment_provider, "close", None)
        if not callable(close):
            return

        should_start = False
        with self._provider_close_lock:
            if self._provider_closed:
                return
            close_event = self._provider_close_event
            if close_event is None:
                remaining = self._remaining_budget(deadline)
                if remaining is not None and remaining <= 0:
                    raise TimeoutError(
                        "Environment provider close exceeded shutdown deadline"
                    )
                close_event = threading.Event()
                self._provider_close_event = close_event
                self._provider_close_error = None
                should_start = True

        assert close_event is not None
        if should_start:

            def invoke_close() -> None:
                close_error: Optional[BaseException] = None
                try:
                    close()
                except BaseException as error:
                    close_error = error
                finally:
                    with self._provider_close_lock:
                        self._provider_close_error = close_error
                        if close_error is None:
                            self._provider_closed = True
                    close_event.set()

            worker = threading.Thread(
                target=invoke_close,
                name="wtb-environment-provider-close",
                daemon=True,
            )
            try:
                worker.start()
            except BaseException:
                with self._provider_close_lock:
                    if self._provider_close_event is close_event:
                        self._provider_close_event = None
                        self._provider_close_error = None
                raise

        remaining = self._remaining_budget(deadline)
        if remaining is None:
            close_event.wait()
        elif remaining <= 0 or not close_event.wait(timeout=remaining):
            raise TimeoutError(
                "Environment provider close exceeded shutdown deadline"
            )

        with self._provider_close_lock:
            close_error = self._provider_close_error
            if close_error is not None and self._provider_close_event is close_event:
                self._provider_close_event = None
                self._provider_close_error = None
        if close_error is not None:
            raise close_error

    def _shutdown_locked(self) -> None:
        """
        Shutdown Ray resources.
        
        Kills all actors and cleans up state. If Ray cannot confirm actor
        termination, live handles and resources remain tracked for a retry.
        """
        logger.info("Shutting down RayBatchTestRunner")
        
        # Stop accepting new work before taking the active-run snapshot.
        # The deadline includes actor-lock acquisition inside cancel().
        shutdown_deadline = (
            time.monotonic() + self._shutdown_timeout_seconds
        )
        with self._running_tests_lock:
            if self._closed:
                return
            self._shutting_down = True
            active_tests = list(self._running_tests.values())

        for running in active_tests:
            remaining = max(0.0, shutdown_deadline - time.monotonic())
            self.cancel(
                running.batch_test_id,
                _lock_timeout=remaining,
            )
        
        # cancel() confirms actor termination, but the run thread still owns
        # result/event/workspace finalization. Do not close shared providers
        # until every run has completed that finally block.
        for running in active_tests:
            remaining = max(0.0, shutdown_deadline - time.monotonic())
            if not running.finished_event.wait(timeout=remaining):
                self._poisoned = True
                raise BatchRunnerError(
                    "RayBatchTestRunner shutdown timed out waiting for active "
                    f"batch {running.batch_test_id} to finish"
                )
        
        with self._running_tests_lock:
            if self._running_tests:
                self._poisoned = True
                active_ids = ", ".join(self._running_tests)
                raise BatchRunnerError(
                    "RayBatchTestRunner shutdown found unfinished batches: "
                    f"{active_ids}"
                )

        with self._actor_pool_lock:
            failed_actors = []
            termination_errors = []
            for actor in list(self._actors):
                try:
                    self._run_with_deadline(
                        lambda actor=actor: ray.kill(
                            actor, no_restart=True
                        ),
                        shutdown_deadline,
                        "Ray actor termination during shutdown",
                    )
                except Exception as error:
                    failed_actors.append(actor)
                    termination_errors.append(str(error))
                    logger.warning("Failed to kill actor: %s", error)

            self._actors = failed_actors
            self._actor_pool = None
            if failed_actors:
                self._poisoned = True
                raise BatchRunnerError(
                    "RayBatchTestRunner shutdown could not confirm termination of "
                    f"{len(failed_actors)} actor(s): {termination_errors[0]}"
                )

            self._actors.clear()
        
        self._orphaned_refs.clear()
        cleanup_errors = []

        if self._event_bridge and self._pending_event_cleanup_ids:
            remaining_event_cleanup_ids = set()
            for batch_id in list(self._pending_event_cleanup_ids):
                try:
                    self._run_with_deadline(
                        lambda batch_id=batch_id: self._event_bridge.cleanup_batch(
                            batch_id
                        ),
                        shutdown_deadline,
                        f"Event cleanup for batch {batch_id}",
                    )
                except Exception as error:
                    remaining_event_cleanup_ids.add(batch_id)
                    cleanup_errors.append(f"event batch {batch_id}: {error}")
                    logger.warning(
                        "Failed to retry event cleanup for batch %s: %s",
                        batch_id,
                        error,
                    )
            self._pending_event_cleanup_ids = remaining_event_cleanup_ids

        if self._environment_provider and self._provisioned_env_ids:
            logger.info(
                f"Cleaning up {len(self._provisioned_env_ids)} provisioned UV venvs"
            )
            remaining_env_ids = []
            for env_id in list(self._provisioned_env_ids):
                try:
                    self._cleanup_environment_with_deadline(
                        env_id,
                        shutdown_deadline,
                    )
                except Exception as e:
                    remaining_env_ids.append(env_id)
                    cleanup_errors.append(f"environment {env_id}: {e}")
                    logger.warning(f"Failed to cleanup env {env_id}: {e}")
            self._provisioned_env_ids = remaining_env_ids

        if self._unsafe_batch_ids:
            remaining_unsafe_batches = set()
            for batch_id in list(self._unsafe_batch_ids):
                if not self._workspace_manager:
                    continue
                try:
                    self._run_with_deadline(
                        lambda batch_id=batch_id: self._workspace_manager.cleanup_batch(
                            batch_id=batch_id,
                            reason="runner_shutdown_after_timeout",
                        ),
                        shutdown_deadline,
                        f"Workspace cleanup for batch {batch_id}",
                    )
                except Exception as e:
                    remaining_unsafe_batches.add(batch_id)
                    cleanup_errors.append(f"workspace batch {batch_id}: {e}")
                    logger.warning(
                        "Failed to cleanup preserved workspaces for batch %s: %s",
                        batch_id,
                        e,
                    )
            self._unsafe_batch_ids = remaining_unsafe_batches

        if cleanup_errors:
            self._poisoned = True
            raise BatchRunnerError(
                "RayBatchTestRunner shutdown cleanup incomplete: "
                f"{cleanup_errors[0]}"
            )

        if self._environment_provider and self._owns_environment_provider:
            try:
                self._close_owned_environment_provider(shutdown_deadline)
            except Exception as e:
                self._poisoned = True
                raise BatchRunnerError(
                    "RayBatchTestRunner shutdown cleanup incomplete: "
                    f"environment provider close: {e}"
                ) from e

        self._poisoned = False
        with self._running_tests_lock:
            self._closed = True
            # Keep the terminal gate closed because providers were closed.
            self._shutting_down = True
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
    
    def create_rollback_coordinator(
        self,
        config: Optional["WTBConfig"] = None,
    ) -> "BatchExecutionCoordinator":
        """
        Create BatchExecutionCoordinator reusing this runner's configuration.
        
        v1.8: Factory method for creating coordinator that shares configuration
        with the Ray batch runner for consistent rollback/fork operations.
        
        v1.9: Added config parameter for rollback cleanup options.
        
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
            
            # v1.9: With cleanup enabled
            from wtb.config import WTBConfig
            wtb_config = WTBConfig(rollback_cleanup_enabled=True)
            coordinator = runner.create_rollback_coordinator(config=wtb_config)
        
        Args:
            config: Optional WTBConfig for rollback cleanup options (v1.9).
                   If not provided, file cleanup after rollback is disabled.
        
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
            config=config,
        )
    
    def _create_shared_state_adapter(self) -> Optional["IStateAdapter"]:
        """
        Create StateAdapter with same config as actors.
        
        v1.8: Used by create_rollback_coordinator() for consistent state access.
        v1.9: Uses base wtb_db_url for default checkpoint path. For per-execution
        rollback, the coordinator should resolve the correct actor-specific
        checkpoint DB from execution.metadata["checkpoint_db_path"].
        v1.9: Uses base wtb_db_url for default checkpoint path. For per-execution
        rollback, the coordinator should resolve the correct actor-specific
        checkpoint DB from execution.metadata["checkpoint_db_path"].
        
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
                import os as _os
                base_path = (
                    self._wtb_db_url.replace("sqlite:///", "")
                    if self._wtb_db_url.startswith("sqlite:///")
                    else self._wtb_db_url
                )
                data_dir = _os.path.dirname(base_path) or "."
                checkpoint_db = _os.path.join(data_dir, "wtb_checkpoints.db")
                return LangGraphStateAdapter(LangGraphConfig.for_development(checkpoint_db))
            
        except ImportError:
            pass
        
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
            from pathlib import Path
            from wtb.config import FileTrackingConfig
            
            config = FileTrackingConfig.from_dict(self._filetracker_config)
            if config.postgres_url:
                from wtb.infrastructure.file_tracking import FileTrackerService

                return FileTrackerService(config)

            from wtb.infrastructure.file_tracking import SqliteFileTrackingService

            return SqliteFileTrackingService(
                workspace_path=Path(config.storage_path),
                db_name="filetrack.db",
            )
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
