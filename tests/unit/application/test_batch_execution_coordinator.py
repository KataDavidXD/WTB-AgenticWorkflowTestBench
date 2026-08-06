"""
Unit Tests for BatchExecutionCoordinator.

v1.8 (2026-02-05): Tests for rollback/fork coordination across batch test results.

Test Categories:
================
1. Single Operations: rollback, fork, rollback_and_run, fork_and_run
2. Batch Operations: batch_operate, batch_rollback, batch_fork
3. Transaction Handling: ACID compliance, error handling
4. File Restore: post-commit file restoration

SOLID Test Principles:
=====================
- Test behavior, not implementation
- Mock only at boundaries (interfaces)
- Each test has single assertion focus
"""

import uuid
from unittest.mock import MagicMock, call

import pytest

from wtb.domain.interfaces.batch_coordinator import (
    BatchOperationRequest,
    BatchOperationResult,
    IBatchExecutionCoordinator,
    IExecutionControllerFactory,
    OperationType,
)
from wtb.domain.models.outbox import OutboxEventType
from wtb.domain.models.workflow import (
    Execution,
    ExecutionState,
    ExecutionStatus,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_uow():
    """Create mock UnitOfWork."""
    uow = MagicMock()
    stored_execution = Execution(
        id="fixture-execution",
        workflow_id="fixture-workflow",
        status=ExecutionStatus.PAUSED,
        state=ExecutionState(current_node_id="fixture-node"),
        metadata={},
    )
    uow.executions = MagicMock()
    uow.executions.get.return_value = stored_execution
    uow.outbox = MagicMock()
    uow.__enter__ = MagicMock(return_value=uow)
    uow.__exit__ = MagicMock(return_value=None)
    return uow


@pytest.fixture
def mock_uow_factory(mock_uow):
    """Create mock UoW factory."""
    return MagicMock(return_value=mock_uow)


@pytest.fixture
def mock_state_adapter():
    """Create mock StateAdapter."""
    return MagicMock()


@pytest.fixture
def mock_file_tracking():
    """Create mock FileTrackingService."""
    service = MagicMock()
    service.is_available.return_value = True
    service.restore_commit.return_value = MagicMock(files_restored=3)
    service.get_commit_for_checkpoint.return_value = "ft-linked-checkpoint"
    return service


@pytest.fixture
def mock_execution():
    """Create mock Execution."""
    execution = MagicMock(spec=Execution)
    execution.id = str(uuid.uuid4())
    execution.status = ExecutionStatus.PAUSED
    execution.state = MagicMock(spec=ExecutionState)
    execution.state.workflow_variables = {
        "_file_tracking_result": {"commit_id": "ft-commit-123"}
    }
    return execution


@pytest.fixture
def mock_forked_execution():
    """Create mock forked Execution."""
    execution = MagicMock(spec=Execution)
    execution.id = str(uuid.uuid4())
    execution.status = ExecutionStatus.PAUSED
    execution.state = MagicMock(spec=ExecutionState)
    execution.state.workflow_variables = {}
    return execution


@pytest.fixture
def mock_controller(mock_execution, mock_forked_execution):
    """Create mock ExecutionController."""
    controller = MagicMock()
    controller.rollback.return_value = mock_execution
    controller.fork.return_value = mock_forked_execution
    controller.run.return_value = mock_execution
    return controller


@pytest.fixture
def mock_controller_factory(mock_controller):
    """Create mock ExecutionControllerFactory."""
    factory = MagicMock(spec=IExecutionControllerFactory)
    factory.create.return_value = mock_controller
    return factory


@pytest.fixture
def coordinator(mock_uow_factory, mock_controller_factory, mock_state_adapter, mock_file_tracking):
    """Create BatchExecutionCoordinator with mocks."""
    from wtb.application.services.batch_execution_coordinator import (
        BatchExecutionCoordinator,
    )
    
    return BatchExecutionCoordinator(
        uow_factory=mock_uow_factory,
        controller_factory=mock_controller_factory,
        state_adapter=mock_state_adapter,
        file_tracking=mock_file_tracking,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Rollback Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRollback:
    """Tests for rollback operation."""
    
    def test_rollback_restores_state_and_emits_events(
        self, 
        coordinator, 
        mock_controller, 
        mock_uow,
        mock_execution,
    ):
        """
        Rollback should restore state and emit outbox events.
        
        v1.9 Architecture: Coordinator emits two events:
        1. ROLLBACK_PERFORMED - domain event for tracking
        2. ROLLBACK_FILE_RESTORE - for OutboxProcessor to handle file restoration
        """
        exec_id = str(uuid.uuid4())
        checkpoint_id = str(uuid.uuid4())
        
        result = coordinator.rollback(exec_id, checkpoint_id)
        
        # Verify controller.rollback was called
        mock_controller.rollback.assert_called_once_with(exec_id, checkpoint_id)
        
        # v1.9: Verify outbox events were added (2 events now)
        assert mock_uow.outbox.add.call_count == 2
        
        # Find ROLLBACK_PERFORMED event
        calls = mock_uow.outbox.add.call_args_list
        events_by_type = {call[0][0].event_type: call[0][0] for call in calls}
        
        assert OutboxEventType.ROLLBACK_PERFORMED in events_by_type
        performed_event = events_by_type[OutboxEventType.ROLLBACK_PERFORMED]
        assert performed_event.aggregate_id == exec_id
        assert performed_event.payload["execution_id"] == exec_id
        assert performed_event.payload["checkpoint_id"] == checkpoint_id
        
        # Verify commit was called
        mock_uow.commit.assert_called_once()
        
        # Verify return value
        assert result == mock_execution
    
    def test_rollback_emits_file_restore_outbox_event(
        self, 
        coordinator, 
        mock_uow,
        mock_execution,
    ):
        """
        v1.9 Architecture: File restore is deferred to OutboxProcessor.
        
        Coordinator creates ROLLBACK_FILE_RESTORE event with necessary info.
        OutboxProcessor handles the actual file restoration.
        """
        exec_id = str(uuid.uuid4())
        checkpoint_id = str(uuid.uuid4())
        
        coordinator.rollback(exec_id, checkpoint_id)
        
        # Find ROLLBACK_FILE_RESTORE event
        calls = mock_uow.outbox.add.call_args_list
        file_restore_events = [
            call[0][0] for call in calls 
            if call[0][0].event_type == OutboxEventType.ROLLBACK_FILE_RESTORE
        ]
        
        assert len(file_restore_events) == 1
        file_restore_event = file_restore_events[0]
        
        # Verify event payload contains necessary info for OutboxProcessor
        assert file_restore_event.payload["execution_id"] == exec_id
        assert file_restore_event.payload["target_checkpoint_id"] == checkpoint_id
        assert "source_commit_id" in file_restore_event.payload

    def test_rollback_resolves_file_commit_from_checkpoint_link(
        self,
        coordinator,
        mock_uow,
        mock_file_tracking,
    ):
        """Rollback audit should use checkpoint_id -> file_commit_id link."""
        exec_id = str(uuid.uuid4())
        checkpoint_id = str(uuid.uuid4())
        mock_file_tracking.get_commit_for_checkpoint.return_value = "linked-from-cp"

        coordinator.rollback(exec_id, checkpoint_id)

        mock_file_tracking.get_commit_for_checkpoint.assert_called_with(checkpoint_id)
        file_restore_event = next(
            call_args.args[0]
            for call_args in mock_uow.outbox.add.call_args_list
            if call_args.args[0].event_type == OutboxEventType.ROLLBACK_FILE_RESTORE
        )
        assert file_restore_event.payload["source_commit_id"] == "linked-from-cp"
    
    def test_rollback_handles_errors_gracefully(
        self, 
        coordinator, 
        mock_uow,
    ):
        """Rollback should handle errors and still complete transaction."""
        exec_id = str(uuid.uuid4())
        checkpoint_id = str(uuid.uuid4())
        
        # Rollback completes successfully
        result = coordinator.rollback(exec_id, checkpoint_id)
        
        # Commit should have succeeded
        mock_uow.commit.assert_called_once()
        assert result is not None
    
    def test_rollback_rolls_back_uow_on_controller_failure(
        self, 
        coordinator, 
        mock_controller,
        mock_uow,
    ):
        """UoW should rollback if controller.rollback fails."""
        mock_controller.rollback.side_effect = ValueError("Execution not found")
        
        with pytest.raises(ValueError, match="Execution not found"):
            coordinator.rollback("bad-exec-id", "bad-cp-id")
        
        # Verify UoW was rolled back
        mock_uow.rollback.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# Fork Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFork:
    """Tests for fork operation."""
    
    def test_fork_creates_new_execution(
        self, 
        coordinator, 
        mock_controller,
        mock_uow,
        mock_forked_execution,
    ):
        """Fork should create new execution without modifying original."""
        exec_id = str(uuid.uuid4())
        checkpoint_id = str(uuid.uuid4())
        new_state = {"temperature": 0.7}
        
        result = coordinator.fork(exec_id, checkpoint_id, new_state)
        
        # Verify controller.fork was called
        mock_controller.fork.assert_called_once_with(exec_id, checkpoint_id, new_state)
        
        # Verify outbox event was added
        mock_uow.outbox.add.assert_called_once()
        event = mock_uow.outbox.add.call_args[0][0]
        assert event.event_type == OutboxEventType.EXECUTION_FORKED
        assert event.aggregate_id == mock_forked_execution.id
        assert event.payload["source_execution_id"] == exec_id
        assert event.payload["fork_execution_id"] == mock_forked_execution.id
        
        # Verify return value is new execution
        assert result == mock_forked_execution
    
    def test_fork_without_new_state(
        self, 
        coordinator, 
        mock_controller,
    ):
        """Fork should work without new_state parameter."""
        exec_id = str(uuid.uuid4())
        checkpoint_id = str(uuid.uuid4())
        
        coordinator.fork(exec_id, checkpoint_id)
        
        mock_controller.fork.assert_called_once_with(exec_id, checkpoint_id, None)


# ═══════════════════════════════════════════════════════════════════════════════
# Compound Operation Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRollbackAndRun:
    """Tests for rollback_and_run operation."""
    
    def test_rollback_and_run_requires_graph(self, coordinator):
        """rollback_and_run should raise if graph is None."""
        with pytest.raises(ValueError, match="Graph is required"):
            coordinator.rollback_and_run("exec-id", "cp-id", graph=None)
    
    def test_rollback_and_run_atomic(
        self, 
        coordinator, 
        mock_controller,
        mock_uow,
    ):
        """Compound operation should be atomic (single UoW)."""
        exec_id = str(uuid.uuid4())
        checkpoint_id = str(uuid.uuid4())
        mock_graph = MagicMock()
        
        coordinator.rollback_and_run(exec_id, checkpoint_id, mock_graph)
        
        # Both rollback and run should use same UoW session
        mock_controller.rollback.assert_called_once()
        mock_controller.run.assert_called_once_with(exec_id, graph=mock_graph)
        
        # Single commit for both operations
        mock_uow.commit.assert_called_once()


class TestForkAndRun:
    """Tests for fork_and_run operation."""
    
    def test_fork_and_run_requires_graph(self, coordinator):
        """fork_and_run should raise if graph is None."""
        with pytest.raises(ValueError, match="Graph is required"):
            coordinator.fork_and_run("exec-id", "cp-id", graph=None)
    
    def test_fork_and_run_atomic(
        self, 
        coordinator, 
        mock_controller,
        mock_uow,
        mock_forked_execution,
    ):
        """Compound operation should be atomic (single UoW)."""
        exec_id = str(uuid.uuid4())
        checkpoint_id = str(uuid.uuid4())
        mock_graph = MagicMock()
        new_state = {"param": "value"}
        
        coordinator.fork_and_run(exec_id, checkpoint_id, mock_graph, new_state)
        
        # Fork then run with new execution ID
        mock_controller.fork.assert_called_once_with(exec_id, checkpoint_id, new_state)
        mock_controller.run.assert_called_once_with(mock_forked_execution.id, graph=mock_graph)
        
        # Single commit for both operations
        mock_uow.commit.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# Batch Operation Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBatchOperate:
    """Tests for batch_operate method."""
    
    def test_batch_operate_continues_on_error_by_default(
        self, 
        mock_uow_factory,
        mock_controller_factory,
        mock_state_adapter,
        mock_file_tracking,
        mock_controller,
    ):
        """Batch should continue processing on individual failures."""
        from wtb.application.services.batch_execution_coordinator import (
            BatchExecutionCoordinator,
        )
        
        # First call succeeds, second fails, third succeeds
        mock_execution_1 = MagicMock(spec=Execution)
        mock_execution_1.id = "exec-1"
        mock_execution_1.status = ExecutionStatus.PAUSED
        mock_execution_1.state = MagicMock()
        mock_execution_1.state.workflow_variables = {}
        
        mock_execution_3 = MagicMock(spec=Execution)
        mock_execution_3.id = "exec-3"
        mock_execution_3.status = ExecutionStatus.PAUSED
        mock_execution_3.state = MagicMock()
        mock_execution_3.state.workflow_variables = {}
        
        mock_controller.rollback.side_effect = [
            mock_execution_1,
            ValueError("Checkpoint not found"),
            mock_execution_3,
        ]
        
        coordinator = BatchExecutionCoordinator(
            uow_factory=mock_uow_factory,
            controller_factory=mock_controller_factory,
            state_adapter=mock_state_adapter,
            file_tracking=mock_file_tracking,
        )
        
        requests = [
            BatchOperationRequest("exec-1", "cp-1", OperationType.ROLLBACK),
            BatchOperationRequest("exec-2", "cp-2", OperationType.ROLLBACK),
            BatchOperationRequest("exec-3", "cp-3", OperationType.ROLLBACK),
        ]
        
        results = coordinator.batch_operate(requests)
        
        assert len(results) == 3
        assert results[0].success is True
        assert results[1].success is False
        assert results[1].error == "Checkpoint not found"
        assert results[2].success is True
    
    def test_batch_operate_stops_on_error_when_requested(
        self, 
        mock_uow_factory,
        mock_controller_factory,
        mock_state_adapter,
        mock_file_tracking,
        mock_controller,
    ):
        """Batch should stop on first error when stop_on_error=True."""
        from wtb.application.services.batch_execution_coordinator import (
            BatchExecutionCoordinator,
        )
        
        mock_execution = MagicMock(spec=Execution)
        mock_execution.id = "exec-1"
        mock_execution.status = ExecutionStatus.PAUSED
        mock_execution.state = MagicMock()
        mock_execution.state.workflow_variables = {}
        
        mock_controller.rollback.side_effect = [
            mock_execution,
            ValueError("Error on second"),
            mock_execution,  # Should not be called
        ]
        
        coordinator = BatchExecutionCoordinator(
            uow_factory=mock_uow_factory,
            controller_factory=mock_controller_factory,
            state_adapter=mock_state_adapter,
            file_tracking=mock_file_tracking,
        )
        
        requests = [
            BatchOperationRequest("exec-1", "cp-1", OperationType.ROLLBACK),
            BatchOperationRequest("exec-2", "cp-2", OperationType.ROLLBACK),
            BatchOperationRequest("exec-3", "cp-3", OperationType.ROLLBACK),
        ]
        
        results = coordinator.batch_operate(requests, stop_on_error=True)
        
        # Should stop after second request
        assert len(results) == 2
        assert results[0].success is True
        assert results[1].success is False


# ═══════════════════════════════════════════════════════════════════════════════
# Convenience Method Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestConvenienceMethods:
    """Tests for batch_rollback and batch_fork convenience methods."""
    
    def test_batch_rollback_creates_rollback_requests(
        self, 
        coordinator,
        mock_controller,
    ):
        """batch_rollback should create ROLLBACK operation requests."""
        mock_execution = MagicMock(spec=Execution)
        mock_execution.id = "exec-id"
        mock_execution.status = ExecutionStatus.PAUSED
        mock_execution.state = MagicMock()
        mock_execution.state.workflow_variables = {}
        mock_controller.rollback.return_value = mock_execution
        
        items = [
            ("exec-1", "cp-1"),
            ("exec-2", "cp-2"),
        ]
        
        results = coordinator.batch_rollback(items)
        
        assert len(results) == 2
        assert all(r.operation == OperationType.ROLLBACK for r in results)
    
    def test_batch_fork_creates_fork_requests_with_new_state(
        self, 
        coordinator,
        mock_controller,
        mock_forked_execution,
    ):
        """batch_fork should create FORK operation requests with optional new_state."""
        items = [
            ("exec-1", "cp-1"),  # Without new_state
            ("exec-2", "cp-2", {"temperature": 0.5}),  # With new_state
        ]
        
        results = coordinator.batch_fork(items)
        
        assert len(results) == 2
        assert all(r.operation == OperationType.FORK for r in results)
        
        # Verify fork was called with correct arguments
        calls = mock_controller.fork.call_args_list
        assert calls[0] == call("exec-1", "cp-1", None)
        assert calls[1] == call("exec-2", "cp-2", {"temperature": 0.5})


# ═══════════════════════════════════════════════════════════════════════════════
# BatchTestResult Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBatchTestResultFields:
    """Tests for BatchTestResult rollback support fields."""
    
    def test_batch_test_result_has_rollback_fields(self):
        """BatchTestResult should include rollback support fields."""
        from wtb.domain.models.batch_test import BatchTestResult
        
        result = BatchTestResult(
            combination_name="Config_A",
            execution_id="exec-123",
            success=True,
            file_commit_id="ft-commit-456",
            checkpoint_count=5,
            last_checkpoint_id="cp-789",
        )
        
        assert result.file_commit_id == "ft-commit-456"
        assert result.checkpoint_count == 5
        assert result.last_checkpoint_id == "cp-789"
    
    def test_batch_test_result_to_dict_includes_rollback_fields(self):
        """to_dict() should include rollback support fields."""
        from wtb.domain.models.batch_test import BatchTestResult
        
        result = BatchTestResult(
            combination_name="Config_A",
            execution_id="exec-123",
            success=True,
            file_commit_id="ft-commit-456",
            checkpoint_count=5,
            last_checkpoint_id="cp-789",
        )
        
        data = result.to_dict()
        
        assert data["file_commit_id"] == "ft-commit-456"
        assert data["checkpoint_count"] == 5
        assert data["last_checkpoint_id"] == "cp-789"
    
    def test_batch_test_result_from_dict_restores_rollback_fields(self):
        """from_dict() should restore rollback support fields."""
        from wtb.domain.models.batch_test import BatchTestResult
        
        data = {
            "combination_name": "Config_A",
            "execution_id": "exec-123",
            "success": True,
            "file_commit_id": "ft-commit-456",
            "checkpoint_count": 5,
            "last_checkpoint_id": "cp-789",
        }
        
        result = BatchTestResult.from_dict(data)
        
        assert result.file_commit_id == "ft-commit-456"
        assert result.checkpoint_count == 5
        assert result.last_checkpoint_id == "cp-789"


# ═══════════════════════════════════════════════════════════════════════════════
# Interface Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestInterfaceCompliance:
    """Tests for interface compliance."""
    
    def test_coordinator_implements_interface(self, coordinator):
        """BatchExecutionCoordinator should implement IBatchExecutionCoordinator."""
        assert isinstance(coordinator, IBatchExecutionCoordinator)
    
    def test_operation_result_serialization(self):
        """BatchOperationResult should be serializable."""
        result = BatchOperationResult(
            execution_id="exec-123",
            checkpoint_id="cp-456",
            operation=OperationType.ROLLBACK,
            success=True,
            new_execution_id=None,
            files_restored=3,
            error=None,
        )
        
        data = result.to_dict()
        
        assert data["execution_id"] == "exec-123"
        assert data["checkpoint_id"] == "cp-456"
        assert data["operation"] == "rollback"
        assert data["success"] is True
        assert data["files_restored"] == 3


# ═══════════════════════════════════════════════════════════════════════════════
# v1.9 Rollback Cleanup Architecture Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRollbackCleanupArchitecture:
    """
    Tests for v1.9 rollback cleanup architecture.
    
    Key Design Principle:
    - BatchExecutionCoordinator produces events with cleanup config in payload
    - OutboxProcessor consumes events and performs actual cleanup
    - FileCleanupService is injected into OutboxProcessor, NOT coordinator
    """
    
    def test_coordinator_accepts_config_parameter(
        self, 
        mock_uow_factory, 
        mock_controller_factory, 
        mock_state_adapter, 
        mock_file_tracking
    ):
        """Coordinator should accept optional WTBConfig parameter."""
        from wtb.application.services.batch_execution_coordinator import (
            BatchExecutionCoordinator,
        )
        from wtb.config import WTBConfig
        
        config = WTBConfig(
            rollback_cleanup_enabled=True,
            rollback_cleanup_dry_run=True,
            rollback_cleanup_backup=False,
            rollback_cleanup_max_files=50,
        )
        
        coordinator = BatchExecutionCoordinator(
            uow_factory=mock_uow_factory,
            controller_factory=mock_controller_factory,
            state_adapter=mock_state_adapter,
            file_tracking=mock_file_tracking,
            config=config,
        )
        
        assert coordinator._config == config
    
    def test_coordinator_does_not_require_file_cleanup_service(
        self, 
        mock_uow_factory, 
        mock_controller_factory, 
        mock_state_adapter, 
        mock_file_tracking
    ):
        """Coordinator should NOT have file_cleanup_service parameter."""
        import inspect

        from wtb.application.services.batch_execution_coordinator import (
            BatchExecutionCoordinator,
        )
        
        sig = inspect.signature(BatchExecutionCoordinator.__init__)
        param_names = list(sig.parameters.keys())
        
        # file_cleanup_service should NOT be in coordinator params
        assert "file_cleanup_service" not in param_names
        # config should be in coordinator params
        assert "config" in param_names
    
    def test_rollback_includes_cleanup_config_in_outbox_payload(
        self, 
        mock_uow_factory, 
        mock_controller_factory, 
        mock_state_adapter, 
        mock_file_tracking,
        mock_controller,
        mock_uow,
        mock_execution,
    ):
        """Rollback should include cleanup configuration in outbox event payload."""
        from wtb.application.services.batch_execution_coordinator import (
            BatchExecutionCoordinator,
        )
        from wtb.config import WTBConfig
        
        config = WTBConfig(
            rollback_cleanup_enabled=True,
            rollback_cleanup_dry_run=True,
            rollback_cleanup_backup=True,
            rollback_cleanup_max_files=200,
        )
        
        coordinator = BatchExecutionCoordinator(
            uow_factory=mock_uow_factory,
            controller_factory=mock_controller_factory,
            state_adapter=mock_state_adapter,
            file_tracking=mock_file_tracking,
            config=config,
        )
        
        exec_id = str(uuid.uuid4())
        checkpoint_id = str(uuid.uuid4())
        
        coordinator.rollback(exec_id, checkpoint_id)
        
        # Verify outbox event payload contains cleanup config
        calls = mock_uow.outbox.add.call_args_list
        # Look for ROLLBACK_FILE_RESTORE event
        file_restore_event = None
        for call in calls:
            event = call[0][0]
            if event.event_type == OutboxEventType.ROLLBACK_FILE_RESTORE:
                file_restore_event = event
                break
        
        if file_restore_event:
            payload = file_restore_event.payload
            assert payload.get("cleanup_orphaned_files") is True
            assert payload.get("cleanup_dry_run") is True
            assert payload.get("cleanup_backup") is True
            assert payload.get("cleanup_max_files") == 200
    
    def test_rollback_without_config_does_not_enable_cleanup(
        self, 
        mock_uow_factory, 
        mock_controller_factory, 
        mock_state_adapter, 
        mock_file_tracking,
        mock_controller,
        mock_uow,
        mock_execution,
    ):
        """Rollback without config should not enable cleanup (opt-in feature)."""
        from wtb.application.services.batch_execution_coordinator import (
            BatchExecutionCoordinator,
        )
        
        # No config passed
        coordinator = BatchExecutionCoordinator(
            uow_factory=mock_uow_factory,
            controller_factory=mock_controller_factory,
            state_adapter=mock_state_adapter,
            file_tracking=mock_file_tracking,
        )
        
        exec_id = str(uuid.uuid4())
        checkpoint_id = str(uuid.uuid4())
        
        coordinator.rollback(exec_id, checkpoint_id)
        
        # Verify outbox event payload has cleanup disabled
        calls = mock_uow.outbox.add.call_args_list
        for call in calls:
            event = call[0][0]
            if event.event_type == OutboxEventType.ROLLBACK_FILE_RESTORE:
                payload = event.payload
                # cleanup_orphaned_files should be False or not present
                assert payload.get("cleanup_orphaned_files", False) is False
                break


class TestRollbackCleanupOptional:
    """Tests verifying cleanup feature is optional."""
    
    def test_cleanup_disabled_by_default(self):
        """WTBConfig should have cleanup disabled by default."""
        from wtb.config import WTBConfig
        
        config = WTBConfig()
        
        assert config.rollback_cleanup_enabled is False
    
    def test_cleanup_can_be_enabled_via_config(self):
        """Cleanup can be explicitly enabled via WTBConfig."""
        from wtb.config import WTBConfig
        
        config = WTBConfig(rollback_cleanup_enabled=True)
        
        assert config.rollback_cleanup_enabled is True
        
    def test_cleanup_config_options_have_safe_defaults(self):
        """Cleanup options should have safe defaults."""
        from wtb.config import WTBConfig
        
        config = WTBConfig()
        
        # Default: disabled
        assert config.rollback_cleanup_enabled is False
        # Default: dry run disabled (but feature itself is off)
        assert config.rollback_cleanup_dry_run is False
        # Default: backup enabled for safety
        assert config.rollback_cleanup_backup is True
        # Default: reasonable limit to prevent runaway deletion
        assert config.rollback_cleanup_max_files == 100
