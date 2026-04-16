"""
Unit tests for Environment Events.

Tests the domain events for UV Venv Manager operations.
"""

import pytest
from datetime import datetime

from wtb.domain.events import (
    EnvironmentEvent,
    EnvironmentCreationStartedEvent,
    EnvironmentCreatedEvent,
    EnvironmentCreationFailedEvent,
    EnvironmentDeletedEvent,
    DependenciesAddedEvent,
    DependenciesUpdatedEvent,
    DependenciesRemovedEvent,
    DependencyOperationFailedEvent,
    EnvironmentSyncedEvent,
    EnvironmentSyncFailedEvent,
    EnvironmentCleanupStartedEvent,
    EnvironmentCleanupCompletedEvent,
    EnvironmentLockAcquiredEvent,
    EnvironmentLockReleasedEvent,
    EnvironmentLockTimeoutEvent,
)


class TestEnvironmentEvent:
    """Test base EnvironmentEvent functionality."""
    
    def test_env_id_without_version(self):
        """Test env_id generation without version."""
        event = EnvironmentCreatedEvent(
            workflow_id="wf-001",
            node_id="node-a",
            version_id=None,
        )
        assert event.env_id == "wf-001_node-a"
    
    def test_env_id_with_version(self):
        """Test env_id generation with version."""
        event = EnvironmentCreatedEvent(
            workflow_id="wf-001",
            node_id="node-a",
            version_id="v1",
        )
        assert event.env_id == "wf-001_node-a_v1"
    
    def test_event_type(self):
        """Test event_type property."""
        event = EnvironmentCreatedEvent()
        assert event.event_type == "EnvironmentCreatedEvent"
    
    def test_event_id_unique(self):
        """Test that each event gets a unique ID."""
        event1 = EnvironmentCreatedEvent()
        event2 = EnvironmentCreatedEvent()
        assert event1.event_id != event2.event_id
    
    def test_timestamp_set(self):
        """Test that timestamp is set on creation."""
        event = EnvironmentCreatedEvent()
        assert isinstance(event.timestamp, datetime)


class TestEnvironmentLifecycleEvents:
    """Test environment lifecycle events."""
    
    def test_creation_started_event(self):
        """Test EnvironmentCreationStartedEvent."""
        event = EnvironmentCreationStartedEvent(
            workflow_id="ml-pipeline",
            node_id="rag",
            version_id="v2",
            python_version="3.11",
            packages=["langchain", "chromadb"],
            source="grpc",
        )
        assert event.workflow_id == "ml-pipeline"
        assert event.node_id == "rag"
        assert event.python_version == "3.11"
        assert event.packages == ["langchain", "chromadb"]
        assert event.source == "grpc"
    
    def test_created_event(self):
        """Test EnvironmentCreatedEvent."""
        event = EnvironmentCreatedEvent(
            workflow_id="ml-pipeline",
            node_id="rag",
            env_path="/data/envs/ml-pipeline_rag",
            python_version="3.11",
            packages=["langchain"],
            duration_ms=5000.0,
            source="api",
        )
        assert event.env_path == "/data/envs/ml-pipeline_rag"
        assert event.duration_ms == 5000.0
    
    def test_creation_failed_event(self):
        """Test EnvironmentCreationFailedEvent."""
        event = EnvironmentCreationFailedEvent(
            workflow_id="ml-pipeline",
            node_id="rag",
            error="Package resolution failed",
            python_version="3.11",
            packages=["nonexistent-package"],
            duration_ms=1000.0,
            rollback_performed=True,
            source="grpc",
        )
        assert event.error == "Package resolution failed"
        assert event.rollback_performed is True
    
    def test_deleted_event(self):
        """Test EnvironmentDeletedEvent."""
        event = EnvironmentDeletedEvent(
            workflow_id="ml-pipeline",
            node_id="rag",
            env_path="/data/envs/ml-pipeline_rag",
            duration_ms=200.0,
        )
        assert event.env_path == "/data/envs/ml-pipeline_rag"


class TestDependencyEvents:
    """Test dependency management events."""
    
    def test_dependencies_added_event(self):
        """Test DependenciesAddedEvent."""
        event = DependenciesAddedEvent(
            workflow_id="wf-001",
            node_id="node-a",
            packages=["numpy", "pandas"],
            duration_ms=3000.0,
            exit_code=0,
        )
        assert event.packages == ["numpy", "pandas"]
        assert event.exit_code == 0
    
    def test_dependencies_updated_event(self):
        """Test DependenciesUpdatedEvent."""
        event = DependenciesUpdatedEvent(
            workflow_id="wf-001",
            node_id="node-a",
            packages=["numpy>=1.25"],
            duration_ms=2000.0,
            exit_code=0,
        )
        assert event.packages == ["numpy>=1.25"]
    
    def test_dependencies_removed_event(self):
        """Test DependenciesRemovedEvent."""
        event = DependenciesRemovedEvent(
            workflow_id="wf-001",
            node_id="node-a",
            packages=["old-package"],
            duration_ms=1000.0,
            exit_code=0,
        )
        assert "old-package" in event.packages
    
    def test_dependency_operation_failed_event(self):
        """Test DependencyOperationFailedEvent."""
        event = DependencyOperationFailedEvent(
            workflow_id="wf-001",
            node_id="node-a",
            operation="add",
            packages=["nonexistent"],
            error="No solution found",
            duration_ms=500.0,
        )
        assert event.operation == "add"
        assert event.error == "No solution found"


class TestSyncEvents:
    """Test environment sync events."""
    
    def test_synced_event(self):
        """Test EnvironmentSyncedEvent."""
        event = EnvironmentSyncedEvent(
            workflow_id="wf-001",
            node_id="node-a",
            duration_ms=4000.0,
            exit_code=0,
        )
        assert event.exit_code == 0
    
    def test_sync_failed_event(self):
        """Test EnvironmentSyncFailedEvent."""
        event = EnvironmentSyncFailedEvent(
            workflow_id="wf-001",
            node_id="node-a",
            error="Lock file corrupted",
            duration_ms=100.0,
        )
        assert event.error == "Lock file corrupted"


class TestCleanupEvents:
    """Test cleanup events."""
    
    def test_cleanup_started_event(self):
        """Test EnvironmentCleanupStartedEvent."""
        event = EnvironmentCleanupStartedEvent(idle_hours=24)
        assert event.idle_hours == 24
    
    def test_cleanup_completed_event(self):
        """Test EnvironmentCleanupCompletedEvent."""
        event = EnvironmentCleanupCompletedEvent(
            deleted_envs=["env1", "env2", "env3"],
            deleted_count=3,
            duration_ms=1500.0,
            idle_hours=24,
        )
        assert event.deleted_count == 3
        assert len(event.deleted_envs) == 3


class TestLockEvents:
    """Test lock-related events."""
    
    def test_lock_acquired_event(self):
        """Test EnvironmentLockAcquiredEvent."""
        event = EnvironmentLockAcquiredEvent(
            workflow_id="wf-001",
            node_id="node-a",
            operation="create_env",
        )
        assert event.operation == "create_env"
    
    def test_lock_released_event(self):
        """Test EnvironmentLockReleasedEvent."""
        event = EnvironmentLockReleasedEvent(
            workflow_id="wf-001",
            node_id="node-a",
            operation="create_env",
            held_ms=5000.0,
        )
        assert event.held_ms == 5000.0
    
    def test_lock_timeout_event(self):
        """Test EnvironmentLockTimeoutEvent."""
        event = EnvironmentLockTimeoutEvent(
            workflow_id="wf-001",
            node_id="node-a",
            operation="add_deps",
            timeout_ms=30000.0,
        )
        assert event.timeout_ms == 30000.0


class TestEventDataclass:
    """Test event dataclass behavior."""
    
    def test_event_has_default_values(self):
        """Test that events have sensible default values."""
        event = EnvironmentCreatedEvent()
        assert event.workflow_id == ""
        assert event.node_id == ""
        assert event.packages == []
        assert event.duration_ms == 0.0
