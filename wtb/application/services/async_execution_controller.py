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
from pathlib import Path
import asyncio
import logging
from threading import Lock
import uuid

from wtb.domain.interfaces.async_state_adapter import IAsyncStateAdapter
from wtb.domain.interfaces.async_unit_of_work import IAsyncUnitOfWork
from wtb.domain.interfaces.async_file_tracking import IAsyncFileTrackingService
from wtb.domain.interfaces.state_adapter import CheckpointTrigger
from wtb.domain.models.workflow import Execution, ExecutionState, ExecutionStatus
from wtb.domain.models.outbox import OutboxEvent, OutboxEventType

if TYPE_CHECKING:
    from langgraph.graph import StateGraph

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Result Types
# ═══════════════════════════════════════════════════════════════════════════════


_ADAPTER_LOCK_ATTRIBUTE = "_wtb_async_execution_lock"
_ADAPTER_LOCK_CREATION = Lock()


def _shared_state_adapter_lock(state_adapter: IAsyncStateAdapter) -> asyncio.Lock:
    """Return one operation lock shared by every controller for an adapter."""
    with _ADAPTER_LOCK_CREATION:
        adapter_vars = getattr(state_adapter, "__dict__", None)
        if not isinstance(adapter_vars, dict):
            raise ValueError(
                "Async state adapter must support shared operation locking"
            )
        lock = adapter_vars.get(_ADAPTER_LOCK_ATTRIBUTE)
        if lock is None:
            lock = asyncio.Lock()
            setattr(state_adapter, _ADAPTER_LOCK_ATTRIBUTE, lock)
        if not isinstance(lock, asyncio.Lock):
            raise ValueError("Async state adapter operation lock is invalid")
        return lock


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
        self._state_adapter_lock = _shared_state_adapter_lock(state_adapter)

    async def _activate_current_adapter_session(self) -> None:
        """Bind the shared adapter to this task's execution before state access."""
        execution = self._current_execution_var.get()
        if execution is None:
            raise RuntimeError("No active async execution")
        if not execution.session_id:
            raise RuntimeError("Active async execution has no session")

        activate = getattr(self._state_adapter, "aset_current_session", None)
        if not callable(activate):
            raise RuntimeError(
                "Async state adapter cannot activate an execution session"
            )
        activated = await activate(
            execution.session_id,
            execution_id=execution.id,
        )
        if activated is False:
            raise RuntimeError(
                f"Could not activate async execution session {execution.session_id}"
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
        """Run with exclusive access to the adapter's mutable session and graph."""
        try:
            async with self._state_adapter_lock:
                return await self._arun_with_adapter(
                    execution_id,
                    graph=graph,
                    track_output_files=track_output_files,
                )
        except asyncio.CancelledError as cancellation:
            try:
                await self._apersist_cancelled_execution(execution_id)
            except Exception as persistence_error:
                cancellation.add_note(
                    "Additionally failed to persist CANCELLED execution state: "
                    f"{persistence_error}"
                )
                raise cancellation from persistence_error
            raise

    async def _arun_with_adapter(
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
                
                # Resolve the real LangGraph checkpoint before linking files.
                # CAS commit IDs and graph checkpoint IDs are separate domains.
                current_state = await self._state_adapter.aget_current_state()
                checkpoint_id = await self._aresolve_final_checkpoint_id(
                    current_state=current_state,
                    final_state=final_state,
                    current_node_id=execution.state.current_node_id,
                )

                if track_output_files and self._file_tracking:
                    tracking_result = await self._atrack_and_link(
                        uow,
                        checkpoint_id=checkpoint_id,
                        file_paths=track_output_files,
                        message=f"Execution {execution_id} output files",
                    )
                    if not tracking_result or not getattr(
                        tracking_result, "commit_id", None
                    ):
                        raise RuntimeError("File tracking did not produce a file commit")
                    expected_count = len(track_output_files)
                    tracked_count = getattr(tracking_result, "files_tracked", None)
                    if tracked_count != expected_count:
                        raise RuntimeError(
                            f"Expected {expected_count} files to be tracked, "
                            f"but tracked {tracked_count}"
                        )
                
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
                # The active transaction may be unusable (especially after a
                # commit failure). Roll it back, then persist FAILED through a
                # fresh UoW without allowing secondary errors to mask `e`.
                try:
                    await uow.arollback()
                except Exception as rollback_error:
                    e.add_note(
                        "Additionally failed to roll back the execution transaction: "
                        f"{rollback_error}"
                    )
                    logger.error(
                        "Could not roll back failed async execution transaction: "
                        f"{rollback_error}"
                    )

                await self._apersist_failed_execution(execution_id, e)
                execution.status = ExecutionStatus.FAILED
                execution.error_message = str(e)
                execution.completed_at = datetime.now()

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

    async def _apersist_failed_execution(
        self,
        execution_id: str,
        primary_error: Exception,
    ) -> None:
        """Persist FAILED through a clean transaction or surface the primary error."""
        try:
            async with self._uow_factory() as failure_uow:
                failed_execution = await failure_uow.executions.aget(execution_id)
                if failed_execution is None:
                    raise RuntimeError(
                        f"Execution not found while persisting failure: {execution_id}"
                    )
                failed_execution.status = ExecutionStatus.FAILED
                failed_execution.error_message = str(primary_error)
                failed_execution.completed_at = datetime.now()
                await failure_uow.executions.aupdate(failed_execution)
                await failure_uow.acommit()
        except Exception as persistence_error:
            primary_error.add_note(
                "Additionally failed to persist FAILED execution state: "
                f"{persistence_error}"
            )
            logger.error(
                "Could not persist FAILED async execution state: "
                f"{persistence_error}"
            )
            raise primary_error from persistence_error

    async def _aresolve_final_checkpoint_id(
        self,
        current_state: Dict[str, Any],
        final_state: Dict[str, Any],
        current_node_id: Optional[str],
    ) -> str:
        """Resolve or create the final graph checkpoint ID."""
        checkpoint_id = None
        if isinstance(current_state, dict):
            checkpoint_id = current_state.get("_checkpoint_id")
        if checkpoint_id:
            return str(checkpoint_id)

        get_checkpoints = getattr(self._state_adapter, "aget_checkpoints", None)
        if callable(get_checkpoints):
            history = await get_checkpoints(limit=1)
            for checkpoint in history or []:
                if not isinstance(checkpoint, dict):
                    continue
                checkpoint_id = checkpoint.get("checkpoint_id") or checkpoint.get("id")
                if checkpoint_id:
                    return str(checkpoint_id)

        save_checkpoint = getattr(self._state_adapter, "asave_checkpoint", None)
        if not callable(save_checkpoint):
            raise RuntimeError("State adapter did not expose a final checkpoint")

        if not current_node_id:
            raise RuntimeError("Cannot save final checkpoint without a current node")

        state_values = final_state or current_state or {}
        checkpoint_id = await save_checkpoint(
            state=ExecutionState(workflow_variables=dict(state_values or {})),
            node_id=current_node_id,
            trigger=CheckpointTrigger.AUTO,
            name="Final execution state",
        )
        if not checkpoint_id:
            raise RuntimeError("State adapter did not create a final checkpoint")
        return str(checkpoint_id)

    async def _atrack_and_link(
        self,
        uow: IAsyncUnitOfWork,
        *,
        checkpoint_id: str,
        file_paths: List[str],
        message: str,
    ):
        """Track files atomically in the execution's caller-owned UoW."""
        track_in_uow = getattr(
            self._file_tracking,
            "atrack_and_link_in_uow",
            None,
        )
        implementation = getattr(
            type(self._file_tracking),
            "atrack_and_link_in_uow",
            None,
        )
        if (
            not callable(track_in_uow)
            or implementation is IAsyncFileTrackingService.atrack_and_link_in_uow
        ):
            raise RuntimeError(
                "File tracking service must support a shared unit of work"
            )
        try:
            return await track_in_uow(
                uow,
                checkpoint_id=checkpoint_id,
                file_paths=file_paths,
                message=message,
            )
        except NotImplementedError as error:
            raise RuntimeError(
                "File tracking service must support a shared unit of work"
            ) from error
    
    async def astream(
        self,
        execution_id: str,
        graph: Optional["StateGraph"] = None,
        stream_mode: str = "updates",
    ) -> AsyncIterator[AsyncStreamEvent]:
        """Stream with exclusive access to the adapter session and graph."""
        async with self._state_adapter_lock:
            try:
                async for event in self._astream_with_adapter(
                    execution_id,
                    graph=graph,
                    stream_mode=stream_mode,
                ):
                    yield event
            except (asyncio.CancelledError, GeneratorExit):
                await self._apersist_cancelled_execution(execution_id)
                raise

    async def _apersist_cancelled_execution(self, execution_id: str) -> None:
        """Persist stream cancellation so an interrupted stream is terminal."""
        async with self._uow_factory() as uow:
            execution = await uow.executions.aget(execution_id)
            if execution is None:
                raise RuntimeError(
                    f"Execution not found while persisting cancellation: {execution_id}"
                )
            if execution.status not in (
                ExecutionStatus.COMPLETED,
                ExecutionStatus.FAILED,
                ExecutionStatus.CANCELLED,
            ):
                execution.cancel()
                await uow.executions.aupdate(execution)
                await uow.acommit()

    async def _astream_with_adapter(
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
        """Save a checkpoint without sharing mutable adapter context."""
        async with self._state_adapter_lock:
            return await self._asave_checkpoint_with_adapter(
                node_id,
                name=name,
                track_files=track_files,
            )

    async def _asave_checkpoint_with_adapter(
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
        await self._activate_current_adapter_session()

        async with self._uow_factory() as uow:
            # Save state checkpoint
            current_state = await self._state_adapter.aget_current_state()
            checkpoint_id = await self._state_adapter.asave_checkpoint(
                state=ExecutionState(workflow_variables=current_state),
                node_id=node_id,
                trigger=CheckpointTrigger.NODE_END,
                name=name,
            )
            
            # Track files if provided
            if track_files and self._file_tracking:
                await self._atrack_and_link(
                    uow,
                    checkpoint_id=checkpoint_id,
                    file_paths=track_files,
                    message=f"Checkpoint {name or node_id} files",
                )
            
            # Update execution record
            self._current_execution_var.get().checkpoint_id = checkpoint_id
            await uow.executions.aupdate(self._current_execution_var.get())
            
            await uow.acommit()
            
            return checkpoint_id
    
    async def arollback_to_checkpoint(
        self,
        checkpoint_id: str,
        restore_output_dir: Optional[Path] = None,
    ) -> ExecutionState:
        """Rollback without sharing mutable adapter context."""
        async with self._state_adapter_lock:
            return await self._arollback_with_adapter(
                checkpoint_id,
                restore_output_dir=restore_output_dir,
            )

    async def _arollback_with_adapter(
        self,
        checkpoint_id: str,
        restore_output_dir: Optional[Path] = None,
    ) -> ExecutionState:
        """Rollback state and, when requested, its linked files asynchronously.

        Without ``restore_output_dir`` this is explicitly a state-only rollback.
        When a directory is provided, linked files must be restored completely
        before the execution record is updated and committed.

        Args:
            checkpoint_id: Checkpoint to rollback to.
            restore_output_dir: Optional destination for checkpoint-linked files.

        Returns:
            ExecutionState after rollback.
        """
        current_execution = self._current_execution_var.get()
        if not current_execution:
            raise RuntimeError("No active execution")
        await self._activate_current_adapter_session()

        async with self._uow_factory() as uow:
            preflight_state = None
            if restore_output_dir is not None:
                if self._file_tracking is None:
                    raise RuntimeError(
                        "File restoration requested but no file tracking service is configured"
                    )
                # Load and restore files before moving the adapter checkpoint.
                # CAS/link failures therefore leave adapter state unchanged.
                preflight_state = await self._state_adapter.aload_checkpoint(
                    checkpoint_id
                )
                restored_count = await self._file_tracking.arestore_files(
                    checkpoint_id,
                    Path(restore_output_dir),
                )
                values = (
                    preflight_state.workflow_variables
                    if isinstance(preflight_state, ExecutionState)
                    else preflight_state
                )
                output_files = (
                    values.get("_output_files", {})
                    if isinstance(values, dict)
                    else {}
                )
                expected_count = (
                    len(output_files) if isinstance(output_files, dict) else 0
                )
                if expected_count > 0 and restored_count != expected_count:
                    raise RuntimeError(
                        f"Expected {expected_count} checkpoint files to be restored, "
                        f"but restored {restored_count}"
                    )
                if expected_count == 0 and restored_count <= 0:
                    raise RuntimeError(
                        "Expected at least 1 checkpoint file to be restored, "
                        f"but restored {restored_count}"
                    )

            state = await self._state_adapter.arollback(checkpoint_id)
            current_execution.state = state
            current_execution.checkpoint_id = checkpoint_id
            await uow.executions.aupdate(current_execution)
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
        """Fork without sharing mutable adapter context."""
        async with self._state_adapter_lock:
            return await self._afork_with_adapter(
                new_execution_id,
                from_checkpoint_id=from_checkpoint_id,
            )

    async def _afork_with_adapter(
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
        await self._activate_current_adapter_session()

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
        async with self._state_adapter_lock:
            await self._activate_current_adapter_session()
            return await self._state_adapter.aget_current_state()
    
    async def aget_checkpoints(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get checkpoint history for current execution."""
        async with self._state_adapter_lock:
            await self._activate_current_adapter_session()
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

        try:
            selected_type = CheckpointerType(checkpointer_type)
        except ValueError as error:
            raise ValueError(
                f"Unsupported async checkpointer type: {checkpointer_type!r}"
            ) from error

        if selected_type is CheckpointerType.MEMORY:
            config = LangGraphConfig.for_testing()
        elif selected_type is CheckpointerType.SQLITE:
            if not db_path:
                raise ValueError("db_path is required for sqlite checkpointer")
            config = LangGraphConfig.for_development(db_path)
        else:
            if not db_path:
                raise ValueError("db_path is required for postgres checkpointer")
            config = LangGraphConfig.for_production(db_path)
        
        state_adapter = AsyncLangGraphStateAdapter(config)
        
        return AsyncExecutionController(
            state_adapter=state_adapter,
            uow_factory=self._uow_factory,
            file_tracking_service=self._file_tracking,
        )
