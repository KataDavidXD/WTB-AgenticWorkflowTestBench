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

import logging
import uuid
from typing import Optional, Dict, Any, List, TYPE_CHECKING
from datetime import datetime

from wtb.domain.interfaces.execution_controller import IExecutionController
from wtb.domain.interfaces.state_adapter import IStateAdapter, CheckpointTrigger
from wtb.domain.interfaces.node_executor import INodeExecutor, NodeExecutionResult
from wtb.domain.interfaces.repositories import IExecutionRepository, IWorkflowRepository

if TYPE_CHECKING:
    from wtb.domain.interfaces.unit_of_work import IUnitOfWork
    from wtb.domain.interfaces.file_tracking import IFileTrackingService

logger = logging.getLogger(__name__)
from wtb.domain.models import (
    Execution,
    ExecutionState,
    ExecutionStatus,
    TestWorkflow,
    WorkflowNode,
)


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
        context: Dict[str, Any]
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
    
    def _execute_action(self, node: WorkflowNode, context: Dict[str, Any]) -> Any:
        """Execute an action node."""
        result = {
            "node_id": node.id,
            "node_name": node.name,
            "tool_name": node.tool_name,
        }
        
        if node.config:
            result.update(node.config)
        
        return result
    
    def _evaluate_decision(self, node: WorkflowNode, context: Dict[str, Any]) -> Dict[str, Any]:
        """Evaluate a decision node."""
        condition = node.config.get("condition", "True")
        
        try:
            result = eval(condition, {"__builtins__": {}}, context)
            return {"decision": bool(result), "condition": condition}
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
        node_executor: Optional[INodeExecutor] = None,
        unit_of_work: Optional["IUnitOfWork"] = None,
        file_tracking_service: Optional["IFileTrackingService"] = None,
        output_dir: Optional[str] = None,
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
    
    def _commit(self) -> None:
        """Commit UoW transaction if available (ACID: Durability)."""
        if self._uow is not None:
            self._uow.commit()
    
    def create_execution(
        self, 
        workflow: TestWorkflow,
        initial_state: Optional[Dict[str, Any]] = None,
        breakpoints: Optional[List[str]] = None
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
            workflow_id=workflow.id,
            status=ExecutionStatus.PENDING,
            state=state,
            breakpoints=breakpoints or [],
        )
        
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
    
    def run(self, execution_id: str, graph: Optional[Any] = None) -> Execution:
        """
        Start or continue execution.
        
        Execution Strategy:
        1. If graph is provided AND adapter supports LangGraph -> native LangGraph execution
        2. Otherwise -> use DefaultNodeExecutor with WTB workflow nodes
        """
        execution = self._get_execution(execution_id)
        
        # Check if we should use LangGraph execution
        if graph is not None and self._supports_langgraph_execution():
            return self._run_with_langgraph(execution, graph)
        
        # Fall back to legacy WTB execution
        return self._run_with_node_executor(execution)
    
    def _supports_langgraph_execution(self) -> bool:
        """Check if state adapter supports LangGraph native execution."""
        return (
            hasattr(self._state_adapter, 'execute') and
            hasattr(self._state_adapter, 'set_workflow_graph') and
            hasattr(self._state_adapter, 'get_checkpointer')
        )
    
    def _run_with_langgraph(self, execution: Execution, graph: Any) -> Execution:
        """
        Execute workflow using LangGraph native execution.
        
        Provides:
        - Automatic checkpointing at each super-step
        - Thread-based execution isolation
        - Time-travel support via checkpoint history
        """
        try:
            # Set graph on adapter - adapter recompiles with its checkpointer
            self._state_adapter.set_workflow_graph(graph, force_recompile=True)
            
            # Initialize session for this execution
            initial_state = execution.state.workflow_variables.copy()
            exec_state = ExecutionState(
                current_node_id="__start__",
                workflow_variables=initial_state,
            )
            session_id = self._state_adapter.initialize_session(execution.id, exec_state)
            execution.session_id = session_id
            
            # Mark execution as running
            execution.start()
            
            # Execute via LangGraph
            final_state = self._state_adapter.execute(initial_state)
            
            # Update execution with results
            execution.state.workflow_variables = final_state if isinstance(final_state, dict) else {}
            execution.state.node_results["final"] = final_state
            
            # Extract common result fields
            if isinstance(final_state, dict):
                if "answer" in final_state:
                    execution.state.workflow_variables["answer"] = final_state["answer"]
                if "messages" in final_state:
                    execution.state.execution_path = final_state.get("messages", [])
            
            # Track output files if file tracking is enabled
            if self._file_tracking and self._file_tracking.is_available():
                self._track_output_files(execution, final_state)
            
            execution.complete()
            
        except Exception as e:
            logger.error(f"LangGraph execution failed: {e}")
            execution.fail(str(e), execution.state.current_node_id)
        
        # Persist final state
        self._exec_repo.update(execution)
        self._commit()
        
        return execution
    
    def _track_output_files(self, execution: Execution, final_state: Any) -> None:
        """Write output files to disk and track them."""
        import os
        from pathlib import Path
        
        if not isinstance(final_state, dict):
            return
        
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
        
        # Determine output directory
        if self._output_dir:
            output_dir = Path(self._output_dir)
        else:
            output_dir = Path("outputs")
        
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Write files to disk
        written_paths: List[str] = []
        for filename, content in output_files_data.items():
            try:
                file_path = output_dir / filename
                file_path.parent.mkdir(parents=True, exist_ok=True)
                
                if isinstance(content, bytes):
                    file_path.write_bytes(content)
                elif isinstance(content, str):
                    file_path.write_text(content, encoding="utf-8")
                else:
                    import json
                    file_path.write_text(json.dumps(content, indent=2), encoding="utf-8")
                
                written_paths.append(str(file_path.absolute()))
            except Exception as e:
                logger.warning(f"Failed to write output file {filename}: {e}")
        
        if not written_paths:
            return
        
        # Track written files
        try:
            tracking_result = self._file_tracking.track_files(
                file_paths=written_paths,
                message=f"Execution {execution.id} output files",
            )
            
            execution.state.workflow_variables["_file_tracking_result"] = {
                "commit_id": tracking_result.commit_id,
                "files_tracked": tracking_result.files_tracked,
                "total_size_bytes": tracking_result.total_size_bytes,
            }
            
            logger.info(
                f"Tracked {tracking_result.files_tracked} files "
                f"for execution {execution.id}"
            )
        except Exception as e:
            logger.warning(f"File tracking failed (non-fatal): {e}")
    
    def _run_with_node_executor(self, execution: Execution) -> Execution:
        """Legacy execution using DefaultNodeExecutor with WTB workflow nodes."""
        workflow = self._get_workflow(execution.workflow_id)
        
        # Start execution if pending
        if execution.status == ExecutionStatus.PENDING:
            execution.start()
        elif execution.status == ExecutionStatus.PAUSED:
            execution.resume()
        else:
            raise RuntimeError(f"Cannot run execution in status {execution.status.value}")
        
        # Ensure state adapter session is set
        if execution.session_id:
            self._state_adapter.set_current_session(execution.session_id)
        
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
                    execution.pause()
                    break
                
                # Get node
                node = workflow.get_node(current_node_id)
                if not node:
                    raise ValueError(f"Node {current_node_id} not found in workflow")
                
                # Create checkpoint before node execution
                entry_cp_id = self._create_checkpoint(
                    execution,
                    current_node_id,
                    f"Before: {current_node_id}"
                )
                
                # Mark node as started
                self._state_adapter.mark_node_started(current_node_id, entry_cp_id)
                
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
                
                # Create checkpoint after node execution
                exit_cp_id = self._create_checkpoint(
                    execution,
                    current_node_id,
                    f"After: {current_node_id}"
                )
                
                # Mark node as completed
                self._state_adapter.mark_node_completed(current_node_id, exit_cp_id)
                
                # Determine next node
                next_node_id = self._determine_next_node(workflow, execution, result.output)
                execution.state.current_node_id = next_node_id
        
        except Exception as e:
            execution.fail(str(e), execution.state.current_node_id)
        
        # Persist final state
        self._exec_repo.update(execution)
        self._commit()
        
        return execution
    
    def pause(self, execution_id: str) -> Execution:
        """Pause execution at current position."""
        execution = self._get_execution(execution_id)
        
        if not execution.can_pause():
            raise ValueError(f"Cannot pause execution in status {execution.status.value}")
        
        self._create_checkpoint(
            execution,
            execution.state.current_node_id or "unknown",
            "Manual Pause"
        )
        
        execution.pause()
        self._exec_repo.update(execution)
        self._commit()
        
        return execution
    
    def resume(
        self, 
        execution_id: str, 
        modified_state: Optional[Dict[str, Any]] = None
    ) -> Execution:
        """Resume paused execution."""
        execution = self._get_execution(execution_id)
        
        if not execution.can_resume():
            raise ValueError(f"Cannot resume execution in status {execution.status.value}")
        
        if modified_state:
            execution.state.workflow_variables.update(modified_state)
        
        return self.run(execution_id)
    
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
        
        Args:
            execution_id: Execution to rollback
            checkpoint_id: Checkpoint ID (UUID string)
        """
        execution = self._get_execution(execution_id)
        
        if not execution.can_rollback():
            raise ValueError(f"Cannot rollback execution in status {execution.status.value}")
        
        # v1.8: Initialize session for rollback (required by LangGraphStateAdapter)
        # This connects the adapter to the execution's checkpoint history
        if hasattr(self._state_adapter, 'initialize_session'):
            try:
                # Use existing state from execution, or create minimal state
                initial_state = execution.state or ExecutionState(
                    current_node_id="__rollback__",
                    workflow_variables={},
                )
                self._state_adapter.initialize_session(execution_id, initial_state)
            except Exception as init_err:
                logger.debug(f"Session init for rollback: {init_err}")
        
        # Perform state rollback via state adapter
        restored_state = self._state_adapter.rollback(checkpoint_id)
        
        # Restore files from restored state
        files_restored = False
        file_restore_error = None
        files_restored_count = 0
        if self._file_tracking and self._file_tracking.is_available() and self._output_dir:
            try:
                import json
                from pathlib import Path
                
                output_files_data = None
                if isinstance(restored_state, dict):
                    output_files_data = restored_state.get("_output_files")
                elif isinstance(restored_state, ExecutionState):
                    output_files_data = restored_state.workflow_variables.get("_output_files")
                
                if output_files_data and isinstance(output_files_data, dict):
                    output_dir = Path(self._output_dir)
                    output_dir.mkdir(parents=True, exist_ok=True)
                    
                    for filename, content in output_files_data.items():
                        try:
                            file_path = output_dir / filename
                            file_path.parent.mkdir(parents=True, exist_ok=True)
                            
                            if isinstance(content, bytes):
                                file_path.write_bytes(content)
                            elif isinstance(content, str):
                                file_path.write_text(content, encoding="utf-8")
                            else:
                                file_path.write_text(json.dumps(content, indent=2), encoding="utf-8")
                            
                            files_restored_count += 1
                        except Exception as e:
                            logger.warning(f"Failed to restore file {filename}: {e}")
                    
                    files_restored = files_restored_count > 0
            except Exception as e:
                file_restore_error = str(e)
                logger.warning(f"File restore failed (non-fatal): {e}")
        
        # Update execution
        execution.state = restored_state
        execution.status = ExecutionStatus.PAUSED
        execution.checkpoint_id = checkpoint_id
        
        # Store file restore status
        if self._file_tracking and self._output_dir:
            execution.state.workflow_variables["_file_restore_status"] = {
                "attempted": True,
                "success": files_restored,
                "files_restored": files_restored_count,
                "error": file_restore_error,
            }
        
        self._exec_repo.update(execution)
        self._commit()
        
        return execution
    
    def fork(
        self,
        execution_id: str,
        checkpoint_id: str,
        new_initial_state: Optional[Dict[str, Any]] = None
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
        # Get source execution
        source_execution = self._get_execution(execution_id)
        
        # Get workflow
        workflow = self._workflow_repo.get(source_execution.workflow_id)
        if not workflow:
            raise ValueError(f"Workflow '{source_execution.workflow_id}' not found")
        
        # v1.8: Initialize session for source execution (required by LangGraphStateAdapter
        # to load checkpoint from the execution's thread)
        if hasattr(self._state_adapter, 'initialize_session'):
            try:
                initial_state = source_execution.state or ExecutionState(
                    current_node_id="__fork__",
                    workflow_variables={},
                )
                self._state_adapter.initialize_session(execution_id, initial_state)
            except Exception as init_err:
                logger.debug(f"Session init for fork: {init_err}")
        
        # Load checkpoint state
        checkpoint_state = self._state_adapter.load_checkpoint(checkpoint_id)
        
        # Merge checkpoint state with new_initial_state
        forked_state_vars = {}
        if checkpoint_state and isinstance(checkpoint_state, ExecutionState):
            forked_state_vars = checkpoint_state.workflow_variables.copy()
        elif checkpoint_state and isinstance(checkpoint_state, dict):
            forked_state_vars = checkpoint_state.copy()
        
        if new_initial_state:
            forked_state_vars.update(new_initial_state)
        
        # Create new execution ID
        fork_execution_id = str(uuid.uuid4())
        
        # Create new execution state
        forked_exec_state = ExecutionState(
            current_node_id=workflow.entry_point,
            workflow_variables=forked_state_vars,
            execution_path=[],
            node_results={},
        )
        
        # Create new execution record
        forked_execution = Execution(
            id=fork_execution_id,
            workflow_id=source_execution.workflow_id,
            status=ExecutionStatus.PENDING,
            state=forked_exec_state,
            metadata={
                "forked_from": execution_id,
                "source_checkpoint_id": checkpoint_id,
                "fork_type": "checkpoint_fork",
            },
        )
        
        # Initialize session for new execution
        session_id = self._state_adapter.initialize_session(
            execution_id=fork_execution_id,
            initial_state=forked_exec_state,
        )
        forked_execution.session_id = session_id
        
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
        name: str
    ) -> str:
        """Create a checkpoint via state adapter. Returns checkpoint_id (string)."""
        checkpoint_id = self._state_adapter.save_checkpoint(
            state=execution.state,
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
    ) -> Optional[str]:
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
        variables: Dict[str, Any],
        last_result: Any
    ) -> bool:
        """Evaluate an edge condition."""
        context = {**variables, "_last_result": last_result}
        try:
            return bool(eval(condition, {"__builtins__": {}}, context))
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
    
    def get_checkpoint_history(self, execution_id: str) -> List[Dict[str, Any]]:
        """Get full checkpoint history for time-travel."""
        execution = self._get_execution(execution_id)
        
        if execution.session_id:
            self._state_adapter.set_current_session(
                execution.session_id,
                execution_id=execution.id,
            )
        
        return self._state_adapter.get_checkpoint_history()
    
    def update_execution_state(
        self, 
        execution_id: str, 
        values: Dict[str, Any]
    ) -> bool:
        """Update execution state mid-execution (human-in-the-loop)."""
        execution = self._get_execution(execution_id)
        
        if execution.session_id:
            self._state_adapter.set_current_session(execution.session_id)
        
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
        
        if execution.session_id:
            self._state_adapter.set_current_session(execution.session_id)
        
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
