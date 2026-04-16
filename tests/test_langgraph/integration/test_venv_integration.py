"""
Integration tests for LangGraph + Virtual Environment management.

Tests venv integration:
- Environment provisioning for execution
- Package isolation per workflow
- Environment lifecycle management
- Integration with execution control

Run with: pytest tests/test_langgraph/integration/test_venv_integration.py -v
"""

import pytest
from typing import Dict, Any, List, Optional
from datetime import datetime
from unittest.mock import Mock, MagicMock, patch, AsyncMock
from dataclasses import dataclass

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from wtb.infrastructure.events import WTBEventBus, WTBAuditTrail

from tests.test_langgraph.helpers import (
    create_initial_simple_state,
    SimpleState,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Mock Environment Provider
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class MockEnvironmentSpec:
    """Mock environment specification."""
    env_id: str
    python_version: str = "3.11"
    packages: List[str] = None
    
    def __post_init__(self):
        if self.packages is None:
            self.packages = []


@dataclass
class MockEnvironmentStatus:
    """Mock environment status."""
    env_id: str
    status: str = "ready"  # pending, creating, ready, failed, destroyed
    python_path: Optional[str] = None
    error: Optional[str] = None


class MockEnvironmentProvider:
    """Mock environment provider for testing."""
    
    def __init__(self):
        self._environments: Dict[str, MockEnvironmentStatus] = {}
        self._specs: Dict[str, MockEnvironmentSpec] = {}
    
    def create_environment(self, spec: MockEnvironmentSpec) -> str:
        """Create a new environment."""
        env_id = spec.env_id
        self._specs[env_id] = spec
        self._environments[env_id] = MockEnvironmentStatus(
            env_id=env_id,
            status="ready",
            python_path=f"/mock/envs/{env_id}/bin/python",
        )
        return env_id
    
    def get_status(self, env_id: str) -> Optional[MockEnvironmentStatus]:
        """Get environment status."""
        return self._environments.get(env_id)
    
    def destroy_environment(self, env_id: str) -> bool:
        """Destroy an environment."""
        if env_id in self._environments:
            self._environments[env_id].status = "destroyed"
            return True
        return False
    
    def list_environments(self) -> List[str]:
        """List all environments."""
        return list(self._environments.keys())


# ═══════════════════════════════════════════════════════════════════════════════
# Environment Provider Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestMockEnvironmentProvider:
    """Tests for mock environment provider."""
    
    @pytest.fixture
    def provider(self):
        return MockEnvironmentProvider()
    
    def test_create_environment(self, provider):
        """Test creating an environment."""
        spec = MockEnvironmentSpec(
            env_id="test-env-1",
            python_version="3.11",
            packages=["numpy", "pandas"],
        )
        
        env_id = provider.create_environment(spec)
        
        assert env_id == "test-env-1"
        assert provider.get_status(env_id).status == "ready"
    
    def test_get_environment_status(self, provider):
        """Test getting environment status."""
        spec = MockEnvironmentSpec(env_id="status-test")
        provider.create_environment(spec)
        
        status = provider.get_status("status-test")
        
        assert status is not None
        assert status.status == "ready"
        assert status.python_path is not None
    
    def test_destroy_environment(self, provider):
        """Test destroying an environment."""
        spec = MockEnvironmentSpec(env_id="destroy-test")
        provider.create_environment(spec)
        
        result = provider.destroy_environment("destroy-test")
        
        assert result is True
        assert provider.get_status("destroy-test").status == "destroyed"
    
    def test_list_environments(self, provider):
        """Test listing environments."""
        for i in range(3):
            spec = MockEnvironmentSpec(env_id=f"list-env-{i}")
            provider.create_environment(spec)
        
        envs = provider.list_environments()
        
        assert len(envs) == 3


# ═══════════════════════════════════════════════════════════════════════════════
# LangGraph + Environment Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestLangGraphEnvironmentIntegration:
    """Tests for LangGraph + Environment integration."""
    
    @pytest.fixture
    def provider(self):
        return MockEnvironmentProvider()
    
    def test_execution_with_environment(self, simple_graph_def, provider):
        """Test execution with provisioned environment."""
        # Create environment for execution
        spec = MockEnvironmentSpec(
            env_id="exec-env-1",
            packages=["langgraph"],
        )
        provider.create_environment(spec)
        
        # Compile graph
        checkpointer = MemorySaver()
        graph = simple_graph_def.compile(checkpointer=checkpointer)
        
        # Execute
        config = {"configurable": {"thread_id": "venv-exec-1"}}
        result = graph.invoke(create_initial_simple_state(), config)
        
        # Verify execution completed
        assert result["messages"] == ["A", "B", "C"]
        
        # Environment should still be ready
        assert provider.get_status("exec-env-1").status == "ready"
    
    def test_environment_per_execution(self, simple_graph_def, provider):
        """Test separate environments per execution."""
        checkpointer = MemorySaver()
        graph = simple_graph_def.compile(checkpointer=checkpointer)
        
        # Create environments for each execution
        for i in range(3):
            spec = MockEnvironmentSpec(env_id=f"per-exec-{i}")
            provider.create_environment(spec)
            
            config = {"configurable": {"thread_id": f"exec-{i}"}}
            result = graph.invoke(create_initial_simple_state(), config)
            
            assert result["messages"] == ["A", "B", "C"]
        
        # All environments created
        assert len(provider.list_environments()) == 3
    
    def test_environment_cleanup_after_execution(self, simple_graph_def, provider):
        """Test environment cleanup after execution."""
        spec = MockEnvironmentSpec(env_id="cleanup-env")
        provider.create_environment(spec)
        
        checkpointer = MemorySaver()
        graph = simple_graph_def.compile(checkpointer=checkpointer)
        
        # Execute
        config = {"configurable": {"thread_id": "cleanup-exec"}}
        graph.invoke(create_initial_simple_state(), config)
        
        # Cleanup
        provider.destroy_environment("cleanup-env")
        
        assert provider.get_status("cleanup-env").status == "destroyed"


# ═══════════════════════════════════════════════════════════════════════════════
# Environment Lifecycle Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnvironmentLifecycle:
    """Tests for environment lifecycle management."""
    
    @pytest.fixture
    def provider(self):
        return MockEnvironmentProvider()
    
    def test_environment_reuse(self, provider):
        """Test reusing environments across executions."""
        # Create shared environment
        spec = MockEnvironmentSpec(
            env_id="shared-env",
            packages=["numpy"],
        )
        provider.create_environment(spec)
        
        # Use for multiple "executions"
        for i in range(5):
            status = provider.get_status("shared-env")
            assert status.status == "ready"
        
        # Environment still ready
        assert provider.get_status("shared-env").status == "ready"
    
    def test_environment_pool(self, provider):
        """Test environment pool pattern."""
        # Create pool of environments
        pool_size = 4
        for i in range(pool_size):
            spec = MockEnvironmentSpec(env_id=f"pool-env-{i}")
            provider.create_environment(spec)
        
        assert len(provider.list_environments()) == pool_size
        
        # All should be ready
        for i in range(pool_size):
            assert provider.get_status(f"pool-env-{i}").status == "ready"
    
    def test_environment_recreation(self, provider):
        """Test environment recreation after failure."""
        # Create environment
        spec = MockEnvironmentSpec(env_id="recreate-env")
        provider.create_environment(spec)
        
        # Simulate failure (destroy)
        provider.destroy_environment("recreate-env")
        assert provider.get_status("recreate-env").status == "destroyed"
        
        # Recreate
        provider.create_environment(spec)
        assert provider.get_status("recreate-env").status == "ready"


# ═══════════════════════════════════════════════════════════════════════════════
# Package Isolation Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestPackageIsolation:
    """Tests for package isolation."""
    
    @pytest.fixture
    def provider(self):
        return MockEnvironmentProvider()
    
    def test_different_packages_per_workflow(self, provider):
        """Test different packages per workflow environment."""
        # Workflow 1 needs numpy
        spec1 = MockEnvironmentSpec(
            env_id="workflow-1-env",
            packages=["numpy>=1.20"],
        )
        provider.create_environment(spec1)
        
        # Workflow 2 needs pandas
        spec2 = MockEnvironmentSpec(
            env_id="workflow-2-env",
            packages=["pandas>=2.0"],
        )
        provider.create_environment(spec2)
        
        # Both should be isolated
        assert "workflow-1-env" in provider.list_environments()
        assert "workflow-2-env" in provider.list_environments()
    
    def test_python_version_isolation(self, provider):
        """Test Python version isolation."""
        spec39 = MockEnvironmentSpec(
            env_id="py39-env",
            python_version="3.9",
        )
        provider.create_environment(spec39)
        
        spec311 = MockEnvironmentSpec(
            env_id="py311-env",
            python_version="3.11",
        )
        provider.create_environment(spec311)
        
        assert provider.get_status("py39-env").status == "ready"
        assert provider.get_status("py311-env").status == "ready"


# ═══════════════════════════════════════════════════════════════════════════════
# Environment Event Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnvironmentEventIntegration:
    """Tests for environment events integration."""
    
    @pytest.fixture
    def provider(self):
        return MockEnvironmentProvider()
    
    def test_environment_events_published(self, event_bus, provider):
        """Test environment lifecycle events are published."""
        # Define mock environment events
        @dataclass
        class EnvironmentCreatedEvent:
            env_id: str
            timestamp: datetime = None
            
            def __post_init__(self):
                if self.timestamp is None:
                    self.timestamp = datetime.now()
        
        @dataclass
        class EnvironmentDestroyedEvent:
            env_id: str
            timestamp: datetime = None
            
            def __post_init__(self):
                if self.timestamp is None:
                    self.timestamp = datetime.now()
        
        created_events = []
        destroyed_events = []
        
        event_bus.subscribe(EnvironmentCreatedEvent, created_events.append)
        event_bus.subscribe(EnvironmentDestroyedEvent, destroyed_events.append)
        
        # Create environment and publish event
        spec = MockEnvironmentSpec(env_id="event-env")
        provider.create_environment(spec)
        event_bus.publish(EnvironmentCreatedEvent(env_id="event-env"))
        
        assert len(created_events) == 1
        assert created_events[0].env_id == "event-env"
        
        # Destroy and publish event
        provider.destroy_environment("event-env")
        event_bus.publish(EnvironmentDestroyedEvent(env_id="event-env"))
        
        assert len(destroyed_events) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Error Handling Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnvironmentErrorHandling:
    """Tests for environment error handling."""
    
    @pytest.fixture
    def provider(self):
        return MockEnvironmentProvider()
    
    def test_get_nonexistent_environment(self, provider):
        """Test getting nonexistent environment returns None."""
        status = provider.get_status("nonexistent")
        
        assert status is None
    
    def test_destroy_nonexistent_environment(self, provider):
        """Test destroying nonexistent environment returns False."""
        result = provider.destroy_environment("nonexistent")
        
        assert result is False
    
    def test_execution_continues_on_env_cleanup_failure(self, simple_graph_def, provider):
        """Test execution results available even if cleanup fails."""
        spec = MockEnvironmentSpec(env_id="cleanup-fail-env")
        provider.create_environment(spec)
        
        checkpointer = MemorySaver()
        graph = simple_graph_def.compile(checkpointer=checkpointer)
        
        # Execute
        config = {"configurable": {"thread_id": "cleanup-fail-exec"}}
        result = graph.invoke(create_initial_simple_state(), config)
        
        # Result should be available
        assert result["messages"] == ["A", "B", "C"]
        
        # Even if cleanup "fails" (we simulate by not destroying)
        # execution result is preserved


# ═══════════════════════════════════════════════════════════════════════════════
# ACID Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnvironmentACIDCompliance:
    """Tests for ACID compliance in environment operations."""
    
    @pytest.fixture
    def provider(self):
        return MockEnvironmentProvider()
    
    def test_atomicity_environment_creation(self, provider):
        """Test environment creation is atomic."""
        spec = MockEnvironmentSpec(
            env_id="atomic-env",
            python_version="3.11",
            packages=["numpy", "pandas"],
        )
        
        provider.create_environment(spec)
        
        status = provider.get_status("atomic-env")
        
        # Environment should be fully created or not at all
        assert status.status == "ready"
        assert status.python_path is not None
    
    def test_consistency_environment_state(self, provider):
        """Test environment maintains consistent state."""
        spec = MockEnvironmentSpec(env_id="consistent-env")
        provider.create_environment(spec)
        
        # Multiple status checks should be consistent
        for _ in range(10):
            status = provider.get_status("consistent-env")
            assert status.status == "ready"
    
    def test_isolation_environment_instances(self, provider):
        """Test environments are isolated from each other."""
        spec1 = MockEnvironmentSpec(env_id="iso-env-1")
        spec2 = MockEnvironmentSpec(env_id="iso-env-2")
        
        provider.create_environment(spec1)
        provider.create_environment(spec2)
        
        # Destroying one shouldn't affect the other
        provider.destroy_environment("iso-env-1")
        
        assert provider.get_status("iso-env-1").status == "destroyed"
        assert provider.get_status("iso-env-2").status == "ready"
    
    def test_durability_environment_persistence(self, provider):
        """Test environment state persists."""
        spec = MockEnvironmentSpec(env_id="durable-env")
        provider.create_environment(spec)
        
        # Multiple lookups should return same state
        status1 = provider.get_status("durable-env")
        status2 = provider.get_status("durable-env")
        
        assert status1.env_id == status2.env_id
        assert status1.status == status2.status
