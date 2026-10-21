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
import inspect
import json
import logging
import operator
import os
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from wtb.domain.interfaces.execution_controller import IExecutionController
from wtb.domain.interfaces.node_executor import INodeExecutor, NodeExecutionResult
from wtb.domain.interfaces.repositories import IExecutionRepository, IWorkflowRepository
from wtb.domain.interfaces.state_adapter import CheckpointTrigger, IStateAdapter
from wtb.domain.interfaces.workflow_runtime import (
    RuntimeAttemptPlan,
    RuntimeBindingEnvelope,
    RuntimeOutcomeReceipt,
    RuntimeOutcomeUnknownError,
    RuntimePreDispatchError,
    RuntimeTerminalError,
    RuntimeTerminalOutcome,
    WorkflowRuntime,
)
from wtb.domain.models import (
    Execution,
    ExecutionState,
    ExecutionStatus,
    TestWorkflow,
    WorkflowNode,
)
from wtb.domain.models.outbox import OutboxEvent, OutboxEventType
from wtb.domain.runtime_identity import canonical_json_sha256

if TYPE_CHECKING:
    from wtb.domain.interfaces.file_tracking import IFileTrackingService
    from wtb.domain.interfaces.unit_of_work import IUnitOfWork

logger = logging.getLogger(__name__)


class NodeBoundaryClaimConflict(RuntimeError):
    """A competing runner owns or has advanced the durable node boundary."""


class RuntimeAttemptClaimConflict(RuntimeError):
    """A competing operator owns or advanced the durable runtime attempt."""


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
            if node.config.get("_wtb_native_callable_required"):
                raise RuntimeError(
                    f"Native workflow node {node.id!r} requires its graph code; "
                    "attach the WorkflowProject before resume or inject a "
                    "framework-neutral WorkflowRuntime"
                )
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


class NativeGraphNodeExecutor(INodeExecutor):
    """Execute callables exposed by a framework-neutral graph-like object.

    WTB's native graph contract is deliberately small: ``graph.nodes`` is a
    mapping from node ID to a synchronous callable accepting a copied state
    mapping and returning a mapping of state updates. Nodes without a callable
    delegate to the configured fallback executor.
    """

    def __init__(self, graph: Any, fallback: INodeExecutor):
        self._graph = graph
        self._fallback = fallback

    def _handler(self, node_id: str) -> Callable | None:
        nodes = getattr(self._graph, "nodes", None)
        if not isinstance(nodes, Mapping):
            return None
        candidate = nodes.get(node_id)
        if callable(candidate):
            return candidate
        for attribute in ("handler", "action", "func", "runnable"):
            value = getattr(candidate, attribute, None)
            if callable(value):
                return value
        return None

    def execute(
        self,
        node: WorkflowNode,
        context: dict[str, Any],
    ) -> NodeExecutionResult:
        handler = self._handler(node.id)
        if handler is None:
            return self._fallback.execute(node, context)

        started_at = datetime.now()
        try:
            output = handler(copy.deepcopy(context))
            if inspect.isawaitable(output):
                close = getattr(output, "close", None)
                if callable(close):
                    close()
                raise TypeError(
                    "Native workflow node callables must be synchronous; "
                    "use an async WorkflowRuntime for awaitable nodes"
                )
            if output is None:
                output = {}
            if not isinstance(output, Mapping):
                raise TypeError(
                    f"Native workflow node {node.id!r} must return a mapping"
                )
            duration_ms = (datetime.now() - started_at).total_seconds() * 1000
            return NodeExecutionResult(
                success=True,
                output=dict(output),
                duration_ms=duration_ms,
                tool_invocations=1,
            )
        except Exception as error:
            duration_ms = (datetime.now() - started_at).total_seconds() * 1000
            return NodeExecutionResult(
                success=False,
                error=str(error),
                duration_ms=duration_ms,
            )

    def can_execute(self, node: WorkflowNode) -> bool:
        return self._handler(node.id) is not None or self._fallback.can_execute(node)

    def get_supported_node_types(self) -> list:
        return self._fallback.get_supported_node_types()


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
        workflow_runtime: WorkflowRuntime | None = None,
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
            workflow_runtime: Optional framework-neutral runtime. When omitted,
                LangGraph-capable state adapters receive a compatibility facade.
        """
        self._exec_repo = execution_repository
        self._workflow_repo = workflow_repository
        self._state_adapter = state_adapter
        self._node_executor = node_executor or DefaultNodeExecutor()
        self._uow = unit_of_work
        self._file_tracking = file_tracking_service
        self._output_dir = output_dir
        self._workflow_runtime = workflow_runtime
        self._workflow_runtime_explicit = workflow_runtime is not None
        supports_graph_execution = getattr(
            state_adapter,
            "supports_graph_execution",
            None,
        )
        if (
            self._workflow_runtime is None
            and callable(supports_graph_execution)
            and supports_graph_execution()
        ):
            from wtb.infrastructure.adapters.langgraph_workflow_runtime import (
                LangGraphWorkflowRuntime,
            )

            self._workflow_runtime = LangGraphWorkflowRuntime(state_adapter)
        self._deferred_commit = False
        self._last_runtime_mutation_won: bool | None = None
    
    def set_deferred_commit(self, deferred: bool) -> None:
        """When True, _commit() becomes a no-op so the caller owns the commit."""
        self._deferred_commit = deferred

    def observe_local_execution(self, *, event, checkpoint, control) -> None:
        """Observe native execution and request cooperative control at safe boundaries.

        Opt-in observers receive durable checkpoint identities and actual node
        results. They do not own execution transitions or adapter recovery claims.
        External WorkflowRuntime instances continue to use their own protocol.
        """
        self._local_observer = dict(event=event, checkpoint=checkpoint, control=control)

    def last_runtime_mutation_won(self) -> bool | None:
        """Report whether this controller won its latest runtime CAS.

        Operation-scoped decorators use this transient signal to avoid
        publishing lifecycle claims for a losing concurrent mutation.
        """
        return self._last_runtime_mutation_won
    
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

        adapter_backend = getattr(
            self._state_adapter,
            "state_adapter_backend",
            None,
        )
        if adapter_backend == "node_sqlite" and not metadata.get(
            "checkpoint_db_path"
        ):
            storage_path = getattr(self._state_adapter, "storage_path", None)
            if storage_path is None:
                raise RuntimeError(
                    "node_sqlite state adapter has no durable storage path"
                )
            metadata["checkpoint_db_path"] = str(storage_path)

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
    
    def run(
        self,
        execution_id: str,
        graph: Any | None = None,
        *,
        workflow: Any | None = None,
    ) -> Execution:
        """
        Start or continue execution.
        
        Capability-based routing (LSP-compliant, no isinstance checks):
        1. If a workflow is provided and a runtime is configured -> bind it
        2. If a runtime already has a bound workflow -> reuse it
        3. Otherwise -> DefaultNodeExecutor with WTB workflow nodes
        """
        if graph is not None and workflow is not None:
            raise ValueError("Pass workflow or legacy graph, not both")
        execution = self._get_execution(execution_id)
        self._require_runtime_outcome_reconciled(execution, operation="run")
        if execution.status in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }:
            # Terminal executions are idempotent reads.  In particular, never
            # manufacture a fresh runtime attempt for an already completed
            # turn merely because a workflow remains bound to the runtime.
            return execution
        return self._run_execution(
            execution,
            graph=workflow if workflow is not None else graph,
        )

    def _require_runtime_checkpoint(
        self,
        execution_id: str,
        checkpoint_id: str,
        *,
        operation: str,
    ) -> None:
        """Reject rewind claims unsupported by an explicitly injected runtime."""
        if not self._workflow_runtime_explicit:
            return
        self._require_explicit_runtime_bound(operation=operation)
        capability = self._runtime_checkpoint_recovery_capability()
        if capability is False:
            raise RuntimeError(
                f"Workflow runtime does not support {operation} at checkpoint "
                f"{checkpoint_id!r}"
            )
        snapshot = self._workflow_runtime.snapshot(
            execution_id,
            checkpoint_id=checkpoint_id,
        )
        if (
            snapshot is None
            or snapshot.execution_id != execution_id
            or snapshot.checkpoint_id != checkpoint_id
        ):
            raise RuntimeError(
                f"Workflow runtime does not support {operation} at checkpoint "
                f"{checkpoint_id!r}"
            )

    def _require_explicit_runtime_bound(self, *, operation: str) -> None:
        """Reject state-only fallback for an explicit but unbound runtime."""
        if not self._workflow_runtime_explicit:
            return
        runtime = self._workflow_runtime
        is_bound = getattr(runtime, "is_bound", None)
        try:
            bound = callable(is_bound) and bool(is_bound())
        except Exception:
            bound = False
        if not bound:
            raise RuntimeError(
                f"Cannot {operation}: explicit workflow runtime is not bound"
            )

    def _runtime_checkpoint_recovery_capability(self) -> bool | None:
        """Read an explicit capability while tolerating pre-contract runtimes.

        ``None`` means the runtime predates the capability method. Individual
        recovery operations may still validate those runtimes by requesting an
        exact checkpoint snapshot, but feature discovery must not claim support.
        """
        if self._workflow_runtime is None:
            return None
        capability = getattr(
            self._workflow_runtime,
            "supports_checkpoint_recovery",
            None,
        )
        if not callable(capability):
            return None
        try:
            return bool(capability())
        except Exception:
            return False

    def _runtime_snapshot(
        self,
        execution_id: str,
        checkpoint_id: str | None = None,
    ):
        if self._workflow_runtime is None:
            return None
        return self._workflow_runtime.snapshot(
            execution_id,
            checkpoint_id=checkpoint_id,
        )

    @staticmethod
    def _require_runtime_outcome_reconciled(
        execution: Execution,
        *,
        operation: str,
    ) -> None:
        metadata = execution.metadata or {}
        recovery_reason = metadata.get("recovery_reason")
        active_attempt = metadata.get("runtime_attempt")
        if recovery_reason == "runtime_outcome_unknown":
            raise RuntimeError(
                f"Cannot {operation} while runtime outcome is unknown; "
                "reconcile the recorded runtime attempt first"
            )
        if recovery_reason == "runtime_dispatch_inflight" or isinstance(
            active_attempt,
            dict,
        ):
            raise RuntimeError(
                f"Cannot {operation} while a runtime attempt requires "
                "reconciliation; reconcile the recorded runtime attempt first"
            )

    @staticmethod
    def _runtime_input_sha256(input_state: Mapping[str, Any] | None) -> str:
        """Return a stable input identity for one durable runtime attempt."""
        return canonical_json_sha256(input_state or {})

    def _persist_runtime_dispatch_intent(
        self,
        execution: Execution,
        *,
        attempt_id: str,
        input_state: Mapping[str, Any] | None,
        input_sha256: str,
        checkpoint_id: str | None,
        attempt_identity_forwarded: bool,
        plan: RuntimeAttemptPlan,
    ) -> bool:
        """Commit a fail-closed dispatch fence before the external side effect.

        This prevents a later WTB result-commit failure from restoring the
        execution to PENDING and silently dispatching the same external turn a
        second time. The exact expected status/metadata CAS below also makes
        the claim single-winner across independent repository sessions.
        """
        self._require_runtime_outcome_reconciled(execution, operation="dispatch")
        expected_status = execution.status
        expected_metadata = copy.deepcopy(dict(execution.metadata or {}))
        if execution.status is ExecutionStatus.PENDING:
            execution.start()
        elif execution.status is ExecutionStatus.PAUSED:
            execution.resume()
        else:
            raise RuntimeError(
                f"Cannot dispatch execution in status {execution.status.value}"
            )

        execution.metadata = dict(execution.metadata or {})
        attempt = {
            "attempt_id": attempt_id,
            "dispatch_state": "prepared",
            "input_sha256": input_sha256,
            "checkpoint_id": checkpoint_id,
            "attempt_identity_forwarded": attempt_identity_forwarded,
            "session_id": plan.session_id,
            "binding": plan.binding.to_dict(),
            "runtime_metadata": dict(plan.metadata),
            "prepared_at": datetime.now().isoformat(),
        }
        execution.metadata["runtime_attempt"] = attempt
        execution.metadata["recovery_required"] = True
        execution.metadata["recovery_reason"] = "runtime_dispatch_inflight"
        if plan.session_id is not None:
            execution.metadata["runtime_session_id"] = plan.session_id
        execution.error_message = None
        if not self._compare_and_swap_runtime_execution(
            execution,
            expected_status=expected_status,
            expected_metadata=expected_metadata,
        ):
            return False

        outbox = getattr(self._uow, "outbox", None)
        add_outbox = getattr(outbox, "add", None)
        if callable(add_outbox):
            add_outbox(
                OutboxEvent.create(
                    event_type=OutboxEventType.EXECUTION_DISPATCH_PREPARED,
                    aggregate_type="Execution",
                    aggregate_id=execution.id,
                    idempotency_key=f"runtime-dispatch:{attempt_id}",
                    payload={
                        "execution_id": execution.id,
                        "attempt_id": attempt_id,
                        "input_sha256": input_sha256,
                        "checkpoint_id": checkpoint_id,
                        "attempt_identity_forwarded": attempt_identity_forwarded,
                        "runtime_session_id": plan.session_id,
                        "runtime_binding": plan.binding.to_dict(),
                        "runtime_metadata": dict(plan.metadata),
                    },
                )
            )

        # This boundary intentionally bypasses deferred final-result commits.
        # The attempt fence must be durable before runtime.invoke can happen.
        if self._uow is not None:
            self._uow.commit()
        elif self._deferred_commit:
            raise RuntimeError(
                "Durable workflow runtime dispatch requires a unit of work"
            )
        else:
            self._commit()
        return True

    def _compare_and_swap_runtime_execution(
        self,
        execution: Execution,
        *,
        expected_status: ExecutionStatus,
        expected_metadata: Mapping[str, Any],
    ) -> bool:
        """Advance an external-runtime aggregate without last-writer-wins."""
        compare_and_swap = getattr(self._exec_repo, "compare_and_swap", None)
        if not callable(compare_and_swap):
            raise RuntimeError(
                "External workflow runtime dispatch requires execution CAS"
            )
        try:
            claimed = compare_and_swap(
                execution,
                expected_status=expected_status,
                expected_metadata=copy.deepcopy(dict(expected_metadata)),
            )
        except NotImplementedError as error:
            raise RuntimeError(
                "External workflow runtime dispatch requires execution CAS"
            ) from error
        return bool(claimed)

    def _reload_after_runtime_cas_loss(self, execution_id: str) -> Execution:
        """Discard the loser's transaction before observing the CAS winner."""
        rollback = getattr(self._uow, "rollback", None)
        if callable(rollback):
            try:
                rollback()
            except Exception as error:
                raise RuntimeError(
                    "Could not discard the stale runtime CAS transaction"
                ) from error
        return self._get_execution(execution_id)

    def _plan_runtime_attempt(
        self,
        execution: Execution,
        *,
        attempt_id: str,
        input_sha256: str,
        checkpoint_id: str | None,
    ) -> RuntimeAttemptPlan:
        prepare_attempt = getattr(self._workflow_runtime, "prepare_attempt", None)
        if not callable(prepare_attempt):
            raise RuntimePreDispatchError(
                "Workflow runtime cannot provide a durable binding envelope"
            )
        plan = prepare_attempt(
            execution.id,
            attempt_id,
            input_sha256=input_sha256,
            checkpoint_id=checkpoint_id,
        )
        if not isinstance(plan, RuntimeAttemptPlan):
            raise RuntimeError("Workflow runtime returned no valid attempt plan")
        if plan.execution_id != execution.id or plan.attempt_id != attempt_id:
            raise RuntimeError("Workflow runtime attempt plan has foreign ownership")
        if not isinstance(plan.session_id, str) or not plan.session_id:
            raise RuntimeError("Workflow runtime attempt plan has invalid session")
        if not isinstance(plan.binding, RuntimeBindingEnvelope):
            raise RuntimeError("Workflow runtime attempt plan has invalid binding")
        if plan.binding.input_sha256 != input_sha256:
            raise RuntimeError("Workflow runtime attempt plan has foreign input")
        if not isinstance(plan.metadata, Mapping):
            raise RuntimeError("Workflow runtime attempt plan metadata is invalid")
        return plan

    def _runtime_accepts_attempt_id(self) -> bool:
        """Return whether invoke accepts the complete durable binding."""
        invoke = getattr(self._workflow_runtime, "invoke", None)
        if not callable(invoke):
            return False
        try:
            parameters = inspect.signature(invoke).parameters.values()
        except (TypeError, ValueError):
            return False
        names = {parameter.name for parameter in parameters}
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        return accepts_kwargs or {"attempt_id", "binding"}.issubset(names)

    def _runtime_accepts_attempt_metadata(self) -> bool:
        """Return whether invoke accepts persisted runtime plan metadata."""
        invoke = getattr(self._workflow_runtime, "invoke", None)
        if not callable(invoke):
            return False
        try:
            parameters = inspect.signature(invoke).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        ) or any(parameter.name == "attempt_metadata" for parameter in parameters)

    @staticmethod
    def _active_runtime_attempt_id(execution: Execution) -> str | None:
        attempt = (execution.metadata or {}).get("runtime_attempt")
        if not isinstance(attempt, dict):
            return None
        attempt_id = attempt.get("attempt_id")
        return attempt_id if isinstance(attempt_id, str) and attempt_id else None

    @staticmethod
    def _runtime_attempt_fence_proof(
        active_attempt: Mapping[str, Any],
    ) -> RuntimeBindingEnvelope:
        """Return the complete immutable identity committed before dispatch."""
        raw_binding = active_attempt.get("binding")
        try:
            binding = RuntimeBindingEnvelope.from_mapping(raw_binding)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "Recorded runtime attempt has no valid binding envelope"
            ) from error
        input_sha256 = active_attempt.get("input_sha256")
        if input_sha256 != binding.input_sha256:
            raise RuntimeError(
                "Recorded runtime attempt input disagrees with its binding envelope"
            )
        session_id = active_attempt.get("session_id")
        if session_id != binding.exact_session_id:
            raise RuntimeError(
                "Recorded runtime attempt session disagrees with its binding envelope"
            )
        attempt_id = active_attempt.get("attempt_id")
        if attempt_id != binding.attempt_id:
            raise RuntimeError(
                "Recorded runtime attempt ID disagrees with its binding envelope"
            )
        return binding

    @staticmethod
    def _runtime_outcome_from_finish_reason(
        finish_reason: str,
    ) -> RuntimeTerminalOutcome:
        normalized = finish_reason.strip().lower()
        if normalized in {
            "aborted",
            "cancelled",
            "canceled",
            "interrupted",
            "stopped",
        }:
            return RuntimeTerminalOutcome.CANCELLED
        return RuntimeTerminalOutcome.FAILED

    @staticmethod
    def _clear_runtime_recovery_gate(execution: Execution) -> None:
        execution.metadata = dict(execution.metadata or {})
        execution.metadata.pop("recovery_required", None)
        execution.metadata.pop("recovery_reason", None)
        execution.metadata.pop("runtime_session_id", None)
        execution.metadata.pop("runtime_resume_checkpoint_id", None)

    @classmethod
    def _finish_runtime_attempt(
        cls,
        execution: Execution,
        *,
        outcome: RuntimeTerminalOutcome,
        session_id: str | None = None,
        finish_reason: str | None = None,
        receipt: Mapping[str, Any] | None = None,
    ) -> None:
        execution.metadata = dict(execution.metadata or {})
        active_attempt = execution.metadata.pop("runtime_attempt", None)
        attempt_id = (
            active_attempt.get("attempt_id")
            if isinstance(active_attempt, dict)
            else None
        )
        input_sha256 = (
            active_attempt.get("input_sha256")
            if isinstance(active_attempt, dict)
            else None
        )
        terminal = {
            "attempt_id": attempt_id,
            "input_sha256": input_sha256,
            "outcome": outcome.value,
            "session_id": session_id,
            "receipt": dict(receipt or {}),
            "binding": (
                copy.deepcopy(active_attempt.get("binding"))
                if isinstance(active_attempt, dict)
                else None
            ),
            "runtime_metadata": (
                copy.deepcopy(active_attempt.get("runtime_metadata", {}))
                if isinstance(active_attempt, dict)
                else {}
            ),
            "completed_at": datetime.now().isoformat(),
        }
        if finish_reason is not None:
            terminal["finish_reason"] = finish_reason
        execution.metadata["last_runtime_attempt"] = terminal
        cls._clear_runtime_recovery_gate(execution)

    @staticmethod
    def _validate_runtime_outcome_receipt(
        receipt: Any,
        *,
        execution_id: str,
        attempt_id: str,
        input_sha256: str,
        session_id: str,
        binding: RuntimeBindingEnvelope,
        required_outcome: RuntimeTerminalOutcome | None = None,
    ) -> RuntimeOutcomeReceipt:
        if not isinstance(receipt, RuntimeOutcomeReceipt):
            raise RuntimeError("Workflow runtime returned no verified outcome receipt")
        if (
            receipt.execution_id != execution_id
            or receipt.attempt_id != attempt_id
        ):
            raise RuntimeError(
                "Workflow runtime receipt does not own this execution attempt"
            )
        if not isinstance(receipt.outcome, RuntimeTerminalOutcome):
            raise RuntimeError("Workflow runtime receipt has an invalid outcome")
        if (
            not isinstance(receipt.input_sha256, str)
            or len(receipt.input_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in receipt.input_sha256
            )
        ):
            raise RuntimeError("Workflow runtime receipt has an invalid input proof")
        if receipt.input_sha256 != input_sha256:
            raise RuntimeError(
                "Workflow runtime receipt does not own this runtime input"
            )
        if receipt.binding != binding:
            raise RuntimeError(
                "Workflow runtime receipt does not own the persisted binding"
            )
        if not isinstance(receipt.values, Mapping) or not isinstance(
            receipt.receipt,
            Mapping,
        ):
            raise RuntimeError("Workflow runtime receipt has invalid evidence")
        if receipt.session_id is not None and (
            not isinstance(receipt.session_id, str) or not receipt.session_id
        ):
            raise RuntimeError("Workflow runtime receipt has an invalid session")
        if receipt.session_id != session_id:
            raise RuntimeError(
                "Workflow runtime receipt does not own the planned session"
            )
        if required_outcome is not None and receipt.outcome is not required_outcome:
            raise RuntimeError(
                "Workflow runtime returned no verified cancellation acknowledgement"
            )
        if not isinstance(receipt.values, Mapping) or not isinstance(
            receipt.receipt,
            Mapping,
        ):
            raise RuntimeError("Workflow runtime receipt payload is invalid")
        return receipt

    @classmethod
    def _apply_runtime_outcome_receipt(
        cls,
        execution: Execution,
        receipt: RuntimeOutcomeReceipt,
    ) -> None:
        if not isinstance(receipt.binding, RuntimeBindingEnvelope):
            raise RuntimeError("Workflow runtime receipt has no typed binding")
        materialized = dict(receipt.values)
        if materialized:
            execution.state.workflow_variables = materialized
            execution.state.node_results["final"] = dict(materialized)
            if "messages" in materialized:
                execution.state.execution_path = materialized.get("messages", [])

        if receipt.outcome is RuntimeTerminalOutcome.COMPLETED:
            execution.complete()
            execution.error_message = None
            execution.error_node_id = None
        elif receipt.outcome is RuntimeTerminalOutcome.FAILED:
            execution.fail(
                receipt.detail or "Workflow runtime reported a terminal failure",
                execution.state.current_node_id,
            )
        elif receipt.outcome is RuntimeTerminalOutcome.CANCELLED:
            execution.cancel()
        else:  # pragma: no cover - enum validation above is fail closed
            raise RuntimeError("Unsupported runtime terminal outcome")

        cls._finish_runtime_attempt(
            execution,
            outcome=receipt.outcome,
            session_id=receipt.session_id,
            receipt=receipt.receipt,
        )
        execution.metadata["last_runtime_reconciliation"] = {
            "attempt_id": receipt.attempt_id,
            "input_sha256": receipt.input_sha256,
            "outcome": receipt.outcome.value,
            "session_id": receipt.session_id,
            "receipt": dict(receipt.receipt),
            "runtime_binding": receipt.binding.to_dict(),
        }

    def supports_runtime_forking(self) -> bool:
        """Return explicit cross-execution runtime clone capability."""
        if self._workflow_runtime is None:
            return bool(self._state_adapter.supports_time_travel())
        capability = getattr(self._workflow_runtime, "supports_forking", None)
        if not callable(capability):
            return False
        try:
            return bool(capability()) and bool(
                self._state_adapter.supports_time_travel()
            )
        except Exception:
            return False

    def _run_execution(
        self,
        execution: Execution,
        graph: Any | None = None,
        *,
        session_activated: bool = False,
    ) -> Execution:
        """Run an already-loaded execution without discarding local updates."""
        if (
            graph is None
            and execution.status == ExecutionStatus.PAUSED
            and self._workflow_runtime_explicit
        ):
            self._require_explicit_runtime_bound(operation="resume")
        if graph is not None and self._workflow_runtime is not None:
            self._workflow_runtime.bind(graph, force_rebind=True)
            return self._run_with_runtime(
                execution,
                session_activated=session_activated,
            )
        
        if (
            self._workflow_runtime is not None
            and self._workflow_runtime.is_bound()
        ):
            return self._run_with_runtime(
                execution,
                session_activated=session_activated,
            )
        
        return self._run_with_node_executor(
            execution,
            graph=graph,
            session_activated=session_activated,
        )
    
    def _supports_langgraph_execution(self) -> bool:
        """Backward-compatible capability query for LangGraph callers."""
        return self._workflow_runtime is not None

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

        fenced_status = execution.status
        fenced_metadata = copy.deepcopy(metadata)
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
        if not self._compare_and_swap_runtime_execution(
            execution,
            expected_status=fenced_status,
            expected_metadata=fenced_metadata,
        ):
            # Preserve the caller's aggregate identity while observing the
            # concurrent durable node-recovery winner.
            current = self._reload_after_runtime_cas_loss(execution.id)
            execution.__dict__.clear()
            execution.__dict__.update(copy.deepcopy(current.__dict__))
            return True, execution.status in {
                ExecutionStatus.COMPLETED,
                ExecutionStatus.FAILED,
                ExecutionStatus.CANCELLED,
            }
        self._commit()
        return True, terminal
    
    def _run_with_runtime(
        self,
        execution: Execution,
        *,
        session_activated: bool = False,
    ) -> Execution:
        """
        Execute a workflow through the framework-neutral runtime.
        
        Handles both fresh start (PENDING) and resume (PAUSED):
        - PENDING: execution.start() + adapter.execute(initial_state)
        - PAUSED: execution.resume() + adapter.execute(None) to resume from checkpoint
        
        A workflow must already be bound to the runtime.
        """
        self._last_runtime_mutation_won = None
        self._require_runtime_outcome_reconciled(execution, operation="run")
        if (
            execution.status == ExecutionStatus.PENDING
            and self._file_tracking
            and self._file_tracking.is_available()
            and self._workflow_runtime_explicit
            and self._runtime_checkpoint_recovery_capability() is not True
        ):
            raise RuntimeError(
                "Cannot invoke workflow runtime with file tracking without "
                "checkpoint recovery support"
            )
        if (
            not session_activated
            and not self._activate_execution_session(execution)
        ):
            raise RuntimeError("Could not activate execution session")

        resume_checkpoint_id = None
        if execution.status == ExecutionStatus.PAUSED:
            resume_checkpoint_id = (execution.metadata or {}).get(
                "resume_checkpoint_id"
            )
            if resume_checkpoint_id is not None:
                self._require_runtime_checkpoint(
                    execution.id,
                    str(resume_checkpoint_id),
                    operation="resume",
                )

        if execution.status is ExecutionStatus.PENDING:
            initial_state: Mapping[str, Any] | None = (
                execution.state.workflow_variables.copy()
            )
        elif execution.status is ExecutionStatus.PAUSED:
            initial_state = None
        else:
            raise RuntimeError(
                f"Cannot run execution in status {execution.status.value}"
            )

        attempt_id = str(uuid.uuid4())
        input_sha256 = self._runtime_input_sha256(initial_state)
        attempt_identity_forwarded = self._runtime_accepts_attempt_id()
        if not attempt_identity_forwarded:
            raise RuntimePreDispatchError(
                "Workflow runtime invoke cannot accept the durable binding envelope"
            )
        attempt_plan = self._plan_runtime_attempt(
            execution,
            attempt_id=attempt_id,
            input_sha256=input_sha256,
            checkpoint_id=(
                str(resume_checkpoint_id)
                if resume_checkpoint_id is not None
                else None
            ),
        )
        claim_won = self._persist_runtime_dispatch_intent(
            execution,
            attempt_id=attempt_id,
            input_state=initial_state,
            input_sha256=input_sha256,
            checkpoint_id=(
                str(resume_checkpoint_id)
                if resume_checkpoint_id is not None
                else None
            ),
            attempt_identity_forwarded=attempt_identity_forwarded,
            plan=attempt_plan,
        )
        if not claim_won:
            # Another worker durably owns this command. Returning its current
            # aggregate is an idempotent observation and, critically, does not
            # call the external runtime a second time.
            self._last_runtime_mutation_won = False
            return self._reload_after_runtime_cas_loss(execution.id)
        fenced_status = execution.status
        fenced_metadata = copy.deepcopy(dict(execution.metadata or {}))

        try:
            active_attempt = (execution.metadata or {}).get("runtime_attempt")
            if not isinstance(active_attempt, Mapping):
                raise RuntimePreDispatchError(
                    "Durable runtime attempt fence disappeared before dispatch"
                )
            persisted_binding = self._runtime_attempt_fence_proof(active_attempt)
            raw_runtime_metadata = active_attempt.get("runtime_metadata", {})
            if not isinstance(raw_runtime_metadata, Mapping):
                raise RuntimePreDispatchError(
                    "Durable runtime attempt metadata is invalid"
                )
            persisted_runtime_metadata = copy.deepcopy(
                dict(raw_runtime_metadata)
            )
            if initial_state is not None:
                invoke_kwargs: dict[str, Any] = {"execution_id": execution.id}
            else:
                invoke_kwargs = {
                    "execution_id": execution.id,
                    "checkpoint_id": resume_checkpoint_id,
                }
            if attempt_identity_forwarded:
                invoke_kwargs["attempt_id"] = attempt_id
                invoke_kwargs["binding"] = persisted_binding
                if self._runtime_accepts_attempt_metadata():
                    invoke_kwargs["attempt_metadata"] = persisted_runtime_metadata
                elif "process_generation" in persisted_runtime_metadata:
                    raise RuntimePreDispatchError(
                        "Workflow runtime invoke cannot accept its persisted "
                        "process-generation pin"
                    )
            final_state = self._workflow_runtime.invoke(
                initial_state,
                **invoke_kwargs,
            )
            if not isinstance(final_state, Mapping):
                raise TypeError("Workflow runtime result must be a mapping")
            materialized_state = dict(final_state)
            execution.state.workflow_variables = materialized_state
            execution.state.node_results["final"] = dict(materialized_state)
            
            if "messages" in materialized_state:
                execution.state.execution_path = materialized_state.get("messages", [])
            
            snapshot = self._runtime_snapshot(execution.id)
            if snapshot is not None and snapshot.execution_id != execution.id:
                raise RuntimeError(
                    "Workflow runtime snapshot belongs to another execution"
                )
            if snapshot is not None and snapshot.checkpoint_id is not None:
                execution.checkpoint_id = snapshot.checkpoint_id

            active_attempt = (execution.metadata or {}).get("runtime_attempt")
            planned_session_id = (
                active_attempt.get("session_id")
                if isinstance(active_attempt, dict)
                else None
            )
            receipt = snapshot.metadata if snapshot is not None else {}
            observed_session_id = receipt.get("session_id")
            if observed_session_id is not None:
                if not isinstance(observed_session_id, str) or not observed_session_id:
                    raise RuntimeError(
                        "Workflow runtime snapshot has an invalid session identity"
                    )
                if (
                    planned_session_id is not None
                    and planned_session_id != observed_session_id
                ):
                    raise RuntimeError(
                        "Workflow runtime snapshot belongs to another planned session"
                    )
                planned_session_id = observed_session_id
            observed_attempt_id = receipt.get("attempt_id")
            if (
                observed_attempt_id is not None
                and observed_attempt_id != attempt_id
            ):
                raise RuntimeError(
                    "Workflow runtime snapshot belongs to another dispatch attempt"
                )

            local_console = (
                not self._workflow_runtime_explicit
                and isinstance(getattr(self._state_adapter, "_runtime_hooks", None), dict)
            )
            if self._file_tracking and self._file_tracking.is_available() and not local_console:
                if (
                    self._workflow_runtime_explicit
                    and (
                        snapshot is None
                        or snapshot.checkpoint_id is None
                    )
                ):
                    raise RuntimeError(
                        "Cannot link runtime output files without a restorable "
                        "runtime checkpoint"
                    )
                self._track_checkpoint_history_output_files(
                    execution,
                    materialized_state,
                )
            
            # Cooperative local LangGraph invocation has finished at a durable
            # boundary; the workflow can remain paused. External runtime receipt
            # semantics are unchanged by this opt-in local adapter integration.
            local_outcome = getattr(self._state_adapter, "runtime_outcome", "completed") if local_console else "completed"
            if local_outcome == "paused":
                execution.pause()
            elif local_outcome == "cancelled":
                execution.cancel()
            else:
                execution.complete()
            self._finish_runtime_attempt(
                execution,
                outcome=(RuntimeTerminalOutcome.CANCELLED if local_outcome == "cancelled" else RuntimeTerminalOutcome.COMPLETED),
                session_id=planned_session_id,
                receipt=receipt,
            )
            if execution.metadata and "resume_checkpoint_id" in execution.metadata:
                execution.metadata.pop("resume_checkpoint_id", None)

        except RuntimePreDispatchError as error:
            execution.fail(str(error), execution.state.current_node_id)
            self._finish_runtime_attempt(
                execution,
                outcome=RuntimeTerminalOutcome.FAILED,
                finish_reason="pre-dispatch-error",
                receipt={"dispatched": False},
            )
        except RuntimeTerminalError as error:
            active_attempt = (execution.metadata or {}).get("runtime_attempt")
            planned_session_id = (
                active_attempt.get("session_id")
                if isinstance(active_attempt, dict)
                else None
            )
            terminal_receipt = error.receipt
            terminal_evidence_error: BaseException | None = None
            if terminal_receipt is not None and not isinstance(
                terminal_receipt,
                Mapping,
            ):
                terminal_evidence_error = TypeError(
                    "Trusted runtime terminal receipt must be a mapping"
                )
            elif error.session_id is not None and (
                not isinstance(error.session_id, str) or not error.session_id
            ):
                terminal_evidence_error = TypeError(
                    "Trusted runtime terminal session must be a non-empty string"
                )
            elif (
                error.session_id is not None
                and planned_session_id is not None
                and error.session_id != planned_session_id
            ):
                terminal_evidence_error = RuntimeError(
                    "Trusted runtime terminal receipt belongs to another session"
                )
            elif isinstance(terminal_receipt, Mapping):
                receipt_execution_id = terminal_receipt.get("execution_id")
                receipt_attempt_id = terminal_receipt.get("attempt_id")
                receipt_session_id = terminal_receipt.get("session_id")
                if (
                    receipt_execution_id is not None
                    and receipt_execution_id != execution.id
                ):
                    terminal_evidence_error = RuntimeError(
                        "Trusted runtime terminal receipt belongs to another execution"
                    )
                elif (
                    receipt_attempt_id is not None
                    and receipt_attempt_id != attempt_id
                ):
                    terminal_evidence_error = RuntimeError(
                        "Trusted runtime terminal receipt belongs to another attempt"
                    )
                elif receipt_session_id is not None and (
                    not isinstance(receipt_session_id, str)
                    or not receipt_session_id
                ):
                    terminal_evidence_error = TypeError(
                        "Trusted runtime terminal receipt has an invalid session"
                    )
                elif (
                    receipt_session_id is not None
                    and receipt_session_id
                    not in {error.session_id, planned_session_id}
                ):
                    terminal_evidence_error = RuntimeError(
                        "Trusted runtime terminal receipt belongs to another session"
                    )
            if terminal_evidence_error is not None:
                self._mark_runtime_outcome_unknown(
                    execution,
                    terminal_evidence_error,
                    resume_checkpoint_id=resume_checkpoint_id,
                )

            terminal_state = error.result_state
            if (
                terminal_evidence_error is None
                and terminal_state is not None
            ):
                if not isinstance(terminal_state, Mapping):
                    self._mark_runtime_outcome_unknown(
                        execution,
                        TypeError("Trusted runtime terminal state must be a mapping"),
                        resume_checkpoint_id=resume_checkpoint_id,
                    )
                else:
                    materialized_state = dict(terminal_state)
                    execution.state.workflow_variables = materialized_state
                    execution.state.node_results["final"] = dict(
                        materialized_state
                    )
                    if "messages" in materialized_state:
                        execution.state.execution_path = materialized_state.get(
                            "messages",
                            [],
                        )

            if (execution.metadata or {}).get("recovery_reason") != (
                "runtime_outcome_unknown"
            ):
                outcome = self._runtime_outcome_from_finish_reason(
                    error.finish_reason
                )
                if outcome is RuntimeTerminalOutcome.CANCELLED:
                    execution.cancel()
                else:
                    execution.fail(str(error), execution.state.current_node_id)
                self._finish_runtime_attempt(
                    execution,
                    outcome=outcome,
                    session_id=error.session_id,
                    finish_reason=error.finish_reason,
                    receipt=error.receipt,
                )
        except RuntimeOutcomeUnknownError as error:
            self._mark_runtime_outcome_unknown(
                execution,
                error,
                resume_checkpoint_id=resume_checkpoint_id,
            )
        except Exception as e:
            # Once the dispatch fence is committed, an untyped failure cannot
            # prove whether the external side effect happened. Fail closed and
            # require runtime-owned reconciliation instead of auto-retrying.
            self._mark_runtime_outcome_unknown(
                execution,
                e,
                resume_checkpoint_id=resume_checkpoint_id,
            )
        
        self._sync_external_cache_metadata(execution)
        if not self._compare_and_swap_runtime_execution(
            execution,
            expected_status=fenced_status,
            expected_metadata=fenced_metadata,
        ):
            # A reconcile or cancel won while invoke was in flight. Discard
            # this stale result instead of reviving/overwriting the winner.
            self._last_runtime_mutation_won = False
            return self._reload_after_runtime_cas_loss(execution.id)
        self._last_runtime_mutation_won = True
        self._commit()
        
        return execution

    @staticmethod
    def _mark_runtime_outcome_unknown(
        execution: Execution,
        error: BaseException,
        *,
        resume_checkpoint_id: str | None = None,
    ) -> None:
        """Persist a non-terminal reconciliation gate after dispatch."""
        logger.error("Workflow runtime outcome is unknown: %s", error)
        execution.metadata = dict(execution.metadata or {})
        execution.metadata["recovery_required"] = True
        execution.metadata["recovery_reason"] = "runtime_outcome_unknown"
        active_attempt = execution.metadata.get("runtime_attempt")
        if isinstance(active_attempt, dict):
            active_attempt = dict(active_attempt)
            active_attempt["dispatch_state"] = "outcome_unknown"
            execution.metadata["runtime_attempt"] = active_attempt
        session_id = getattr(error, "session_id", None)
        if session_id is not None:
            execution.metadata["runtime_session_id"] = session_id
        if resume_checkpoint_id is not None:
            execution.metadata["runtime_resume_checkpoint_id"] = (
                resume_checkpoint_id
            )
        execution.error_message = str(error)

    def _run_with_langgraph(
        self,
        execution: Execution,
        *,
        session_activated: bool = False,
    ) -> Execution:
        """Deprecated alias retained for integrations migrating to runtimes."""
        return self._run_with_runtime(
            execution,
            session_activated=session_activated,
        )
    
    def _track_checkpoint_history_output_files(self, execution: Execution, final_state: Any) -> None:
        """Link every checkpoint's output files to a CAS commit."""
        if not isinstance(final_state, Mapping):
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
            raw_values = cp.get("values") or {}
            if isinstance(raw_values, ExecutionState):
                values = raw_values.workflow_variables
            elif isinstance(raw_values, Mapping):
                nested_values = raw_values.get("workflow_variables")
                values = (
                    nested_values
                    if isinstance(nested_values, Mapping)
                    else raw_values
                )
            else:
                values = {}
            output_files = values.get("_output_files")
            step = cp.get("step", 0)
            if checkpoint_id and isinstance(output_files, Mapping) and output_files:
                checkpoints.append((step, checkpoint_id, dict(output_files)))

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
        if not isinstance(output_files_data, Mapping):
            output_files_data = {}
        else:
            output_files_data = dict(output_files_data)
        
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
        graph: Any | None = None,
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
        node_executor: INodeExecutor = self._node_executor
        if graph is not None:
            node_executor = NativeGraphNodeExecutor(graph, node_executor)
        
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

                observer = getattr(self, "_local_observer", None)
                if observer:
                    request = observer["control"](execution)
                    if request in {"pause", "stop"}:
                        self._create_checkpoint(execution, current_node_id or "__end__", "Control boundary")
                        if request == "stop":
                            execution.cancel()
                        else:
                            self._prepare_node_resume_claim_token(execution, refresh=True)
                            execution.pause()
                        break
                
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
                if observer:
                    observer["event"](execution, current_node_id, "started", None)
                result = node_executor.execute(
                    node=node,
                    context=execution.state.workflow_variables,
                )

                if observer:
                    observer["event"](execution, current_node_id, "completed" if result.success else "failed", result.error)
                
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
        
        if self._file_tracking and self._file_tracking.is_available() and not getattr(self, "_local_observer", None):
            self._track_checkpoint_history_output_files(
                execution,
                execution.state.workflow_variables,
            )

        # Persist final state
        self._sync_external_cache_metadata(execution)
        self._exec_repo.update(execution)
        self._commit()
        
        return execution
    
    def pause(self, execution_id: str) -> Execution:
        """Pause execution at current position."""
        execution = self._get_execution(execution_id)
        self._require_runtime_outcome_reconciled(execution, operation="pause")
        
        if not execution.can_pause():
            raise ValueError(f"Cannot pause execution in status {execution.status.value}")

        runtime_checkpoint_id: str | None = None
        if self._workflow_runtime_explicit:
            self._require_explicit_runtime_bound(operation="pause")
            if self._runtime_checkpoint_recovery_capability() is not True:
                raise RuntimeError(
                    "Workflow runtime does not support pause with a "
                    "recoverable checkpoint"
                )

            shares_checkpoint_store = getattr(
                self._workflow_runtime,
                "shares_checkpoint_store",
                None,
            )
            if (
                not callable(shares_checkpoint_store)
                or not shares_checkpoint_store(self._state_adapter)
            ):
                raise RuntimeError(
                    "Workflow runtime cannot prove pause checkpoint ownership"
                )

        if not self._activate_execution_session(execution):
            raise RuntimeError("Could not activate execution session")

        if self._workflow_runtime_explicit:
            snapshot = self._runtime_snapshot(execution.id)
            if (
                snapshot is None
                or snapshot.execution_id != execution.id
                or snapshot.checkpoint_id is None
            ):
                raise RuntimeError(
                    "Workflow runtime does not expose an owned pause checkpoint"
                )
            runtime_checkpoint_id = snapshot.checkpoint_id
            execution.checkpoint_id = runtime_checkpoint_id
            execution.metadata = dict(execution.metadata or {})
            execution.metadata["resume_checkpoint_id"] = runtime_checkpoint_id
        else:
            self._create_checkpoint(
                execution,
                execution.state.current_node_id or "unknown",
                "Manual Pause",
            )
        self._prepare_node_resume_claim_token(execution, refresh=True)
        
        execution.pause()
        self._exec_repo.update(execution)
        self._commit()
        
        return execution
    
    def resume(
        self,
        execution_id: str,
        modified_state: dict[str, Any] | None = None,
        *,
        graph: Any = None,
        workflow: Any = None,
    ) -> Execution:
        """Resume paused execution."""
        execution = self._get_execution(execution_id)
        self._require_runtime_outcome_reconciled(execution, operation="resume")

        metadata = execution.metadata or {}
        pending_checkpoint_fork = (
            execution.status == ExecutionStatus.PENDING
            and metadata.get("fork_type") == "checkpoint_fork"
        )
        if not execution.can_resume() and not pending_checkpoint_fork:
            raise ValueError(
                f"Cannot resume execution in status {execution.status.value}"
            )

        if self._workflow_runtime_explicit:
            self._require_explicit_runtime_bound(operation="resume")

        # Runtime snapshot lookup is session-scoped for LangGraph. Select the
        # execution before validating ownership, while still preceding any
        # checkpoint/CAS/domain mutation.
        if not self._activate_execution_session(execution):
            raise RuntimeError("Could not activate resume session")

        if self._workflow_runtime_explicit:
            runtime_checkpoint_id = (
                metadata.get("resume_checkpoint_id")
                or execution.checkpoint_id
            )
            if runtime_checkpoint_id is None:
                raise RuntimeError(
                    "Workflow runtime does not expose a checkpoint for resume"
                )
            self._require_runtime_checkpoint(
                execution.id,
                str(runtime_checkpoint_id),
                operation="resume",
            )

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
            self._workflow_runtime is not None
            and self._workflow_runtime.is_bound()
        )
        if graph_resume:
            if state_updates:
                if not self._workflow_runtime_explicit:
                    update_state = getattr(
                        self._state_adapter,
                        "update_state",
                        None,
                    )
                    if not callable(update_state):
                        raise RuntimeError(
                            "State adapter cannot apply resume state"
                        )
                    updated = update_state(
                        dict(state_updates),
                        as_node=metadata.get("source_checkpoint_as_node"),
                    )
                    if updated is False:
                        raise RuntimeError("Could not apply resume state")
                else:
                    patch_checkpoint = getattr(
                        self._workflow_runtime,
                        "patch_checkpoint",
                        None,
                    )
                    if not callable(patch_checkpoint):
                        raise RuntimeError(
                            "Workflow runtime cannot apply resume state"
                        )
                    checkpoint_id = (
                        metadata.get("resume_checkpoint_id")
                        or execution.checkpoint_id
                    )
                    if checkpoint_id is None:
                        raise RuntimeError(
                            "Workflow runtime has no checkpoint to patch"
                        )
                    continuation_checkpoint_id = patch_checkpoint(
                        execution.id,
                        str(checkpoint_id),
                        dict(state_updates),
                        as_node=metadata.get("source_checkpoint_as_node"),
                    )
                    if (
                        not isinstance(continuation_checkpoint_id, str)
                        or not continuation_checkpoint_id
                    ):
                        raise RuntimeError(
                            "Workflow runtime returned no continuation checkpoint"
                        )
                    execution.metadata = dict(execution.metadata or {})
                    execution.metadata["resume_checkpoint_id"] = (
                        continuation_checkpoint_id
                    )

        # Only mutate the domain object after every external preflight has
        # succeeded. A checkpoint fork is persisted as PENDING by older stores
        # but must enter the ordinary PAUSED -> RUNNING resume transition.
        if pending_checkpoint_fork:
            execution.status = ExecutionStatus.PAUSED
        if state_updates:
            execution.state.workflow_variables.update(state_updates)

        selected_workflow = workflow if workflow is not None else graph
        return self._run_execution(
            execution,
            graph=selected_workflow,
            session_activated=True,
        )
    
    def reconcile_runtime_outcome(
        self,
        execution_id: str,
        *,
        attempt_id: str | None = None,
    ) -> Execution:
        """Resolve one fenced dispatch using runtime-owned terminal evidence."""
        self._last_runtime_mutation_won = None
        execution = self._get_execution(execution_id)
        active_attempt_id = self._active_runtime_attempt_id(execution)
        runtime_expected_status: ExecutionStatus | None = None
        runtime_expected_metadata: dict[str, Any] | None = None
        if active_attempt_id is None:
            raise RuntimeError("Execution has no runtime attempt to reconcile")
        active_attempt = (execution.metadata or {}).get("runtime_attempt")
        if (
            not isinstance(active_attempt, dict)
            or active_attempt.get("attempt_identity_forwarded") is not True
        ):
            raise RuntimeError(
                "Runtime attempt identity was not bound to the external dispatch"
            )
        if attempt_id is not None and attempt_id != active_attempt_id:
            raise RuntimeError("Requested attempt does not own this execution")
        binding = self._runtime_attempt_fence_proof(active_attempt)
        input_sha256 = binding.input_sha256
        planned_session_id = binding.exact_session_id
        expected_status = execution.status
        expected_metadata = copy.deepcopy(dict(execution.metadata or {}))
        reconcile_input = copy.deepcopy(
            dict(execution.state.workflow_variables or {})
        )

        runtime = self._workflow_runtime
        reconcile = getattr(runtime, "reconcile", None)
        if not callable(reconcile):
            raise RuntimeError(
                "Workflow runtime cannot provide verified outcome reconciliation"
            )
        reconcile_kwargs: dict[str, Any] = {}
        try:
            reconcile_parameters = inspect.signature(reconcile).parameters.values()
        except (TypeError, ValueError):
            reconcile_parameters = ()
        if any(
            parameter.name == "input_state"
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in reconcile_parameters
        ):
            if self._runtime_input_sha256(reconcile_input) != input_sha256:
                raise RuntimeError(
                    "Recorded runtime input changed after the dispatch fence"
                )
            reconcile_kwargs["input_state"] = reconcile_input
        if any(
            parameter.name == "attempt_metadata"
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in reconcile_parameters
        ):
            raw_runtime_metadata = active_attempt.get("runtime_metadata", {})
            if not isinstance(raw_runtime_metadata, Mapping):
                raise RuntimeError(
                    "Runtime attempt metadata is invalid for reconciliation"
                )
            reconcile_kwargs["attempt_metadata"] = copy.deepcopy(
                dict(raw_runtime_metadata)
            )
        reconcile_kwargs["binding"] = binding
        receipt = self._validate_runtime_outcome_receipt(
            reconcile(execution.id, active_attempt_id, **reconcile_kwargs),
            execution_id=execution.id,
            attempt_id=active_attempt_id,
            input_sha256=input_sha256,
            session_id=planned_session_id,
            binding=binding,
        )
        recorded_session_id = (execution.metadata or {}).get(
            "runtime_session_id"
        )
        if (
            recorded_session_id is not None
            and receipt.session_id != recorded_session_id
        ):
            raise RuntimeError("Workflow runtime receipt belongs to another session")
        self._apply_runtime_outcome_receipt(execution, receipt)
        self._sync_external_cache_metadata(execution)
        if not self._compare_and_swap_runtime_execution(
            execution,
            expected_status=expected_status,
            expected_metadata=expected_metadata,
        ):
            # A concurrent reconcile/cancel already advanced this exact
            # attempt. Never overwrite it with a stale post-runtime result.
            self._last_runtime_mutation_won = False
            return self._reload_after_runtime_cas_loss(execution.id)
        self._last_runtime_mutation_won = True
        self._commit()
        return execution

    def stop(self, execution_id: str) -> Execution:
        """Cancel locally only after an external runtime acknowledges cancel."""
        self._last_runtime_mutation_won = None
        execution = self._get_execution(execution_id)
        active_attempt_id = self._active_runtime_attempt_id(execution)
        runtime_expected_status: ExecutionStatus | None = None
        runtime_expected_metadata: dict[str, Any] | None = None

        if (
            (execution.metadata or {}).get("recovery_reason")
            == "runtime_outcome_unknown"
            and active_attempt_id is None
        ):
            self._require_runtime_outcome_reconciled(execution, operation="stop")

        if (
            self._workflow_runtime_explicit
            and active_attempt_id is None
            and execution.status in {
                ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED
            }
        ):
            # Reconciliation can commit before this operation's first read,
            # as well as after its CAS fence. Both losing schedules are no-ops:
            # preserve the terminal winner and suppress a false STOPPED event.
            self._last_runtime_mutation_won = False
            return execution

        if (
            self._workflow_runtime_explicit
            and execution.status is ExecutionStatus.RUNNING
        ):
            if active_attempt_id is None:
                raise RuntimeError(
                    "Running workflow runtime execution has no cancellable attempt"
                )
            active_attempt = (execution.metadata or {}).get("runtime_attempt")
            if (
                not isinstance(active_attempt, dict)
                or active_attempt.get("attempt_identity_forwarded") is not True
            ):
                raise RuntimeError(
                    "Runtime attempt identity was not bound to a cancellable dispatch"
                )
            binding = self._runtime_attempt_fence_proof(active_attempt)
            input_sha256 = binding.input_sha256
            planned_session_id = binding.exact_session_id
            cancel_input = copy.deepcopy(
                dict(execution.state.workflow_variables or {})
            )
            runtime_expected_status = execution.status
            runtime_expected_metadata = copy.deepcopy(
                dict(execution.metadata or {})
            )
            cancel = getattr(self._workflow_runtime, "cancel", None)
            if not callable(cancel):
                raise RuntimeError(
                    "Workflow runtime cannot provide a verified cancel acknowledgement"
                )
            cancel_kwargs: dict[str, Any] = {}
            raw_runtime_metadata = active_attempt.get("runtime_metadata", {})
            if not isinstance(raw_runtime_metadata, Mapping):
                raise RuntimeError(
                    "Runtime attempt metadata is invalid for cancellation"
                )
            persisted_runtime_metadata = copy.deepcopy(
                dict(raw_runtime_metadata)
            )
            try:
                cancel_parameters = inspect.signature(cancel).parameters.values()
            except (TypeError, ValueError):
                cancel_parameters = ()
            if any(
                parameter.name == "input_state"
                or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in cancel_parameters
            ):
                if self._runtime_input_sha256(cancel_input) != input_sha256:
                    raise RuntimeError(
                        "Recorded runtime input changed after the dispatch fence"
                    )
                cancel_kwargs["input_state"] = cancel_input
            if any(
                parameter.name == "input_sha256"
                or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in cancel_parameters
            ):
                cancel_kwargs["input_sha256"] = input_sha256
            if any(
                parameter.name == "attempt_metadata"
                or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in cancel_parameters
            ):
                cancel_kwargs["attempt_metadata"] = persisted_runtime_metadata
            elif "process_generation" in persisted_runtime_metadata:
                raise RuntimeOutcomeUnknownError(
                    "Workflow runtime cannot generation-fence cancellation; "
                    "the durable attempt outcome remains unknown",
                    session_id=planned_session_id,
                )
            cancel_kwargs["binding"] = binding
            receipt = self._validate_runtime_outcome_receipt(
                cancel(execution.id, active_attempt_id, **cancel_kwargs),
                execution_id=execution.id,
                attempt_id=active_attempt_id,
                input_sha256=input_sha256,
                session_id=planned_session_id,
                binding=binding,
                required_outcome=RuntimeTerminalOutcome.CANCELLED,
            )
            recorded_session_id = (execution.metadata or {}).get(
                "runtime_session_id"
            )
            if (
                recorded_session_id is not None
                and receipt.session_id != recorded_session_id
            ):
                raise RuntimeError(
                    "Workflow runtime cancel receipt belongs to another session"
                )
            self._apply_runtime_outcome_receipt(execution, receipt)
        else:
            self._require_runtime_outcome_reconciled(execution, operation="stop")
            execution.cancel()

        if (
            runtime_expected_status is not None
            and runtime_expected_metadata is not None
        ):
            if not self._compare_and_swap_runtime_execution(
                execution,
                expected_status=runtime_expected_status,
                expected_metadata=runtime_expected_metadata,
            ):
                self._last_runtime_mutation_won = False
                return self._reload_after_runtime_cas_loss(execution.id)
        else:
            self._exec_repo.update(execution)
        self._last_runtime_mutation_won = True
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
        self._require_runtime_outcome_reconciled(execution, operation="rollback")
        self._require_explicit_runtime_bound(operation="rollback")

        if not self._activate_execution_session(execution):
            raise RuntimeError("Could not activate execution session")

        self._require_runtime_checkpoint(
            execution.id,
            checkpoint_id,
            operation="rollback",
        )

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
        self._require_runtime_outcome_reconciled(
            source_execution,
            operation="fork",
        )
        self._require_explicit_runtime_bound(operation="fork")
        if not self.supports_runtime_forking():
            raise RuntimeError("Workflow runtime does not support fork")
        source_session_id = source_execution.session_id
        fork_execution_id = str(uuid.uuid4())
        forked_execution: Execution | None = None
        operation_error: Exception | None = None
        persistence_attempted = False

        try:
            forked_execution = self._fork_impl(
                execution_id,
                checkpoint_id,
                new_initial_state,
                fork_execution_id=fork_execution_id,
            )
            persistence_attempted = True
            self._exec_repo.add(forked_execution)
            self._commit()
        except Exception as exc:
            operation_error = exc
            rollback = getattr(self._uow, "rollback", None)
            if callable(rollback):
                try:
                    rollback()
                except Exception as rollback_error:
                    logger.warning(f"Could not roll back failed fork: {rollback_error}")

            if persistence_attempted:
                try:
                    self._exec_repo.delete(fork_execution_id)
                    self._commit()
                except Exception as cleanup_error:
                    logger.warning(
                        f"Could not remove failed fork record: {cleanup_error}"
                    )
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
        logger.info(
            f"Forked execution {fork_execution_id} from {execution_id} "
            f"at checkpoint {checkpoint_id[:8]}..."
        )
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
        with an isolated session. The public ``fork`` method persists it only
        after every checkpoint/thread setup step succeeds.
        
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

        self._require_runtime_checkpoint(
            source_execution.id,
            checkpoint_id,
            operation="fork",
        )
        
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
        # Graph selection is persisted in execution metadata and takes
        # precedence over state during SDK resume. Only the two explicit
        # control fields may cross this state-to-metadata boundary.
        if new_initial_state:
            for graph_identity_key in (
                "_variant_config",
                "_workflow_variant",
            ):
                if graph_identity_key in new_initial_state:
                    forked_execution.metadata[graph_identity_key] = copy.deepcopy(
                        new_initial_state[graph_identity_key]
                    )
        
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

            target_checkpoint_id = (
                updated
                if isinstance(updated, str) and updated
                else None
            )
            if target_checkpoint_id is None:
                target_history = self._state_adapter.get_checkpoint_history()
                for checkpoint in target_history or []:
                    candidate = checkpoint.get("checkpoint_id") or checkpoint.get(
                        "id"
                    )
                    if (
                        isinstance(candidate, str)
                        and candidate
                        and candidate != checkpoint_id
                    ):
                        target_checkpoint_id = candidate
                        break
            if (
                target_checkpoint_id is None
                or target_checkpoint_id == checkpoint_id
            ):
                raise RuntimeError(
                    "Could not establish a target-owned fork checkpoint"
                )
            forked_execution.checkpoint_id = target_checkpoint_id
            forked_execution.metadata["resume_checkpoint_id"] = (
                target_checkpoint_id
            )

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
        observer = getattr(self, "_local_observer", None)
        if observer:
            observer["checkpoint"](execution, checkpoint_id, state if state is not None else execution.state, name)
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
        """Check that both persistence and workflow runtime can recover."""
        adapter_support = bool(self._state_adapter.supports_time_travel())
        if not adapter_support or self._workflow_runtime is None:
            return adapter_support
        return self._runtime_checkpoint_recovery_capability() is True
    
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
        self._require_runtime_outcome_reconciled(
            execution,
            operation="update execution state",
        )

        if not self._activate_execution_session(execution):
            return False

        if self._workflow_runtime_explicit:
            self._require_explicit_runtime_bound(
                operation="update execution state"
            )
            runtime = self._workflow_runtime
            shares_checkpoint_store = getattr(
                runtime,
                "shares_checkpoint_store",
                None,
            )
            try:
                shared_owner = callable(shares_checkpoint_store) and bool(
                    shares_checkpoint_store(self._state_adapter)
                )
            except Exception:
                shared_owner = False
            checkpoint_id = (execution.metadata or {}).get(
                "resume_checkpoint_id"
            ) or execution.checkpoint_id
            if (
                not shared_owner
                or self._runtime_checkpoint_recovery_capability() is not True
                or not isinstance(checkpoint_id, str)
                or not checkpoint_id
            ):
                raise RuntimeError(
                    "Explicit workflow runtime has no owned runtime checkpoint "
                    "for state update"
                )
            self._require_runtime_checkpoint(
                execution.id,
                checkpoint_id,
                operation="state update",
            )
            patch_checkpoint = getattr(runtime, "patch_checkpoint", None)
            if not callable(patch_checkpoint):
                raise RuntimeError(
                    "Explicit workflow runtime cannot patch its owned checkpoint"
                )
            continuation_checkpoint_id = patch_checkpoint(
                execution.id,
                checkpoint_id,
                dict(values),
            )
            if (
                not isinstance(continuation_checkpoint_id, str)
                or not continuation_checkpoint_id
            ):
                raise RuntimeError(
                    "Workflow runtime returned no continuation checkpoint"
                )
            execution.state.workflow_variables.update(values)
            execution.checkpoint_id = continuation_checkpoint_id
            execution.metadata = dict(execution.metadata or {})
            execution.metadata["resume_checkpoint_id"] = (
                continuation_checkpoint_id
            )
            self._exec_repo.update(execution)
            self._commit()
            return True

        # Compatibility runtimes share the legacy state adapter by
        # construction, so retain the established adapter mutation path.
        if self._state_adapter.update_state(values):
            execution.state.workflow_variables.update(values)
            self._exec_repo.update(execution)
            self._commit()
            return True
        return False
    
    def rollback_to_node(self, execution_id: str, node_id: str) -> Execution:
        """Rollback to after a specific node completed."""
        execution = self._get_execution(execution_id)
        self._require_runtime_outcome_reconciled(
            execution,
            operation="rollback to node",
        )
        self._require_explicit_runtime_bound(operation="rollback to node")
        if not self.supports_time_travel():
            raise ValueError("Adapter does not support time-travel")
        
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
