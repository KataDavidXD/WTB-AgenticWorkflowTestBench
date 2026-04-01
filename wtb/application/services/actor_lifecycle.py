"""
ActorLifecycleManager - Manages Ray actor lifecycle for WTB operations.

Handles actor creation, reset, pause strategies (HOT/WARM/COLD), rollback,
and fork operations with proper resource management.

Design Principles:
- SOLID: Single responsibility (actor lifecycle management)
- ACID: Transactional state transitions with event sourcing
- DIP: Depends on abstractions (IEnvironmentProvider, WTBEventBus)

Architecture:
- ActorLifecycleManager: Central service for actor lifecycle
- PauseStrategy: Enum for pause behavior
- RollbackStrategy: Enum for rollback behavior
- PausedActorState: State preserved during pause

Thread Safety:
- Thread-safe via RLock for all public methods
- Actor handles are Ray-managed for distributed safety

Related Documents:
- docs/issues/WORKSPACE_ISOLATION_DESIGN.md (Sections 11, 12)
"""

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from wtb.infrastructure.events.wtb_event_bus import WTBEventBus
    from wtb.infrastructure.workspace.manager import WorkspaceManager
    from wtb.domain.models.workspace import Workspace, WorkspaceConfig

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Enums
# ═══════════════════════════════════════════════════════════════════════════════


class PauseStrategy(Enum):
    """
    Pause strategy for actor state management.
    
    HOT: Keep actor alive, all resources held (fast resume, expensive)
    WARM: Release expensive resources, keep actor alive (balanced)
    COLD: Kill actor, release all resources (slow resume, cost-effective)
    """
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class RollbackStrategy(Enum):
    """
    Rollback strategy for actor state management.
    
    RESET: Reset actor state in-place (fast, same actor)
    RECREATE: Kill actor and create new one (clean, new actor)
    """
    RESET = "reset"
    RECREATE = "recreate"


class SessionType(Enum):
    """
    Session type for pause strategy selection.
    """
    INTERACTIVE = "interactive"
    BATCH = "batch"


# ═══════════════════════════════════════════════════════════════════════════════
# Data Classes
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class ActorResources:
    """
    Resources allocated to an actor.
    """
    num_cpus: float = 1.0
    num_gpus: float = 0.0
    memory_gb: float = 2.0
    custom: Dict[str, float] = field(default_factory=dict)
    
    @property
    def gpu_memory_gb(self) -> float:
        """Estimate GPU memory based on num_gpus."""
        return self.num_gpus * 8.0  # Assume 8GB per GPU unit


@dataclass
class ActorConfig:
    """
    Configuration for actor creation.
    """
    execution_id: str
    workspace_id: str
    resources: ActorResources = field(default_factory=ActorResources)
    thread_id: Optional[str] = None
    from_checkpoint: Optional[int] = None
    runtime_env: Optional[Dict[str, Any]] = None
    max_restarts: int = 3
    

@dataclass
class PausedActorState:
    """
    State preserved during actor pause.
    
    Used for resuming actors after pause, especially for COLD pause
    where the actor is killed and must be recreated.
    """
    actor_id: str
    execution_id: str
    workspace_id: str
    thread_id: str
    checkpoint_id: str
    strategy: PauseStrategy
    paused_at: datetime
    actor_config: ActorConfig
    
    # State for resume
    last_node: Optional[str] = None
    nodes_completed: int = 0
    
    # Resource tracking
    gpu_memory_held_gb: float = 0.0
    connections_held: List[str] = field(default_factory=list)
    
    @property
    def pause_duration(self) -> timedelta:
        """Duration since pause."""
        return datetime.now() - self.paused_at


@dataclass
class ActorHandle:
    """
    Handle to a Ray actor with lifecycle metadata.
    """
    actor_id: str
    execution_id: str
    workspace_id: str
    ray_handle: Any  # ray.actor.ActorHandle
    config: ActorConfig
    created_at: datetime = field(default_factory=datetime.now)
    state: str = "active"  # active | paused | killed
    
    # Tracking
    executions_completed: int = 0
    last_reset_at: Optional[datetime] = None


# ═══════════════════════════════════════════════════════════════════════════════
# Pause Strategy Selector
# ═══════════════════════════════════════════════════════════════════════════════


class PauseStrategySelector:
    """
    Intelligent pause strategy selection based on context.
    
    Considers:
    - Session type (interactive vs batch)
    - Expected pause duration
    - Actor resources (GPU memory)
    - User preferences
    """
    
    def __init__(
        self,
        default_strategy: PauseStrategy = PauseStrategy.WARM,
        cold_threshold_hours: float = 2.0,
        gpu_memory_threshold_gb: float = 8.0,
    ):
        """
        Initialize selector.
        
        Args:
            default_strategy: Default pause strategy
            cold_threshold_hours: Hours after which to use COLD
            gpu_memory_threshold_gb: GPU threshold for COLD in batch
        """
        self.default_strategy = default_strategy
        self.cold_threshold_hours = cold_threshold_hours
        self.gpu_memory_threshold_gb = gpu_memory_threshold_gb
    
    def select(
        self,
        session_type: SessionType,
        expected_duration: Optional[timedelta] = None,
        actor_resources: Optional[ActorResources] = None,
        explicit_strategy: Optional[PauseStrategy] = None,
    ) -> PauseStrategy:
        """
        Select appropriate pause strategy.
        
        Args:
            session_type: Interactive or batch session
            expected_duration: Expected pause duration
            actor_resources: Actor's resource allocation
            explicit_strategy: User-requested strategy (overrides)
            
        Returns:
            Selected PauseStrategy
        """
        # User explicitly requested a strategy
        if explicit_strategy is not None:
            return explicit_strategy
        
        hours = None
        if expected_duration:
            hours = expected_duration.total_seconds() / 3600
        
        gpu_memory = 0.0
        if actor_resources:
            gpu_memory = actor_resources.gpu_memory_gb
        
        # Interactive sessions: Prioritize UX
        if session_type == SessionType.INTERACTIVE:
            if hours and hours > self.cold_threshold_hours:
                return PauseStrategy.COLD  # Long break
            return PauseStrategy.WARM  # Keep models ready
        
        # Batch sessions: Prioritize cost
        if session_type == SessionType.BATCH:
            if gpu_memory > self.gpu_memory_threshold_gb:
                if hours is None or hours > 1.0:
                    return PauseStrategy.COLD  # Expensive GPU
            return PauseStrategy.WARM
        
        return self.default_strategy


# ═══════════════════════════════════════════════════════════════════════════════
# Actor Lifecycle Manager
# ═══════════════════════════════════════════════════════════════════════════════


class ActorLifecycleManager:
    """
    Manages Ray actor lifecycle for WTB operations.
    
    Responsibilities:
    - Actor creation with workspace/venv binding
    - Resource cleanup between variant executions
    - Pause strategies (HOT/WARM/COLD)
    - Rollback handling (actor reset or recreation)
    - Fork handling (new actor creation)
    
    Thread Safety:
    - All public methods are thread-safe via RLock
    - Actor handles managed by Ray for distributed safety
    
    ACID Properties:
    - Atomic: Actor transitions are all-or-nothing
    - Consistent: State always valid between transitions
    - Isolated: Actor state independent of other actors
    - Durable: Events persisted for audit trail
    
    Usage:
        manager = ActorLifecycleManager(event_bus)
        
        # Create actor
        handle = manager.create_actor(config)
        
        # Pause actor
        paused_state = manager.pause_actor(handle, PauseStrategy.WARM)
        
        # Resume actor
        handle = manager.resume_actor(paused_state)
        
        # Handle rollback
        handle = manager.handle_rollback(handle, checkpoint_id)
    """
    
    def __init__(
        self,
        event_bus: Optional["WTBEventBus"] = None,
        workspace_manager: Optional["WorkspaceManager"] = None,
        strategy_selector: Optional[PauseStrategySelector] = None,
    ):
        """
        Initialize ActorLifecycleManager.
        
        Args:
            event_bus: Optional event bus for publishing events
            workspace_manager: Optional workspace manager
            strategy_selector: Optional pause strategy selector
        """
        self._event_bus = event_bus
        self._workspace_manager = workspace_manager
        self._strategy_selector = strategy_selector or PauseStrategySelector()
        self._lock = threading.RLock()
        
        # Active actors: actor_id -> ActorHandle
        self._actors: Dict[str, ActorHandle] = {}
        
        # Paused actors: actor_id -> PausedActorState
        self._paused_actors: Dict[str, PausedActorState] = {}
        
        # Actor counter for unique IDs
        self._actor_counter = 0
        
        # Ray availability check
        self._ray_available = self._check_ray_available()
    
    def _check_ray_available(self) -> bool:
        """Check if Ray is available."""
        try:
            import ray
            return True
        except ImportError:
            logger.warning("Ray not available, ActorLifecycleManager will use mock actors")
            return False
    
    def _generate_actor_id(self) -> str:
        """Generate unique actor ID."""
        self._actor_counter += 1
        return f"actor-{os.getpid()}-{self._actor_counter}"
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Actor Creation
    # ═══════════════════════════════════════════════════════════════════════════
    
    def create_actor(
        self,
        config: ActorConfig,
        for_fork: bool = False,
        for_resume: bool = False,
    ) -> ActorHandle:
        """
        Create a new Ray actor bound to workspace.
        
        Args:
            config: Actor configuration
            for_fork: Whether this actor is created for a fork operation
            for_resume: Whether this actor is created for cold pause resume
            
        Returns:
            ActorHandle for the created actor
            
        Raises:
            RuntimeError: If actor creation fails
        """
        actor_id = self._generate_actor_id()
        
        with self._lock:
            try:
                ray_handle = None
                
                if self._ray_available:
                    ray_handle = self._create_ray_actor(config, actor_id)
                
                handle = ActorHandle(
                    actor_id=actor_id,
                    execution_id=config.execution_id,
                    workspace_id=config.workspace_id,
                    ray_handle=ray_handle,
                    config=config,
                )
                
                self._actors[actor_id] = handle
                
                logger.info(
                    f"Created actor {actor_id} for execution {config.execution_id}, "
                    f"workspace {config.workspace_id}"
                )
                
                # Publish event
                self._publish_actor_created_event(
                    actor_id=actor_id,
                    config=config,
                    for_fork=for_fork,
                    for_resume=for_resume,
                )
                
                return handle
                
            except Exception as e:
                logger.error(f"Failed to create actor: {e}")
                raise RuntimeError(f"Actor creation failed: {e}") from e
    
    def _create_ray_actor(
        self,
        config: ActorConfig,
        actor_id: str,
    ) -> Any:
        """
        Create Ray actor with configuration.
        
        Args:
            config: Actor configuration
            actor_id: Generated actor ID
            
        Returns:
            Ray ActorHandle
        """
        import ray
        
        # Build runtime environment
        runtime_env = config.runtime_env or {}
        
        # If workspace has venv, use it
        if config.workspace_id and self._workspace_manager:
            workspace = self._workspace_manager.get_workspace(config.workspace_id)
            if workspace and workspace.venv_dir.exists():
                runtime_env.update({
                    "py_executable": str(workspace.python_path),
                    "env_vars": {
                        "VIRTUAL_ENV": str(workspace.venv_dir),
                        "WTB_WORKSPACE_ID": config.workspace_id,
                        "WTB_EXECUTION_ID": config.execution_id,
                    },
                    "working_dir": str(workspace.root_path),
                })
        
        # Ray actor creation via ActorLifecycleManager is not fully wired;
        # RayBatchTestRunner._create_actor() handles the real remote actors.
        # Return None so the lifecycle manager tracks metadata without a Ray handle.
        logger.warning(
            f"_create_ray_actor: returning None handle for actor {actor_id}. "
            "Real Ray actors are created by RayBatchTestRunner."
        )
        return None
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Actor Reset
    # ═══════════════════════════════════════════════════════════════════════════
    
    def reset_actor(
        self,
        handle: ActorHandle,
        reason: str = "variant_switch",
    ) -> None:
        """
        Reset actor state for new variant (pool reuse).
        
        Clears caches and state to prevent leakage between variants.
        
        Args:
            handle: Actor handle to reset
            reason: Reason for reset (for audit)
        """
        with self._lock:
            if handle.actor_id not in self._actors:
                logger.warning(f"Cannot reset unknown actor: {handle.actor_id}")
                return
            
            resources_cleared = []
            
            # If Ray actor exists, call reset method
            if handle.ray_handle is not None:
                try:
                    # Call actor's reset method
                    import ray
                    ray.get(handle.ray_handle.reset_for_new_variant.remote())
                    resources_cleared.extend([
                        "checkpoint_cache",
                        "execution_controller",
                        "file_tracking_service",
                    ])
                except Exception as e:
                    logger.warning(f"Actor reset failed: {e}")
            
            handle.last_reset_at = datetime.now()
            
            logger.info(f"Reset actor {handle.actor_id}: reason={reason}")
            
            # Publish event
            self._publish_actor_reset_event(
                actor_id=handle.actor_id,
                reason=reason,
                resources_cleared=resources_cleared,
            )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Pause/Resume
    # ═══════════════════════════════════════════════════════════════════════════
    
    def pause_actor(
        self,
        handle: ActorHandle,
        strategy: Optional[PauseStrategy] = None,
        expected_duration: Optional[timedelta] = None,
        session_type: SessionType = SessionType.INTERACTIVE,
        checkpoint_id: str = "",
    ) -> PausedActorState:
        """
        Pause actor with specified strategy.
        
        Args:
            handle: Actor handle to pause
            strategy: Explicit pause strategy (or auto-select)
            expected_duration: Expected pause duration
            session_type: Interactive or batch session
            checkpoint_id: Current checkpoint for resume
            
        Returns:
            PausedActorState for later resume
        """
        # Select strategy if not specified
        if strategy is None:
            strategy = self._strategy_selector.select(
                session_type=session_type,
                expected_duration=expected_duration,
                actor_resources=handle.config.resources,
            )
        
        with self._lock:
            paused_state = PausedActorState(
                actor_id=handle.actor_id,
                execution_id=handle.execution_id,
                workspace_id=handle.workspace_id,
                thread_id=handle.config.thread_id or handle.execution_id,
                checkpoint_id=checkpoint_id,
                strategy=strategy,
                paused_at=datetime.now(),
                actor_config=handle.config,
            )
            
            # Execute strategy-specific pause
            if strategy == PauseStrategy.HOT:
                self._execute_hot_pause(handle, paused_state)
                handle.state = "paused"
            elif strategy == PauseStrategy.WARM:
                self._execute_warm_pause(handle, paused_state)
                handle.state = "paused"
            elif strategy == PauseStrategy.COLD:
                self._execute_cold_pause(handle, paused_state)
                # COLD pause kills the actor, state already set to "killed"
            
            self._paused_actors[handle.actor_id] = paused_state
            
            logger.info(
                f"Paused actor {handle.actor_id} with strategy {strategy.value}"
            )
            
            return paused_state
    
    def _execute_hot_pause(
        self,
        handle: ActorHandle,
        state: PausedActorState,
    ) -> None:
        """
        Execute HOT pause - keep everything, fast resume.
        """
        # Actor keeps running, all resources held
        state.connections_held = ["database", "grpc"]
        state.gpu_memory_held_gb = handle.config.resources.gpu_memory_gb
    
    def _execute_warm_pause(
        self,
        handle: ActorHandle,
        state: PausedActorState,
    ) -> None:
        """
        Execute WARM pause - release expensive resources, keep actor.
        """
        if handle.ray_handle is not None:
            try:
                import ray
                # Call actor's warm pause method (release GPU, keep model refs)
                ray.get(handle.ray_handle.warm_pause.remote())
            except Exception as e:
                logger.warning(f"Warm pause execution failed: {e}")
        
        # Publish resources released event
        self._publish_resources_released_event(
            actor_id=handle.actor_id,
            reason="warm_pause",
            gpu_memory_freed_mb=handle.config.resources.gpu_memory_gb * 1024,
        )
    
    def _execute_cold_pause(
        self,
        handle: ActorHandle,
        state: PausedActorState,
    ) -> None:
        """
        Execute COLD pause - kill actor, release all.
        """
        if handle.ray_handle is not None:
            try:
                import ray
                ray.kill(handle.ray_handle)
                handle.ray_handle = None
            except Exception as e:
                logger.warning(f"Cold pause kill failed: {e}")
        
        # Remove from active actors
        self._actors.pop(handle.actor_id, None)
        handle.state = "killed"
        
        # Publish actor killed event
        self._publish_actor_killed_event(
            actor_id=handle.actor_id,
            reason="cold_pause",
            resources_released=["gpu", "memory", "connections"],
        )
    
    def resume_actor(
        self,
        paused_state: PausedActorState,
    ) -> ActorHandle:
        """
        Resume a paused actor.
        
        Args:
            paused_state: State from pause operation
            
        Returns:
            ActorHandle (new or existing based on strategy)
        """
        with self._lock:
            # Remove from paused actors
            self._paused_actors.pop(paused_state.actor_id, None)
            
            if paused_state.strategy == PauseStrategy.HOT:
                return self._resume_hot_pause(paused_state)
            elif paused_state.strategy == PauseStrategy.WARM:
                return self._resume_warm_pause(paused_state)
            elif paused_state.strategy == PauseStrategy.COLD:
                return self._resume_cold_pause(paused_state)
            
            raise ValueError(f"Unknown pause strategy: {paused_state.strategy}")
    
    def _resume_hot_pause(self, state: PausedActorState) -> ActorHandle:
        """Resume from HOT pause - verify and continue."""
        handle = self._actors.get(state.actor_id)
        if not handle:
            # Actor lost somehow, recreate
            return self._resume_cold_pause(state)
        
        # Verify connections are healthy
        if handle.ray_handle is not None:
            try:
                import ray
                ray.get(handle.ray_handle.health_check.remote())
            except Exception as e:
                logger.warning(f"Actor unhealthy after HOT pause: {e}, recreating")
                return self._resume_cold_pause(state)
        
        handle.state = "active"
        return handle
    
    def _resume_warm_pause(self, state: PausedActorState) -> ActorHandle:
        """Resume from WARM pause - reconnect resources."""
        handle = self._actors.get(state.actor_id)
        if not handle:
            return self._resume_cold_pause(state)
        
        if handle.ray_handle is not None:
            try:
                import ray
                # Reconnect DB, reload models if needed
                ray.get(handle.ray_handle.warm_resume.remote())
            except Exception as e:
                logger.warning(f"Warm resume failed: {e}, recreating")
                return self._resume_cold_pause(state)
        
        # Publish resources reacquired event
        self._publish_resources_reacquired_event(
            actor_id=handle.actor_id,
            gpu_memory_mb=state.actor_config.resources.gpu_memory_gb * 1024,
        )
        
        handle.state = "active"
        return handle
    
    def _resume_cold_pause(self, state: PausedActorState) -> ActorHandle:
        """Resume from COLD pause - create new actor."""
        config = state.actor_config
        config.from_checkpoint = state.checkpoint_id
        
        return self.create_actor(
            config=config,
            for_resume=True,
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Rollback
    # ═══════════════════════════════════════════════════════════════════════════
    
    def handle_rollback(
        self,
        handle: ActorHandle,
        checkpoint_id: str,
        strategy: Optional[RollbackStrategy] = None,
    ) -> ActorHandle:
        """
        Handle rollback with proper resource cleanup.
        
        Args:
            handle: Actor handle for rollback
            checkpoint_id: Checkpoint to rollback to
            strategy: Rollback strategy (or auto-select)
            
        Returns:
            ActorHandle (reset or new based on strategy)
        """
        # Auto-select strategy based on checkpoint distance
        if strategy is None:
            strategy = RollbackStrategy.RESET  # Default to faster option
        
        with self._lock:
            if strategy == RollbackStrategy.RESET:
                return self._rollback_reset(handle, checkpoint_id)
            elif strategy == RollbackStrategy.RECREATE:
                return self._rollback_recreate(handle, checkpoint_id)
            
            raise ValueError(f"Unknown rollback strategy: {strategy}")
    
    def _rollback_reset(
        self,
        handle: ActorHandle,
        checkpoint_id: str,
    ) -> ActorHandle:
        """
        Reset actor state for rollback (fast, same actor).
        """
        if handle.ray_handle is not None:
            try:
                import ray
                # Clear caches, rollback transaction
                ray.get(handle.ray_handle.reset_state.remote())
                # Release GPU memory
                ray.get(handle.ray_handle.clear_gpu_memory.remote())
            except Exception as e:
                logger.warning(f"Rollback reset failed: {e}")
        
        # Reset the actor
        self.reset_actor(handle, reason="rollback")
        
        # Publish actor reset event
        self._publish_actor_reset_event(
            actor_id=handle.actor_id,
            reason="rollback",
            resources_cleared=["caches", "gpu_memory", "file_handles"],
        )
        
        return handle
    
    def _rollback_recreate(
        self,
        handle: ActorHandle,
        checkpoint_id: str,
    ) -> ActorHandle:
        """
        Kill and recreate actor for rollback (clean, new actor).
        """
        # Kill old actor
        if handle.ray_handle is not None:
            try:
                import ray
                ray.kill(handle.ray_handle)
            except Exception as e:
                logger.warning(f"Failed to kill actor for rollback: {e}")
        
        self._actors.pop(handle.actor_id, None)
        
        # Publish actor killed event
        self._publish_actor_killed_event(
            actor_id=handle.actor_id,
            reason="rollback_recreate",
            resources_released=["all"],
        )
        
        # Create new actor
        config = handle.config
        config.from_checkpoint = checkpoint_id
        
        return self.create_actor(config=config)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Fork
    # ═══════════════════════════════════════════════════════════════════════════
    
    def fork_actor(
        self,
        source_checkpoint_id: str,
        new_workspace_id: str,
        new_execution_id: str,
        source_config: Optional[ActorConfig] = None,
    ) -> ActorHandle:
        """
        Create new actor for fork operation.
        
        Args:
            source_checkpoint_id: Checkpoint to fork from
            new_workspace_id: New workspace for fork
            new_execution_id: New execution ID
            source_config: Source actor configuration to clone
            
        Returns:
            New ActorHandle for fork
        """
        # Create config for fork
        config = ActorConfig(
            execution_id=new_execution_id,
            workspace_id=new_workspace_id,
            resources=source_config.resources if source_config else ActorResources(),
            thread_id=new_execution_id,
            from_checkpoint=source_checkpoint_id,
        )
        
        return self.create_actor(
            config=config,
            for_fork=True,
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Cleanup
    # ═══════════════════════════════════════════════════════════════════════════
    
    def kill_actor(
        self,
        handle: ActorHandle,
        reason: str = "shutdown",
    ) -> bool:
        """
        Kill an actor and release resources.
        
        Args:
            handle: Actor handle to kill
            reason: Reason for killing
            
        Returns:
            True if killed, False if not found
        """
        with self._lock:
            if handle.actor_id not in self._actors:
                return False
            
            if handle.ray_handle is not None:
                try:
                    import ray
                    ray.kill(handle.ray_handle)
                except Exception as e:
                    logger.warning(f"Failed to kill actor: {e}")
            
            self._actors.pop(handle.actor_id, None)
            handle.state = "killed"
            
            logger.info(f"Killed actor {handle.actor_id}: reason={reason}")
            
            self._publish_actor_killed_event(
                actor_id=handle.actor_id,
                reason=reason,
                resources_released=["all"],
            )
            
            return True
    
    def shutdown(self) -> int:
        """
        Shutdown all managed actors.
        
        Returns:
            Number of actors killed
        """
        with self._lock:
            count = 0
            for actor_id in list(self._actors.keys()):
                handle = self._actors.get(actor_id)
                if handle and self.kill_actor(handle, reason="manager_shutdown"):
                    count += 1
            
            self._paused_actors.clear()
            return count
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Query
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_actor(self, actor_id: str) -> Optional[ActorHandle]:
        """Get actor by ID."""
        with self._lock:
            return self._actors.get(actor_id)
    
    def get_active_actors(self) -> List[ActorHandle]:
        """Get all active actors."""
        with self._lock:
            return [h for h in self._actors.values() if h.state == "active"]
    
    def get_paused_actors(self) -> List[PausedActorState]:
        """Get all paused actors."""
        with self._lock:
            return list(self._paused_actors.values())
    
    def get_stats(self) -> Dict[str, Any]:
        """Get lifecycle manager statistics."""
        with self._lock:
            return {
                "active_actors": len([a for a in self._actors.values() if a.state == "active"]),
                "paused_actors": len(self._paused_actors),
                "total_actors_created": self._actor_counter,
                "ray_available": self._ray_available,
            }
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Event Publishing
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _publish_event(self, event: Any) -> None:
        """Publish event to event bus if available."""
        if self._event_bus:
            try:
                self._event_bus.publish(event)
            except Exception as e:
                logger.error(f"Failed to publish event: {e}")
    
    def _publish_actor_created_event(
        self,
        actor_id: str,
        config: ActorConfig,
        for_fork: bool,
        for_resume: bool,
    ) -> None:
        """Publish ActorCreatedEvent."""
        from wtb.domain.events.workspace_events import ActorCreatedEvent
        
        self._publish_event(ActorCreatedEvent(
            actor_id=actor_id,
            execution_id=config.execution_id,
            workspace_id=config.workspace_id,
            for_fork=for_fork,
            for_resume=for_resume,
            resources={
                "num_cpus": config.resources.num_cpus,
                "num_gpus": config.resources.num_gpus,
                "memory_gb": config.resources.memory_gb,
            },
        ))
    
    def _publish_actor_reset_event(
        self,
        actor_id: str,
        reason: str,
        resources_cleared: List[str],
    ) -> None:
        """Publish ActorResetEvent."""
        from wtb.domain.events.workspace_events import ActorResetEvent
        
        self._publish_event(ActorResetEvent(
            actor_id=actor_id,
            reason=reason,
            resources_cleared=resources_cleared,
        ))
    
    def _publish_actor_killed_event(
        self,
        actor_id: str,
        reason: str,
        resources_released: List[str],
    ) -> None:
        """Publish ActorKilledEvent."""
        from wtb.domain.events.workspace_events import ActorKilledEvent
        
        self._publish_event(ActorKilledEvent(
            actor_id=actor_id,
            reason=reason,
            resources_released=resources_released,
        ))
    
    def _publish_resources_released_event(
        self,
        actor_id: str,
        reason: str,
        gpu_memory_freed_mb: float,
    ) -> None:
        """Publish ResourcesReleasedEvent."""
        from wtb.domain.events.workspace_events import ResourcesReleasedEvent
        
        self._publish_event(ResourcesReleasedEvent(
            actor_id=actor_id,
            reason=reason,
            gpu_memory_freed_mb=gpu_memory_freed_mb,
            resources_list=["gpu_memory", "cached_tensors"],
        ))
    
    def _publish_resources_reacquired_event(
        self,
        actor_id: str,
        gpu_memory_mb: float,
    ) -> None:
        """Publish ResourcesReacquiredEvent."""
        from wtb.domain.events.workspace_events import ResourcesReacquiredEvent
        
        self._publish_event(ResourcesReacquiredEvent(
            actor_id=actor_id,
            gpu_memory_allocated_mb=gpu_memory_mb,
            reacquisition_time_ms=0.0,
        ))


# ═══════════════════════════════════════════════════════════════════════════════
# Factory Functions
# ═══════════════════════════════════════════════════════════════════════════════


def create_actor_lifecycle_manager(
    event_bus: Optional["WTBEventBus"] = None,
    workspace_manager: Optional["WorkspaceManager"] = None,
) -> ActorLifecycleManager:
    """
    Factory function to create ActorLifecycleManager.
    
    Args:
        event_bus: Optional event bus
        workspace_manager: Optional workspace manager
        
    Returns:
        Configured ActorLifecycleManager
    """
    return ActorLifecycleManager(
        event_bus=event_bus,
        workspace_manager=workspace_manager,
    )
