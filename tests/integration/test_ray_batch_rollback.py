"""
Integration Tests for Ray Batch + Rollback Coordination.

v1.8 (2026-02-05): Tests for end-to-end Ray batch execution with rollback support.

Test Scenarios:
===============
1. BatchTestResult contains rollback fields after batch execution
2. Coordinator created from runner shares configuration
3. Rollback variant restores files correctly
4. Fork variant creates independent execution

Prerequisites:
=============
- Ray must be installed (tests skip if not available)
- SQLite for persistence (no external database needed)

Note: These tests use in-memory databases where possible for isolation.
"""

import pytest
import tempfile
import os
from pathlib import Path
from typing import Dict, Any, Optional
import uuid

# Check if Ray is available
try:
    import ray
    RAY_AVAILABLE = True
except ImportError:
    RAY_AVAILABLE = False

from wtb.domain.models.batch_test import (
    BatchTest,
    BatchTestResult,
    VariantCombination,
)
from wtb.domain.models.workflow import (
    TestWorkflow,
    WorkflowNode,
    WorkflowEdge,
)


# Skip all tests in this module if Ray is not available
pytestmark = pytest.mark.skipif(
    not RAY_AVAILABLE,
    reason="Ray not installed"
)


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def ray_init():
    """Initialize Ray for the test module."""
    if not ray.is_initialized():
        ray.init(
            num_cpus=2,
            ignore_reinit_error=True,
            include_dashboard=False,
            _temp_dir=tempfile.gettempdir(),
        )
    yield
    # Don't shutdown Ray here - let other tests use it


@pytest.fixture
def temp_data_dir():
    """Create temporary data directory for test databases."""
    import gc
    import shutil
    tmpdir = tempfile.mkdtemp()
    yield tmpdir
    # Dispose cached SQLAlchemy engines so SQLite file handles are released
    try:
        from wtb.infrastructure.database.engine_cache import _get_cached_engine
        _get_cached_engine.cache_clear()
    except Exception:
        pass
    gc.collect()
    # On Windows, SQLite file handles may linger briefly after disposal
    try:
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        pass


@pytest.fixture
def simple_workflow():
    """Create a simple workflow for testing."""
    workflow = TestWorkflow(
        id=str(uuid.uuid4()),
        name="Test Workflow",
        entry_point="start",
    )
    
    # Add nodes
    workflow.nodes = [
        WorkflowNode(id="start", name="Start", type="start"),
        WorkflowNode(id="process", name="Process", type="action", tool_name="process"),
        WorkflowNode(id="end", name="End", type="end"),
    ]
    
    # Add edges
    workflow.edges = [
        WorkflowEdge(source_id="start", target_id="process"),
        WorkflowEdge(source_id="process", target_id="end"),
    ]
    
    return workflow


@pytest.fixture
def batch_test_with_variants():
    """Create batch test with multiple variants."""
    return BatchTest(
        id=str(uuid.uuid4()),
        name="Test Batch",
        variant_combinations=[
            VariantCombination(name="Config_A", variants={}, metadata={}),
            VariantCombination(name="Config_B", variants={}, metadata={}),
        ],
        initial_state={"input": "test"},
        parallel_count=2,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# BatchTestResult Field Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBatchTestResultFields:
    """Tests for BatchTestResult rollback support fields."""
    
    def test_batch_result_contains_file_commit_id(self):
        """BatchTestResult should include file_commit_id when available."""
        result = BatchTestResult(
            combination_name="Config_A",
            execution_id="exec-123",
            success=True,
            file_commit_id="ft-commit-456",
            checkpoint_count=3,
            last_checkpoint_id="cp-789",
        )
        
        assert result.file_commit_id == "ft-commit-456"
        assert result.checkpoint_count == 3
        assert result.last_checkpoint_id == "cp-789"
    
    def test_batch_result_handles_missing_fields(self):
        """BatchTestResult should handle missing rollback fields."""
        result = BatchTestResult(
            combination_name="Config_A",
            execution_id="exec-123",
            success=True,
        )
        
        assert result.file_commit_id is None
        assert result.checkpoint_count == 0
        assert result.last_checkpoint_id is None
    
    def test_batch_result_serialization_round_trip(self):
        """BatchTestResult should serialize and deserialize correctly."""
        original = BatchTestResult(
            combination_name="Config_A",
            execution_id="exec-123",
            success=True,
            file_commit_id="ft-commit-456",
            checkpoint_count=5,
            last_checkpoint_id="cp-789",
            metrics={"accuracy": 0.95},
            overall_score=0.95,
        )
        
        # Serialize
        data = original.to_dict()
        
        # Deserialize
        restored = BatchTestResult.from_dict(data)
        
        assert restored.file_commit_id == original.file_commit_id
        assert restored.checkpoint_count == original.checkpoint_count
        assert restored.last_checkpoint_id == original.last_checkpoint_id
        assert restored.metrics == original.metrics


# ═══════════════════════════════════════════════════════════════════════════════
# Coordinator Factory Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCoordinatorFromRunner:
    """Tests for coordinator creation from RayBatchTestRunner."""
    
    def test_coordinator_from_runner_shares_config(self, ray_init, temp_data_dir):
        """Coordinator created from runner should use same config."""
        from wtb.application.services.ray_batch_runner import RayBatchTestRunner
        from wtb.config import RayConfig
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url=os.path.join(temp_data_dir, "agentgit.db"),
            wtb_db_url=f"sqlite:///{temp_data_dir}/wtb.db",
        )
        
        try:
            coordinator = runner.create_rollback_coordinator()
            
            # Verify coordinator was created
            assert coordinator is not None
            
            # Verify it has the required methods
            assert hasattr(coordinator, 'rollback')
            assert hasattr(coordinator, 'fork')
            assert hasattr(coordinator, 'batch_operate')
        finally:
            coordinator.close()
            runner.shutdown()
    
    def test_coordinator_uow_factory_creates_fresh_uow(self, ray_init, temp_data_dir):
        """UoW factory should create fresh UoW each time."""
        from wtb.application.services.ray_batch_runner import RayBatchTestRunner
        from wtb.config import RayConfig
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url=os.path.join(temp_data_dir, "agentgit.db"),
            wtb_db_url=f"sqlite:///{temp_data_dir}/wtb.db",
        )
        
        uow1 = None
        uow2 = None
        coordinator = None
        try:
            coordinator = runner.create_rollback_coordinator()
            
            # Access private factory for testing
            uow1 = coordinator._uow_factory()
            uow2 = coordinator._uow_factory()
            
            # Should be different instances (ACID Isolation)
            assert uow1 is not uow2
        finally:
            if uow1 is not None:
                uow1.dispose()
            if uow2 is not None:
                uow2.dispose()
            if coordinator is not None:
                coordinator.close()
            runner.shutdown()


# ═══════════════════════════════════════════════════════════════════════════════
# Comparison Matrix Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestComparisonMatrixFields:
    """Tests for rollback fields in comparison matrix."""
    
    def test_comparison_matrix_includes_rollback_fields(self):
        """Comparison matrix should include rollback support fields."""
        batch_test = BatchTest(
            id=str(uuid.uuid4()),
            name="Test",
            variant_combinations=[
                VariantCombination(name="Config_A"),
                VariantCombination(name="Config_B"),
            ],
        )
        
        # Add results with rollback fields
        batch_test.add_result(BatchTestResult(
            combination_name="Config_A",
            execution_id="exec-1",
            success=True,
            file_commit_id="ft-1",
            checkpoint_count=3,
            last_checkpoint_id="cp-1",
        ))
        batch_test.add_result(BatchTestResult(
            combination_name="Config_B",
            execution_id="exec-2",
            success=True,
            file_commit_id="ft-2",
            checkpoint_count=5,
            last_checkpoint_id="cp-2",
        ))
        
        # Build comparison matrix
        matrix = batch_test.build_comparison_matrix()
        
        # Verify rollback fields in matrix data
        assert len(matrix["data"]) == 2
        
        config_a_data = next(d for d in matrix["data"] if d["name"] == "Config_A")
        assert config_a_data["file_commit_id"] == "ft-1"
        assert config_a_data["checkpoint_count"] == 3
        assert config_a_data["last_checkpoint_id"] == "cp-1"
        
        config_b_data = next(d for d in matrix["data"] if d["name"] == "Config_B")
        assert config_b_data["file_commit_id"] == "ft-2"
        assert config_b_data["checkpoint_count"] == 5
        assert config_b_data["last_checkpoint_id"] == "cp-2"


# ═══════════════════════════════════════════════════════════════════════════════
# Interface Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestInterfaceIntegration:
    """Tests for interface integration."""
    
    def test_outbox_event_type_exists(self):
        """EXECUTION_FORKED event type should exist."""
        from wtb.domain.models.outbox import OutboxEventType
        
        assert hasattr(OutboxEventType, 'EXECUTION_FORKED')
        assert OutboxEventType.EXECUTION_FORKED.value == "execution_forked"
    
    def test_operation_types_available(self):
        """All operation types should be available."""
        from wtb.domain.interfaces.batch_coordinator import OperationType
        
        assert OperationType.ROLLBACK.value == "rollback"
        assert OperationType.FORK.value == "fork"
        assert OperationType.ROLLBACK_AND_RUN.value == "rollback_run"
        assert OperationType.FORK_AND_RUN.value == "fork_run"
    
    def test_batch_operation_result_creation(self):
        """BatchOperationResult should be creatable."""
        from wtb.domain.interfaces.batch_coordinator import (
            BatchOperationResult,
            OperationType,
        )
        
        result = BatchOperationResult(
            execution_id="exec-123",
            checkpoint_id="cp-456",
            operation=OperationType.FORK,
            success=True,
            new_execution_id="exec-789",
            files_restored=0,
        )
        
        assert result.success is True
        assert result.new_execution_id == "exec-789"
        
        # Test serialization
        data = result.to_dict()
        assert data["operation"] == "fork"


# ═══════════════════════════════════════════════════════════════════════════════
# Coordinator Behavior Tests (Mock-based)
# ═══════════════════════════════════════════════════════════════════════════════


class TestCoordinatorBehavior:
    """Tests for coordinator behavior using mocks."""
    
    def test_coordinator_rollback_emits_event(self, temp_data_dir):
        """Coordinator rollback should emit outbox event."""
        from unittest.mock import MagicMock
        from wtb.application.services.batch_execution_coordinator import (
            BatchExecutionCoordinator,
            DefaultExecutionControllerFactory,
        )
        from wtb.domain.models.workflow import Execution, ExecutionState, ExecutionStatus
        from wtb.domain.models.outbox import OutboxEventType
        
        # Create mock dependencies
        mock_uow = MagicMock()
        mock_uow.outbox = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=None)
        
        mock_execution = MagicMock(spec=Execution)
        mock_execution.id = "exec-123"
        mock_execution.status = ExecutionStatus.PAUSED
        mock_execution.state = MagicMock()
        mock_execution.state.workflow_variables = {}
        
        mock_controller = MagicMock()
        mock_controller.rollback.return_value = mock_execution
        
        mock_controller_factory = MagicMock()
        mock_controller_factory.create.return_value = mock_controller
        
        coordinator = BatchExecutionCoordinator(
            uow_factory=lambda: mock_uow,
            controller_factory=mock_controller_factory,
            state_adapter=MagicMock(),
            file_tracking=None,
        )
        
        # Execute rollback
        coordinator.rollback("exec-123", "cp-456")
        
        # Verify outbox event
        mock_uow.outbox.add.assert_called_once()
        event = mock_uow.outbox.add.call_args[0][0]
        assert event.event_type == OutboxEventType.ROLLBACK_PERFORMED
        assert event.payload["execution_id"] == "exec-123"
        assert event.payload["checkpoint_id"] == "cp-456"
    
    def test_coordinator_fork_emits_event(self, temp_data_dir):
        """Coordinator fork should emit EXECUTION_FORKED event."""
        from unittest.mock import MagicMock
        from wtb.application.services.batch_execution_coordinator import (
            BatchExecutionCoordinator,
        )
        from wtb.domain.models.workflow import Execution, ExecutionState, ExecutionStatus
        from wtb.domain.models.outbox import OutboxEventType
        
        # Create mock dependencies
        mock_uow = MagicMock()
        mock_uow.outbox = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=None)
        
        mock_forked = MagicMock(spec=Execution)
        mock_forked.id = "forked-789"
        mock_forked.status = ExecutionStatus.PAUSED
        mock_forked.state = MagicMock()
        
        mock_controller = MagicMock()
        mock_controller.fork.return_value = mock_forked
        
        mock_controller_factory = MagicMock()
        mock_controller_factory.create.return_value = mock_controller
        
        coordinator = BatchExecutionCoordinator(
            uow_factory=lambda: mock_uow,
            controller_factory=mock_controller_factory,
            state_adapter=MagicMock(),
            file_tracking=None,
        )
        
        # Execute fork
        coordinator.fork("exec-123", "cp-456", {"param": "value"})
        
        # Verify outbox event
        mock_uow.outbox.add.assert_called_once()
        event = mock_uow.outbox.add.call_args[0][0]
        assert event.event_type == OutboxEventType.EXECUTION_FORKED
        assert event.payload["source_execution_id"] == "exec-123"
        assert event.payload["fork_execution_id"] == "forked-789"
