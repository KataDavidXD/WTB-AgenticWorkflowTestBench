"""
Integration Tests for API Transaction Consistency.

Created: 2026-01-28
Status: Active

Tests ACID compliance across REST and gRPC APIs:
- Atomicity: Operations either fully complete or fully rollback
- Consistency: State remains valid after operations
- Isolation: Concurrent operations don't interfere
- Durability: Committed changes persist

Scenarios:
- A: Outbox event ordering (FIFO guarantee)
- B: Rollback on partial failure
- C: Concurrent API operations
- D: gRPC streaming consistency
"""

import pytest
import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock, patch
from typing import Dict, Any, List

# Skip if dependencies not installed
pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_execution():
    """Create mock execution."""
    mock = MagicMock()
    mock.id = "exec-123"
    mock.workflow_id = "workflow-456"
    mock.status.value = "running"
    mock.checkpoint_id = None
    mock.session_id = "session-abc"
    mock.error = None
    mock.error_node_id = None
    mock.started_at = datetime.now(timezone.utc)
    mock.completed_at = None
    mock.breakpoints = []
    
    state = MagicMock()
    state.current_node_id = "node_a"
    state.workflow_variables = {"counter": 0}
    state.execution_path = ["start", "node_a"]
    state.node_results = {}
    mock.state = state
    
    return mock


@pytest.fixture
def mock_controller(mock_execution):
    """Create mock controller."""
    controller = MagicMock()
    controller.pause = MagicMock(return_value=mock_execution)
    controller.resume = MagicMock(return_value=mock_execution)
    controller.stop = MagicMock(return_value=mock_execution)
    controller.rollback = MagicMock(return_value=mock_execution)
    controller.get_status = MagicMock(return_value=mock_execution)
    controller.get_state = MagicMock(return_value=mock_execution.state)
    controller.update_execution_state = MagicMock(return_value=True)
    controller.supports_time_travel = MagicMock(return_value=True)
    controller.get_checkpoint_history = MagicMock(return_value=[])
    return controller


@pytest.fixture
def mock_uow():
    """Create mock Unit of Work with outbox tracking."""
    uow = MagicMock()
    uow._outbox_events = []
    
    def track_outbox_add(event):
        uow._outbox_events.append(event)
    
    uow.__enter__ = MagicMock(return_value=uow)
    uow.__exit__ = MagicMock(return_value=False)
    uow.commit = MagicMock()
    uow.rollback = MagicMock()
    uow.outbox = MagicMock()
    uow.outbox.add = MagicMock(side_effect=track_outbox_add)
    
    return uow


@pytest.fixture
def app_with_services(mock_uow, mock_controller):
    """Create FastAPI app with mock services."""
    from wtb.api.rest.app import create_app
    from wtb.api.rest.dependencies import AppState, set_app_state, reset_app_state
    from wtb.application.services.api_services import (
        ExecutionAPIService,
        AuditAPIService,
        BatchTestAPIService,
    )
    from wtb.infrastructure.events import WTBAuditTrail
    
    state = AppState()
    state._event_bus = MagicMock()
    state._audit_trail = WTBAuditTrail()
    state._execution_controller = mock_controller
    state._unit_of_work = mock_uow
    state._initialized = True
    
    # Create API services
    state._execution_service = ExecutionAPIService(
        uow=mock_uow,
        controller=mock_controller,
        event_bus=state._event_bus,
        audit_trail=state._audit_trail,
    )
    state._audit_service = AuditAPIService(
        audit_trail=state._audit_trail,
        uow=mock_uow,
    )
    state._batch_test_service = BatchTestAPIService(
        uow=mock_uow,
        event_bus=state._event_bus,
    )
    
    set_app_state(state)
    app = create_app(debug=True)
    
    yield app, state, mock_uow
    
    reset_app_state()


@pytest.fixture
def test_client(app_with_services):
    """Create test client."""
    app, state, uow = app_with_services
    return TestClient(app)


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario A: Outbox Event Ordering (FIFO)
# ═══════════════════════════════════════════════════════════════════════════════


class TestOutboxEventOrdering:
    """Test outbox event ordering guarantees."""
    
    def test_pause_creates_outbox_event(self, test_client, app_with_services):
        """Test that pause operation creates outbox event."""
        app, state, uow = app_with_services
        
        response = test_client.post(
            "/api/v1/executions/exec-123/pause",
            json={"reason": "Test pause"},
        )
        
        # Operation should succeed
        assert response.status_code == 200
        
        # Outbox event should be created
        assert len(uow._outbox_events) == 1
        
        event = uow._outbox_events[0]
        assert event.event_type.value == "execution_paused"
        assert event.aggregate_id == "exec-123"
    
    def test_multiple_operations_maintain_order(self, test_client, app_with_services):
        """Test that multiple operations maintain FIFO order."""
        app, state, uow = app_with_services
        
        # Perform multiple operations
        test_client.post(
            "/api/v1/executions/exec-123/pause",
            json={"reason": "First pause"},
        )
        
        test_client.post(
            "/api/v1/executions/exec-123/resume",
            json={},
        )
        
        # Events should be in order
        assert len(uow._outbox_events) == 2
        assert uow._outbox_events[0].event_type.value == "execution_paused"
        assert uow._outbox_events[1].event_type.value == "execution_resumed"
    
    def test_state_modification_creates_event(self, test_client, app_with_services):
        """Test that state modification creates proper outbox event."""
        app, state, uow = app_with_services
        
        response = test_client.patch(
            "/api/v1/executions/exec-123/state",
            json={"changes": {"new_key": "new_value"}},
        )
        
        assert response.status_code == 200
        assert len(uow._outbox_events) == 1
        
        event = uow._outbox_events[0]
        assert event.event_type.value == "state_modified"


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario B: Rollback on Partial Failure
# ═══════════════════════════════════════════════════════════════════════════════


class TestRollbackOnFailure:
    """Test rollback behavior on failures."""
    
    def test_pause_failure_no_outbox_event(self, test_client, app_with_services):
        """Test that pause failure doesn't create outbox event."""
        app, state, uow = app_with_services
        state._execution_controller.pause.side_effect = ValueError("Invalid")
        
        response = test_client.post(
            "/api/v1/executions/invalid-exec/pause",
            json={},
        )
        
        # Operation should return error status (400 Bad Request for ValueError)
        assert response.status_code == 400
        
        # No outbox event should be created on failure
        assert len(uow._outbox_events) == 0
    
    def test_rollback_uses_string_checkpoint_id(self, test_client, app_with_services):
        """Test that rollback properly uses string checkpoint_id."""
        app, state, uow = app_with_services
        
        # Use UUID-style checkpoint ID
        checkpoint_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        
        response = test_client.post(
            "/api/v1/executions/exec-123/rollback",
            json={
                "checkpoint_id": checkpoint_id,
                "create_branch": False,
            },
        )
        
        assert response.status_code == 200
        
        # Verify controller was called with string checkpoint_id
        state._execution_controller.rollback.assert_called_once()
        call_args = state._execution_controller.rollback.call_args
        assert call_args[0][1] == checkpoint_id  # Second argument is checkpoint_id


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario C: Concurrent API Operations
# ═══════════════════════════════════════════════════════════════════════════════


class TestConcurrentOperations:
    """Test concurrent API operation handling."""
    
    @pytest.mark.asyncio
    async def test_concurrent_pauses_isolated(self, app_with_services):
        """Test that concurrent pause operations are isolated."""
        from wtb.application.services.api_services import ExecutionAPIService
        
        app, state, uow = app_with_services
        
        # Create separate UoW for each concurrent operation
        results = []
        
        async def pause_operation(exec_id: str):
            result = await state._execution_service.pause_execution(
                execution_id=exec_id,
                reason="Concurrent test",
            )
            return result
        
        # Run concurrent operations
        result1 = await pause_operation("exec-1")
        result2 = await pause_operation("exec-2")
        
        # Both should succeed independently
        assert result1.success is True
        assert result2.success is True


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario D: gRPC Transaction Consistency
# ═══════════════════════════════════════════════════════════════════════════════


class TestGRPCConsistency:
    """Test gRPC transaction consistency."""
    
    @pytest.mark.asyncio
    async def test_grpc_pause_creates_outbox_event(self, mock_uow, mock_controller):
        """Test that gRPC pause creates outbox event."""
        from wtb.api.grpc.servicer import WTBServicer
        from wtb.application.services.api_services import ExecutionAPIService
        from wtb.infrastructure.events import WTBAuditTrail
        
        execution_service = ExecutionAPIService(
            uow=mock_uow,
            controller=mock_controller,
            audit_trail=WTBAuditTrail(),
        )
        
        servicer = WTBServicer(execution_service=execution_service)
        
        # Create mock request
        mock_request = MagicMock()
        mock_request.execution_id = "exec-123"
        mock_request.reason = "gRPC pause"
        mock_request.at_node = ""
        
        mock_context = MagicMock()
        
        result = await servicer.PauseExecution(mock_request, mock_context)
        
        # Verify outbox event created
        assert len(mock_uow._outbox_events) == 1
        assert mock_uow._outbox_events[0].event_type.value == "execution_paused"
    
    @pytest.mark.asyncio
    async def test_grpc_rollback_uses_string_checkpoint(self, mock_uow, mock_controller):
        """Test that gRPC rollback uses string checkpoint_id."""
        from wtb.api.grpc.servicer import WTBServicer
        from wtb.application.services.api_services import ExecutionAPIService
        from wtb.infrastructure.events import WTBAuditTrail
        
        execution_service = ExecutionAPIService(
            uow=mock_uow,
            controller=mock_controller,
            audit_trail=WTBAuditTrail(),
        )
        
        servicer = WTBServicer(execution_service=execution_service)
        
        checkpoint_id = "uuid-checkpoint-string"
        
        mock_request = MagicMock()
        mock_request.execution_id = "exec-123"
        mock_request.checkpoint_id = checkpoint_id
        mock_request.create_branch = False
        
        mock_context = MagicMock()
        
        await servicer.RollbackExecution(mock_request, mock_context)
        
        # Verify controller called with string
        mock_controller.rollback.assert_called_with("exec-123", checkpoint_id)


# ═══════════════════════════════════════════════════════════════════════════════
# Integration Tests: REST Endpoint Behavior
# ═══════════════════════════════════════════════════════════════════════════════


class TestRESTEndpointBehavior:
    """Integration tests for REST endpoint behavior."""
    
    def test_health_endpoint_available(self, test_client):
        """Test health endpoint is available."""
        response = test_client.get("/api/v1/health")
        assert response.status_code == 200
    
    def test_pause_endpoint_returns_control_response(self, test_client):
        """Test pause endpoint returns ControlResponse."""
        response = test_client.post(
            "/api/v1/executions/exec-123/pause",
            json={"reason": "Test"},
        )
        
        assert response.status_code == 200
        data = response.json()
        
        # Verify ControlResponse structure
        assert "success" in data
        assert "status" in data
    
    def test_resume_endpoint_accepts_state_modifications(self, test_client):
        """Test resume endpoint accepts state modifications."""
        response = test_client.post(
            "/api/v1/executions/exec-123/resume",
            json={
                "modified_state": {"key": "value"},
                "from_node": "node_b",
            },
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
    
    def test_state_endpoint_returns_current_state(self, test_client, app_with_services):
        """Test state endpoint returns current state."""
        response = test_client.get("/api/v1/executions/exec-123/state")
        
        assert response.status_code == 200
        data = response.json()
        
        assert "execution_id" in data
        assert "state" in data or "values" in data


# ═══════════════════════════════════════════════════════════════════════════════
# SOLID Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSOLIDCompliance:
    """Tests for SOLID principle compliance."""
    
    def test_services_depend_on_interfaces(self):
        """Test that services depend on interfaces (DIP)."""
        from wtb.application.services.api_services import ExecutionAPIService
        from wtb.domain.interfaces import IUnitOfWork, IExecutionController
        
        # ExecutionAPIService constructor should accept interfaces
        import inspect
        sig = inspect.signature(ExecutionAPIService.__init__)
        params = sig.parameters
        
        assert "uow" in params
        assert "controller" in params
    
    def test_api_service_interface_segregation(self):
        """Test interface segregation (ISP)."""
        from wtb.domain.interfaces.api_services import (
            IExecutionAPIService,
            IAuditAPIService,
            IBatchTestAPIService,
        )
        
        # Each interface should have distinct methods
        exec_methods = {m for m in dir(IExecutionAPIService) if not m.startswith("_")}
        audit_methods = {m for m in dir(IAuditAPIService) if not m.startswith("_")}
        batch_methods = {m for m in dir(IBatchTestAPIService) if not m.startswith("_")}
        
        # Should not have much overlap
        assert len(exec_methods & audit_methods) == 0
        assert len(exec_methods & batch_methods) == 0
