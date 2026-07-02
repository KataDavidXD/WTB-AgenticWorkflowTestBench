"""
Async Execution Controller - Non-blocking workflow orchestration.

Created: 2026-01-28
Status: Active
Reference: ASYNC_ARCHITECTURE_PLAN.md §4.2.1

Design Principles:
- SOLID: SRP (orchestration only), DIP (depends on abstractions)
- ACID: Async UoW transactions ensure atomicity
- Non-blocking: All I/O operations are async

Architecture:
    API Route (async)
           │
           ▼
    ExecutionService
           │
           ▼
    AsyncExecutionController
           │
           ├──► IAsyncStateAdapter (state management)
           ├──► IAsyncUnitOfWork (persistence)
           └──► IAsyncFileTrackingService (file tracking)
"""

from contextvars import ContextVar
from typing import Optional, Dict, Any, List, AsyncIterator, Callable, TYPE_CHECKING
from dataclasses import dataclass
from datetime import datetime
import logging
import uuid

from wtb.domain.interfaces.async_state_adapter import IAsyncStateAdapter
from wtb.domain.interfaces.async_unit_of_work import IAsyncUnitOfWork
from wtb.domain.interfaces.async_file_tracking import IAsyncFileTrackingService
from wtb.domain.models.workflow import Execution, ExecutionState, ExecutionStatus
from wtb.domain.models.outbox import OutboxEvent, OutboxEventType

if TYPE_CHECKING:
    from langgraph.graph import StateGraph

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Result Types
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class AsyncExecutionResult:
    """Result of async execution."""
    execution_id: str
    status: ExecutionStatus
    final_state: Dict[str, Any]
    checkpoint_id: Optional[str] = None
    error_message: Optional[str] = None
    duration_ms: Optional[int] = None
    
    @property
    def is_success(self) -> bool:
        return self.status == ExecutionStatus.COMPLETED


@dataclass
class AsyncStreamEvent:
    """Event from async streaming execution."""
    event_type: str  # "update", "checkpoint", "error", "complete"
    node_id: Optional[str]
    state: Dict[str, Any]
    timestamp: datetime = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


# ═══════════════════════════════════════════════════════════════════════════════
# Async Execution Controller
# ═══════════════════════════════════════════════════════════════════════════════


class AsyncExecutionController:
    """
    Async Execution Controller for non-blocking workflow orchestration.
    
    Orchestrates workflow execution with full async support:
    - Non-blocking execution via arun()
    - Streaming via astream()
    - Async checkpoint operations
    - Async file tracking
    - ACID transaction management
    
    SOLID Compliance:
    - SRP: Orchestration only, delegates to adapters and services
    - OCP: New adapters via IAsyncStateAdapter interface
    - DIP: Depends on abstractions (interfaces), not implementations
    
    ACID Compliance:
    - Atomicity: Async UoW transactions - all changes committed together
    - Consistency: State validation before commit
    - Isolation: Session-level isolation per execution
    - Durability: Async commit to persistent storage
    
    Transaction Consistency:
    - All DB operations within single UoW transaction
    - Outbox pattern for cross-system consistency
    - CHECKPOINT_VERIFY events for LangGraph-WTB sync verification
    """
    
    def __init__(
        self,
        state_adapter: IAsyncStateAdapter,
        uow_factory: Callable[[], IAsyncUnitOfWork],
        file_tracking_service: Optional[IAsyncFileTrackingService] = None,
    ):
        """
        Initialize async execution controller.
        
        Args:
            state_adapter: Async state adapter for checkpoint management
            uow_factory: Factory for creating async UoW instances
            file_tracking_service: Optional async file tracking service
        """
        self._state_adapter = state_adapter
        self._uow_factory = uow_factory
        self._file_tracking = file_tracking_service
        self._current_execution_var: ContextVar[Optional[Execution]] = ContextVar(
            "async_exec_current_execution", default=None
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Main Execution Methods
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def arun(
        self, 
        execution_id: str, 
        graph: Optional["StateGraph"] = None,
        track_output_files: Optional[List[str]] = None,
    ) -> AsyncExecutionResult:
        """
        Run workflow asynchronously.
        
        Non-blocking execution that yields control to event loop
        during I/O operations (LLM calls, checkpoints, file I/O).
        
        CROSS-DB CONSISTENCY: After execution completes, a CHECKPOINT_VERIFY
        outbox event is created in the same transaction. The OutboxProcessor
        verifies LangGraph checkpoints match WTB execution records.
        
        Args:
            execution_id: WTB execution ID
            graph: Optional LangGraph StateGraph (if not already set)
            track_output_files: Optional list of output file paths to track
            
        Returns:
            AsyncExecutionResult with execution outcome
        """
        start_time = datetime.now()
        
        async with self._uow_factory() as uow:
            # Load execution from database
            execution = await uow.executions.aget(execution_id)
            if not execution:
                raise ValueError(f"Execution not found: {execution_id}")
            
            self._current_execution_var.set(execution)
            
            # Set graph on adapter if provided
            if graph:
                if hasattr(self._state_adapter, 'set_workflow_graph'):
                    self._state_adapter.set_workflow_graph(graph, force_recompile=True)
                elif hasattr(self._state_adapter, 'aset_workflow_graph'):
                    await self._state_adapter.aset_workflow_graph(graph, force_recompile=True)
            
            try:
                # Initialize session
                initial_state = execution.state.workflow_variables.copy()
                session_id = await self._state_adapter.ainitialize_session(
                    execution.id, 
                    execution.state
                )
                
                # Update execution record
                execution.session_id = session_id
                execution.status = ExecutionStatus.RUNNING
                execution.started_at = datetime.now()
                await uow.executions.aupdate(execution)
                
                # Execute via async LangGraph
                final_state = await self._state_adapter.aexecute(initial_state)
                
                # Update execution with results
                execution.state.workflow_variables = final_state
                
                # Track output files asynchronously if configured
                checkpoint_id = None
                if track_output_files and self._file_tracking:
                    tracking_result = await self._file_tracking.atrack_files(
                        file_paths=track_output_files,
                        message=f"Execution {execution_id} output files",
                    )
                    checkpoint_id = tracking_result.commit_id
                
                # Get final checkpoint ID from state adapter
                if not checkpoint_id:
                    current_state = await self._state_adapter.aget_current_state()
                    checkpoint_id = current_state.get("_checkpoint_id")
                
                # Mark execution as completed
                execution.status = ExecutionStatus.COMPLETED
                execution.completed_at = datetime.now()
                execution.checkpoint_id = checkpoint_id
                await uow.executions.aupdate(execution)
                
                # Create CHECKPOINT_VERIFY outbox event for cross-DB consistency
                verify_event = OutboxEvent(
                    event_id=str(uuid.uuid4()),
                    event_type=OutboxEventType.CHECKPOINT_VERIFY,
                    aggregate_type="Execution",
                    aggregate_id=execution_id,
                    payload={
                        "execution_id": execution_id,
                        "checkpoint_id": checkpoint_id,
                        "session_id": session_id,
                    }
                )
                await uow.outbox.aadd(verify_event)
                
                # Commit all changes atomically
                await uow.acommit()
                
                duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
                
                logger.info(
                    f"Async execution completed: {execution_id}, "
                    f"duration={duration_ms}ms, checkpoint={checkpoint_id}"
                )
                
                return AsyncExecutionResult(
                    execution_id=execution_id,
                    status=ExecutionStatus.COMPLETED,
                    final_state=final_state,
                    checkpoint_id=checkpoint_id,
                    duration_ms=duration_ms,
                )
                
            except Exception as e:
                # Mark execution as failed
                execution.status = ExecutionStatus.FAILED
                execution.error_message = str(e)
                execution.completed_at = datetime.now()
                await uow.executions.aupdate(execution)
                await uow.acommit()
                
                duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
                
                logger.error(
                    f"Async execution failed: {execution_id}, error={e}"
                )
                
                return AsyncExecutionResult(
                    execution_id=execution_id,
                    status=ExecutionStatus.FAILED,
                    final_state={},
                    error_message=str(e),
                    duration_ms=duration_ms,
                )
    
    async def astream(
        self, 
        execution_id: str, 
        graph: Optional["StateGraph"] = None,
        stream_mode: str = "updates",
    ) -> AsyncIterator[AsyncStreamEvent]:
        """
        Stream workflow execution asynchronously.
        
        Yields events as workflow progresses. Handles errors gracefully
        and ensures execution status is updated even on failure.
        
        Args:
            execution_id: WTB execution ID
            graph: Optional LangGraph StateGraph
            stream_mode: LangGraph stream mode
            
        Yields:
            AsyncStreamEvent for each state update
        """
        async with self._uow_factory() as uow:
            execution = await uow.executions.aget(execution_id)
            if not execution:
                raise ValueError(f"Execution not found: {execution_id}")
            
            self._current_execution_var.set(execution)
            
            # Set graph
            if graph and hasattr(self._state_adapter, 'set_workflow_graph'):
                self._state_adapter.set_workflow_graph(graph, force_recompile=True)
            
            # Initialize session
            initial_state = execution.state.workflow_variables.copy()
            session_id = await self._state_adapter.ainitialize_session(
                execution.id, 
                execution.state
            )
            
            execution.session_id = session_id
            execution.status = ExecutionStatus.RUNNING
            execution.started_at = datetime.now()
            await uow.executions.aupdate(execution)
            await uow.acommit()
        
        # Stream execution (outside transaction for real-time events)
        try:
            async for event in self._state_adapter.astream(initial_state, stream_mode):
                # Extract node info from event
                node_id = None
                if isinstance(event, dict):
                    node_id = list(event.keys())[0] if event else None
                
                yield AsyncStreamEvent(
                    event_type="update",
                    node_id=node_id,
                    state=event if isinstance(event, dict) else {"value": event},
                )
            
            # Stream completed successfully
            async with self._uow_factory() as uow:
                execution = await uow.executions.aget(execution_id)
                if execution:
                    execution.status = ExecutionStatus.COMPLETED
                    execution.completed_at = datetime.now()
                    await uow.executions.aupdate(execution)
                    await uow.acommit()
            
            yield AsyncStreamEvent(
                event_type="complete",
                node_id=None,
                state=await self._state_adapter.aget_current_state(),
            )
            
        except Exception as e:
            # Update status on error
            async with self._uow_factory() as uow:
                execution = await uow.executions.aget(execution_id)
                if execution:
                    execution.status = ExecutionStatus.FAILED
                    execution.error_message = str(e)
                    execution.completed_at = datetime.now()
                    await uow.executions.aupdate(execution)
                    await uow.acommit()
            
            yield AsyncStreamEvent(
                event_type="error",
                node_id=None,
                state={"error": str(e)},
            )
            
            logger.error(f"Stream execution failed: {execution_id}, error={e}")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Checkpoint Operations
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def asave_checkpoint(
        self,
        node_id: str,
        name: Optional[str] = None,
        track_files: Optional[List[str]] = None,
    ) -> str:
        """
        Save checkpoint with optional file tracking.
        
        ACID: All operations in single transaction.
        
        Args:
            node_id: Node that triggered checkpoint
            name: Optional checkpoint name
            track_files: Optional files to track with checkpoint
            
        Returns:
            Checkpoint ID
        """
        if not self._current_execution_var.get():
            raise RuntimeError("No active execution")
        
        async with self._uow_factory() as uow:
            from wtb.domain.models.workflow import CheckpointTrigger
            
            # Save state checkpoint
            current_state = await self._state_adapter.aget_current_state()
            checkpoint_id = await self._state_adapter.asave_checkpoint(
                state=ExecutionState(workflow_variables=current_state),
                node_id=node_id,
                trigger=CheckpointTrigger.NODE_COMPLETE,
                name=name,
            )
            
            # Track files if provided
            if track_files and self._file_tracking:
                await self._file_tracking.atrack_and_link(
                    checkpoint_id=checkpoint_id,
                    file_paths=track_files,
                    message=f"Checkpoint {name or node_id} files",
                )
            
            # Update execution record
            self._current_execution_var.get().checkpoint_id = checkpoint_id
            await uow.executions.aupdate(self._current_execution_var.get())
            
            await uow.acommit()
            
            return checkpoint_id
    
    async def arollback_to_checkpoint(self, checkpoint_id: str) -> ExecutionState:
        """
        Rollback to checkpoint asynchronously.
        
        Restores state and optionally files from checkpoint.
        
        Args:
            checkpoint_id: Checkpoint to rollback to
            
        Returns:
            ExecutionState after rollback
        """
        if not self._current_execution_var.get():
            raise RuntimeError("No active execution")
        
        async with self._uow_factory() as uow:
            # Rollback state
            state = await self._state_adapter.arollback(checkpoint_id)
            
            # Update execution record
            self._current_execution_var.get().state = state
            self._current_execution_var.get().checkpoint_id = checkpoint_id
            await uow.executions.aupdate(self._current_execution_var.get())
            
            await uow.acommit()
            
            logger.info(f"Rolled back to checkpoint: {checkpoint_id}")
            
            return state
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Fork Operations
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def afork(
        self,
        new_execution_id: str,
        from_checkpoint_id: Optional[str] = None,
    ) -> "AsyncExecutionController":
        """
        Create a fork of current execution asynchronously.
        
        ACID: Fork creation is atomic - either all records created or none.
        
        Args:
            new_execution_id: ID for the forked execution
            from_checkpoint_id: Optional checkpoint to fork from
            
        Returns:
            New AsyncExecutionController for the fork
        """
        if not self._current_execution_var.get():
            raise RuntimeError("No active execution")
        
        async with self._uow_factory() as uow:
            # Create forked state adapter
            fork_thread_id = f"wtb-{new_execution_id}"
            
            if hasattr(self._state_adapter, 'acreate_fork'):
                fork_adapter = await self._state_adapter.acreate_fork(
                    fork_thread_id=fork_thread_id,
                    from_checkpoint_id=from_checkpoint_id,
                )
            else:
                raise RuntimeError("State adapter does not support forking")
            
            # Create new execution record. A checkpoint fork is resumable from
            # the forked thread, so it starts PAUSED rather than PENDING.
            fork_execution = Execution(
                id=new_execution_id,
                workflow_id=self._current_execution_var.get().workflow_id,
                state=await self._state_adapter.aget_current_state() if not from_checkpoint_id else 
                      await self._state_adapter.aload_checkpoint(from_checkpoint_id),
                status=ExecutionStatus.PAUSED,
                session_id=fork_thread_id,
                metadata={
                    **(self._current_execution_var.get().metadata or {}),
                    "forked_from": self._current_execution_var.get().id,
                    "source_checkpoint_id": from_checkpoint_id,
                    "fork_type": "checkpoint_fork",
                },
            )
            
            await uow.executions.aadd(fork_execution)
            await uow.acommit()
            
            # Create new controller for fork
            fork_controller = AsyncExecutionController(
                state_adapter=fork_adapter,
                uow_factory=self._uow_factory,
                file_tracking_service=self._file_tracking,
            )
            fork_controller._current_execution_var.set(fork_execution)
            
            logger.info(
                f"Created async fork: {self._current_execution_var.get().id} -> {new_execution_id}"
            )
            
            return fork_controller
    
    # ═══════════════════════════════════════════════════════════════════════════
    # State Accessors
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def aget_current_state(self) -> Dict[str, Any]:
        """Get current execution state."""
        return await self._state_adapter.aget_current_state()
    
    async def aget_checkpoints(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get checkpoint history for current execution."""
        if hasattr(self._state_adapter, 'aget_checkpoints'):
            return await self._state_adapter.aget_checkpoints(limit)
        return []
    
    @property
    def current_execution(self) -> Optional[Execution]:
        """Get current execution record."""
        return self._current_execution_var.get()


# ═══════════════════════════════════════════════════════════════════════════════
# Factory
# ═══════════════════════════════════════════════════════════════════════════════


class AsyncExecutionControllerFactory:
    """Factory for creating AsyncExecutionController instances."""
    
    def __init__(
        self,
        uow_factory: Callable[[], IAsyncUnitOfWork],
        file_tracking_service: Optional[IAsyncFileTrackingService] = None,
    ):
        self._uow_factory = uow_factory
        self._file_tracking = file_tracking_service
    
    async def acreate(
        self,
        state_adapter: IAsyncStateAdapter,
    ) -> AsyncExecutionController:
        """
        Create async execution controller with provided adapter.
        
        Args:
            state_adapter: Async state adapter to use
            
        Returns:
            Configured AsyncExecutionController
        """
        return AsyncExecutionController(
            state_adapter=state_adapter,
            uow_factory=self._uow_factory,
            file_tracking_service=self._file_tracking,
        )
    
    async def acreate_with_langgraph(
        self,
        checkpointer_type: str = "memory",
        db_path: Optional[str] = None,
    ) -> AsyncExecutionController:
        """
        Create controller with LangGraph async state adapter.
        
        Args:
            checkpointer_type: "memory", "sqlite", or "postgres"
            db_path: Database path for sqlite/postgres
            
        Returns:
            AsyncExecutionController with LangGraph adapter
        """
        from wtb.infrastructure.adapters.async_langgraph_state_adapter import (
            AsyncLangGraphStateAdapter,
            LangGraphConfig,
            CheckpointerType,
        )
        
        config = LangGraphConfig(
            checkpointer_type=CheckpointerType(checkpointer_type),
            db_path=db_path,
        )
        
        state_adapter = AsyncLangGraphStateAdapter(config)
        
        return AsyncExecutionController(
            state_adapter=state_adapter,
            uow_factory=self._uow_factory,
            file_tracking_service=self._file_tracking,
        )
