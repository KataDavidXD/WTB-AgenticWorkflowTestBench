"""
Tests for WTB Factories and Configuration.

Tests:
- WTBConfig creation and modes
- UnitOfWorkFactory 
- ExecutionControllerFactory
- InMemoryUnitOfWork
"""

import pytest
from datetime import datetime

from wtb.config import WTBConfig, get_config, set_config, reset_config
from wtb.infrastructure.database import (
    UnitOfWorkFactory,
    InMemoryUnitOfWork,
    SQLAlchemyUnitOfWork,
)
from wtb.infrastructure.adapters import InMemoryStateAdapter
from wtb.application import ExecutionControllerFactory, NodeReplacerFactory
from wtb.domain.models import (
    TestWorkflow,
    WorkflowNode,
    WorkflowEdge,
    Execution,
    ExecutionStatus,
    NodeBoundary,
    CheckpointFile,
)


class TestWTBConfig:
    """Tests for WTBConfig."""
    
    def test_default_config(self):
        """Test default configuration values."""
        config = WTBConfig()
        assert config.wtb_storage_mode == "inmemory"
        assert config.state_adapter_mode == "inmemory"
        assert config.data_dir == "data"
    
    def test_for_testing(self):
        """Test testing configuration preset."""
        config = WTBConfig.for_testing()
        assert config.wtb_storage_mode == "inmemory"
        assert config.state_adapter_mode == "inmemory"
        assert config.filetracker_enabled is False
        assert config.ide_sync_enabled is False
    
    def test_for_development(self):
        """Test development configuration preset."""
        config = WTBConfig.for_development()
        assert config.wtb_storage_mode == "sqlalchemy"
        assert config.state_adapter_mode == "agentgit"
        assert "wtb.db" in config.wtb_db_url
        assert config.log_sql is True
    
    def test_for_production(self):
        """Test production configuration preset."""
        db_url = "postgresql://user:pass@localhost/wtb"
        config = WTBConfig.for_production(db_url)
        assert config.wtb_storage_mode == "sqlalchemy"
        assert config.wtb_db_url == db_url
        assert config.state_adapter_mode == "agentgit"
        assert config.log_sql is False
    
    def test_for_standalone(self):
        """Test standalone configuration preset."""
        config = WTBConfig.for_standalone("data/test")
        assert config.wtb_storage_mode == "sqlalchemy"
        assert config.state_adapter_mode == "agentgit"
        assert "data/test/wtb.db" in config.wtb_db_url
    
    def test_to_dict(self):
        """Test serialization to dictionary."""
        config = WTBConfig.for_testing()
        d = config.to_dict()
        assert d["wtb_storage_mode"] == "inmemory"
        assert d["state_adapter_mode"] == "inmemory"
    
    def test_global_config(self):
        """Test global config management."""
        reset_config()
        
        # Get creates default
        config = get_config()
        assert config is not None
        
        # Set overrides
        custom = WTBConfig.for_testing()
        set_config(custom)
        assert get_config() is custom
        
        # Reset clears
        reset_config()


class TestUnitOfWorkFactory:
    """Tests for UnitOfWorkFactory."""
    
    def test_create_inmemory(self):
        """Test creating in-memory UoW."""
        uow = UnitOfWorkFactory.create_inmemory()
        assert isinstance(uow, InMemoryUnitOfWork)
    
    def test_create_sqlalchemy(self):
        """Test creating SQLAlchemy UoW."""
        uow = UnitOfWorkFactory.create_sqlalchemy("sqlite:///:memory:")
        assert isinstance(uow, SQLAlchemyUnitOfWork)
    
    def test_create_with_mode_inmemory(self):
        """Test create with mode=inmemory."""
        uow = UnitOfWorkFactory.create(mode="inmemory")
        assert isinstance(uow, InMemoryUnitOfWork)
    
    def test_create_with_mode_sqlalchemy(self):
        """Test create with mode=sqlalchemy."""
        uow = UnitOfWorkFactory.create(mode="sqlalchemy", db_url="sqlite:///:memory:")
        assert isinstance(uow, SQLAlchemyUnitOfWork)
    
    def test_create_with_unknown_mode_raises(self):
        """Test that unknown mode raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            UnitOfWorkFactory.create(mode="unknown")
        assert "Unknown storage mode" in str(exc_info.value)
    
    def test_create_for_testing(self):
        """Test convenience method for testing."""
        uow = UnitOfWorkFactory.create_for_testing()
        assert isinstance(uow, InMemoryUnitOfWork)


class TestInMemoryUnitOfWork:
    """Tests for InMemoryUnitOfWork."""
    
    def test_context_manager(self):
        """Test UoW as context manager."""
        uow = InMemoryUnitOfWork()
        with uow:
            assert uow._in_transaction is True
        assert uow._in_transaction is False
    
    def test_workflow_repository(self):
        """Test workflow repository operations."""
        uow = InMemoryUnitOfWork()
        with uow:
            # Create workflow
            workflow = TestWorkflow(name="test", description="Test workflow")
            workflow.add_node(WorkflowNode(id="start", name="Start", type="start"))
            
            # Add
            uow.workflows.add(workflow)
            
            # Get
            retrieved = uow.workflows.get(workflow.id)
            assert retrieved is not None
            assert retrieved.name == "test"
            
            # Update
            retrieved.description = "Updated"
            uow.workflows.update(retrieved)
            
            # Verify
            updated = uow.workflows.get(workflow.id)
            assert updated.description == "Updated"
            
            # Delete
            assert uow.workflows.delete(workflow.id) is True
            assert uow.workflows.get(workflow.id) is None
    
    def test_execution_repository(self):
        """Test execution repository operations."""
        uow = InMemoryUnitOfWork()
        with uow:
            # Create execution
            execution = Execution(
                workflow_id="wf-1",
                status=ExecutionStatus.PENDING,
            )
            
            # Add
            uow.executions.add(execution)
            
            # Get
            retrieved = uow.executions.get(execution.id)
            assert retrieved is not None
            assert retrieved.workflow_id == "wf-1"
            
            # Find by workflow
            by_workflow = uow.executions.find_by_workflow("wf-1")
            assert len(by_workflow) == 1
            
            # Find by status
            by_status = uow.executions.find_by_status(ExecutionStatus.PENDING)
            assert len(by_status) == 1
    
    def test_node_boundary_repository(self):
        """Test node boundary repository operations."""
        uow = InMemoryUnitOfWork()
        with uow:
            # Create boundary
            boundary = NodeBoundary(
                execution_id="exec-1",
                internal_session_id=1,
                node_id="node-1",
                entry_checkpoint_id=10,
            )
            
            # Add (assigns ID)
            saved = uow.node_boundaries.add(boundary)
            assert saved.id is not None
            
            # Find by session
            by_session = uow.node_boundaries.find_by_session(1)
            assert len(by_session) == 1
            
            # Find by node
            by_node = uow.node_boundaries.find_by_node(1, "node-1")
            assert by_node is not None
            
            # Complete and find completed
            by_node.exit_checkpoint_id = 20
            by_node.node_status = "completed"
            uow.node_boundaries.update(by_node)
            
            completed = uow.node_boundaries.find_completed(1)
            assert len(completed) == 1
    
    def test_checkpoint_file_repository(self):
        """Test checkpoint file repository operations."""
        uow = InMemoryUnitOfWork()
        with uow:
            # Create checkpoint file link
            cf = CheckpointFile(
                checkpoint_id=100,
                file_commit_id="commit-abc",
                file_count=5,
                total_size_bytes=1024,
            )
            
            # Add
            saved = uow.checkpoint_files.add(cf)
            assert saved.id is not None
            
            # Find by checkpoint
            by_cp = uow.checkpoint_files.find_by_checkpoint(100)
            assert by_cp is not None
            assert by_cp.file_commit_id == "commit-abc"
            
            # Find by file commit
            by_commit = uow.checkpoint_files.find_by_file_commit("commit-abc")
            assert len(by_commit) == 1
    
    def test_reset(self):
        """Test resetting all repositories."""
        uow = InMemoryUnitOfWork()
        with uow:
            workflow = TestWorkflow(name="test")
            uow.workflows.add(workflow)
            assert uow.workflows.get(workflow.id) is not None
        
        uow.reset()
        
        with uow:
            assert uow.workflows.get(workflow.id) is None


class TestExecutionControllerFactory:
    """Tests for ExecutionControllerFactory."""
    
    def test_create_for_testing(self):
        """Test creating controller for testing."""
        controller = ExecutionControllerFactory.create_for_testing()
        assert controller is not None
    
    def test_create_with_config(self):
        """Test creating controller with config."""
        config = WTBConfig.for_testing()
        controller = ExecutionControllerFactory.create(config)
        assert controller is not None
    
    def test_create_with_dependencies(self):
        """Test creating controller with explicit dependencies."""
        uow = InMemoryUnitOfWork()
        adapter = InMemoryStateAdapter()
        
        controller = ExecutionControllerFactory.create_with_dependencies(
            uow=uow,
            state_adapter=adapter,
        )
        assert controller is not None


class TestNodeReplacerFactory:
    """Tests for NodeReplacerFactory."""
    
    def test_create_for_testing(self):
        """Test creating replacer for testing."""
        replacer = NodeReplacerFactory.create_for_testing()
        assert replacer is not None
    
    def test_create_with_dependencies(self):
        """Test creating replacer with explicit UoW."""
        uow = InMemoryUnitOfWork()
        replacer = NodeReplacerFactory.create_with_dependencies(uow)
        assert replacer is not None


class TestSQLAlchemyPersistence:
    """Tests for SQLAlchemy persistence to wtb.db."""
    
    @pytest.fixture
    def sqlalchemy_uow(self, tmp_path):
        """Create SQLAlchemy UoW with temp database."""
        db_path = tmp_path / "test_wtb.db"
        uow = UnitOfWorkFactory.create_sqlalchemy(f"sqlite:///{db_path}")
        yield uow
    
    def test_workflow_persistence(self, sqlalchemy_uow):
        """Test workflow persistence to database."""
        with sqlalchemy_uow as uow:
            workflow = TestWorkflow(name="persisted_workflow", description="Test")
            workflow.add_node(WorkflowNode(id="start", name="Start", type="start"))
            uow.workflows.add(workflow)
            uow.commit()
            
            # Query back
            retrieved = uow.workflows.get(workflow.id)
            assert retrieved is not None
            assert retrieved.name == "persisted_workflow"
    
    def test_execution_persistence(self, sqlalchemy_uow):
        """Test execution persistence to database."""
        with sqlalchemy_uow as uow:
            execution = Execution(
                workflow_id="wf-test",
                status=ExecutionStatus.RUNNING,
            )
            uow.executions.add(execution)
            uow.commit()
            
            # Query back
            retrieved = uow.executions.get(execution.id)
            assert retrieved is not None
            assert retrieved.status == ExecutionStatus.RUNNING
    
    def test_node_boundary_persistence(self, sqlalchemy_uow):
        """Test node boundary persistence to database."""
        with sqlalchemy_uow as uow:
            boundary = NodeBoundary(
                execution_id="exec-test",
                internal_session_id=123,
                node_id="process_node",
                entry_checkpoint_id=100,
            )
            saved = uow.node_boundaries.add(boundary)
            uow.commit()
            
            # Query by session
            boundaries = uow.node_boundaries.find_by_session(123)
            assert len(boundaries) == 1
            assert boundaries[0].node_id == "process_node"
    
    def test_checkpoint_file_persistence(self, sqlalchemy_uow):
        """Test checkpoint-file link persistence (bridges WTB-FileTracker)."""
        with sqlalchemy_uow as uow:
            cf = CheckpointFile(
                checkpoint_id=42,
                file_commit_id="ft-commit-uuid-123",
                file_count=3,
                total_size_bytes=4096,
            )
            uow.checkpoint_files.add(cf)
            uow.commit()
            
            # Query by checkpoint
            link = uow.checkpoint_files.find_by_checkpoint(42)
            assert link is not None
            assert link.file_commit_id == "ft-commit-uuid-123"
            
            # Query by file commit
            links = uow.checkpoint_files.find_by_file_commit("ft-commit-uuid-123")
            assert len(links) == 1
    
    def test_transaction_rollback(self, sqlalchemy_uow):
        """Test that rollback discards uncommitted changes."""
        with sqlalchemy_uow as uow:
            workflow = TestWorkflow(name="rollback_test")
            uow.workflows.add(workflow)
            # Don't commit - should be rolled back
        
        # New transaction - should not see the workflow
        with sqlalchemy_uow as uow:
            retrieved = uow.workflows.find_by_name("rollback_test")
            assert retrieved is None

