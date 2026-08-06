"""
Execution Controller Implementation.

Refactored (v1.6): Uses string IDs throughout, aligned with LangGraph.

Orchestrates workflow execution with support for:
- Run/Pause/Resume/Stop lifecycle
- Breakpoint handling
- State persistence via adapters (string IDs)
- Rollback capabilities
- File tracking and restore
- Fork operations (moved from SDK)
"""

import ast
import copy
import logging
import operator
import os
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from wtb.domain.interfaces.execution_controller import IExecutionController
from wtb.domain.interfaces.node_executor import INodeExecutor, NodeExecutionResult
from wtb.domain.interfaces.repositories import IExecutionRepository, IWorkflowRepository
from wtb.domain.interfaces.state_adapter import CheckpointTrigger, IStateAdapter
from wtb.domain.models import (
    Execution,
    ExecutionState,
    ExecutionStatus,
    TestWorkflow,
    WorkflowNode,
)

if TYPE_CHECKING:
    from wtb.domain.interfaces.file_tracking import IFileTrackingService
    from wtb.domain.interfaces.unit_of_work import IUnitOfWork

logger = logging.getLogger(__name__)


class NodeBoundaryClaimConflict(RuntimeError):
    """A competing runner owns or has advanced the durable node boundary."""


_SAFE_COMPARE_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

_SAFE_BOOL_OPS = {ast.And: all, ast.Or: any}

_SAFE_UNARY_OPS = {ast.Not: operator.not_, ast.USub: operator.neg}

_SAFE_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
}


def _safe_eval_node(node: ast.AST, ctx: dict[str, Any]) -> Any:
    """Recursively evaluate an AST node against *ctx* without exec/eval."""
    if isinstance(node, ast.Expression):
        return _safe_eval_node(node.body, ctx)

    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        if node.id in ctx:
            return ctx[node.id]
        raise NameError(f"Name '{node.id}' is not defined in condition context")

    if isinstance(node, ast.UnaryOp):
        op_fn = _SAFE_UNARY_OPS.get(type(node.op))
        if op_fn is None:
            raise ValueError(f"Unsupported unary op: {type(node.op).__name__}")
        return op_fn(_safe_eval_node(node.operand, ctx))

    if isinstance(node, ast.BoolOp):
        fn = _SAFE_BOOL_OPS.get(type(node.op))
        if fn is None:
            raise ValueError(f"Unsupported bool op: {type(node.op).__name__}")
        return fn(_safe_eval_node(v, ctx) for v in node.values)

    if isinstance(node, ast.BinOp):
        op_fn = _SAFE_BIN_OPS.get(type(node.op))
        if op_fn is None:
            raise ValueError(f"Unsupported binary op: {type(node.op).__name__}")
        return op_fn(
            _safe_eval_node(node.left, ctx), _safe_eval_node(node.right, ctx)
        )

    if isinstance(node, ast.Compare):
        left = _safe_eval_node(node.left, ctx)
        for op, comparator in zip(node.ops, node.comparators):
            op_fn = _SAFE_COMPARE_OPS.get(type(op))
            if op_fn is None:
                raise ValueError(f"Unsupported compare op: {type(op).__name__}")
            right = _safe_eval_node(comparator, ctx)
            if not op_fn(left, right):
                return False
            left = right
        return True

    if isinstance(node, ast.IfExp):
        if _safe_eval_node(node.test, ctx):
            return _safe_eval_node(node.body, ctx)
        return _safe_eval_node(node.orelse, ctx)

    if isinstance(node, ast.Attribute):
        value = _safe_eval_node(node.value, ctx)
        return getattr(value, node.attr)

    if isinstance(node, ast.Subscript):
        value = _safe_eval_node(node.value, ctx)
        sl = _safe_eval_node(node.slice, ctx)
        return value[sl]

    raise ValueError(
        f"Unsupported expression node: {type(node).__name__}. "
        "Only comparisons, boolean logic, constants, and variable lookups are allowed."
    )


def safe_eval_condition(expr: str, context: dict[str, Any]) -> bool:
    """Evaluate a condition expression safely without eval()."""
    tree = ast.parse(expr, mode="eval")
    return bool(_safe_eval_node(tree.body, context))


class DefaultNodeExecutor(INodeExecutor):
    """
    Default node executor that runs node logic.
    
    For now, this is a simple pass-through executor.
    In production, this would integrate with ToolManager.
    """
    
    def __init__(self):
        self._supported_types = ['action', 'start', 'end', 'decision']
    
    def execute(
        self,
        node: WorkflowNode,
        context: dict[str, Any]
    ) -> NodeExecutionResult:
        """Execute a workflow node."""
        start_time = datetime.now()
        
        try:
            # Handle different node types
            if node.type == 'start':
                result = {"started": True}
            elif node.type == 'end':
                result = {"completed": True}
            elif node.type == 'decision':
                result = self._evaluate_decision(node, context)
            else:
                result = self._execute_action(node, context)
            
            duration = (datetime.now() - start_time).total_seconds() * 1000
            
            return NodeExecutionResult(
                success=True,
                output=result,
                duration_ms=duration,
                tool_invocations=1 if node.type == 'action' else 0,
            )
            
        except Exception as e:
            duration = (datetime.now() - start_time).total_seconds() * 1000
            return NodeExecutionResult(
                success=False,
                error=str(e),
                duration_ms=duration,
            )
    
    def _execute_action(self, node: WorkflowNode, context: dict[str, Any]) -> Any:
        """Execute an action node."""
        result = {
            "node_id": node.id,
            "node_name": node.name,
            "tool_name": node.tool_name,
        }
        
        if node.config:
            result.update(node.config)
        
        return result
    
    def _evaluate_decision(self, node: WorkflowNode, context: dict[str, Any]) -> dict[str, Any]:
        """Evaluate a decision node using safe AST-based evaluator."""
        condition = node.config.get("condition", "True")
        
        try:
            result = safe_eval_condition(condition, context)
            return {"decision": result, "condition": condition}
        except Exception:
            return {"decision": False, "condition": condition, "error": "evaluation_failed"}
    
    def can_execute(self, node: WorkflowNode) -> bool:
        """Check if this executor can handle the given node."""
        return node.type in self._supported_types
    
    def get_supported_node_types(self) -> list:
        """Get list of supported node types."""
        return self._supported_types


class ExecutionController(IExecutionController):
    """
    Main execution controller implementation.
    
    Refactored (v1.6):
    - All IDs are strings (session_id, checkpoint_id)
    - Removed AgentGit-specific code paths
    - Added fork() method (moved from SDK)
    
    Orchestrates the execution of workflows with full lifecycle management:
    - Creates executions from workflow definitions
    - Manages run/pause/resume/stop operations
    - Handles breakpoints
    - Supports rollback to checkpoints
    - Persists state through IStateAdapter
    
    SOLID Compliance:
    - SRP: Orchestration logic only, delegates state to adapter
    - OCP: New backends via new IStateAdapter implementations
    - LSP: Any adapter works
    - ISP: Core interface required, extended methods optional
    - DIP: Depends on IStateAdapter abstraction
    """
    
    def __init__(
        self,
        execution_repository: IExecutionRepository,
        workflow_repository: IWorkflowRepository,
        state_adapter: IStateAdapter,
        node_executor: INodeExecutor | None = None,
        unit_of_work: Optional["IUnitOfWork"] = None,
        file_tracking_service: Optional["IFileTrackingService"] = None,
        output_dir: str | None = None,
    ):
        """
        Initialize the execution controller.
        
        Args:
            execution_repository: Repository for executions
            workflow_repository: Repository for workflows
            state_adapter: Adapter for state persistence (v1.6: string IDs)
            node_executor: Optional node executor (defaults to DefaultNodeExecutor)
            unit_of_work: Optional UoW for transaction management (ACID compliance)
            file_tracking_service: Optional file tracking for file rollback
            output_dir: Optional directory for writing output files
        """
        self._exec_repo = execution_repository
        self._workflow_repo = workflow_repository
        self._state_adapter = state_adapter
        self._node_executor = node_executor or DefaultNodeExecutor()
        self._uow = unit_of_work
        self._file_tracking = file_tracking_service
        self._output_dir = output_dir
        self._deferred_commit = False
    
    def set_deferred_commit(self, deferred: bool) -> None:
        """When True, _commit() becomes a no-op so the caller owns the commit."""
        self._deferred_commit = deferred
    
    def _commit(self) -> None:
        """Commit UoW transaction if available (ACID: Durability)."""
        if self._deferred_commit:
            return
        if self._uow is not None:
            self._uow.commit()

    def _sync_external_cache_metadata(self, execution: Execution) -> None:
        """Mirror cache references/hit flags into execution metadata."""
        if execution is None:
            return

        metadata = execution.metadata
        if not isinstance(metadata, dict):
            metadata = {}
            execution.metadata = metadata

        workflow_vars = getattr(execution.state, "workflow_variables", {}) or {}
        for key in ("llm_cache_refs", "llm_cache_hits"):
            value = workflow_vars.get(key)
            if value is not None:
                metadata[key] = copy.deepcopy(value)

        env_fallbacks = (
            ("checkpoint_db_path", "WTB_CHECKPOINT_DB_PATH"),
            ("llm_cache_path", "WTB_LLM_CACHE_PATH"),
            ("actor_id", "WTB_CACHE_ACTOR_ID"),
            ("cache_storage_scope", "WTB_CACHE_STORAGE_SCOPE"),
        )
        for field_name, env_name in env_fallbacks:
            value = metadata.get(field_name) or os.getenv(env_name)
            if value:
                metadata[field_name] = value

        checkpoint_db_path = metadata.get("checkpoint_db_path")
        if checkpoint_db_path:
            adapter_backend = getattr(
                self._state_adapter,
                "state_adapter_backend",
                None,
            )
            allowed_backends = {"langgraph_sqlite", "node_sqlite"}
            if adapter_backend not in allowed_backends:
                raise RuntimeError(
                    "Cannot persist checkpoint_db_path without a recognized "
                    "state_adapter_backend"
                )
            persisted_backend = metadata.get("state_adapter_backend")
            if persisted_backend and persisted_backend != adapter_backend:
                raise RuntimeError(
                    "Execution state_adapter_backend does not match the current "
                    "state adapter"
                )
            metadata["state_adapter_backend"] = adapter_backend

        if (
            "cache_storage_scope" not in metadata
            and (
                metadata.get("checkpoint_db_path")
                or metadata.get("llm_cache_path")
                or metadata.get("actor_id")
            )
        ):
            metadata["cache_storage_scope"] = "actor_local"
    
    def create_execution(
        self, 
        workflow: TestWorkflow,
        initial_state: dict[str, Any] | None = None,
        breakpoints: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        execution_id: str | None = None,
    ) -> Execution:
        """Create a new execution for a workflow."""
        # Validate workflow
        errors = workflow.validate()
        if errors:
            raise ValueError(f"Invalid workflow: {', '.join(errors)}")
        
        # Create initial state
        state = ExecutionState(
            current_node_id=workflow.entry_point,
            workflow_variables=initial_state or {},
            execution_path=[],
            node_results={},
        )
        
        # Create execution
        execution = Execution(
            id=execution_id if execution_id is not None else str(uuid.uuid4()),
            workflow_id=workflow.id,
            status=ExecutionStatus.PENDING,
            state=state,
            breakpoints=breakpoints or [],
            metadata=copy.deepcopy(metadata or {}),
        )

        self._sync_external_cache_metadata(execution)
        
        # Initialize state adapter session (returns session_id string)
        session_id = self._state_adapter.initialize_session(
            execution_id=execution.id,
            initial_state=state,
        )
        execution.session_id = session_id
        
        # Persist execution
        self._exec_repo.add(execution)
        self._commit()
        
        return execution
    
    def run(self, execution_id: str, graph: Any | None = None) -> Execution:
        """
        Start or continue execution.
        
        Capability-based routing (LSP-compliant, no isinstance checks):
        1. If graph provided AND adapter supports LangGraph -> set graph, LangGraph path
        2. If adapter already has a graph (resume scenario) -> reuse, LangGraph path
        3. Otherwise -> DefaultNodeExecutor with WTB workflow nodes
        """
        execution = self._get_execution(execution_id)
        return self._run_execution(execution, graph=graph)

    def _run_execution(
        self,
        execution: Execution,
        graph: Any | None = None,
        *,
        session_activated: bool = False,
    ) -> Execution:
        """Run an already-loaded execution without discarding local updates."""
        if graph is not None and self._supports_langgraph_execution():
            self._state_adapter.set_workflow_graph(graph, force_recompile=True)
            return self._run_with_langgraph(
                execution,
                session_activated=session_activated,
            )
        
        if self._supports_langgraph_execution() and self._state_adapter.has_graph():
            return self._run_with_langgraph(
                execution,
                session_activated=session_activated,
            )
        
        return self._run_with_node_executor(
            execution,
            session_activated=session_activated,
        )
    
    def _supports_langgraph_execution(self) -> bool:
        """Check if state adapter supports LangGraph native execution."""
        return self._state_adapter.supports_graph_execution()

    def _activate_execution_session(self, execution: Execution) -> bool:
        """Activate one execution's adapter session without accepting fail-open False."""
        if not execution.session_id:
            return False

        activated = self._state_adapter.set_current_session(
            execution.session_id,
            execution_id=execution.id,
        )
        return activated is not False

    def _prepare_node_resume_claim_token(
        self,
        execution: Execution,
        *,
        refresh: bool = False,
    ) -> str | None:
        """Return a stable, durable token for one explicit graphless resume."""
        if getattr(
            self._state_adapter,
            "state_adapter_backend",
            None,
        ) != "node_sqlite":
            return None

        metadata = execution.metadata
        if not isinstance(metadata, dict):
            metadata = {}
            execution.metadata = metadata

        token = metadata.get("node_resume_claim_token")
        if refresh:
            token = str(uuid.uuid4())
            prepare_resume = getattr(self._state_adapter, "prepare_resume", None)
            if not callable(prepare_resume):
                raise RuntimeError("node_sqlite adapter cannot prepare resume")
            if prepare_resume(token) is False:
                raise RuntimeError("Could not prepare explicit resume")
        elif not isinstance(token, str) or not token:
            raise RuntimeError(
                "Paused node_sqlite execution has no durable resume token"
            )
        metadata["node_resume_claim_token"] = token
        return token

    @staticmethod
    def _normalized_checkpoint_path(value: Any) -> str:
        """Normalize a declared local checkpoint path without discovering one."""
        try:
            path_value = os.fspath(value)
        except TypeError as error:
            raise RuntimeError("Invalid checkpoint_db_path") from error
        if not path_value:
            raise RuntimeError("Missing checkpoint_db_path")
        return os.path.normcase(
            os.path.abspath(os.path.expanduser(path_value))
        )

    def _reconcile_node_sqlite_recovery(
        self,
        execution: Execution,
    ) -> tuple[bool, bool]:
        """Reconcile one durable node head before any graphless side effect.

        Only PENDING/RUNNING executions that explicitly declare the exact
        ``node_sqlite`` backend and path participate. The result is
        ``(reconciled, terminal)`` so only a proven recovery continuation may
        enter the node loop in persisted RUNNING state.
        """
        if execution.status not in (
            ExecutionStatus.PENDING,
            ExecutionStatus.RUNNING,
        ):
            return False, False

        metadata = execution.metadata
        if not isinstance(metadata, dict):
            return False, False
        if metadata.get("state_adapter_backend") != "node_sqlite":
            return False, False

        declared_path = metadata.get("checkpoint_db_path")
        if not declared_path:
            raise RuntimeError(
                "node_sqlite recovery requires an explicit checkpoint_db_path"
            )
        if getattr(self._state_adapter, "state_adapter_backend", None) != "node_sqlite":
            raise RuntimeError(
                "Declared node_sqlite recovery backend does not match state adapter"
            )
        adapter_path = getattr(self._state_adapter, "storage_path", None)
        if adapter_path is None:
            raise RuntimeError("node_sqlite state adapter has no storage path")
        if self._normalized_checkpoint_path(declared_path) != (
            self._normalized_checkpoint_path(adapter_path)
        ):
            raise RuntimeError(
                "Declared checkpoint_db_path does not match state adapter storage"
            )

        get_recovery_head = getattr(self._state_adapter, "get_recovery_head", None)
        if not callable(get_recovery_head):
            raise RuntimeError("node_sqlite state adapter cannot recover durable state")
        head = get_recovery_head()
        if head is None and execution.status is ExecutionStatus.PENDING:
            return False, False

        terminal = False
        if head is None:
            execution.status = ExecutionStatus.FAILED
            execution.error_message = (
                "Recovery required: persisted running execution has no durable node head"
            )
            execution.error_node_id = execution.state.current_node_id
            execution.completed_at = datetime.now()
            metadata["recovery_required"] = True
            metadata["recovery_reason"] = "recovery_head_missing"
            terminal = True
        elif not isinstance(head, dict):
            raise RuntimeError("node_sqlite recovery head is invalid")
        elif head.get("status") == "completed":
            recovered_state = head.get("state")
            exit_checkpoint_id = head.get("exit_checkpoint_id")
            if not isinstance(recovered_state, ExecutionState) or not exit_checkpoint_id:
                raise RuntimeError(
                    "Completed recovery boundary has no valid owned exit checkpoint"
                )
            execution.state = copy.deepcopy(recovered_state)
            execution.checkpoint_id = str(exit_checkpoint_id)
            execution.status = ExecutionStatus.RUNNING
            execution.error_message = None
            execution.error_node_id = None
            execution.completed_at = None
            if execution.started_at is None and head.get("started_at"):
                try:
                    execution.started_at = datetime.fromisoformat(head["started_at"])
                except (TypeError, ValueError):
                    execution.started_at = datetime.now()
            metadata.pop("recovery_required", None)
            metadata.pop("recovery_reason", None)
        elif head.get("status") == "started":
            node_id = head.get("node_id")
            execution.status = ExecutionStatus.FAILED
            execution.error_message = (
                f"Recovery required: node {node_id} outcome is unknown"
            )
            execution.error_node_id = node_id
            execution.completed_at = datetime.now()
            metadata["recovery_required"] = True
            metadata["recovery_reason"] = "node_outcome_unknown"
            terminal = True
        elif head.get("status") == "failed":
            node_id = head.get("node_id")
            execution.status = ExecutionStatus.FAILED
            execution.error_message = head.get("error_message") or f"Node {node_id} failed"
            execution.error_node_id = node_id
            execution.completed_at = datetime.now()
            metadata.pop("recovery_required", None)
            metadata.pop("recovery_reason", None)
            terminal = True
        else:
            raise RuntimeError("node_sqlite recovery head has invalid status")

        self._sync_external_cache_metadata(execution)
        self._exec_repo.update(execution)
        self._commit()
        return True, terminal
    
    def _run_with_langgraph(
        self,
        execution: Execution,
        *,
        session_activated: bool = False,
    ) -> Execution:
        """
        Execute workflow using LangGraph native execution.
        
        Handles both fresh start (PENDING) and resume (PAUSED):
        - PENDING: execution.start() + adapter.execute(initial_state)
        - PAUSED: execution.resume() + adapter.execute(None) to resume from checkpoint
        
        Graph must already be set on adapter (done by run() before dispatching).
        """
        if (
            not session_activated
            and not self._activate_execution_session(execution)
        ):
            raise RuntimeError("Could not activate execution session")

        try:
            if execution.status == ExecutionStatus.PENDING:
                execution.start()
                initial_state = execution.state.workflow_variables.copy()
                final_state = self._state_adapter.execute(initial_state)
            elif execution.status == ExecutionStatus.PAUSED:
                execution.resume()
                resume_checkpoint_id = (execution.metadata or {}).get("resume_checkpoint_id")
                if resume_checkpoint_id and hasattr(self._state_adapter, "_resume_checkpoint_id"):
                    self._state_adapter._resume_checkpoint_id = resume_checkpoint_id
                final_state = self._state_adapter.execute(None)
                if execution.metadata and "resume_checkpoint_id" in execution.metadata:
                    execution.metadata.pop("resume_checkpoint_id", None)
            else:
                raise RuntimeError(f"Cannot run execution in status {execution.status.value}")
            
            execution.state.workflow_variables = final_state if isinstance(final_state, dict) else {}
            execution.state.node_results["final"] = final_state
            
            if isinstance(final_state, dict):
                if "answer" in final_state:
                    execution.state.workflow_variables["answer"] = final_state["answer"]
                if "messages" in final_state:
                    execution.state.execution_path = final_state.get("messages", [])
            
            if hasattr(self._state_adapter, 'get_config'):
                try:
                    cfg = self._state_adapter.get_config()
                    snap = self._state_adapter._compiled_graph.get_state(cfg)
                    if snap and snap.config:
                        execution.checkpoint_id = snap.config.get(
                            "configurable", {}
                        ).get("checkpoint_id")
                except Exception:
                    pass

            if self._file_tracking and self._file_tracking.is_available():
                self._track_checkpoint_history_output_files(execution, final_state)
            
            execution.complete()
            
        except Exception as e:
            logger.error(f"LangGraph execution failed: {e}")
            if execution.status == ExecutionStatus.RUNNING:
                execution.fail(str(e), execution.state.current_node_id)
            elif execution.status not in (
                ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED
            ):
                execution.status = ExecutionStatus.FAILED
                execution.error_message = str(e)
        
        self._sync_external_cache_metadata(execution)
        self._exec_repo.update(execution)
        self._commit()
        
        return execution
    
    def _track_checkpoint_history_output_files(self, execution: Execution, final_state: Any) -> None:
        """Link every checkpoint's output files to a CAS commit."""
        if not isinstance(final_state, dict):
            return

        linked_count = 0
        history = []
        get_history = getattr(self._state_adapter, "get_checkpoint_history", None)
        if callable(get_history):
            try:
                history = get_history()
            except Exception as e:
                logger.warning(f"Could not read checkpoint history for file tracking: {e}")

        checkpoints = []
        for cp in history or []:
            checkpoint_id = cp.get("checkpoint_id") or cp.get("id")
            values = cp.get("values") or {}
            output_files = values.get("_output_files")
            step = cp.get("step", 0)
            if checkpoint_id and isinstance(output_files, dict) and output_files:
                checkpoints.append((step, checkpoint_id, output_files))

        checkpoints.sort(key=lambda item: item[0])
        for _, checkpoint_id, output_files in checkpoints:
            if self._get_commit_for_checkpoint(checkpoint_id):
                continue
            written_paths = self._write_output_files(output_files)
            if not written_paths:
                raise RuntimeError(f"Checkpoint {checkpoint_id} has _output_files but no files were written")
            result = self._track_paths_for_checkpoint(
                checkpoint_id=checkpoint_id,
                file_paths=written_paths,
                message=f"Checkpoint {checkpoint_id} output files",
            )
            if not result or not getattr(result, "commit_id", None):
                raise RuntimeError(f"Failed to link files for checkpoint {checkpoint_id}")
            linked_count += 1
        
        output_files_data = final_state.get("_output_files", {})
        if not isinstance(output_files_data, dict):
            output_files_data = {}
        
        # Auto-save common output fields
        if "answer" in final_state and "answer.txt" not in output_files_data:
            answer = final_state["answer"]
            if isinstance(answer, str) and answer.strip():
                output_files_data["answer.txt"] = answer
        
        if "result" in final_state and "result.json" not in output_files_data:
            result = final_state["result"]
            if result is not None:
                output_files_data["result.json"] = result
        
        if not output_files_data:
            return

        written_paths = self._write_output_files(output_files_data)
        if not written_paths:
            raise RuntimeError("Final state has _output_files but no files were written")

        commit_id = self._get_commit_for_checkpoint(execution.checkpoint_id)
        if not commit_id:
            tracking_result = self._track_paths_for_checkpoint(
                    checkpoint_id=execution.checkpoint_id,
                    file_paths=written_paths,
                    message=f"Execution {execution.id} output files",
                )
            commit_id = getattr(tracking_result, "commit_id", None)
        else:
            tracking_result = None

        if not commit_id:
            raise RuntimeError(f"Final checkpoint {execution.checkpoint_id} has no linked file commit")

        execution.state.workflow_variables["_file_tracking_result"] = {
            "commit_id": commit_id,
            "files_tracked": getattr(tracking_result, "files_tracked", len(written_paths)),
            "total_size_bytes": getattr(tracking_result, "total_size_bytes", 0),
            "linked_checkpoints": linked_count,
        }

        logger.info(
            f"Linked output files for {linked_count} checkpoints "
            f"for execution {execution.id}"
        )

    def _write_output_files(self, output_files_data: dict[str, Any]) -> list[str]:
        """Materialize _output_files under output_dir and return absolute paths."""
        import json
        from pathlib import Path

        if not output_files_data:
            return []

        output_dir = (
            Path(self._output_dir) if self._output_dir else Path("outputs")
        ).resolve()
        validated_outputs: list[tuple[Path, Any, bool]] = []
        for filename, content in output_files_data.items():
            if not isinstance(filename, str) or not filename.strip():
                raise ValueError(f"Invalid output file path: {filename!r}")

            relative_path = Path(filename)
            if (
                relative_path.is_absolute()
                or relative_path.drive
                or relative_path.root
                or ".." in relative_path.parts
            ):
                raise ValueError(f"Unsafe output file path: {filename}")

            file_path = (output_dir / relative_path).resolve()
            try:
                file_path.relative_to(output_dir)
            except ValueError as exc:
                raise ValueError(f"Unsafe output file path: {filename}") from exc
            if file_path == output_dir:
                raise ValueError(f"Unsafe output file path: {filename}")

            if isinstance(content, bytes):
                encoded_content, is_binary = content, True
            elif isinstance(content, str):
                encoded_content, is_binary = content, False
            else:
                encoded_content = json.dumps(
                    content, indent=2, ensure_ascii=False
                )
                is_binary = False
            validated_outputs.append((file_path, encoded_content, is_binary))

        output_dir.mkdir(parents=True, exist_ok=True)

        written_paths: list[str] = []
        for file_path, content, is_binary in validated_outputs:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            if is_binary:
                file_path.write_bytes(content)
            else:
                file_path.write_text(content, encoding="utf-8")
            written_paths.append(str(file_path))

        return written_paths

    def _track_paths_for_checkpoint(
        self,
        checkpoint_id: str | None,
        file_paths: list[str],
        message: str | None = None,
    ):
        """Track file paths and link the CAS commit to checkpoint_id."""
        if not self._file_tracking or not file_paths:
            return None

        if checkpoint_id and hasattr(self._file_tracking, "track_and_link"):
            return self._file_tracking.track_and_link(
                checkpoint_id=checkpoint_id,
                file_paths=file_paths,
                message=message,
            )

        result = self._file_tracking.track_files(file_paths=file_paths, message=message)
        if checkpoint_id and result and getattr(result, "commit_id", None):
            link_fn = getattr(self._file_tracking, "link_to_checkpoint", None)
            if callable(link_fn):
                link_fn(checkpoint_id, result.commit_id)
        return result

    def _get_commit_for_checkpoint(self, checkpoint_id: str | None) -> str | None:
        if not checkpoint_id or not self._file_tracking:
            return None
        get_commit = getattr(self._file_tracking, "get_commit_for_checkpoint", None)
        if not callable(get_commit):
            return None
        try:
            return get_commit(checkpoint_id)
        except Exception:
            return None

    @staticmethod
    def _checkpoint_has_output_files(state: Any) -> bool:
        """Return whether a checkpoint state declares materialized files."""
        if isinstance(state, ExecutionState):
            values = state.workflow_variables
        elif isinstance(state, dict):
            values = state
        else:
            return False

        output_files = values.get("_output_files") if isinstance(values, dict) else None
        return isinstance(output_files, dict) and bool(output_files)

    def _require_checkpoint_file_commit(
        self,
        checkpoint_id: str | None,
    ) -> str | None:
        """Require checkpoint_id to have a CAS file commit."""
        if not checkpoint_id or not self._file_tracking or not self._file_tracking.is_available():
            return None

        existing = self._get_commit_for_checkpoint(checkpoint_id)
        if existing:
            return existing

        raise RuntimeError(
            f"Checkpoint {checkpoint_id} has no linked file commit. "
            "Files must be linked with track_and_link at checkpoint creation time."
        )
    
    def _run_with_node_executor(
        self,
        execution: Execution,
        *,
        session_activated: bool = False,
    ) -> Execution:
        """Legacy execution using DefaultNodeExecutor with WTB workflow nodes."""
        if (
            not session_activated
            and not self._activate_execution_session(execution)
        ):
            raise RuntimeError("Could not activate execution session")

        recovery_reconciled, recovery_terminal = (
            self._reconcile_node_sqlite_recovery(execution)
        )
        if recovery_terminal:
            return execution

        resume_claim_id: str | None = None
        if execution.status == ExecutionStatus.PAUSED:
            resume_claim_token = self._prepare_node_resume_claim_token(execution)
            if resume_claim_token is not None:
                claim_resume = getattr(self._state_adapter, "claim_resume", None)
                if not callable(claim_resume):
                    raise RuntimeError("node_sqlite adapter cannot claim resume")
                claimed_resume = claim_resume(resume_claim_token)
                if not claimed_resume:
                    raise NodeBoundaryClaimConflict(
                        "Could not claim explicit resume for execution"
                    )
                resume_claim_id = str(claimed_resume)
                if isinstance(execution.metadata, dict):
                    execution.metadata.pop("node_resume_claim_token", None)

        workflow = self._get_workflow(execution.workflow_id)
        
        # Start execution if pending
        if execution.status == ExecutionStatus.PENDING:
            execution.start()
        elif execution.status == ExecutionStatus.PAUSED:
            execution.resume()
        elif execution.status == ExecutionStatus.RUNNING and recovery_reconciled:
            pass
        else:
            raise RuntimeError(f"Cannot run execution in status {execution.status.value}")
        
        # Main execution loop
        try:
            while execution.status == ExecutionStatus.RUNNING:
                current_node_id = execution.state.current_node_id
                
                if not current_node_id:
                    execution.complete()
                    break
                
                # Check for breakpoint
                if execution.is_at_breakpoint():
                    self._create_checkpoint(
                        execution, 
                        current_node_id,
                        f"Breakpoint: {current_node_id}"
                    )
                    execution.remove_breakpoint(current_node_id)
                    self._prepare_node_resume_claim_token(execution, refresh=True)
                    execution.pause()
                    break
                
                # Get node
                node = workflow.get_node(current_node_id)
                if not node:
                    raise ValueError(f"Node {current_node_id} not found in workflow")
                
                # Create checkpoint before node execution
                expected_predecessor_checkpoint_id = execution.checkpoint_id
                entry_cp_id = self._create_checkpoint(
                    execution,
                    current_node_id,
                    f"Before: {current_node_id}"
                )
                
                # Mark node as started
                if getattr(
                    self._state_adapter,
                    "state_adapter_backend",
                    None,
                ) == "node_sqlite":
                    boundary_id = self._state_adapter.mark_node_started(
                        current_node_id,
                        entry_cp_id,
                        expected_predecessor_checkpoint_id=(
                            expected_predecessor_checkpoint_id
                        ),
                        enforce_predecessor=True,
                        resume_claim_id=resume_claim_id,
                    )
                else:
                    boundary_id = self._state_adapter.mark_node_started(
                        current_node_id,
                        entry_cp_id,
                    )
                if not boundary_id:
                    raise NodeBoundaryClaimConflict(
                        f"Could not claim node {current_node_id} for execution"
                    )
                resume_claim_id = None
                
                # Execute node
                result = self._node_executor.execute(
                    node=node,
                    context=execution.state.workflow_variables,
                )
                
                if not result.success:
                    self._state_adapter.mark_node_failed(current_node_id, result.error or "Unknown error")
                    execution.fail(result.error or "Node execution failed", current_node_id)
                    break
                
                # Update state
                execution.state.execution_path.append(current_node_id)
                execution.state.node_results[current_node_id] = result.output
                
                if isinstance(result.output, dict):
                    execution.state.workflow_variables.update(result.output)

                # The durable after-node checkpoint represents continuation
                # from the successor, not re-entry into the completed node.
                next_node_id = self._determine_next_node(
                    workflow,
                    execution,
                    result.output,
                )
                checkpoint_state = copy.deepcopy(execution.state)
                checkpoint_state.current_node_id = next_node_id

                # Create checkpoint after node execution
                exit_cp_id = self._create_checkpoint(
                    execution,
                    current_node_id,
                    f"After: {current_node_id}",
                    state=checkpoint_state,
                )
                
                # Mark node as completed
                boundary_completed = self._state_adapter.mark_node_completed(
                    current_node_id,
                    exit_cp_id,
                )
                if boundary_completed is False:
                    raise RuntimeError(
                        f"Could not mark node {current_node_id} as completed"
                    )
                
                execution.state.current_node_id = next_node_id
        
        except NodeBoundaryClaimConflict:
            raise
        except Exception as e:
            execution.fail(str(e), execution.state.current_node_id)
        
        # Persist final state
        self._sync_external_cache_metadata(execution)
        self._exec_repo.update(execution)
        self._commit()
        
        return execution
    
    def pause(self, execution_id: str) -> Execution:
        """Pause execution at current position."""
        execution = self._get_execution(execution_id)
        
        if not execution.can_pause():
            raise ValueError(f"Cannot pause execution in status {execution.status.value}")

        if not self._activate_execution_session(execution):
            raise RuntimeError("Could not activate execution session")
        
        self._create_checkpoint(
            execution,
            execution.state.current_node_id or "unknown",
            "Manual Pause"
        )
        self._prepare_node_resume_claim_token(execution, refresh=True)
        
        execution.pause()
        self._exec_repo.update(execution)
        self._commit()
        
        return execution
    
    def resume(
        self, 
        execution_id: str, 
        modified_state: dict[str, Any] | None = None
    ) -> Execution:
        """Resume paused execution."""
        execution = self._get_execution(execution_id)

        metadata = execution.metadata or {}
        pending_checkpoint_fork = (
            execution.status == ExecutionStatus.PENDING
            and metadata.get("fork_type") == "checkpoint_fork"
        )
        if not execution.can_resume() and not pending_checkpoint_fork:
            raise ValueError(
                f"Cannot resume execution in status {execution.status.value}"
            )

        # Session selection is the first external preflight. It must succeed
        # before checkpoint files, graph state, or the aliased domain object
        # can be changed.
        if not self._activate_execution_session(execution):
            raise RuntimeError("Could not activate resume session")

        source_has_output_files = metadata.get("source_checkpoint_has_output_files")
        if source_has_output_files is None:
            source_has_output_files = self._checkpoint_has_output_files(execution.state)

        # Restore checkpoint-linked files before mutating the execution. Some
        # repositories return their stored domain instance directly, so an
        # early status/state update would otherwise leak through a failed CAS
        # restore even without an explicit repository update or commit.
        restore_status = None
        source_checkpoint_id = metadata.get("source_checkpoint_id")
        if (
            source_checkpoint_id
            and metadata.get("fork_type") == "checkpoint_fork"
            and source_has_output_files
            and self._file_tracking
            and self._file_tracking.is_available()
            and self._output_dir
        ):
            restore_status = self._restore_checkpoint_files(
                source_checkpoint_id,
                execution.state,
                has_output_files=True,
            )

        state_updates = dict(modified_state or {})
        if restore_status is not None:
            state_updates["_file_restore_status"] = restore_status

        # A LangGraph resume must update the checkpoint for this execution's
        # thread, not whichever session happens to be active on the adapter.
        graph_resume = (
            self._supports_langgraph_execution()
            and self._state_adapter.has_graph()
        )
        if graph_resume:
            if state_updates:
                update_state = getattr(self._state_adapter, "update_state", None)
                if not callable(update_state):
                    raise RuntimeError("State adapter cannot apply resume state")
                updated = update_state(
                    dict(state_updates),
                    as_node=metadata.get("source_checkpoint_as_node"),
                )
                if updated is False:
                    raise RuntimeError("Could not apply resume state")

        # Only mutate the domain object after every external preflight has
        # succeeded. A checkpoint fork is persisted as PENDING by older stores
        # but must enter the ordinary PAUSED -> RUNNING resume transition.
        if pending_checkpoint_fork:
            execution.status = ExecutionStatus.PAUSED
        if state_updates:
            execution.state.workflow_variables.update(state_updates)

        return self._run_execution(execution, session_activated=True)
    
    def stop(self, execution_id: str) -> Execution:
        """Stop and cancel execution."""
        execution = self._get_execution(execution_id)
        
        execution.cancel()
        self._exec_repo.update(execution)
        self._commit()
        
        return execution
    
    def rollback(self, execution_id: str, checkpoint_id: str) -> Execution:
        """
        Rollback to a previous checkpoint.
        
        Uses Execution.restore_from_checkpoint() for proper domain validation.
        
        Args:
            execution_id: Execution to rollback
            checkpoint_id: Checkpoint ID (UUID string)
        """
        execution = self._get_execution(execution_id)

        if not self._activate_execution_session(execution):
            raise RuntimeError("Could not activate execution session")

        # Load and restore checkpoint-linked files before moving the adapter's
        # active checkpoint. A CAS/link failure must leave adapter state intact.
        preflight_state = self._state_adapter.load_checkpoint(checkpoint_id)
        if isinstance(preflight_state, dict):
            preflight_state = ExecutionState(
                current_node_id=preflight_state.get("current_node_id"),
                workflow_variables=dict(preflight_state),
                execution_path=preflight_state.get("execution_path", []),
                node_results=preflight_state.get("node_results", {}),
            )
        restore_status = None
        if self._file_tracking and self._file_tracking.is_available() and self._output_dir:
            restore_status = self._restore_checkpoint_files(
                checkpoint_id,
                preflight_state,
            )

        restored_state = self._state_adapter.rollback(checkpoint_id)

        # Type normalization: ensure ExecutionState for domain model
        if isinstance(restored_state, dict):
            restored_state = ExecutionState(
                current_node_id=restored_state.get("current_node_id"),
                workflow_variables=dict(restored_state),
                execution_path=restored_state.get("execution_path", []),
                node_results=restored_state.get("node_results", {}),
            )

        # Domain model validates transition, clones state, clears errors, sets PAUSED
        execution.restore_from_checkpoint(restored_state)
        if restore_status is not None:
            execution.state.workflow_variables["_file_restore_status"] = restore_status
        execution.checkpoint_id = checkpoint_id
        execution.metadata = dict(execution.metadata or {})
        execution.metadata["resume_checkpoint_id"] = checkpoint_id
        self._prepare_node_resume_claim_token(execution, refresh=True)
        self._sync_external_cache_metadata(execution)
        
        self._exec_repo.update(execution)
        self._commit()
        
        return execution
    
    def _restore_checkpoint_files(
        self,
        checkpoint_id: str,
        source_state: ExecutionState,
        *,
        has_output_files: bool | None = None,
    ) -> dict[str, Any] | None:
        """Restore checkpoint files via required CAS checkpoint link."""
        if has_output_files is None:
            has_output_files = self._checkpoint_has_output_files(source_state)
        if not has_output_files:
            return None

        self._require_checkpoint_file_commit(checkpoint_id)
        restore_fn = getattr(self._file_tracking, "restore_from_checkpoint", None)
        if not callable(restore_fn):
            raise RuntimeError("File tracking service does not support restore_from_checkpoint")

        result = restore_fn(checkpoint_id)
        files_restored_count = getattr(result, "files_restored", 0)
        file_restore_error = getattr(result, "error_message", None)
        if file_restore_error:
            raise RuntimeError(file_restore_error)

        values = (
            source_state.workflow_variables
            if isinstance(source_state, ExecutionState)
            else source_state
        )
        output_files = values.get("_output_files", {}) if isinstance(values, dict) else {}
        expected_file_count = (
            len(output_files)
            if isinstance(output_files, dict) and output_files
            else None
        )
        if files_restored_count <= 0:
            expected_description = expected_file_count or "at least 1"
            raise RuntimeError(
                f"Expected {expected_description} checkpoint files to be restored, "
                f"but restored {files_restored_count}"
            )
        if expected_file_count is not None and files_restored_count != expected_file_count:
            raise RuntimeError(
                f"Expected {expected_file_count} checkpoint files to be restored, "
                f"but restored {files_restored_count}"
            )

        return {
            "attempted": True,
            "success": True,
            "files_restored": files_restored_count,
            "error": None,
        }
    
    def fork(
        self,
        execution_id: str,
        checkpoint_id: str,
        new_initial_state: dict[str, Any] | None = None,
    ) -> Execution:
        """Atomically fork while always restoring the source adapter session."""
        source_execution = self._get_execution(execution_id)
        source_session_id = source_execution.session_id
        fork_execution_id = str(uuid.uuid4())
        forked_execution: Execution | None = None
        operation_error: Exception | None = None

        try:
            forked_execution = self._fork_impl(
                execution_id,
                checkpoint_id,
                new_initial_state,
                fork_execution_id=fork_execution_id,
            )
        except Exception as exc:
            operation_error = exc
            rollback = getattr(self._uow, "rollback", None)
            if callable(rollback):
                try:
                    rollback()
                except Exception as rollback_error:
                    logger.warning(f"Could not roll back failed fork: {rollback_error}")

            try:
                self._exec_repo.delete(fork_execution_id)
                self._commit()
            except Exception as cleanup_error:
                logger.warning(f"Could not remove failed fork record: {cleanup_error}")
            raise
        finally:
            if source_session_id:
                try:
                    restored = self._state_adapter.set_current_session(
                        source_session_id,
                        execution_id=execution_id,
                    )
                    if restored is False:
                        raise RuntimeError("Could not restore source session")
                except Exception as restore_error:
                    if forked_execution is not None:
                        try:
                            self._exec_repo.delete(fork_execution_id)
                            self._commit()
                        except Exception as cleanup_error:
                            logger.error(
                                "Could not remove fork after source session "
                                f"restore failure: {cleanup_error}"
                            )
                    if operation_error is not None:
                        operation_error.add_note(
                            "Additionally failed to restore source session: "
                            f"{restore_error}"
                        )
                        logger.error(
                            f"Failed to restore source session: {restore_error}"
                        )
                    else:
                        raise

        if forked_execution is None:
            raise RuntimeError("Fork did not produce an execution")
        return forked_execution

    def _fork_impl(
        self,
        execution_id: str,
        checkpoint_id: str,
        new_initial_state: dict[str, Any] | None = None,
        *,
        fork_execution_id: str,
    ) -> Execution:
        """
        Fork an execution to create a new independent execution.
        
        Creates a new Execution record starting from the checkpoint state,
        with an isolated session. The new execution is persisted.
        
        Args:
            execution_id: Source execution ID
            checkpoint_id: Checkpoint ID to fork from (UUID string)
            new_initial_state: Optional state to merge with checkpoint state
            
        Returns:
            New forked Execution
            
        ACID Compliance:
        - Atomicity: New execution created in single transaction
        - Consistency: Execution state matches checkpoint + new_initial_state
        - Isolation: New session isolates forked execution
        - Durability: Persisted to database
        """
        source_execution = self._get_execution(execution_id)
        
        workflow = self._workflow_repo.get(source_execution.workflow_id)
        if not workflow:
            raise ValueError(f"Workflow '{source_execution.workflow_id}' not found")
        
        if not self._activate_execution_session(source_execution):
            raise RuntimeError("Could not activate execution session")
        
        # Load checkpoint state
        checkpoint_state = self._state_adapter.load_checkpoint(checkpoint_id)
        if (
            self._checkpoint_has_output_files(checkpoint_state)
            and self._file_tracking
            and self._file_tracking.is_available()
        ):
            self._require_checkpoint_file_commit(checkpoint_id)
        fork_as_node: str | None = None
        initial_input_state: dict[str, Any] | None = None
        graph_execution = self._state_adapter.supports_graph_execution()
        if graph_execution:
            checkpoint_history = self._state_adapter.get_checkpoint_history()
            source_checkpoint = next(
                (
                    item
                    for item in checkpoint_history
                    if (item.get("checkpoint_id") or item.get("id"))
                    == checkpoint_id
                ),
                None,
            )
            if source_checkpoint is None:
                raise RuntimeError(
                    f"Cannot preserve continuation for checkpoint {checkpoint_id}"
                )
            writes = source_checkpoint.get("writes")
            writer_nodes = list(writes) if isinstance(writes, dict) else []
            if len(writer_nodes) == 1:
                fork_as_node = writer_nodes[0]
            else:
                source_step = source_checkpoint.get("step")
                preceding_checkpoints = [
                    item
                    for item in checkpoint_history
                    if isinstance(source_step, int)
                    and isinstance(item.get("step"), int)
                    and item["step"] < source_step
                ]
                if preceding_checkpoints:
                    parent_checkpoint = max(
                        preceding_checkpoints,
                        key=lambda item: item["step"],
                    )
                    scheduled_nodes = list(
                        parent_checkpoint.get("next") or []
                    )
                    if len(scheduled_nodes) == 1:
                        fork_as_node = scheduled_nodes[0]
                    else:
                        remaining_nodes = set(
                            source_checkpoint.get("next") or []
                        )
                        completed_nodes = [
                            node_id
                            for node_id in scheduled_nodes
                            if node_id not in remaining_nodes
                        ]
                        if len(completed_nodes) == 1:
                            fork_as_node = completed_nodes[0]
            if (
                fork_as_node is None
                and source_checkpoint.get("step") == -1
                and list(source_checkpoint.get("next") or []) == ["__start__"]
            ):
                step_zero_checkpoints = [
                    item
                    for item in checkpoint_history
                    if item.get("step") == 0
                ]
                if (
                    len(step_zero_checkpoints) != 1
                    or not isinstance(step_zero_checkpoints[0].get("values"), dict)
                ):
                    raise RuntimeError(
                        f"Cannot preserve initial state for checkpoint {checkpoint_id}"
                    )
                # LangGraph's canonical input checkpoint has no writer. Treat
                # its virtual start task as the writer so a fork resumes at
                # the graph entry point without broadening ambiguous cases.
                initial_input_state = copy.deepcopy(
                    step_zero_checkpoints[0]["values"]
                )
                fork_as_node = "__start__"
            if fork_as_node is None:
                raise RuntimeError(
                    f"Checkpoint {checkpoint_id} has ambiguous continuation metadata"
                )

        if graph_execution:
            # LangGraph owns continuation in its forked thread. The domain
            # execution mirrors workflow values while the graph schedules the
            # successor from checkpoint metadata.
            if initial_input_state is not None:
                forked_state_vars = copy.deepcopy(initial_input_state)
            elif isinstance(checkpoint_state, ExecutionState):
                forked_state_vars = copy.deepcopy(
                    checkpoint_state.workflow_variables
                )
            elif isinstance(checkpoint_state, dict):
                forked_state_vars = copy.deepcopy(checkpoint_state)
            else:
                forked_state_vars = {}

            if new_initial_state:
                forked_state_vars.update(copy.deepcopy(new_initial_state))

            forked_exec_state = ExecutionState(
                current_node_id=workflow.entry_point,
                workflow_variables=forked_state_vars,
                execution_path=[],
                node_results={},
            )
        else:
            if not isinstance(checkpoint_state, ExecutionState):
                raise TypeError(
                    "Node checkpoint must restore a complete ExecutionState"
                )
            forked_exec_state = copy.deepcopy(checkpoint_state)
            if new_initial_state:
                forked_exec_state.workflow_variables.update(
                    copy.deepcopy(new_initial_state)
                )
        
        # Seed forked LangGraph thread with source checkpoint state so that
        # time-travel / resume on the fork has the correct checkpoint history.
        # Must happen while adapter is still pointed at the source thread.
        fork_thread_id = f"wtb-{fork_execution_id}"
        _create_fork = getattr(self._state_adapter, "create_fork", None)
        if graph_execution:
            if not callable(_create_fork):
                raise RuntimeError("State adapter does not support checkpoint forks")
            _create_fork(fork_thread_id, from_checkpoint_id=checkpoint_id)
        
        # Create new execution record. A checkpoint fork is resumable from the
        # forked LangGraph thread, so it starts PAUSED rather than PENDING.
        forked_execution = Execution(
            id=fork_execution_id,
            workflow_id=source_execution.workflow_id,
            status=ExecutionStatus.PAUSED,
            state=forked_exec_state,
            metadata=copy.deepcopy(source_execution.metadata or {}),
        )
        forked_execution.metadata.pop("requested_execution_id", None)
        
        # Initialize session for new execution
        session_id = self._state_adapter.initialize_session(
            execution_id=fork_execution_id,
            initial_state=forked_exec_state,
        )
        forked_execution.session_id = session_id

        # Attribute the fork checkpoint to the source writer so LangGraph
        # schedules only the original successor. Normal checkpoints overlay
        # only caller changes to avoid replaying reducers; the input checkpoint
        # must seed the original input values that live in its step-zero child.
        if graph_execution:
            update_state = getattr(self._state_adapter, "update_state", None)
            if not callable(update_state):
                raise RuntimeError("State adapter does not support fork state updates")
            try:
                state_update = dict(new_initial_state or {})
                if initial_input_state is not None:
                    state_update = copy.deepcopy(forked_state_vars)
                updated = update_state(
                    state_update,
                    as_node=fork_as_node,
                )
                if updated is False:
                    raise RuntimeError("Could not establish fork continuation state")
            except Exception:
                raise

        forked_execution.metadata.update(
            {
                "forked_from": execution_id,
                "source_checkpoint_id": checkpoint_id,
                "source_checkpoint_has_output_files": (
                    self._checkpoint_has_output_files(checkpoint_state)
                ),
                "source_checkpoint_as_node": fork_as_node,
                "fork_type": "checkpoint_fork",
            }
        )
        self._prepare_node_resume_claim_token(forked_execution, refresh=True)
        self._sync_external_cache_metadata(forked_execution)
        
        # Persist forked execution
        self._exec_repo.add(forked_execution)
        self._commit()
        
        logger.info(f"Forked execution {fork_execution_id} from {execution_id} at checkpoint {checkpoint_id[:8]}...")
        
        return forked_execution
    
    def get_state(self, execution_id: str) -> ExecutionState:
        """Get current execution state."""
        execution = self._get_execution(execution_id)
        return execution.state
    
    def get_status(self, execution_id: str) -> Execution:
        """Get execution with current status."""
        return self._get_execution(execution_id)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Private Methods
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _get_execution(self, execution_id: str) -> Execution:
        """Get execution or raise ValueError."""
        execution = self._exec_repo.get(execution_id)
        if not execution:
            raise ValueError(f"Execution {execution_id} not found")
        return execution
    
    def _get_workflow(self, workflow_id: str) -> TestWorkflow:
        """Get workflow or raise ValueError."""
        workflow = self._workflow_repo.get(workflow_id)
        if not workflow:
            raise ValueError(f"Workflow {workflow_id} not found")
        return workflow
    
    def _create_checkpoint(
        self, 
        execution: Execution,
        node_id: str,
        name: str,
        state: ExecutionState | None = None,
    ) -> str:
        """Create a checkpoint via state adapter. Returns checkpoint_id (string)."""
        checkpoint_id = self._state_adapter.save_checkpoint(
            state=state if state is not None else execution.state,
            node_id=node_id,
            trigger=CheckpointTrigger.AUTO,
            name=name,
            metadata={
                "execution_id": execution.id,
            }
        )
        execution.checkpoint_id = checkpoint_id
        return checkpoint_id
    
    def _determine_next_node(
        self, 
        workflow: TestWorkflow,
        execution: Execution,
        last_result: Any
    ) -> str | None:
        """Determine the next node based on edges and conditions."""
        current_node_id = execution.state.current_node_id
        if not current_node_id:
            return None
        
        edges = workflow.get_outgoing_edges(current_node_id)
        
        for edge in edges:
            if edge.condition is None:
                return edge.target_id
            
            condition_result = self._evaluate_condition(
                edge.condition,
                execution.state.workflow_variables,
                last_result
            )
            
            if condition_result:
                return edge.target_id
        
        return None
    
    def _evaluate_condition(
        self, 
        condition: str,
        variables: dict[str, Any],
        last_result: Any
    ) -> bool:
        """Evaluate an edge condition using safe AST-based evaluator."""
        context = {**variables, "_last_result": last_result}
        try:
            return safe_eval_condition(condition, context)
        except Exception:
            return False
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Extended Capabilities
    # ═══════════════════════════════════════════════════════════════════════════
    
    def supports_time_travel(self) -> bool:
        """Check if the adapter supports time-travel."""
        return self._state_adapter.supports_time_travel()
    
    def supports_streaming(self) -> bool:
        """Check if the adapter supports event streaming."""
        return self._state_adapter.supports_streaming()
    
    def get_checkpoint_history(self, execution_id: str) -> list[dict[str, Any]]:
        """Get full checkpoint history for time-travel."""
        execution = self._get_execution(execution_id)
        
        if not self._activate_execution_session(execution):
            return []
        
        return self._state_adapter.get_checkpoint_history()
    
    def update_execution_state(
        self, 
        execution_id: str, 
        values: dict[str, Any]
    ) -> bool:
        """Update execution state mid-execution (human-in-the-loop)."""
        execution = self._get_execution(execution_id)
        
        if not self._activate_execution_session(execution):
            return False
        
        if self._state_adapter.update_state(values):
            execution.state.workflow_variables.update(values)
            self._exec_repo.update(execution)
            self._commit()
            return True
        return False
    
    def rollback_to_node(self, execution_id: str, node_id: str) -> Execution:
        """Rollback to after a specific node completed."""
        if not self.supports_time_travel():
            raise ValueError("Adapter does not support time-travel")
        
        execution = self._get_execution(execution_id)
        
        if not self._activate_execution_session(execution):
            raise RuntimeError("Could not activate execution session")
        
        # Find checkpoint for this node
        history = self._state_adapter.get_checkpoint_history()
        
        for checkpoint in history:
            writes = checkpoint.get("writes", {})
            if node_id in writes:
                boundary = self._state_adapter.get_node_boundary(
                    execution.session_id or "", 
                    node_id
                )
                if boundary and boundary.exit_checkpoint_id:
                    return self.rollback(execution_id, boundary.exit_checkpoint_id)
        
        raise ValueError(f"Node {node_id} not found in checkpoint history")
