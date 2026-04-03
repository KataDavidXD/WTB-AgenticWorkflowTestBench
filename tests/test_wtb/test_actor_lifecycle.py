"""
Tests for ActorLifecycleManager.

Tests cover:
- Actor creation and configuration
- Pause strategies (HOT, WARM, COLD)
- Resume from different pause states
- Rollback strategies (RESET, RECREATE)
- Fork operations
- Resource cleanup
"""

import pytest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

from wtb.application.services.actor_lifecycle import (
    ActorLifecycleManager,
    ActorConfig,
    ActorResources,
    ActorHandle,
    PauseStrategy,
    PausedActorState,
    RollbackStrategy,
    SessionType,
    PauseStrategySelector,
    create_actor_lifecycle_manager,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def event_bus():
    """Create mock event bus."""
    bus = MagicMock()
    bus.publish = MagicMock()
    return bus


@pytest.fixture
def workspace_manager():
    """Create mock workspace manager."""
    manager = MagicMock()
    manager.get_workspace = MagicMock(return_value=None)
    return manager


@pytest.fixture
def manager(event_bus, workspace_manager):
    """Create ActorLifecycleManager instance."""
    return ActorLifecycleManager(
        event_bus=event_bus,
        workspace_manager=workspace_manager,
    )


@pytest.fixture
def actor_config():
    """Create test actor configuration."""
    return ActorConfig(
        execution_id="exec-001",
        workspace_id="ws-001",
        resources=ActorResources(
            num_cpus=1.0,
            num_gpus=0.5,
            memory_gb=4.0,
        ),
        thread_id="thread-001",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ActorResources Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestActorResources:
    """Tests for ActorResources."""
    
    def test_default_resources(self):
        """Test default resource values."""
        resources = ActorResources()
        assert resources.num_cpus == 1.0
        assert resources.num_gpus == 0.0
        assert resources.memory_gb == 2.0
    
    def test_gpu_memory_estimate(self):
        """Test GPU memory estimation."""
        resources = ActorResources(num_gpus=2.0)
        # Default 8GB per GPU
        assert resources.gpu_memory_gb == 16.0
    
    def test_custom_resources(self):
        """Test custom resource values."""
        resources = ActorResources(
            num_cpus=4.0,
            num_gpus=1.0,
            memory_gb=16.0,
            custom={"tpu": 1.0},
        )
        assert resources.custom["tpu"] == 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# PauseStrategySelector Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestPauseStrategySelector:
    """Tests for PauseStrategySelector."""
    
    def test_explicit_strategy_overrides(self):
        """Test that explicit strategy overrides auto-selection."""
        selector = PauseStrategySelector()
        
        result = selector.select(
            session_type=SessionType.INTERACTIVE,
            explicit_strategy=PauseStrategy.COLD,
        )
        
        assert result == PauseStrategy.COLD
    
    def test_interactive_session_warm_default(self):
        """Test interactive sessions default to WARM."""
        selector = PauseStrategySelector()
        
        result = selector.select(
            session_type=SessionType.INTERACTIVE,
        )
        
        assert result == PauseStrategy.WARM
    
    def test_interactive_long_pause_cold(self):
        """Test interactive sessions switch to COLD for long pauses."""
        selector = PauseStrategySelector(cold_threshold_hours=2.0)
        
        result = selector.select(
            session_type=SessionType.INTERACTIVE,
            expected_duration=timedelta(hours=3),
        )
        
        assert result == PauseStrategy.COLD
    
    def test_batch_session_high_gpu_cold(self):
        """Test batch sessions with high GPU use COLD."""
        selector = PauseStrategySelector(gpu_memory_threshold_gb=8.0)
        
        resources = ActorResources(num_gpus=2.0)  # 16GB GPU
        
        result = selector.select(
            session_type=SessionType.BATCH,
            expected_duration=timedelta(hours=2),
            actor_resources=resources,
        )
        
        assert result == PauseStrategy.COLD
    
    def test_batch_session_low_gpu_warm(self):
        """Test batch sessions with low GPU use WARM."""
        selector = PauseStrategySelector()
        
        resources = ActorResources(num_gpus=0.5)  # 4GB GPU
        
        result = selector.select(
            session_type=SessionType.BATCH,
            actor_resources=resources,
        )
        
        assert result == PauseStrategy.WARM


# ═══════════════════════════════════════════════════════════════════════════════
# ActorLifecycleManager Creation Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestActorCreation:
    """Tests for actor creation."""
    
    def test_create_actor(self, manager, actor_config, event_bus):
        """Test basic actor creation."""
        handle = manager.create_actor(actor_config)
        
        assert handle.actor_id.startswith("actor-")
        assert handle.execution_id == "exec-001"
        assert handle.workspace_id == "ws-001"
        assert handle.state == "active"
        
        # Event should be published
        event_bus.publish.assert_called()
    
    def test_create_actor_for_fork(self, manager, actor_config, event_bus):
        """Test actor creation for fork operation."""
        handle = manager.create_actor(actor_config, for_fork=True)
        
        assert handle.actor_id.startswith("actor-")
        
        # Check event has for_fork=True
        call_args = event_bus.publish.call_args
        event = call_args[0][0]
        assert event.for_fork is True
    
    def test_create_actor_for_resume(self, manager, actor_config, event_bus):
        """Test actor creation for resume operation."""
        handle = manager.create_actor(actor_config, for_resume=True)
        
        # Check event has for_resume=True
        call_args = event_bus.publish.call_args
        event = call_args[0][0]
        assert event.for_resume is True
    
    def test_actor_id_uniqueness(self, manager, actor_config):
        """Test that actor IDs are unique."""
        handle1 = manager.create_actor(actor_config)
        handle2 = manager.create_actor(actor_config)
        
        assert handle1.actor_id != handle2.actor_id


# ═══════════════════════════════════════════════════════════════════════════════
# Actor Reset Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestActorReset:
    """Tests for actor reset."""
    
    def test_reset_actor(self, manager, actor_config, event_bus):
        """Test actor reset clears state."""
        handle = manager.create_actor(actor_config)
        
        manager.reset_actor(handle, reason="variant_switch")
        
        assert handle.last_reset_at is not None
        
        # Event should be published
        calls = event_bus.publish.call_args_list
        reset_events = [c for c in calls if "ActorResetEvent" in str(c)]
        assert len(reset_events) > 0
    
    def test_reset_unknown_actor(self, manager):
        """Test resetting unknown actor is safe."""
        fake_handle = ActorHandle(
            actor_id="unknown",
            execution_id="exec",
            workspace_id="ws",
            ray_handle=None,
            config=ActorConfig(execution_id="exec", workspace_id="ws"),
        )
        
        # Should not raise
        manager.reset_actor(fake_handle)


# ═══════════════════════════════════════════════════════════════════════════════
# Pause/Resume Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestPauseResume:
    """Tests for pause/resume operations."""
    
    def test_hot_pause(self, manager, actor_config):
        """Test HOT pause keeps actor alive."""
        handle = manager.create_actor(actor_config)
        
        paused_state = manager.pause_actor(
            handle,
            strategy=PauseStrategy.HOT,
            checkpoint_id=5,
        )
        
        assert paused_state.strategy == PauseStrategy.HOT
        assert paused_state.checkpoint_id == 5
        assert handle.state == "paused"
        # HOT keeps connections
        assert "database" in paused_state.connections_held
    
    def test_warm_pause(self, manager, actor_config, event_bus):
        """Test WARM pause releases GPU."""
        handle = manager.create_actor(actor_config)
        
        paused_state = manager.pause_actor(
            handle,
            strategy=PauseStrategy.WARM,
            checkpoint_id=10,
        )
        
        assert paused_state.strategy == PauseStrategy.WARM
        assert handle.state == "paused"
        
        # Should publish ResourcesReleasedEvent
        calls = event_bus.publish.call_args_list
        released_events = [c for c in calls if "ResourcesReleased" in str(c)]
        assert len(released_events) > 0
    
    def test_cold_pause(self, manager, actor_config, event_bus):
        """Test COLD pause kills actor."""
        handle = manager.create_actor(actor_config)
        
        paused_state = manager.pause_actor(
            handle,
            strategy=PauseStrategy.COLD,
            checkpoint_id=15,
        )
        
        assert paused_state.strategy == PauseStrategy.COLD
        assert handle.state == "killed"
        
        # Actor should be removed from active list
        assert manager.get_actor(handle.actor_id) is None
    
    def test_resume_from_hot(self, manager, actor_config):
        """Test resume from HOT pause."""
        handle = manager.create_actor(actor_config)
        paused_state = manager.pause_actor(handle, strategy=PauseStrategy.HOT)
        
        resumed_handle = manager.resume_actor(paused_state)
        
        assert resumed_handle.actor_id == handle.actor_id
        assert resumed_handle.state == "active"
    
    def test_resume_from_cold_creates_new_actor(self, manager, actor_config):
        """Test resume from COLD creates new actor."""
        handle = manager.create_actor(actor_config)
        paused_state = manager.pause_actor(handle, strategy=PauseStrategy.COLD)
        
        resumed_handle = manager.resume_actor(paused_state)
        
        # New actor should be created
        assert resumed_handle.actor_id != handle.actor_id
        assert resumed_handle.state == "active"
    
    def test_auto_strategy_selection(self, manager, actor_config):
        """Test automatic strategy selection."""
        handle = manager.create_actor(actor_config)
        
        # Without explicit strategy, should auto-select
        paused_state = manager.pause_actor(
            handle,
            session_type=SessionType.INTERACTIVE,
        )
        
        # Interactive defaults to WARM
        assert paused_state.strategy == PauseStrategy.WARM


# ═══════════════════════════════════════════════════════════════════════════════
# Rollback Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRollback:
    """Tests for rollback operations."""
    
    def test_rollback_reset(self, manager, actor_config, event_bus):
        """Test rollback with RESET strategy."""
        handle = manager.create_actor(actor_config)
        
        returned_handle = manager.handle_rollback(
            handle,
            checkpoint_id=3,
            strategy=RollbackStrategy.RESET,
        )
        
        # Same actor, just reset
        assert returned_handle.actor_id == handle.actor_id
        assert returned_handle.last_reset_at is not None
    
    def test_rollback_recreate(self, manager, actor_config, event_bus):
        """Test rollback with RECREATE strategy."""
        handle = manager.create_actor(actor_config)
        original_id = handle.actor_id
        
        returned_handle = manager.handle_rollback(
            handle,
            checkpoint_id=3,
            strategy=RollbackStrategy.RECREATE,
        )
        
        # New actor created
        assert returned_handle.actor_id != original_id


# ═══════════════════════════════════════════════════════════════════════════════
# Fork Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFork:
    """Tests for fork operations."""
    
    def test_fork_creates_new_actor(self, manager, actor_config, event_bus):
        """Test fork creates a new actor."""
        source_handle = manager.create_actor(actor_config)
        
        fork_handle = manager.fork_actor(
            source_checkpoint_id=5,
            new_workspace_id="ws-fork-001",
            new_execution_id="exec-fork-001",
            source_config=actor_config,
        )
        
        assert fork_handle.actor_id != source_handle.actor_id
        assert fork_handle.workspace_id == "ws-fork-001"
        assert fork_handle.execution_id == "exec-fork-001"
        
        # Check event has for_fork=True
        calls = event_bus.publish.call_args_list
        created_events = [c for c in calls if "ActorCreatedEvent" in str(c)]
        assert len(created_events) > 0


# ═══════════════════════════════════════════════════════════════════════════════
# Cleanup Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCleanup:
    """Tests for cleanup operations."""
    
    def test_kill_actor(self, manager, actor_config, event_bus):
        """Test killing an actor."""
        handle = manager.create_actor(actor_config)
        
        result = manager.kill_actor(handle, reason="test_cleanup")
        
        assert result is True
        assert handle.state == "killed"
        assert manager.get_actor(handle.actor_id) is None
    
    def test_kill_unknown_actor(self, manager):
        """Test killing unknown actor returns False."""
        fake_handle = ActorHandle(
            actor_id="unknown",
            execution_id="exec",
            workspace_id="ws",
            ray_handle=None,
            config=ActorConfig(execution_id="exec", workspace_id="ws"),
        )
        
        result = manager.kill_actor(fake_handle)
        
        assert result is False
    
    def test_shutdown(self, manager, actor_config):
        """Test shutdown kills all actors."""
        handle1 = manager.create_actor(actor_config)
        handle2 = manager.create_actor(actor_config)
        
        count = manager.shutdown()
        
        assert count == 2
        assert len(manager.get_active_actors()) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Query Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestQuery:
    """Tests for query operations."""
    
    def test_get_actor(self, manager, actor_config):
        """Test getting actor by ID."""
        handle = manager.create_actor(actor_config)
        
        retrieved = manager.get_actor(handle.actor_id)
        
        assert retrieved is not None
        assert retrieved.actor_id == handle.actor_id
    
    def test_get_active_actors(self, manager, actor_config):
        """Test getting active actors."""
        handle1 = manager.create_actor(actor_config)
        handle2 = manager.create_actor(actor_config)
        manager.pause_actor(handle1, strategy=PauseStrategy.HOT)
        
        active = manager.get_active_actors()
        
        # Only handle2 should be active
        assert len(active) == 1
        assert active[0].actor_id == handle2.actor_id
    
    def test_get_paused_actors(self, manager, actor_config):
        """Test getting paused actors."""
        handle = manager.create_actor(actor_config)
        manager.pause_actor(handle, strategy=PauseStrategy.WARM)
        
        paused = manager.get_paused_actors()
        
        assert len(paused) == 1
        assert paused[0].actor_id == handle.actor_id
    
    def test_get_stats(self, manager, actor_config):
        """Test getting manager statistics."""
        manager.create_actor(actor_config)
        
        stats = manager.get_stats()
        
        assert stats["active_actors"] == 1
        assert stats["total_actors_created"] >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# Factory Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFactory:
    """Tests for factory functions."""
    
    def test_create_actor_lifecycle_manager(self):
        """Test factory function creates manager."""
        manager = create_actor_lifecycle_manager()
        
        assert isinstance(manager, ActorLifecycleManager)
        assert manager._event_bus is None
    
    def test_create_actor_lifecycle_manager_with_event_bus(self, event_bus):
        """Test factory with event bus."""
        manager = create_actor_lifecycle_manager(event_bus=event_bus)
        
        assert manager._event_bus is event_bus
