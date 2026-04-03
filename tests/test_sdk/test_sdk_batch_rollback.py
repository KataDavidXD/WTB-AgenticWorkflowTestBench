"""
SDK Batch Rollback/Fork Integration Tests.

Tests the SDK convenience methods for batch rollback and fork operations.
These tests verify the integration between WTBTestBench and BatchExecutionCoordinator.

Design (SOLID):
- SRP: SDK provides convenience methods, coordinator handles actual operations
- DIP: Tests use interfaces, not concrete implementations
- ACID: Verifies transaction consistency in rollback/fork operations
"""

import pytest
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime, timezone

from wtb.sdk import (
    WTBTestBench,
    BatchRollbackResult,
    BatchForkResult,
    BatchTestResult,
    Checkpoint,
    CheckpointId,
    Execution,
    ExecutionStatus,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Test Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_project_service():
    """Create mock project service."""
    service = Mock()
    service.get_workflow_by_name.return_value = Mock(id="workflow-123")
    return service


@pytest.fixture
def mock_variant_service():
    """Create mock variant service."""
    return Mock()


@pytest.fixture
def mock_execution_controller():
    """Create mock execution controller."""
    controller = Mock()
    controller.supports_time_travel.return_value = True
    controller.get_checkpoint_history.return_value = [
        {
            "checkpoint_id": "cp-1",
            "step": 1,
            "writes": {"node_a": {}},
            "next": ["node_b"],
            "values": {"key": "value1"},
        },
        {
            "checkpoint_id": "cp-2",
            "step": 2,
            "writes": {"node_b": {}},
            "next": ["node_c"],
            "values": {"key": "value2"},
        },
    ]
    return controller


@pytest.fixture
def mock_batch_runner():
    """Create mock batch runner with coordinator factory."""
    runner = Mock()
    
    # Create mock coordinator
    mock_coordinator = Mock()
    mock_execution = Mock(spec=Execution)
    mock_execution.id = "exec-rolled-back"
    mock_execution.status = ExecutionStatus.PAUSED
    mock_coordinator.rollback.return_value = mock_execution
    
    mock_forked = Mock(spec=Execution)
    mock_forked.id = "exec-forked"
    mock_forked.status = ExecutionStatus.PENDING
    mock_coordinator.fork.return_value = mock_forked
    
    runner.create_rollback_coordinator.return_value = mock_coordinator
    return runner


@pytest.fixture
def wtb_with_mocks(
    mock_project_service,
    mock_variant_service,
    mock_execution_controller,
    mock_batch_runner,
):
    """Create WTBTestBench with mock dependencies."""
    return WTBTestBench(
        project_service=mock_project_service,
        variant_service=mock_variant_service,
        execution_controller=mock_execution_controller,
        batch_runner=mock_batch_runner,
    )


@pytest.fixture
def sample_batch_result():
    """Create sample BatchTestResult for testing."""
    return BatchTestResult(
        combination_name="Config_A",
        execution_id="exec-123",
        success=True,
        file_commit_id="ft-456",
        checkpoint_count=3,
        last_checkpoint_id="cp-789",
    )


@pytest.fixture
def sample_batch_result_no_checkpoint():
    """Create BatchTestResult without checkpoint info."""
    return BatchTestResult(
        combination_name="Config_B",
        execution_id="exec-456",
        success=True,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# BatchRollbackResult DTO Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBatchRollbackResultDTO:
    """Tests for BatchRollbackResult DTO."""
    
    def test_success_result_has_required_fields(self):
        """Success result should have all required fields."""
        result = BatchRollbackResult(
            execution_id="exec-123",
            checkpoint_id="cp-456",
            success=True,
        )
        
        assert result.execution_id == "exec-123"
        assert result.checkpoint_id == "cp-456"
        assert result.success is True
        assert result.error is None
        assert result.execution is None
        assert result.files_restored == 0
    
    def test_error_result_contains_error_message(self):
        """Error result should contain error message."""
        result = BatchRollbackResult(
            execution_id="exec-123",
            checkpoint_id="cp-456",
            success=False,
            error="Checkpoint not found",
        )
        
        assert result.success is False
        assert result.error == "Checkpoint not found"


class TestBatchForkResultDTO:
    """Tests for BatchForkResult DTO."""
    
    def test_success_result_has_required_fields(self):
        """Success result should have all required fields."""
        result = BatchForkResult(
            source_execution_id="exec-123",
            fork_execution_id="exec-forked",
            checkpoint_id="cp-456",
        )
        
        assert result.source_execution_id == "exec-123"
        assert result.fork_execution_id == "exec-forked"
        assert result.checkpoint_id == "cp-456"
        assert result.error is None
    
    def test_error_result_contains_error_message(self):
        """Error result should contain error message."""
        result = BatchForkResult(
            source_execution_id="exec-123",
            fork_execution_id="",
            checkpoint_id="cp-456",
            error="Fork failed",
        )
        
        assert result.fork_execution_id == ""
        assert result.error == "Fork failed"


# ═══════════════════════════════════════════════════════════════════════════════
# get_batch_coordinator() Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetBatchCoordinator:
    """Tests for WTBTestBench.get_batch_coordinator()."""
    
    def test_returns_coordinator_from_batch_runner(self, wtb_with_mocks, mock_batch_runner):
        """Should return coordinator from batch runner factory."""
        coordinator = wtb_with_mocks.get_batch_coordinator()
        
        assert coordinator is not None
        mock_batch_runner.create_rollback_coordinator.assert_called_once()
    
    def test_caches_coordinator_instance(self, wtb_with_mocks, mock_batch_runner):
        """Should cache coordinator instance for reuse."""
        coord1 = wtb_with_mocks.get_batch_coordinator()
        coord2 = wtb_with_mocks.get_batch_coordinator()
        
        assert coord1 is coord2
        # Factory should only be called once
        assert mock_batch_runner.create_rollback_coordinator.call_count == 1
    
    def test_fallback_without_batch_runner(
        self,
        mock_project_service,
        mock_variant_service,
        mock_execution_controller,
    ):
        """Should create coordinator without batch runner (fallback via Application factory)."""
        wtb = WTBTestBench(
            project_service=mock_project_service,
            variant_service=mock_variant_service,
            execution_controller=mock_execution_controller,
            batch_runner=None,  # No batch runner
        )
        
        # This should delegate to BatchCoordinatorFactory (Application layer)
        with patch('wtb.application.factories.BatchCoordinatorFactory.create_default') as mock_factory:
            mock_factory.return_value = Mock()
            coordinator = wtb.get_batch_coordinator()
            assert coordinator is not None
            mock_factory.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# rollback_batch_result() Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRollbackBatchResult:
    """Tests for WTBTestBench.rollback_batch_result()."""
    
    def test_rollback_with_explicit_checkpoint_id(
        self,
        wtb_with_mocks,
        sample_batch_result,
    ):
        """Should rollback using explicit checkpoint_id."""
        result = wtb_with_mocks.rollback_batch_result(
            sample_batch_result,
            checkpoint_id="cp-explicit",
        )
        
        assert result.success is True
        assert result.checkpoint_id == "cp-explicit"
        assert result.execution_id == "exec-123"
    
    def test_rollback_uses_last_checkpoint_id_by_default(
        self,
        wtb_with_mocks,
        sample_batch_result,
    ):
        """Should use last_checkpoint_id when checkpoint_id not provided."""
        result = wtb_with_mocks.rollback_batch_result(sample_batch_result)
        
        assert result.success is True
        assert result.checkpoint_id == "cp-789"  # From sample_batch_result.last_checkpoint_id
    
    def test_raises_error_without_execution_id(self, wtb_with_mocks):
        """Should raise ValueError if result has no execution_id."""
        result = BatchTestResult(
            combination_name="Config_A",
            execution_id="",  # Empty
            success=True,
        )
        
        with pytest.raises(ValueError, match="no execution_id"):
            wtb_with_mocks.rollback_batch_result(result)
    
    def test_raises_error_without_checkpoint_id(
        self,
        wtb_with_mocks,
        sample_batch_result_no_checkpoint,
    ):
        """Should raise ValueError if no checkpoint_id available."""
        with pytest.raises(ValueError, match="No checkpoint_id"):
            wtb_with_mocks.rollback_batch_result(sample_batch_result_no_checkpoint)
    
    def test_returns_error_result_on_coordinator_failure(
        self,
        wtb_with_mocks,
        sample_batch_result,
        mock_batch_runner,
    ):
        """Should return error result if coordinator fails."""
        coordinator = mock_batch_runner.create_rollback_coordinator.return_value
        coordinator.rollback.side_effect = RuntimeError("DB connection failed")
        
        result = wtb_with_mocks.rollback_batch_result(sample_batch_result)
        
        assert result.success is False
        assert "DB connection failed" in result.error


# ═══════════════════════════════════════════════════════════════════════════════
# fork_batch_result() Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestForkBatchResult:
    """Tests for WTBTestBench.fork_batch_result()."""
    
    def test_fork_with_explicit_checkpoint_id(
        self,
        wtb_with_mocks,
        sample_batch_result,
    ):
        """Should fork using explicit checkpoint_id."""
        result = wtb_with_mocks.fork_batch_result(
            sample_batch_result,
            checkpoint_id="cp-explicit",
        )
        
        assert result.fork_execution_id == "exec-forked"
        assert result.checkpoint_id == "cp-explicit"
        assert result.source_execution_id == "exec-123"
    
    def test_fork_uses_last_checkpoint_id_by_default(
        self,
        wtb_with_mocks,
        sample_batch_result,
    ):
        """Should use last_checkpoint_id when checkpoint_id not provided."""
        result = wtb_with_mocks.fork_batch_result(sample_batch_result)
        
        assert result.checkpoint_id == "cp-789"  # From sample_batch_result.last_checkpoint_id
    
    def test_fork_with_new_state(
        self,
        wtb_with_mocks,
        sample_batch_result,
        mock_batch_runner,
    ):
        """Should pass new_state to coordinator."""
        result = wtb_with_mocks.fork_batch_result(
            sample_batch_result,
            new_state={"temperature": 0.5},
        )
        
        coordinator = mock_batch_runner.create_rollback_coordinator.return_value
        call_args = coordinator.fork.call_args
        assert call_args[0] == ("exec-123", "cp-789", {"temperature": 0.5})
        assert "graph" in call_args[1]
    
    def test_raises_error_without_execution_id(self, wtb_with_mocks):
        """Should raise ValueError if result has no execution_id."""
        result = BatchTestResult(
            combination_name="Config_A",
            execution_id="",  # Empty
            success=True,
        )
        
        with pytest.raises(ValueError, match="no execution_id"):
            wtb_with_mocks.fork_batch_result(result)
    
    def test_raises_error_without_checkpoint_id(
        self,
        wtb_with_mocks,
        sample_batch_result_no_checkpoint,
    ):
        """Should raise ValueError if no checkpoint_id available."""
        with pytest.raises(ValueError, match="No checkpoint_id"):
            wtb_with_mocks.fork_batch_result(sample_batch_result_no_checkpoint)
    
    def test_returns_error_result_on_coordinator_failure(
        self,
        wtb_with_mocks,
        sample_batch_result,
        mock_batch_runner,
    ):
        """Should return error result if coordinator fails."""
        coordinator = mock_batch_runner.create_rollback_coordinator.return_value
        coordinator.fork.side_effect = RuntimeError("Fork failed")
        
        result = wtb_with_mocks.fork_batch_result(sample_batch_result)
        
        assert result.fork_execution_id == ""
        assert "Fork failed" in result.error


# ═══════════════════════════════════════════════════════════════════════════════
# get_batch_result_checkpoints() Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetBatchResultCheckpoints:
    """Tests for WTBTestBench.get_batch_result_checkpoints()."""
    
    def test_returns_checkpoints_for_result(
        self,
        wtb_with_mocks,
        sample_batch_result,
    ):
        """Should return checkpoints for batch result."""
        checkpoints = wtb_with_mocks.get_batch_result_checkpoints(sample_batch_result)
        
        assert len(checkpoints) == 2
        assert checkpoints[0].step == 1
        assert checkpoints[1].step == 2
    
    def test_returns_empty_list_for_result_without_execution_id(self, wtb_with_mocks):
        """Should return empty list if result has no execution_id."""
        result = BatchTestResult(
            combination_name="Config_A",
            execution_id="",
            success=True,
        )
        
        checkpoints = wtb_with_mocks.get_batch_result_checkpoints(result)
        
        assert checkpoints == []


# ═══════════════════════════════════════════════════════════════════════════════
# SDK Export Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSDKExports:
    """Tests for SDK module exports."""
    
    def test_batch_rollback_result_exported(self):
        """BatchRollbackResult should be exported from SDK."""
        from wtb.sdk import BatchRollbackResult
        assert BatchRollbackResult is not None
    
    def test_batch_fork_result_exported(self):
        """BatchForkResult should be exported from SDK."""
        from wtb.sdk import BatchForkResult
        assert BatchForkResult is not None
    
    def test_batch_execution_coordinator_exported(self):
        """BatchExecutionCoordinator should be exported from SDK."""
        from wtb.sdk import BatchExecutionCoordinator
        assert BatchExecutionCoordinator is not None
    
    def test_batch_operation_request_exported(self):
        """BatchOperationRequest should be exported from SDK."""
        from wtb.sdk import BatchOperationRequest
        assert BatchOperationRequest is not None
    
    def test_batch_operation_result_exported(self):
        """BatchOperationResult should be exported from SDK."""
        from wtb.sdk import BatchOperationResult
        assert BatchOperationResult is not None
    
    def test_operation_type_exported(self):
        """OperationType should be exported from SDK."""
        from wtb.sdk import OperationType
        assert OperationType is not None
        assert hasattr(OperationType, 'ROLLBACK')
        assert hasattr(OperationType, 'FORK')


# ═══════════════════════════════════════════════════════════════════════════════
# Integration Pattern Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSDKIntegrationPatterns:
    """Tests for common SDK integration patterns."""
    
    def test_rollback_then_fork_pattern(
        self,
        wtb_with_mocks,
        sample_batch_result,
    ):
        """
        Test common pattern: rollback then fork.
        
        User workflow:
        1. Run batch test
        2. Rollback failed variant
        3. Fork to explore alternative
        """
        # Step 1: Rollback
        rollback_result = wtb_with_mocks.rollback_batch_result(sample_batch_result)
        assert rollback_result.success is True
        
        # Step 2: Fork
        fork_result = wtb_with_mocks.fork_batch_result(
            sample_batch_result,
            new_state={"retry": True},
        )
        assert fork_result.fork_execution_id != ""
    
    def test_inspect_then_rollback_pattern(
        self,
        wtb_with_mocks,
        sample_batch_result,
    ):
        """
        Test common pattern: inspect checkpoints then rollback.
        
        User workflow:
        1. Get checkpoints
        2. Choose checkpoint
        3. Rollback to chosen checkpoint
        """
        # Step 1: Get checkpoints
        checkpoints = wtb_with_mocks.get_batch_result_checkpoints(sample_batch_result)
        assert len(checkpoints) > 0
        
        # Step 2: Choose checkpoint (e.g., first one)
        chosen_cp = checkpoints[0]
        
        # Step 3: Rollback to chosen checkpoint
        result = wtb_with_mocks.rollback_batch_result(
            sample_batch_result,
            checkpoint_id=str(chosen_cp.id.value),
        )
        assert result.checkpoint_id == str(chosen_cp.id.value)
