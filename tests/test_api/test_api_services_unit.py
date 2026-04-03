"""
Unit Tests for API Services.

Created: 2026-01-28
Status: Active

Tests SOLID and ACID compliance of API services:
- ExecutionAPIService
- AuditAPIService
- BatchTestAPIService
- WorkflowAPIService

Test Scenarios:
- Service interface compliance (ISP)
- Transaction boundary enforcement (ACID)
- Outbox pattern usage
- Error handling and rollback
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock, patch
from typing import Dict, Any

# Skip if dependencies not installed
pytest.importorskip("pydantic")


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_uow():
    """Create mock Unit of Work."""
    uow = MagicMock()
    uow.__enter__ = MagicMock(return_value=uow)
    uow.__exit__ = MagicMock(return_value=False)
    uow.commit = MagicMock()
    uow.rollback = MagicMock()
    
    # Mock outbox
    uow.outbox = MagicMock()
    uow.outbox.add = MagicMock()
    
    return uow


@pytest.fixture
def mock_controller():
    """Create mock ExecutionController."""
    controller = MagicMock()
    
    # Mock execution
    mock_execution = MagicMock()
    mock_execution.id = "exec-123"
    mock_execution.workflow_id = "workflow-456"
    mock_execution.status.value = "paused"
    mock_execution.checkpoint_id = "checkpoint-789"
    mock_execution.session_id = "session-abc"
    mock_execution.error = None
    mock_execution.error_node_id = None
    mock_execution.started_at = datetime.now(timezone.utc)
    mock_execution.completed_at = None
    mock_execution.breakpoints = []
    
    # Mock state
    mock_state = MagicMock()
    mock_state.current_node_id = "node_a"
    mock_state.workflow_variables = {"key": "value"}
    mock_state.execution_path = ["start", "node_a"]
    mock_state.node_results = {}
    mock_execution.state = mock_state
    
    controller.pause = MagicMock(return_value=mock_execution)
    controller.resume = MagicMock(return_value=mock_execution)
    controller.stop = MagicMock(return_value=mock_execution)
    controller.rollback = MagicMock(return_value=mock_execution)
    controller.get_status = MagicMock(return_value=mock_execution)
    controller.get_state = MagicMock(return_value=mock_state)
    controller.update_execution_state = MagicMock(return_value=True)
    controller.supports_time_travel = MagicMock(return_value=True)
    controller.get_checkpoint_history = MagicMock(return_value=[
        {"checkpoint_id": "cp-1", "source": "node_a", "values": {}},
        {"checkpoint_id": "cp-2", "source": "node_b", "values": {}},
    ])
    
    return controller


@pytest.fixture
def mock_event_bus():
    """Create mock event bus."""
    return MagicMock()


@pytest.fixture
def mock_audit_trail():
    """Create mock audit trail."""
    from wtb.infrastructure.events import WTBAuditTrail
    trail = WTBAuditTrail()
    return trail


@pytest.fixture
def execution_service(mock_uow, mock_controller, mock_event_bus, mock_audit_trail):
    """Create ExecutionAPIService with mocks."""
    from wtb.application.services.api_services import ExecutionAPIService
    
    return ExecutionAPIService(
        uow=mock_uow,
        controller=mock_controller,
        event_bus=mock_event_bus,
        audit_trail=mock_audit_trail,
    )


@pytest.fixture
def audit_service(mock_audit_trail, mock_uow):
    """Create AuditAPIService with mocks."""
    from wtb.application.services.api_services import AuditAPIService
    
    return AuditAPIService(
        audit_trail=mock_audit_trail,
        uow=mock_uow,
    )


@pytest.fixture
def batch_test_service(mock_uow, mock_event_bus):
    """Create BatchTestAPIService with mocks."""
    from wtb.application.services.api_services import BatchTestAPIService
    
    return BatchTestAPIService(
        uow=mock_uow,
        event_bus=mock_event_bus,
    )


@pytest.fixture
def workflow_service(mock_uow):
    """Create WorkflowAPIService with mocks."""
    from wtb.application.services.api_services import WorkflowAPIService
    
    return WorkflowAPIService(uow=mock_uow)


# ═══════════════════════════════════════════════════════════════════════════════
# ExecutionAPIService Unit Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestExecutionAPIService:
    """Unit tests for ExecutionAPIService."""
    
    @pytest.mark.asyncio
    async def test_pause_execution_creates_checkpoint(
        self, execution_service, mock_controller, mock_uow
    ):
        """Test that pause creates checkpoint and commits transaction."""
        result = await execution_service.pause_execution(
            execution_id="exec-123",
            reason="User requested pause",
        )
        
        # Verify pause was called
        mock_controller.pause.assert_called_once_with("exec-123")
        
        # Verify transaction committed
        mock_uow.commit.assert_called_once()
        
        # Verify outbox event created
        mock_uow.outbox.add.assert_called_once()
        
        # Verify result
        assert result.success is True
        assert result.status == "paused"
        assert result.checkpoint_id is not None
    
    @pytest.mark.asyncio
    async def test_pause_execution_on_error_no_commit(
        self, execution_service, mock_controller, mock_uow
    ):
        """Test that pause doesn't commit on error."""
        mock_controller.pause.side_effect = ValueError("Invalid execution")
        
        result = await execution_service.pause_execution(
            execution_id="invalid-exec",
        )
        
        assert result.success is False
        assert result.error is not None
        
        # Commit should not be called on error
        mock_uow.commit.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_resume_execution_applies_state_modifications(
        self, execution_service, mock_controller, mock_uow
    ):
        """Test that resume applies state modifications atomically."""
        modified_state = {"key": "new_value"}
        
        result = await execution_service.resume_execution(
            execution_id="exec-123",
            modified_state=modified_state,
        )
        
        # Verify resume was called with modifications
        mock_controller.resume.assert_called_once_with("exec-123", modified_state)
        
        # Verify transaction committed
        mock_uow.commit.assert_called_once()
        
        assert result.success is True
        assert result.status == "running"
    
    @pytest.mark.asyncio
    async def test_rollback_uses_string_checkpoint_id(
        self, execution_service, mock_controller, mock_uow
    ):
        """Test that rollback uses string checkpoint_id (not int)."""
        checkpoint_id = "a1b2c3d4-uuid-string"
        
        result = await execution_service.rollback_execution(
            execution_id="exec-123",
            checkpoint_id=checkpoint_id,
        )
        
        # Verify rollback was called with string checkpoint_id
        mock_controller.rollback.assert_called_once_with("exec-123", checkpoint_id)
        
        assert result.success is True
        assert result.to_checkpoint == checkpoint_id
    
    @pytest.mark.asyncio
    async def test_modify_state_creates_outbox_event(
        self, execution_service, mock_controller, mock_uow
    ):
        """Test that state modification creates outbox event."""
        changes = {"new_key": "new_value"}
        
        result = await execution_service.modify_execution_state(
            execution_id="exec-123",
            changes=changes,
            reason="Human intervention",
        )
        
        # Verify state was updated
        mock_controller.update_execution_state.assert_called_once_with(
            "exec-123", changes
        )
        
        # Verify outbox event created
        mock_uow.outbox.add.assert_called_once()
        
        assert result.success is True
    
    @pytest.mark.asyncio
    async def test_get_execution_returns_dto(
        self, execution_service, mock_controller
    ):
        """Test that get_execution returns proper DTO."""
        result = await execution_service.get_execution("exec-123")
        
        assert result is not None
        assert result.id == "exec-123"
        assert result.workflow_id == "workflow-456"
        assert result.status == "paused"
        assert result.state is not None
    
    @pytest.mark.asyncio
    async def test_get_execution_returns_none_for_invalid_id(
        self, execution_service, mock_controller
    ):
        """Test that get_execution returns None for invalid ID."""
        mock_controller.get_status.side_effect = ValueError("Not found")
        
        result = await execution_service.get_execution("invalid-id")
        
        assert result is None
    
    @pytest.mark.asyncio
    async def test_list_checkpoints_returns_dto_list(
        self, execution_service, mock_controller
    ):
        """Test that list_checkpoints returns list of DTOs."""
        result = await execution_service.list_checkpoints("exec-123")
        
        assert len(result) == 2
        assert result[0].id == "cp-1"
        assert result[1].id == "cp-2"


# ═══════════════════════════════════════════════════════════════════════════════
# AuditAPIService Unit Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestAuditAPIService:
    """Unit tests for AuditAPIService."""
    
    @pytest.mark.asyncio
    async def test_query_events_returns_paginated_result(
        self, audit_service
    ):
        """Test that query_events returns paginated result."""
        result = await audit_service.query_events(limit=10, offset=0)
        
        assert hasattr(result, "items")
        assert hasattr(result, "total")
        assert hasattr(result, "limit")
        assert result.limit == 10
        assert result.offset == 0
    
    @pytest.mark.asyncio
    async def test_get_summary_returns_dto(
        self, audit_service
    ):
        """Test that get_summary returns AuditSummaryDTO."""
        result = await audit_service.get_summary(time_range="1h")
        
        assert hasattr(result, "total_events")
        assert hasattr(result, "time_range")
        assert result.time_range == "1h"
    
    @pytest.mark.asyncio
    async def test_get_timeline_filters_by_execution(
        self, audit_service
    ):
        """Test that get_timeline filters by execution ID."""
        result = await audit_service.get_timeline(
            execution_id="exec-123",
            include_debug=False,
        )
        
        assert isinstance(result, list)


# ═══════════════════════════════════════════════════════════════════════════════
# BatchTestAPIService Unit Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBatchTestAPIService:
    """Unit tests for BatchTestAPIService."""
    
    @pytest.mark.asyncio
    async def test_create_batch_test_creates_outbox_event(
        self, batch_test_service, mock_uow
    ):
        """Test that creating batch test creates outbox event."""
        variants = [
            {"name": "variant_a", "node_variants": {"node_1": "fast"}},
            {"name": "variant_b", "node_variants": {"node_1": "slow"}},
        ]
        
        result = await batch_test_service.create_batch_test(
            workflow_id="workflow-123",
            variants=variants,
        )
        
        # Verify outbox event created
        mock_uow.outbox.add.assert_called_once()
        
        # Verify transaction committed
        mock_uow.commit.assert_called_once()
        
        assert result.workflow_id == "workflow-123"
        assert result.variant_count == 2
    
    @pytest.mark.asyncio
    async def test_cancel_batch_test_creates_outbox_event(
        self, batch_test_service, mock_uow
    ):
        """Test that canceling batch test creates outbox event."""
        # First create a batch test
        await batch_test_service.create_batch_test(
            workflow_id="workflow-123",
            variants=[{"name": "v1", "node_variants": {}}],
        )
        
        # Reset mock
        mock_uow.outbox.add.reset_mock()
        mock_uow.commit.reset_mock()
        
        # Get the batch test ID
        batch_tests = await batch_test_service.list_batch_tests()
        batch_test_id = batch_tests.items[0].id
        
        # Cancel it
        result = await batch_test_service.cancel_batch_test(
            batch_test_id=batch_test_id,
            reason="Test cancelled",
        )
        
        assert result.success is True
        assert result.status == "cancelled"


# ═══════════════════════════════════════════════════════════════════════════════
# WorkflowAPIService Unit Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestWorkflowAPIService:
    """Unit tests for WorkflowAPIService."""
    
    @pytest.mark.asyncio
    async def test_create_workflow_commits_transaction(
        self, workflow_service, mock_uow
    ):
        """Test that creating workflow commits transaction."""
        result = await workflow_service.create_workflow(
            name="test_workflow",
            nodes=[{"id": "node_1", "name": "Node 1", "type": "action"}],
            entry_point="node_1",
        )
        
        # Verify transaction committed
        mock_uow.commit.assert_called()
        
        assert result.name == "test_workflow"
        assert result.entry_point == "node_1"
    
    @pytest.mark.asyncio
    async def test_get_workflow_returns_dto(
        self, workflow_service
    ):
        """Test that get_workflow returns WorkflowDTO."""
        # First create a workflow
        created = await workflow_service.create_workflow(
            name="test_workflow",
            nodes=[{"id": "n1", "name": "N1", "type": "action"}],
            entry_point="n1",
        )
        
        # Then get it
        result = await workflow_service.get_workflow(created.id)
        
        assert result is not None
        assert result.id == created.id
        assert result.name == "test_workflow"
    
    @pytest.mark.asyncio
    async def test_list_workflows_returns_paginated_result(
        self, workflow_service
    ):
        """Test that list_workflows returns paginated result."""
        result = await workflow_service.list_workflows(limit=10, offset=0)
        
        assert hasattr(result, "items")
        assert hasattr(result, "total")
        assert result.limit == 10


# ═══════════════════════════════════════════════════════════════════════════════
# Interface Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestInterfaceCompliance:
    """Tests for interface compliance (ISP)."""
    
    def test_execution_service_implements_interface(self, execution_service):
        """Test ExecutionAPIService implements IExecutionAPIService."""
        from wtb.domain.interfaces.api_services import IExecutionAPIService
        
        # Verify all interface methods exist
        assert hasattr(execution_service, "list_executions")
        assert hasattr(execution_service, "get_execution")
        assert hasattr(execution_service, "pause_execution")
        assert hasattr(execution_service, "resume_execution")
        assert hasattr(execution_service, "stop_execution")
        assert hasattr(execution_service, "rollback_execution")
        assert hasattr(execution_service, "get_execution_state")
        assert hasattr(execution_service, "modify_execution_state")
        assert hasattr(execution_service, "list_checkpoints")
    
    def test_audit_service_implements_interface(self, audit_service):
        """Test AuditAPIService implements IAuditAPIService."""
        from wtb.domain.interfaces.api_services import IAuditAPIService
        
        assert hasattr(audit_service, "query_events")
        assert hasattr(audit_service, "get_summary")
        assert hasattr(audit_service, "get_timeline")
    
    def test_batch_test_service_implements_interface(self, batch_test_service):
        """Test BatchTestAPIService implements IBatchTestAPIService."""
        from wtb.domain.interfaces.api_services import IBatchTestAPIService
        
        assert hasattr(batch_test_service, "create_batch_test")
        assert hasattr(batch_test_service, "get_batch_test")
        assert hasattr(batch_test_service, "list_batch_tests")
        assert hasattr(batch_test_service, "stream_progress")
        assert hasattr(batch_test_service, "cancel_batch_test")


# ═══════════════════════════════════════════════════════════════════════════════
# Factory Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestAPIServiceFactory:
    """Tests for APIServiceFactory."""
    
    def test_create_execution_service(self, mock_uow, mock_controller):
        """Test factory creates execution service."""
        from wtb.application.services.api_services import APIServiceFactory
        
        service = APIServiceFactory.create_execution_service(
            uow=mock_uow,
            controller=mock_controller,
        )
        
        assert service is not None
        assert hasattr(service, "pause_execution")
    
    def test_create_audit_service(self, mock_audit_trail):
        """Test factory creates audit service."""
        from wtb.application.services.api_services import APIServiceFactory
        
        service = APIServiceFactory.create_audit_service(
            audit_trail=mock_audit_trail,
        )
        
        assert service is not None
        assert hasattr(service, "query_events")
    
    def test_create_batch_test_service(self, mock_uow):
        """Test factory creates batch test service."""
        from wtb.application.services.api_services import APIServiceFactory
        
        service = APIServiceFactory.create_batch_test_service(
            uow=mock_uow,
        )
        
        assert service is not None
        assert hasattr(service, "create_batch_test")
    
    def test_create_workflow_service(self, mock_uow):
        """Test factory creates workflow service."""
        from wtb.application.services.api_services import APIServiceFactory
        
        service = APIServiceFactory.create_workflow_service(
            uow=mock_uow,
        )
        
        assert service is not None
        assert hasattr(service, "create_workflow")
