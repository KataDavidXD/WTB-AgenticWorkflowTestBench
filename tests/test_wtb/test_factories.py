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
from threading import Event, Thread
from unittest.mock import MagicMock, patch

from wtb.config import WTBConfig, get_config, set_config, reset_config
from wtb.infrastructure.database import (
    UnitOfWorkFactory,
    InMemoryUnitOfWork,
    SQLAlchemyUnitOfWork,
)
from wtb.infrastructure.adapters import InMemoryStateAdapter
from wtb.application import ExecutionControllerFactory, NodeReplacerFactory
from wtb.application.factories import (
    BatchCoordinatorFactory,
    ManagedController,
    WTBTestBenchFactory,
)
from wtb.domain.models import (
    TestWorkflow,
    WorkflowNode,
    WorkflowEdge,
    Execution,
    ExecutionStatus,
    NodeBoundary,
    CheckpointFileLink,
)
from wtb.domain.models.file_processing import CommitId, FileCommit


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
        assert config.state_adapter_mode == "langgraph"
        assert "wtb.db" in config.wtb_db_url
        assert config.log_sql is True
    
    def test_for_production(self):
        """Test production configuration preset."""
        db_url = "postgresql://user:pass@localhost/wtb"
        config = WTBConfig.for_production(db_url)
        assert config.wtb_storage_mode == "sqlalchemy"
        assert config.wtb_db_url == db_url
        assert config.state_adapter_mode == "langgraph"
        assert config.log_sql is False
    
    def test_for_standalone(self):
        """Test standalone configuration preset."""
        config = WTBConfig.for_standalone("data/test")
        assert config.wtb_storage_mode == "sqlalchemy"
        assert config.state_adapter_mode == "langgraph"
        assert "data/test/wtb.db" in config.wtb_db_url

    def test_for_ray_production_uses_supported_durable_backend(self):
        """Ray production must not select a removed state backend."""
        config = WTBConfig.for_ray_production(
            db_url="postgresql://user:pass@localhost/wtb",
            ray_address="ray://cluster:10001",
        )

        assert config.state_adapter_mode == "langgraph"
    
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
        """Test node boundary repository operations (Updated 2026-01-15 for DDD compliance)."""
        uow = InMemoryUnitOfWork()
        with uow:
            # Create boundary using factory method
            boundary = NodeBoundary.create_for_node(
                execution_id="exec-1",
                node_id="node-1",
            )
            boundary.start(entry_checkpoint_id="cp-010")
            
            # Add (assigns ID)
            saved = uow.node_boundaries.add(boundary)
            assert saved.id is not None
            
            # Find by execution
            by_execution = uow.node_boundaries.find_by_execution("exec-1")
            assert len(by_execution) == 1
            
            # Find by node
            by_node = uow.node_boundaries.find_by_execution_and_node("exec-1", "node-1")
            assert by_node is not None
            
            # Complete and find completed
            by_node.complete(exit_checkpoint_id="cp-020")
            uow.node_boundaries.update(by_node)
            
            completed = uow.node_boundaries.find_completed_by_execution("exec-1")
            assert len(completed) == 1
    
    def test_checkpoint_file_link_repository(self):
        """Test checkpoint file link repository operations."""
        uow = InMemoryUnitOfWork()
        with uow:
            # Create checkpoint file link
            cf = CheckpointFileLink.create_from_values(
                checkpoint_id=100,
                commit_id=CommitId.generate(),
                file_count=5,
                total_size_bytes=1024,
            )
            
            # Save (v1.6: using add, returns None)
            uow.checkpoint_file_links.add(cf)
            
            # Find by checkpoint (v1.6: using get_by_checkpoint)
            by_cp = uow.checkpoint_file_links.get_by_checkpoint(100)
            assert by_cp is not None
            assert by_cp.commit_id.value == cf.commit_id.value
            
            # Find by commit
            by_commit = uow.checkpoint_file_links.get_by_commit(cf.commit_id)
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

    @pytest.mark.parametrize("mode", ["agentgit", "unexpected"])
    def test_removed_or_unknown_state_adapter_mode_fails_closed(self, mode):
        """Unsupported backends must never silently become in-memory state."""
        config = WTBConfig(state_adapter_mode=mode)

        with pytest.raises(ValueError, match="Unsupported state adapter mode"):
            ExecutionControllerFactory._create_state_adapter(config, None)

    def test_unavailable_langgraph_backend_fails_closed(self):
        """An explicitly durable backend must not degrade to memory."""
        config = WTBConfig.for_development()

        with (
            patch(
                "wtb.infrastructure.adapters.langgraph_state_adapter."
                "LANGGRAPH_AVAILABLE",
                False,
            ),
            pytest.raises(ImportError, match="LangGraph"),
        ):
            ExecutionControllerFactory._create_state_adapter(config, None)

    def test_production_uses_postgres_langgraph_config(self):
        """Production checkpoint state must use its configured PostgreSQL URL."""
        db_url = "postgresql://user:pass@localhost/wtb"
        config = WTBConfig.for_production(db_url)
        postgres_config = object()
        adapter = object()

        with (
            patch(
                "wtb.infrastructure.adapters.langgraph_state_adapter."
                "LangGraphConfig.for_production",
                return_value=postgres_config,
            ) as for_production,
            patch(
                "wtb.infrastructure.adapters.langgraph_state_adapter."
                "LangGraphConfig.for_development",
            ) as for_development,
            patch(
                "wtb.infrastructure.adapters.langgraph_state_adapter."
                "LangGraphStateAdapter",
                return_value=adapter,
            ) as adapter_class,
        ):
            result = ExecutionControllerFactory._create_state_adapter(config, None)

        assert result is adapter
        for_production.assert_called_once_with(db_url)
        for_development.assert_not_called()
        adapter_class.assert_called_once_with(postgres_config)

    def test_standalone_uses_sqlite_langgraph_config(self, tmp_path):
        """Standalone checkpoint state must remain durable on local SQLite."""
        config = WTBConfig.for_standalone(str(tmp_path))
        sqlite_config = object()
        adapter = object()

        with (
            patch(
                "wtb.infrastructure.adapters.langgraph_state_adapter."
                "LangGraphConfig.for_development",
                return_value=sqlite_config,
            ) as for_development,
            patch(
                "wtb.infrastructure.adapters.langgraph_state_adapter."
                "LangGraphConfig.for_production",
            ) as for_production,
            patch(
                "wtb.infrastructure.adapters.langgraph_state_adapter."
                "LangGraphStateAdapter",
                return_value=adapter,
            ) as adapter_class,
        ):
            result = ExecutionControllerFactory._create_state_adapter(config, None)

        assert result is adapter
        expected_path = str(tmp_path / "wtb_checkpoints.db")
        for_development.assert_called_once_with(expected_path)
        for_production.assert_not_called()
        adapter_class.assert_called_once_with(sqlite_config)

    def test_custom_sqlite_checkpoint_path_is_used_by_state_adapter(self, tmp_path):
        config = WTBConfig.for_development(str(tmp_path))
        custom_path = str(tmp_path / "custom-checkpoints.db")
        config.langgraph_checkpoint_path = custom_path
        sqlite_config = object()
        adapter = object()

        with (
            patch(
                "wtb.infrastructure.adapters.langgraph_state_adapter."
                "LangGraphConfig.for_development",
                return_value=sqlite_config,
            ) as for_development,
            patch(
                "wtb.infrastructure.adapters.langgraph_state_adapter."
                "LangGraphStateAdapter",
                return_value=adapter,
            ),
        ):
            result = ExecutionControllerFactory._create_state_adapter(config, None)

        assert result is adapter
        for_development.assert_called_once_with(custom_path)
    
    def test_create_with_dependencies(self):
        """Test creating controller with explicit dependencies."""
        uow = InMemoryUnitOfWork()
        adapter = InMemoryStateAdapter()
        
        controller = ExecutionControllerFactory.create_with_dependencies(
            uow=uow,
            state_adapter=adapter,
        )
        assert controller is not None

    def test_inmemory_controller_does_not_hold_transaction_lock_for_lifetime(self):
        uow = InMemoryUnitOfWork()
        controller = ExecutionControllerFactory.create_with_dependencies(
            uow=uow,
            state_adapter=InMemoryStateAdapter(),
        )
        acquired = Event()
        worker_errors = []

        def enter_transaction():
            try:
                with uow:
                    acquired.set()
            except BaseException as error:
                worker_errors.append(error)

        worker = Thread(target=enter_transaction, daemon=True)
        worker.start()
        acquired_without_factory_cleanup = acquired.wait(timeout=0.25)

        # RED cleanup: release the construction-time lock so the worker exits.
        if uow._transaction_depth > 0:
            uow.__exit__(None, None, None)
        worker.join(timeout=1.0)

        assert controller._uow is uow
        assert acquired_without_factory_cleanup is True
        assert not worker.is_alive()
        assert worker_errors == []
        assert uow._transaction_depth == 0

    def test_managed_controller_closes_owned_adapter_and_file_tracker(self):
        """An isolated controller must release resources before its UoW exits."""
        controller = MagicMock()
        state_adapter = MagicMock()
        file_tracker = MagicMock()
        controller._state_adapter = state_adapter
        controller._file_tracking = file_tracker
        uow = MagicMock()
        managed = ManagedController(
            controller=controller,
            uow=uow,
            owns_state_adapter=True,
            owns_file_tracking=True,
        )

        with managed:
            pass

        controller.set_deferred_commit.assert_called_once_with(True)
        uow.commit.assert_called_once_with()
        state_adapter.close.assert_called_once_with()
        file_tracker.close.assert_called_once_with()
        uow.__exit__.assert_called_once_with(None, None, None)

    def test_create_isolated_does_not_close_borrowed_file_tracker(self):
        """A caller-provided tracker remains usable after the managed scope."""
        tracker = MagicMock()
        factory = ExecutionControllerFactory(WTBConfig.for_testing())

        managed = factory.create_isolated(file_tracking_service=tracker)
        with managed:
            pass

        tracker.close.assert_not_called()

    def test_create_isolated_closes_entered_uow_when_adapter_setup_fails(self):
        """Factory setup failures must not leak an already-entered UoW."""
        uow = MagicMock()
        factory = ExecutionControllerFactory(WTBConfig.for_testing())

        with (
            patch.object(UnitOfWorkFactory, "create", return_value=uow),
            patch.object(
                factory,
                "_create_state_adapter_instance",
                side_effect=RuntimeError("adapter setup failed"),
            ),
            pytest.raises(RuntimeError, match="adapter setup failed"),
        ):
            factory.create_isolated()

        uow.__enter__.assert_called_once_with()
        exit_args = uow.__exit__.call_args.args
        assert exit_args[0] is RuntimeError
        assert str(exit_args[1]) == "adapter setup failed"


class TestDurableFactoryFailClosed:
    """Explicit durable factories must never degrade to in-memory state."""

    @staticmethod
    def _mock_uow():
        uow = MagicMock()
        uow.__enter__.return_value = uow
        uow.__exit__.return_value = None
        return uow

    def test_batch_coordinator_development_requires_langgraph(self, tmp_path):
        with (
            patch(
                "wtb.infrastructure.adapters.langgraph_state_adapter."
                "LANGGRAPH_AVAILABLE",
                False,
            ),
            pytest.raises(ImportError, match="LangGraph"),
        ):
            BatchCoordinatorFactory.create_for_development(str(tmp_path))

    def test_bench_development_requires_langgraph(self, tmp_path):
        uow = self._mock_uow()

        with (
            patch.object(UnitOfWorkFactory, "create", return_value=uow),
            patch(
                "wtb.infrastructure.adapters.langgraph_state_adapter."
                "LANGGRAPH_AVAILABLE",
                False,
            ),
            pytest.raises(ImportError, match="LangGraph"),
        ):
            WTBTestBenchFactory.create_for_development(str(tmp_path))

    def test_named_langgraph_factory_rejects_unknown_checkpointer(self, tmp_path):
        uow = self._mock_uow()
        adapter = MagicMock()

        with (
            patch.object(UnitOfWorkFactory, "create", return_value=uow),
            patch(
                "wtb.infrastructure.adapters.langgraph_state_adapter."
                "LangGraphStateAdapter",
                return_value=adapter,
            ),
            pytest.raises(ValueError, match="Unsupported checkpointer type"),
        ):
            WTBTestBenchFactory.create_with_langgraph(
                checkpointer_type="unexpected",
                data_dir=str(tmp_path),
            )

    def test_named_langgraph_factory_requires_dependency(self, tmp_path):
        uow = self._mock_uow()

        with (
            patch.object(UnitOfWorkFactory, "create", return_value=uow),
            patch(
                "wtb.infrastructure.adapters.langgraph_state_adapter."
                "LANGGRAPH_AVAILABLE",
                False,
            ),
            pytest.raises(ImportError, match="LangGraph"),
        ):
            WTBTestBenchFactory.create_with_langgraph(
                checkpointer_type="sqlite",
                data_dir=str(tmp_path),
            )

    def test_named_postgres_factory_requires_connection_string(self, tmp_path):
        uow = self._mock_uow()
        adapter = MagicMock()

        with (
            patch.object(UnitOfWorkFactory, "create", return_value=uow),
            patch(
                "wtb.infrastructure.adapters.langgraph_state_adapter."
                "LangGraphStateAdapter",
                return_value=adapter,
            ),
            pytest.raises(ValueError, match="connection_string"),
        ):
            WTBTestBenchFactory.create_with_langgraph(
                checkpointer_type="postgres",
                connection_string=None,
                data_dir=str(tmp_path),
            )


    def test_named_sqlite_factory_shares_custom_path_with_batch(self, tmp_path):
        uow = self._mock_uow()
        adapter = MagicMock()
        state_config = object()
        batch_runner = MagicMock()
        custom_path = str(tmp_path / "named-checkpoints.db")

        with (
            patch.object(UnitOfWorkFactory, "create", return_value=uow),
            patch(
                "wtb.infrastructure.adapters.langgraph_state_adapter."
                "LangGraphConfig.for_development",
                return_value=state_config,
            ) as for_development,
            patch(
                "wtb.infrastructure.adapters.langgraph_state_adapter."
                "LangGraphStateAdapter",
                return_value=adapter,
            ),
            patch.object(
                ExecutionControllerFactory,
                "_create_controller",
                return_value=MagicMock(),
            ),
            patch(
                "wtb.application.factories.OutboxExecutionControllerDecorator",
                return_value=MagicMock(),
            ),
            patch(
                "wtb.application.factories.BatchTestRunnerFactory.create_threadpool",
                return_value=batch_runner,
            ) as create_batch_runner,
            patch.object(
                NodeReplacerFactory,
                "create_with_dependencies",
                return_value=MagicMock(),
            ),
        ):
            bench = WTBTestBenchFactory.create_with_langgraph(
                checkpointer_type="sqlite",
                connection_string=custom_path,
                data_dir=str(tmp_path),
            )

        assert bench is not None
        for_development.assert_called_once_with(custom_path)
        batch_config = create_batch_runner.call_args.args[0]
        assert batch_config.langgraph_checkpoint_path == custom_path

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
            workflow = TestWorkflow(id="wf-test", name="execution_parent")
            uow.workflows.add(workflow)
            uow.commit()

            execution = Execution(
                workflow_id=workflow.id,
                status=ExecutionStatus.RUNNING,
            )
            uow.executions.add(execution)
            uow.commit()
            
            # Query back
            retrieved = uow.executions.get(execution.id)
            assert retrieved is not None
            assert retrieved.status == ExecutionStatus.RUNNING
    
    def test_node_boundary_persistence(self, sqlalchemy_uow):
        """Test node boundary persistence to database (Updated 2026-01-15 for DDD compliance)."""
        with sqlalchemy_uow as uow:
            boundary = NodeBoundary.create_for_node(
                execution_id="exec-test",
                node_id="process_node",
            )
            boundary.start(entry_checkpoint_id="cp-100")
            saved = uow.node_boundaries.add(boundary)
            uow.commit()
            
            # Query by execution
            boundaries = uow.node_boundaries.find_by_execution("exec-test")
            assert len(boundaries) == 1
            assert boundaries[0].node_id == "process_node"
    
    def test_checkpoint_file_link_persistence(self, sqlalchemy_uow):
        """Test checkpoint-file link persistence (bridges WTB-FileTracker)."""
        with sqlalchemy_uow as uow:
            commit = FileCommit.create(message="checkpoint parent")
            uow.file_commits.save(commit)
            uow.commit()

            cf = CheckpointFileLink.create_from_values(
                checkpoint_id="checkpoint-42",
                commit_id=commit.commit_id,
                file_count=3,
                total_size_bytes=4096,
            )
            # v1.6: using add instead of save
            uow.checkpoint_file_links.add(cf)
            uow.commit()
            
            # Query by checkpoint (v1.6: using get_by_checkpoint)
            link = uow.checkpoint_file_links.get_by_checkpoint("checkpoint-42")
            assert link is not None
            assert link.commit_id.value == cf.commit_id.value
            
            # Query by commit
            links = uow.checkpoint_file_links.get_by_commit(cf.commit_id)
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

