"""
Integration Tests for REST API Endpoints.

Tests the full HTTP request/response cycle using TestClient.
"""

import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch

# Skip if FastAPI not installed
pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from wtb.api.rest.app import create_app
from wtb.api.rest.dependencies import AppState, set_app_state


@pytest.fixture
def mock_app_state():
    """Create a mock application state."""
    state = AppState()
    
    # Mock event bus
    mock_event_bus = MagicMock()
    state._event_bus = mock_event_bus
    
    # Mock audit trail
    from wtb.infrastructure.events import WTBAuditTrail
    state._audit_trail = WTBAuditTrail()
    
    state._initialized = True
    
    return state


@pytest.fixture
def test_client(mock_app_state):
    """Create a test client with mock dependencies."""
    set_app_state(mock_app_state)
    app = create_app(debug=True)
    client = TestClient(app)
    yield client
    # Cleanup
    from wtb.api.rest.dependencies import reset_app_state
    reset_app_state()


class TestHealthEndpoints:
    """Test health check endpoints."""
    
    def test_health_check(self, test_client):
        """Test /health endpoint."""
        response = test_client.get("/api/v1/health")
        assert response.status_code == 200
        
        data = response.json()
        assert "status" in data
        assert "version" in data
        assert "components" in data
    
    def test_readiness_check(self, test_client):
        """Test /health/ready endpoint."""
        response = test_client.get("/api/v1/health/ready")
        assert response.status_code == 200
        
        data = response.json()
        assert "ready" in data
        assert "timestamp" in data
    
    def test_liveness_check(self, test_client):
        """Test /health/live endpoint."""
        response = test_client.get("/api/v1/health/live")
        assert response.status_code == 200
        
        data = response.json()
        assert data["alive"] is True


class TestRootEndpoint:
    """Test root endpoint."""
    
    def test_root(self, test_client):
        """Test root endpoint returns API info."""
        response = test_client.get("/")
        assert response.status_code == 200
        
        data = response.json()
        assert "name" in data
        assert "version" in data
        assert "docs" in data


class TestWorkflowEndpoints:
    """Test workflow management endpoints."""
    
    def test_list_workflows(self, test_client):
        """Test listing workflows."""
        response = test_client.get("/api/v1/workflows")
        assert response.status_code == 200
        
        data = response.json()
        assert "workflows" in data
        assert "pagination" in data
    
    def test_create_workflow(self, test_client):
        """Test creating a workflow."""
        workflow_data = {
            "name": "test_workflow",
            "description": "Test workflow description",
            "nodes": [
                {"id": "start", "name": "Start", "type": "start"},
                {"id": "action1", "name": "Action 1", "type": "action"},
                {"id": "end", "name": "End", "type": "end"},
            ],
            "edges": [
                {"source_id": "start", "target_id": "action1"},
                {"source_id": "action1", "target_id": "end"},
            ],
            "entry_point": "start",
        }
        
        response = test_client.post("/api/v1/workflows", json=workflow_data)
        assert response.status_code == 201
        
        data = response.json()
        assert data["name"] == "test_workflow"
        assert data["id"] is not None
        assert len(data["nodes"]) == 3
    
    def test_get_workflow_not_found(self, test_client):
        """Test getting non-existent workflow."""
        response = test_client.get("/api/v1/workflows/nonexistent-id")
        assert response.status_code == 404


class TestBatchTestEndpoints:
    """Test batch test endpoints."""
    
    def test_list_batch_tests(self, test_client):
        """Test listing batch tests."""
        response = test_client.get("/api/v1/batch-tests")
        assert response.status_code == 200
        
        data = response.json()
        assert "batch_tests" in data
        assert "pagination" in data
    
    def test_create_batch_test(self, test_client):
        """Test creating a batch test."""
        batch_test_data = {
            "workflow_id": "wf-123",
            "variants": [
                {"name": "default", "node_variants": {"retriever": "default"}},
                {"name": "bm25", "node_variants": {"retriever": "bm25"}},
            ],
            "initial_state": {"query": "test"},
            "use_ray": True,
        }
        
        response = test_client.post("/api/v1/batch-tests", json=batch_test_data)
        assert response.status_code == 201
        
        data = response.json()
        assert data["workflow_id"] == "wf-123"
        assert data["variant_count"] == 2
        assert data["use_ray"] is True


class TestAuditEndpoints:
    """Test audit trail endpoints."""
    
    def test_list_audit_events(self, test_client):
        """Test listing audit events."""
        response = test_client.get("/api/v1/audit/events")
        assert response.status_code == 200
        
        data = response.json()
        assert "events" in data
        assert "pagination" in data
    
    def test_list_audit_events_with_filters(self, test_client):
        """Test listing audit events with filters."""
        response = test_client.get(
            "/api/v1/audit/events",
            params={
                "event_type": ["execution_started", "node_completed"],
                "severity": ["info", "success"],
                "limit": 50,
            },
        )
        assert response.status_code == 200
    
    def test_audit_summary(self, test_client):
        """Test getting audit summary."""
        response = test_client.get(
            "/api/v1/audit/summary",
            params={"time_range": "1h"},
        )
        assert response.status_code == 200
        
        data = response.json()
        assert "total_events" in data
        assert "time_range" in data


class TestErrorHandling:
    """Test error handling."""
    
    def test_validation_error(self, test_client):
        """Test validation error response."""
        # Invalid workflow (empty name)
        response = test_client.post("/api/v1/workflows", json={
            "name": "",  # Invalid: empty name
            "nodes": [{"id": "n1", "name": "N1", "type": "action"}],
            "entry_point": "n1",
        })
        assert response.status_code == 422  # Validation error
    
    def test_not_found_error(self, test_client):
        """Test 404 error response."""
        response = test_client.get("/api/v1/workflows/nonexistent")
        assert response.status_code == 404


class TestCORSHeaders:
    """Test CORS headers."""
    
    def test_cors_preflight(self, test_client):
        """Test CORS preflight request."""
        response = test_client.options(
            "/api/v1/workflows",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
            },
        )
        # CORS should be enabled
        assert response.status_code in [200, 204]
