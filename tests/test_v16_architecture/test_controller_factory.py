"""
Unit Tests for ExecutionControllerFactory (v1.7).

Tests ACID-compliant isolated controller creation for batch execution.

Test Coverage:
- ManagedController lifecycle (context manager)
- Factory callable for batch runners
- Isolation guarantee (each call creates new UoW)
- UoW proper cleanup on exit
"""

import pytest
from unittest.mock import MagicMock, patch

from wtb.application.factories import (
    ExecutionControllerFactory,
    ManagedController,
)
from wtb.infrastructure.database import InMemoryUnitOfWork
from wtb.infrastructure.adapters import InMemoryStateAdapter
from wtb.domain.models import TestWorkflow, WorkflowNode


class TestManagedController:
    """Tests for ManagedController lifecycle management."""
    
    def test_managed_controller_context_manager_commits_on_success(self):
        """ManagedController should commit UoW on successful exit."""
        # Arrange
        mock_uow = MagicMock()
        mock_controller = MagicMock()
        
        managed = ManagedController(controller=mock_controller, uow=mock_uow)
        
        # Act
        with managed as m:
            assert m.controller == mock_controller
            assert m.uow == mock_uow
        
        # Assert - UoW should be committed
        mock_uow.commit.assert_called_once()
        mock_uow.__exit__.assert_called_once()
    
    def test_managed_controller_context_manager_rollbacks_on_exception(self):
        """ManagedController should rollback UoW on exception."""
        # Arrange
        mock_uow = MagicMock()
        mock_controller = MagicMock()
        
        managed = ManagedController(controller=mock_controller, uow=mock_uow)
        
        # Act & Assert
        with pytest.raises(ValueError):
            with managed:
                raise ValueError("Test error")
        
        # Assert - UoW should be rolled back
        mock_uow.rollback.assert_called_once()
        mock_uow.__exit__.assert_called_once()


class TestExecutionControllerFactoryIsolation:
    """Tests for ACID-compliant isolation in ExecutionControllerFactory."""
    
    def test_create_isolated_returns_managed_controller(self):
        """create_isolated() should return ManagedController with controller and UoW."""
        # Arrange
        from wtb.config import WTBConfig
        config = WTBConfig.for_testing()
        factory = ExecutionControllerFactory(config)
        
        # Act
        with factory.create_isolated() as managed:
            # Assert
            assert isinstance(managed, ManagedController)
            assert managed.controller is not None
            assert managed.uow is not None
    
    def test_create_isolated_creates_new_uow_each_call(self):
        """Each call to create_isolated() should create a new UoW (ACID Isolation)."""
        # Arrange
        from wtb.config import WTBConfig
        config = WTBConfig.for_testing()
        factory = ExecutionControllerFactory(config)
        
        # Act - Create two managed controllers
        with factory.create_isolated() as managed1:
            uow1_id = id(managed1.uow)
        
        with factory.create_isolated() as managed2:
            uow2_id = id(managed2.uow)
        
        # Assert - Different UoW instances
        assert uow1_id != uow2_id
    
    def test_get_factory_callable_returns_callable(self):
        """get_factory_callable() should return a callable that creates ManagedController."""
        # Arrange
        from wtb.config import WTBConfig
        config = WTBConfig.for_testing()
        
        # Act
        factory_callable = ExecutionControllerFactory.get_factory_callable(config)
        
        # Assert
        assert callable(factory_callable)
        
        # Verify it creates ManagedController
        with factory_callable() as managed:
            assert isinstance(managed, ManagedController)
    
    def test_factory_callable_creates_isolated_instances(self):
        """Factory callable should create isolated instances for batch execution."""
        # Arrange
        from wtb.config import WTBConfig
        config = WTBConfig.for_testing()
        factory_callable = ExecutionControllerFactory.get_factory_callable(config)
        
        # Act - Create all instances first, keeping references alive
        # This prevents Python from reusing memory addresses after garbage collection
        managed_instances = []
        for _ in range(3):
            managed = factory_callable()
            managed.__enter__()  # Enter context
            managed_instances.append(managed)
        
        # Collect IDs while all instances are alive
        controller_ids = [id(m.controller) for m in managed_instances]
        uow_ids = [id(m.uow) for m in managed_instances]
        
        # Cleanup - exit contexts in reverse order
        for managed in reversed(managed_instances):
            managed.__exit__(None, None, None)
        
        # Assert - All different instances (isolation)
        assert len(set(controller_ids)) == 3, "Controllers should be different instances"
        assert len(set(uow_ids)) == 3, "UoWs should be different instances"


class TestExecutionControllerFactoryIntegration:
    """Integration tests for ExecutionControllerFactory with real execution."""
    
    def test_controller_can_create_and_run_execution(self):
        """Controller from factory should be able to create and run executions."""
        # Arrange
        from wtb.config import WTBConfig
        config = WTBConfig.for_testing()
        factory = ExecutionControllerFactory(config)
        
        # Create a simple workflow
        workflow = TestWorkflow(id="test-wf", name="Test Workflow")
        workflow.add_node(WorkflowNode(id="start", name="Start", type="start"))
        workflow.add_node(WorkflowNode(id="end", name="End", type="end"))
        workflow.entry_point = "start"
        
        # Act
        with factory.create_isolated() as managed:
            # Register workflow
            managed.uow.workflows.add(workflow)
            
            # Create execution
            execution = managed.controller.create_execution(
                workflow=workflow,
                initial_state={"test": "value"},
            )
            
            assert execution is not None
            assert execution.id is not None
            assert execution.session_id is not None


class TestBackwardCompatibility:
    """Tests for backward compatibility with existing code."""
    
    def test_static_create_still_works(self):
        """Static create() method should still work for backward compatibility."""
        # Arrange
        from wtb.config import WTBConfig
        config = WTBConfig.for_testing()
        
        # Act
        controller = ExecutionControllerFactory.create(config)
        
        # Assert
        assert controller is not None
    
    def test_create_for_testing_still_works(self):
        """create_for_testing() should still work."""
        # Act
        controller = ExecutionControllerFactory.create_for_testing()
        
        # Assert
        assert controller is not None
    
    def test_create_with_dependencies_still_works(self):
        """create_with_dependencies() should still work."""
        # Arrange
        uow = InMemoryUnitOfWork()
        state_adapter = InMemoryStateAdapter()
        
        # Act
        controller = ExecutionControllerFactory.create_with_dependencies(
            uow=uow,
            state_adapter=state_adapter,
        )
        
        # Assert
        assert controller is not None
