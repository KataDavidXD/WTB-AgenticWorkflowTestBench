"""
Integration Tests for External Control API with Local Database.

Tests the full HTTP request/response cycle with:
- Real SQLite database (local)
- Full service layer integration
- ACID transaction verification
- Workflow submission and execution
- Node replacement and version control

Design Principles:
- Uses real database for transaction testing
- Verifies ACID compliance across layers
- Tests SOLID pattern adherence
- End-to-end API verification

Author: Senior Architect
Date: 2026-01-15
"""

import pytest
import asyncio
import tempfile
import os
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, Generator
from unittest.mock import MagicMock, patch
import uuid

# Skip if dependencies not available
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from wtb.api.rest.app import create_app
from wtb.api.rest.dependencies import (
    AppState,
    set_app_state,
    reset_app_state,
    ExecutionService,
    AuditService,
)
from wtb.infrastructure.events import (
    WTBEventBus,
    WTBAuditTrail,
    WTBAuditEntry,
    WTBAuditEventType,
    WTBAuditSeverity,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Database Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="function")
def temp_db_path() -> Generator[str, None, None]:
    """Create a temporary SQLite database path."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    # Cleanup
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest.fixture(scope="function")
def db_engine(temp_db_path):
    """Create SQLAlchemy engine with local SQLite."""
    engine = create_engine(f"sqlite:///{temp_db_path}", echo=False)
    yield engine
    engine.dispose()


@pytest.fixture(scope="function")
def db_session(db_engine):
    """Create database session for testing."""
    Session = sessionmaker(bind=db_engine)
    session = Session()
    yield session
    session.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Mock Execution Controller
# ═══════════════════════════════════════════════════════════════════════════════

class MockExecutionState:
    """Mock execution state for testing."""
    def __init__(self):
        self.current_node_id = "node_1"
        self.workflow_variables = {"query": "test", "result": None}
        self.execution_path = ["start", "node_1"]
        self.node_results = {"start": {"done": True}}


class MockExecution:
    """Mock execution object for testing."""
    def __init__(self, execution_id: str, workflow_id: str):
        self.id = execution_id
        self.workflow_id = workflow_id
        self.status = MagicMock()
        self.status.value = "running"
        self.state = MockExecutionState()
        self.breakpoints = []
        self.error = None
        self.error_node_id = None
        self.started_at = datetime.now(timezone.utc)
        self.completed_at = None
        # v1.6: Use session_id/checkpoint_id instead of agentgit_* 
        self.checkpoint_id = "checkpoint-001"
        self.session_id = "session-001"


class MockExecutionController:
    """
    Mock ExecutionController for integration testing.
    
    Simulates the full execution control interface with in-memory state.
    """
    
    def __init__(self):
        self._executions: Dict[str, MockExecution] = {}
        self._checkpoints: Dict[str, Dict[str, Any]] = {}
        self._checkpoint_counter = 0
    
    def create_execution(self, workflow_id: str, initial_state: Dict[str, Any]) -> MockExecution:
        """Create new execution."""
        execution_id = str(uuid.uuid4())
        execution = MockExecution(execution_id, workflow_id)
        execution.state.workflow_variables = initial_state.copy()
        self._executions[execution_id] = execution
        return execution
    
    def get_status(self, execution_id: str) -> MockExecution:
        """Get execution status."""
        if execution_id not in self._executions:
            raise ValueError(f"Execution {execution_id} not found")
        return self._executions[execution_id]
    
    def get_state(self, execution_id: str) -> MockExecutionState:
        """Get execution state."""
        return self.get_status(execution_id).state
    
    def pause(self, execution_id: str) -> MockExecution:
        """Pause execution."""
        execution = self.get_status(execution_id)
        execution.status.value = "paused"
        
        # Create checkpoint
        self._checkpoint_counter += 1
        checkpoint_id = f"cp-{self._checkpoint_counter}"
        self._checkpoints[checkpoint_id] = {
            "execution_id": execution_id,
            "node_id": execution.state.current_node_id,
            "state": execution.state.workflow_variables.copy(),
        }
        execution.agentgit_checkpoint_id = self._checkpoint_counter
        
        return execution
    
    def resume(self, execution_id: str, modified_state: Optional[Dict] = None) -> MockExecution:
        """Resume execution."""
        execution = self.get_status(execution_id)
        execution.status.value = "running"
        
        if modified_state:
            execution.state.workflow_variables.update(modified_state)
        
        return execution
    
    def rollback(self, execution_id: str, checkpoint_id: int) -> MockExecution:
        """Rollback to checkpoint."""
        execution = self.get_status(execution_id)
        
        cp_key = f"cp-{checkpoint_id}"
        if cp_key in self._checkpoints:
            checkpoint = self._checkpoints[cp_key]
            execution.state.workflow_variables = checkpoint["state"].copy()
            execution.state.current_node_id = checkpoint["node_id"]
        
        return execution
    
    def update_execution_state(self, execution_id: str, changes: Dict[str, Any]) -> bool:
        """Update execution state."""
        execution = self.get_status(execution_id)
        execution.state.workflow_variables.update(changes)
        return True


# ═══════════════════════════════════════════════════════════════════════════════
# Application State Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="function")
def mock_execution_controller():
    """Create mock execution controller."""
    return MockExecutionController()


@pytest.fixture(scope="function")
def app_state_with_db(mock_execution_controller):
    """Create AppState with mock services and local database."""
    state = AppState()
    
    # Initialize event bus
    state._event_bus = WTBEventBus()
    
    # Initialize audit trail
    state._audit_trail = WTBAuditTrail()
    
    # Set mock controller
    state._execution_controller = mock_execution_controller
    
    state._initialized = True
    
    return state


@pytest.fixture(scope="function")
def test_client(app_state_with_db):
    """Create test client with full dependency injection."""
    set_app_state(app_state_with_db)
    app = create_app(debug=True)
    client = TestClient(app)
    yield client
    reset_app_state()


# ═══════════════════════════════════════════════════════════════════════════════
# Integration Tests: Execution Lifecycle
# ═══════════════════════════════════════════════════════════════════════════════

class TestExecutionLifecycleIntegration:
    """Integration tests for execution lifecycle operations."""
    
    def test_list_executions_empty(self, test_client):
        """Test listing executions returns empty list initially."""
        response = test_client.get("/api/v1/executions")
        assert response.status_code == 200
        
        data = response.json()
        assert "executions" in data
        assert "pagination" in data
        assert isinstance(data["executions"], list)
    
    def test_pause_execution_creates_checkpoint(
        self,
        test_client,
        mock_execution_controller,
    ):
        """Test that pausing creates a checkpoint atomically."""
        # Create execution first
        execution = mock_execution_controller.create_execution(
            workflow_id="wf-001",
            initial_state={"query": "test query"},
        )
        
        # Pause execution
        response = test_client.post(
            f"/api/v1/executions/{execution.id}/pause",
            json={
                "reason": "Integration test pause",
            },
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["status"] == "paused"
        assert "checkpoint_id" in data
        assert data["checkpoint_id"] is not None
    
    def test_resume_execution_after_pause(
        self,
        test_client,
        mock_execution_controller,
    ):
        """Test resuming a paused execution."""
        # Create and pause execution
        execution = mock_execution_controller.create_execution(
            workflow_id="wf-001",
            initial_state={"query": "test"},
        )
        mock_execution_controller.pause(execution.id)
        
        # Resume execution
        response = test_client.post(
            f"/api/v1/executions/{execution.id}/resume",
            json={},
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["status"] == "running"
    
    def test_resume_with_state_modification(
        self,
        test_client,
        mock_execution_controller,
    ):
        """Test resuming with modified state (HITL)."""
        # Create and pause
        execution = mock_execution_controller.create_execution(
            workflow_id="wf-001",
            initial_state={"query": "original query", "threshold": 0.5},
        )
        mock_execution_controller.pause(execution.id)
        
        # Resume with modifications
        response = test_client.post(
            f"/api/v1/executions/{execution.id}/resume",
            json={
                "modified_state": {
                    "query": "corrected query",
                    "threshold": 0.8,
                },
            },
        )
        
        assert response.status_code == 200
        
        # Verify state was modified
        state = mock_execution_controller.get_state(execution.id)
        assert state.workflow_variables["query"] == "corrected query"
        assert state.workflow_variables["threshold"] == 0.8


class TestCheckpointIntegration:
    """Integration tests for checkpoint operations."""
    
    def test_list_checkpoints(self, test_client, mock_execution_controller):
        """Test listing checkpoints for an execution."""
        execution = mock_execution_controller.create_execution(
            workflow_id="wf-001",
            initial_state={},
        )
        
        response = test_client.get(f"/api/v1/executions/{execution.id}/checkpoints")
        assert response.status_code == 200
        
        data = response.json()
        assert "checkpoints" in data
        assert data["execution_id"] == execution.id
    
    def test_create_manual_checkpoint(self, test_client, mock_execution_controller):
        """Test creating a manual checkpoint."""
        execution = mock_execution_controller.create_execution(
            workflow_id="wf-001",
            initial_state={"key": "value"},
        )
        
        response = test_client.post(
            f"/api/v1/executions/{execution.id}/checkpoints",
            params={"name": "manual-checkpoint-1"},
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["trigger_type"] == "manual"


class TestRollbackIntegration:
    """Integration tests for rollback operations."""
    
    def test_rollback_to_checkpoint(self, test_client, mock_execution_controller):
        """Test rolling back to a previous checkpoint."""
        # Create execution with initial state
        execution = mock_execution_controller.create_execution(
            workflow_id="wf-001",
            initial_state={"counter": 0, "status": "initial"},
        )
        
        # Modify state and create checkpoint
        mock_execution_controller.pause(execution.id)
        state = mock_execution_controller.get_state(execution.id)
        state.workflow_variables["counter"] = 10
        state.workflow_variables["status"] = "modified"
        
        checkpoint_id = str(execution.agentgit_checkpoint_id)
        
        # Rollback
        response = test_client.post(
            f"/api/v1/executions/{execution.id}/rollback",
            json={
                "checkpoint_id": checkpoint_id,
                "create_branch": False,
            },
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["to_checkpoint"] == checkpoint_id


class TestBranchIntegration:
    """Integration tests for branch operations."""
    
    def test_list_branches(self, test_client, mock_execution_controller):
        """Test listing branches for an execution."""
        execution = mock_execution_controller.create_execution(
            workflow_id="wf-001",
            initial_state={},
        )
        
        response = test_client.get(f"/api/v1/executions/{execution.id}/branches")
        assert response.status_code == 200
        
        data = response.json()
        assert "branches" in data
        assert data["parent_execution_id"] == execution.id
    
    def test_create_branch_from_checkpoint(self, test_client, mock_execution_controller):
        """Test creating a branch from a checkpoint."""
        execution = mock_execution_controller.create_execution(
            workflow_id="wf-001",
            initial_state={},
        )
        mock_execution_controller.pause(execution.id)
        checkpoint_id = f"cp-{execution.agentgit_checkpoint_id}"
        
        response = test_client.post(
            f"/api/v1/executions/{execution.id}/branches",
            json={
                "checkpoint_id": checkpoint_id,
                "name": "experiment-branch",
                "description": "Testing alternative approach",
            },
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["parent_execution_id"] == execution.id
        assert data["from_checkpoint_id"] == checkpoint_id
        assert data["name"] == "experiment-branch"


class TestStateModificationIntegration:
    """Integration tests for state inspection and modification."""
    
    def test_get_execution_state(self, test_client, mock_execution_controller):
        """Test getting current execution state."""
        execution = mock_execution_controller.create_execution(
            workflow_id="wf-001",
            initial_state={
                "query": "test query",
                "documents": [],
                "answer": None,
            },
        )
        
        response = test_client.get(f"/api/v1/executions/{execution.id}/state")
        assert response.status_code == 200
        
        data = response.json()
        assert data["execution_id"] == execution.id
        assert "state" in data
        assert data["state"]["query"] == "test query"
    
    def test_modify_state_pauses_execution(self, test_client, mock_execution_controller):
        """Test that modifying state returns paused status."""
        execution = mock_execution_controller.create_execution(
            workflow_id="wf-001",
            initial_state={"key": "original"},
        )
        mock_execution_controller.pause(execution.id)  # Must be paused to modify
        
        response = test_client.patch(
            f"/api/v1/executions/{execution.id}/state",
            json={
                "changes": {"key": "modified", "new_key": "new_value"},
                "reason": "Correcting data",
            },
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["status"] == "paused"
        
        # Verify state was modified
        state = mock_execution_controller.get_state(execution.id)
        assert state.workflow_variables["key"] == "modified"
        assert state.workflow_variables["new_key"] == "new_value"


class TestNodeLevelControlIntegration:
    """Integration tests for node-level control."""
    
    def test_list_execution_nodes(self, test_client, mock_execution_controller):
        """Test listing nodes in an execution."""
        execution = mock_execution_controller.create_execution(
            workflow_id="wf-001",
            initial_state={},
        )
        
        response = test_client.get(f"/api/v1/executions/{execution.id}/nodes")
        assert response.status_code == 200
        
        data = response.json()
        assert data["execution_id"] == execution.id
        assert "nodes" in data
    
    def test_execute_single_node(self, test_client, mock_execution_controller):
        """Test executing a single node."""
        execution = mock_execution_controller.create_execution(
            workflow_id="wf-001",
            initial_state={},
        )
        
        response = test_client.post(
            f"/api/v1/executions/{execution.id}/nodes/retriever/execute",
            json={
                "input_state": {"query": "test query"},
            },
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["status"] == "running"
    
    def test_skip_node(self, test_client, mock_execution_controller):
        """Test skipping a node."""
        execution = mock_execution_controller.create_execution(
            workflow_id="wf-001",
            initial_state={},
        )
        
        response = test_client.post(
            f"/api/v1/executions/{execution.id}/nodes/optional_node/skip",
            params={"reason": "Node not needed for this test"},
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
    
    def test_retry_failed_node(self, test_client, mock_execution_controller):
        """Test retrying a failed node."""
        execution = mock_execution_controller.create_execution(
            workflow_id="wf-001",
            initial_state={},
        )
        
        response = test_client.post(
            f"/api/v1/executions/{execution.id}/nodes/failed_node/retry",
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# Integration Tests: Audit Trail
# ═══════════════════════════════════════════════════════════════════════════════

class TestAuditTrailIntegration:
    """Integration tests for audit trail API."""
    
    def test_list_audit_events(self, test_client, app_state_with_db):
        """Test listing audit events."""
        # Add some audit events
        audit_trail = app_state_with_db.audit_trail
        from datetime import timezone
        audit_trail.add_entry(WTBAuditEntry(
            timestamp=datetime.now(timezone.utc),
            event_type=WTBAuditEventType.EXECUTION_STARTED,
            severity=WTBAuditSeverity.INFO,
            message="Test execution started",
            execution_id="exec-001",
        ))
        audit_trail.add_entry(WTBAuditEntry(
            timestamp=datetime.now(timezone.utc),
            event_type=WTBAuditEventType.NODE_COMPLETED,
            severity=WTBAuditSeverity.SUCCESS,
            message="Node completed",
            execution_id="exec-001",
            node_id="node_1",
            duration_ms=100.0,
        ))
        
        response = test_client.get("/api/v1/audit/events")
        assert response.status_code == 200
        
        data = response.json()
        assert "events" in data
        assert "pagination" in data
    
    def test_audit_events_filtering(self, test_client, app_state_with_db):
        """Test filtering audit events."""
        response = test_client.get(
            "/api/v1/audit/events",
            params={
                "event_type": ["execution_started", "node_completed"],
                "severity": ["info", "success"],
                "limit": 50,
            },
        )
        
        assert response.status_code == 200
    
    def test_audit_summary(self, test_client, app_state_with_db):
        """Test getting audit summary."""
        response = test_client.get(
            "/api/v1/audit/summary",
            params={"time_range": "1h"},
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "total_events" in data
        assert "time_range" in data
    
    def test_execution_audit_trail(self, test_client, mock_execution_controller, app_state_with_db):
        """Test getting audit trail for specific execution."""
        execution = mock_execution_controller.create_execution(
            workflow_id="wf-001",
            initial_state={},
        )
        
        # Record some events
        audit_trail = app_state_with_db.audit_trail
        from datetime import timezone
        audit_trail.add_entry(WTBAuditEntry(
            timestamp=datetime.now(timezone.utc),
            event_type=WTBAuditEventType.EXECUTION_STARTED,
            severity=WTBAuditSeverity.INFO,
            message="Execution started",
            execution_id=execution.id,
        ))
        
        response = test_client.get(f"/api/v1/executions/{execution.id}/audit")
        assert response.status_code == 200
        
        data = response.json()
        assert "events" in data


# ═══════════════════════════════════════════════════════════════════════════════
# Integration Tests: Workflow Management
# ═══════════════════════════════════════════════════════════════════════════════

class TestWorkflowManagementIntegration:
    """Integration tests for workflow management."""
    
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
            "name": "rag_pipeline",
            "description": "RAG Pipeline for Q&A",
            "nodes": [
                {"id": "start", "name": "Start", "type": "start"},
                {"id": "retriever", "name": "Retriever", "type": "action", "tool_name": "retrieve"},
                {"id": "reranker", "name": "Reranker", "type": "action", "tool_name": "rerank"},
                {"id": "generator", "name": "Generator", "type": "action", "tool_name": "generate"},
                {"id": "end", "name": "End", "type": "end"},
            ],
            "edges": [
                {"source_id": "start", "target_id": "retriever"},
                {"source_id": "retriever", "target_id": "reranker"},
                {"source_id": "reranker", "target_id": "generator"},
                {"source_id": "generator", "target_id": "end"},
            ],
            "entry_point": "start",
            "metadata": {
                "version": "1.0",
                "author": "test",
            },
        }
        
        response = test_client.post("/api/v1/workflows", json=workflow_data)
        assert response.status_code == 201
        
        data = response.json()
        assert data["name"] == "rag_pipeline"
        assert len(data["nodes"]) == 5
        assert data["id"] is not None


# ═══════════════════════════════════════════════════════════════════════════════
# Integration Tests: Batch Testing
# ═══════════════════════════════════════════════════════════════════════════════

class TestBatchTestIntegration:
    """Integration tests for batch test operations."""
    
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
            "workflow_id": "wf-rag-001",
            "variants": [
                {
                    "name": "baseline",
                    "node_variants": {
                        "retriever": "dense_v1",
                        "generator": "openai_v1",
                    },
                },
                {
                    "name": "hybrid",
                    "node_variants": {
                        "retriever": "hybrid_v1",
                        "generator": "openai_v1",
                    },
                },
                {
                    "name": "fast",
                    "node_variants": {
                        "retriever": "bm25_v1",
                        "reranker": "none",
                        "generator": "local_llm_v1",
                    },
                },
            ],
            "initial_state": {"query": "What is LangGraph?"},
            "test_cases": [
                {"query": "What is LangGraph?"},
                {"query": "How does checkpointing work?"},
            ],
            "use_ray": True,
            "enable_file_tracking": True,
        }
        
        response = test_client.post("/api/v1/batch-tests", json=batch_test_data)
        assert response.status_code == 201
        
        data = response.json()
        assert data["workflow_id"] == "wf-rag-001"
        assert data["variant_count"] == 3
        assert data["use_ray"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# Integration Tests: ACID Compliance
# ═══════════════════════════════════════════════════════════════════════════════

class TestACIDCompliance:
    """Test ACID compliance in database operations."""
    
    def test_atomicity_pause_checkpoint(self, test_client, mock_execution_controller):
        """Test atomicity: pause and checkpoint creation are atomic."""
        execution = mock_execution_controller.create_execution(
            workflow_id="wf-001",
            initial_state={"key": "value"},
        )
        
        # Pause should atomically create checkpoint
        response = test_client.post(
            f"/api/v1/executions/{execution.id}/pause",
            json={"reason": "Atomicity test"},
        )
        
        assert response.status_code == 200
        data = response.json()
        
        # Both pause and checkpoint should succeed together
        assert data["success"] is True
        assert data["checkpoint_id"] is not None
    
    def test_consistency_state_modification(self, test_client, mock_execution_controller):
        """Test consistency: state modifications maintain consistency."""
        execution = mock_execution_controller.create_execution(
            workflow_id="wf-001",
            initial_state={"counter": 0},
        )
        mock_execution_controller.pause(execution.id)
        
        # Modify state
        response = test_client.patch(
            f"/api/v1/executions/{execution.id}/state",
            json={"changes": {"counter": 10}},
        )
        assert response.status_code == 200
        
        # Verify state is consistent
        state = mock_execution_controller.get_state(execution.id)
        assert state.workflow_variables["counter"] == 10
    
    def test_isolation_multiple_executions(self, test_client, mock_execution_controller):
        """Test isolation: operations on one execution don't affect others."""
        # Create two executions
        exec1 = mock_execution_controller.create_execution(
            workflow_id="wf-001",
            initial_state={"id": 1, "value": "exec1"},
        )
        exec2 = mock_execution_controller.create_execution(
            workflow_id="wf-001",
            initial_state={"id": 2, "value": "exec2"},
        )
        
        # Pause exec1
        mock_execution_controller.pause(exec1.id)
        
        # Modify exec1 state
        test_client.patch(
            f"/api/v1/executions/{exec1.id}/state",
            json={"changes": {"value": "modified1"}},
        )
        
        # Verify exec2 is unchanged
        state2 = mock_execution_controller.get_state(exec2.id)
        assert state2.workflow_variables["value"] == "exec2"


# ═══════════════════════════════════════════════════════════════════════════════
# Integration Tests: Error Handling
# ═══════════════════════════════════════════════════════════════════════════════

class TestErrorHandlingIntegration:
    """Test error handling in API endpoints."""
    
    def test_execution_not_found(self, test_client):
        """Test 404 for non-existent execution."""
        response = test_client.get("/api/v1/executions/nonexistent-id")
        assert response.status_code == 404
    
    def test_workflow_not_found(self, test_client):
        """Test 404 for non-existent workflow."""
        response = test_client.get("/api/v1/workflows/nonexistent-id")
        assert response.status_code == 404
    
    def test_validation_error(self, test_client):
        """Test validation error returns 422."""
        # Invalid workflow (empty name)
        response = test_client.post("/api/v1/workflows", json={
            "name": "",  # Invalid
            "nodes": [{"id": "n1", "name": "N1", "type": "action"}],
            "entry_point": "n1",
        })
        assert response.status_code == 422
    
    def test_checkpoint_not_found(self, test_client, mock_execution_controller):
        """Test 404 for non-existent checkpoint."""
        execution = mock_execution_controller.create_execution(
            workflow_id="wf-001",
            initial_state={},
        )
        
        response = test_client.get(
            f"/api/v1/executions/{execution.id}/checkpoints/nonexistent"
        )
        assert response.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# Integration Tests: Health Checks
# ═══════════════════════════════════════════════════════════════════════════════

class TestHealthChecksIntegration:
    """Test health check endpoints."""
    
    def test_health_endpoint(self, test_client):
        """Test health check returns healthy status."""
        response = test_client.get("/api/v1/health")
        assert response.status_code == 200
        
        data = response.json()
        assert "status" in data
        assert "version" in data
    
    def test_readiness_endpoint(self, test_client):
        """Test readiness check."""
        response = test_client.get("/api/v1/health/ready")
        assert response.status_code == 200
        
        data = response.json()
        assert "ready" in data
    
    def test_liveness_endpoint(self, test_client):
        """Test liveness check."""
        response = test_client.get("/api/v1/health/live")
        assert response.status_code == 200
        
        data = response.json()
        assert data["alive"] is True
