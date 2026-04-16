"""
Tests for code quality fixes (2026-01-27).

Validates:
1. BranchResult removal from SDK exports
2. list_all() method in workflow repositories
3. DRY factory delegation
"""

import pytest
from typing import List


class TestBranchResultRemoval:
    """Test that BranchResult is no longer exported from SDK."""
    
    def test_branchresult_not_in_sdk_exports(self):
        """Verify BranchResult is not exported from wtb.sdk."""
        from wtb import sdk
        
        # Should NOT have BranchResult
        assert not hasattr(sdk, 'BranchResult'), "BranchResult should be removed from SDK exports"
    
    def test_sdk_all_does_not_contain_branchresult(self):
        """Verify __all__ doesn't contain BranchResult."""
        from wtb.sdk import __all__
        
        assert 'BranchResult' not in __all__, "BranchResult should not be in __all__"
    
    def test_sdk_still_has_valid_exports(self):
        """Verify SDK still has expected valid exports."""
        from wtb.sdk import (
            WTBTestBench,
            WTBTestBenchBuilder,
            WorkflowProject,
            RollbackResult,
            ForkResult,
            Execution,
            ExecutionStatus,
        )
        
        # All these should be importable
        assert WTBTestBench is not None
        assert WTBTestBenchBuilder is not None
        assert WorkflowProject is not None
        assert RollbackResult is not None
        assert ForkResult is not None
        assert Execution is not None
        assert ExecutionStatus is not None


class TestListAllMethod:
    """Test list_all() method in workflow repositories."""
    
    def test_interface_has_list_all(self):
        """Verify IWorkflowRepository interface has list_all method."""
        from wtb.domain.interfaces.repositories import IWorkflowRepository
        import inspect
        
        # Get all abstract methods
        abstract_methods = [
            name for name, method in inspect.getmembers(IWorkflowRepository)
            if hasattr(method, '__isabstractmethod__') and method.__isabstractmethod__
        ]
        
        assert 'list_all' in abstract_methods, "IWorkflowRepository should have list_all abstract method"
    
    def test_inmemory_workflow_repository_has_list_all(self):
        """Verify InMemoryWorkflowRepository implements list_all."""
        from wtb.infrastructure.database.inmemory_unit_of_work import InMemoryWorkflowRepository
        
        repo = InMemoryWorkflowRepository()
        
        # Method should exist and be callable
        assert hasattr(repo, 'list_all'), "InMemoryWorkflowRepository should have list_all method"
        assert callable(repo.list_all), "list_all should be callable"
        
        # Should return empty list initially
        result = repo.list_all()
        assert isinstance(result, list), "list_all should return a list"
        assert len(result) == 0, "Empty repo should return empty list"
    
    def test_inmemory_list_all_returns_all_workflows(self):
        """Verify list_all returns all workflows without pagination."""
        from wtb.infrastructure.database.inmemory_unit_of_work import InMemoryWorkflowRepository
        from wtb.domain.models import TestWorkflow
        
        repo = InMemoryWorkflowRepository()
        
        # Add 5 workflows
        for i in range(5):
            workflow = TestWorkflow(
                id=f"wf-{i}",
                name=f"workflow-{i}",
                description=f"Test workflow {i}",
            )
            repo.add(workflow)
        
        # list_all should return all 5
        all_workflows = repo.list_all()
        assert len(all_workflows) == 5, "list_all should return all workflows"
        
        # Verify by name
        names = {w.name for w in all_workflows}
        expected = {f"workflow-{i}" for i in range(5)}
        assert names == expected, "All workflow names should be returned"
    
    def test_inmemory_list_vs_list_all(self):
        """Verify list() respects pagination while list_all() doesn't."""
        from wtb.infrastructure.database.inmemory_unit_of_work import InMemoryWorkflowRepository
        from wtb.domain.models import TestWorkflow
        
        repo = InMemoryWorkflowRepository()
        
        # Add 10 workflows
        for i in range(10):
            workflow = TestWorkflow(
                id=f"wf-{i}",
                name=f"workflow-{i}",
            )
            repo.add(workflow)
        
        # list() with limit should respect pagination
        paginated = repo.list(limit=3, offset=0)
        assert len(paginated) == 3, "list() should respect limit"
        
        # list_all() should return all
        all_workflows = repo.list_all()
        assert len(all_workflows) == 10, "list_all() should return all without pagination"
    
    def test_base_repository_has_list_all(self):
        """Verify BaseRepository has list_all method."""
        from wtb.infrastructure.database.repositories.base import BaseRepository
        
        assert hasattr(BaseRepository, 'list_all'), "BaseRepository should have list_all method"
    
    def test_project_service_list_workflows_uses_list_all(self):
        """Verify ProjectService.list_workflows works with list_all."""
        from wtb.infrastructure.database import InMemoryUnitOfWork
        from wtb.application.services import ProjectService
        from wtb.domain.models import TestWorkflow
        
        uow = InMemoryUnitOfWork()
        uow.__enter__()
        
        # Add workflows directly to repo
        for i in range(3):
            workflow = TestWorkflow(
                id=f"wf-{i}",
                name=f"workflow-{i}",
            )
            uow.workflows.add(workflow)
        
        # ProjectService should use list_all
        service = ProjectService(uow)
        workflows = service.list_workflows()
        
        assert len(workflows) == 3, "ProjectService.list_workflows should return all workflows"
        
        uow.__exit__(None, None, None)


class TestFactoryDRYDelegation:
    """Test that WTBTestBenchFactory delegates to ExecutionControllerFactory."""
    
    def test_wtb_factory_uses_execution_controller_factory(self):
        """Verify WTBTestBenchFactory._create_state_adapter delegates."""
        from wtb.application.factories import WTBTestBenchFactory, ExecutionControllerFactory
        from wtb.config import WTBConfig
        
        # Create test config
        config = WTBConfig.for_testing()
        
        # Both factories should return the same type of adapter
        adapter1 = WTBTestBenchFactory._create_state_adapter(config)
        adapter2 = ExecutionControllerFactory._create_state_adapter(config, None)
        
        # Should be same type (InMemoryStateAdapter for testing config)
        assert type(adapter1).__name__ == type(adapter2).__name__, \
            "Both factories should return same adapter type"
    
    def test_factory_methods_exist(self):
        """Verify factory methods exist."""
        from wtb.application.factories import (
            WTBTestBenchFactory,
            ExecutionControllerFactory,
            BatchTestRunnerFactory,
            NodeReplacerFactory,
        )
        
        # WTBTestBenchFactory methods
        assert hasattr(WTBTestBenchFactory, 'create')
        assert hasattr(WTBTestBenchFactory, 'create_for_testing')
        assert hasattr(WTBTestBenchFactory, 'create_for_development')
        assert hasattr(WTBTestBenchFactory, '_create_state_adapter')
        
        # ExecutionControllerFactory methods
        assert hasattr(ExecutionControllerFactory, 'create')
        assert hasattr(ExecutionControllerFactory, 'create_isolated')
        assert hasattr(ExecutionControllerFactory, 'get_factory_callable')
        assert hasattr(ExecutionControllerFactory, '_create_state_adapter')
    
    def test_create_for_testing_returns_wtb_test_bench(self):
        """Verify create_for_testing returns valid WTBTestBench."""
        from wtb.application.factories import WTBTestBenchFactory
        from wtb.sdk.test_bench import WTBTestBench
        
        wtb = WTBTestBenchFactory.create_for_testing()
        
        assert isinstance(wtb, WTBTestBench), "Should return WTBTestBench instance"
        assert wtb._project_service is not None, "Should have project service"
        assert wtb._variant_service is not None, "Should have variant service"
        assert wtb._exec_ctrl is not None, "Should have execution controller"


class TestInterfaceConsistency:
    """Test interface consistency after changes."""
    
    def test_workflow_repository_interface_complete(self):
        """Verify IWorkflowRepository has all required methods."""
        from wtb.domain.interfaces.repositories import IWorkflowRepository
        import inspect
        
        # Get all methods including inherited
        methods = [name for name, _ in inspect.getmembers(IWorkflowRepository, predicate=inspect.isfunction)]
        
        required_methods = [
            'get', 'list', 'exists', 'add', 'update', 'delete',  # From IRepository
            'find_by_name', 'find_by_version',  # Domain-specific
            'list_all',  # New method
        ]
        
        for method in required_methods:
            assert method in methods or hasattr(IWorkflowRepository, method), \
                f"IWorkflowRepository should have {method} method"
    
    def test_inmemory_uow_workflow_repository_is_compliant(self):
        """Verify InMemoryWorkflowRepository implements full interface."""
        from wtb.infrastructure.database.inmemory_unit_of_work import InMemoryWorkflowRepository
        from wtb.domain.interfaces.repositories import IWorkflowRepository
        
        # Should be a subclass
        assert issubclass(InMemoryWorkflowRepository, IWorkflowRepository), \
            "InMemoryWorkflowRepository should implement IWorkflowRepository"
        
        # Create instance and verify methods
        repo = InMemoryWorkflowRepository()
        
        required_methods = [
            'get', 'list', 'list_all', 'exists', 'add', 'update', 'delete',
            'find_by_name', 'find_by_version',
        ]
        
        for method in required_methods:
            assert hasattr(repo, method), f"Repo should have {method} method"
            assert callable(getattr(repo, method)), f"{method} should be callable"


class TestEndToEndIntegration:
    """End-to-end integration tests for the fixes."""
    
    def test_full_workflow_registration_and_listing(self):
        """Test full workflow registration and listing cycle."""
        from wtb.application.factories import WTBTestBenchFactory
        from wtb.sdk import WorkflowProject
        
        # Create WTB instance
        wtb = WTBTestBenchFactory.create_for_testing()
        
        # Create and register projects
        def dummy_graph():
            from langgraph.graph import StateGraph
            from typing import TypedDict
            
            class State(TypedDict):
                value: str
            
            graph = StateGraph(State)
            graph.add_node("start", lambda s: s)
            graph.set_entry_point("start")
            return graph.compile()
        
        project1 = WorkflowProject(name="test-project-1", graph_factory=dummy_graph)
        project2 = WorkflowProject(name="test-project-2", graph_factory=dummy_graph)
        
        wtb.register_project(project1)
        wtb.register_project(project2)
        
        # List should return both
        projects = wtb.list_projects()
        assert len(projects) == 2, "Should have 2 registered projects"
        assert "test-project-1" in projects
        assert "test-project-2" in projects
    
    def test_sdk_exports_are_functional(self):
        """Test that all SDK exports work correctly."""
        # Import should not raise
        from wtb.sdk import (
            WTBTestBench,
            WTBTestBenchBuilder,
            WorkflowProject,
            RollbackResult,
            ForkResult,
            Execution,
            ExecutionState,
            ExecutionStatus,
            Checkpoint,
            CheckpointId,
            BatchTest,
            BatchTestResult,
            BatchTestStatus,
        )
        
        # Create instances to verify they work
        rollback = RollbackResult(
            execution_id="exec-1",
            to_checkpoint_id="cp-1",
            success=True,
        )
        assert rollback.success is True
        
        fork = ForkResult(
            fork_execution_id="fork-1",
            source_execution_id="exec-1",
            source_checkpoint_id="cp-1",
        )
        assert fork.fork_execution_id == "fork-1"
