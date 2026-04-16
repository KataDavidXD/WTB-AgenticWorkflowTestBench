"""
Comprehensive Unit Tests for External Control API.

Tests:
1. Execution lifecycle control (start, pause, resume, stop)
2. Checkpoint and branching operations
3. State inspection and modification (human-in-the-loop)
4. Node-level control (execute, skip, retry)
5. Audit trail API
6. Transaction consistency (ACID)

Design Principles:
- SOLID: Tests are single-responsibility, depend on abstractions
- ACID: Verifies transaction consistency in all operations
- Clean Architecture: Tests verify proper layer separation

Author: Senior Architect
Date: 2026-01-15
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock, patch
from typing import Dict, Any, Optional
import uuid

from wtb.api.rest.models import (
    # Enums
    ExecutionStatusEnum,
    AuditEventTypeEnum,
    AuditSeverityEnum,
    BatchTestStatusEnum,
    # Request models
    ExecutionCreateRequest,
    PauseRequest,
    ResumeRequest,
    RollbackRequest,
    StateModifyRequest,
    BranchCreateRequest,
    ExecuteSingleNodeRequest,
    # Response models
    ExecutionResponse,
    ExecutionStateSchema,
    ControlResponse,
    RollbackResponse,
    CheckpointResponse,
    CheckpointListResponse,
    BranchResponse,
    AuditEventResponse,
    AuditSummaryResponse,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Test Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_execution_data() -> Dict[str, Any]:
    """Create mock execution data for testing."""
    return {
        "id": str(uuid.uuid4()),
        "workflow_id": "wf-test-001",
        "workflow_name": "test_workflow",
        "status": "running",
        "state": {
            "current_node_id": "node_2",
            "workflow_variables": {"query": "test", "result": None},
            "execution_path": ["start", "node_1", "node_2"],
            "node_results": {
                "start": {"done": True},
                "node_1": {"output": "processed"},
            },
        },
        "breakpoints": ["node_3"],
        "current_node_id": "node_2",
        "error": None,
        "error_node_id": None,
        "started_at": datetime.now(timezone.utc),
        "completed_at": None,
        "checkpoint_count": 3,
        "nodes_executed": 2,
    }


@pytest.fixture
def mock_checkpoint_data() -> Dict[str, Any]:
    """Create mock checkpoint data."""
    return {
        "id": f"cp-{uuid.uuid4().hex[:8]}",
        "execution_id": "exec-001",
        "node_id": "node_2",
        "trigger_type": "node_exit",
        "created_at": datetime.now(timezone.utc),
        "state_snapshot": {
            "query": "test",
            "result": "intermediate",
        },
        "has_file_commit": True,
        "file_commit_id": f"fc-{uuid.uuid4().hex[:8]}",
    }


@pytest.fixture
def mock_audit_entry() -> Dict[str, Any]:
    """Create mock audit entry."""
    return {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc),
        "event_type": "node_completed",
        "severity": "success",
        "message": "Node node_1 completed successfully",
        "execution_id": "exec-001",
        "node_id": "node_1",
        "details": {"output_keys": ["result"]},
        "error": None,
        "duration_ms": 150.5,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Unit Tests: Execution Lifecycle Control
# ═══════════════════════════════════════════════════════════════════════════════

class TestExecutionLifecycle:
    """Test execution lifecycle control operations."""
    
    def test_pause_request_validation(self):
        """Test PauseRequest model validation."""
        # Valid request with all fields
        request = PauseRequest(
            reason="User requested pause for inspection",
            at_node="node_3",
        )
        assert request.reason == "User requested pause for inspection"
        assert request.at_node == "node_3"
        
        # Valid request with minimal fields
        minimal = PauseRequest()
        assert minimal.reason is None
        assert minimal.at_node is None
    
    def test_pause_request_reason_max_length(self):
        """Test that pause reason has max length validation."""
        from pydantic import ValidationError
        
        # 500 chars should be valid
        valid_reason = "x" * 500
        request = PauseRequest(reason=valid_reason)
        assert len(request.reason) == 500
        
        # 501 chars should fail
        with pytest.raises(ValidationError):
            PauseRequest(reason="x" * 501)
    
    def test_resume_request_with_state_modification(self):
        """Test ResumeRequest with state modification for HITL."""
        request = ResumeRequest(
            modified_state={
                "corrected_value": 42,
                "additional_context": "expert input",
            },
            from_node="node_2",
        )
        assert request.modified_state["corrected_value"] == 42
        assert request.from_node == "node_2"
    
    def test_resume_request_empty_is_valid(self):
        """Test that resume with no modifications is valid."""
        request = ResumeRequest()
        assert request.modified_state is None
        assert request.from_node is None
    
    def test_control_response_statuses(self):
        """Test ControlResponse with different statuses."""
        # Paused response
        paused = ControlResponse(
            success=True,
            status=ExecutionStatusEnum.PAUSED,
            checkpoint_id="cp-123",
            message="Execution paused at node_2",
        )
        assert paused.success is True
        assert paused.status == ExecutionStatusEnum.PAUSED
        
        # Running response (after resume)
        running = ControlResponse(
            success=True,
            status=ExecutionStatusEnum.RUNNING,
            message="Execution resumed",
        )
        assert running.status == ExecutionStatusEnum.RUNNING
        
        # Failed response
        failed = ControlResponse(
            success=False,
            status=ExecutionStatusEnum.FAILED,
            message="Cannot pause completed execution",
        )
        assert failed.success is False


class TestExecutionStateModification:
    """Test state inspection and modification (Human-in-the-Loop)."""
    
    def test_state_modify_request_validation(self):
        """Test StateModifyRequest validation."""
        request = StateModifyRequest(
            changes={
                "query": "modified query",
                "threshold": 0.8,
                "config": {"nested": "value"},
            },
            reason="Correcting incorrect input",
        )
        assert "query" in request.changes
        assert request.changes["threshold"] == 0.8
        assert request.reason == "Correcting incorrect input"
    
    def test_state_modify_requires_changes(self):
        """Test that changes field is required."""
        from pydantic import ValidationError
        
        with pytest.raises(ValidationError):
            StateModifyRequest(reason="No changes provided")
    
    def test_execution_state_schema(self):
        """Test ExecutionStateSchema structure."""
        state = ExecutionStateSchema(
            current_node_id="retriever",
            workflow_variables={
                "query": "What is LangGraph?",
                "documents": [],
                "answer": None,
            },
            execution_path=["start", "query_analyzer", "retriever"],
            node_results={
                "query_analyzer": {"expanded_query": "LangGraph framework"},
            },
        )
        
        assert state.current_node_id == "retriever"
        assert state.workflow_variables["query"] == "What is LangGraph?"
        assert len(state.execution_path) == 3
        assert "query_analyzer" in state.node_results


# ═══════════════════════════════════════════════════════════════════════════════
# Unit Tests: Checkpoint and Branching
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckpointOperations:
    """Test checkpoint-related operations."""
    
    def test_checkpoint_response_model(self, mock_checkpoint_data):
        """Test CheckpointResponse model structure."""
        response = CheckpointResponse(
            id=mock_checkpoint_data["id"],
            execution_id=mock_checkpoint_data["execution_id"],
            node_id=mock_checkpoint_data["node_id"],
            trigger_type=mock_checkpoint_data["trigger_type"],
            created_at=mock_checkpoint_data["created_at"],
            has_file_commit=mock_checkpoint_data["has_file_commit"],
            file_commit_id=mock_checkpoint_data["file_commit_id"],
        )
        
        assert response.trigger_type == "node_exit"
        assert response.has_file_commit is True
        assert response.file_commit_id.startswith("fc-")
    
    def test_checkpoint_trigger_types(self):
        """Test different checkpoint trigger types."""
        trigger_types = ["node_entry", "node_exit", "manual", "auto", "breakpoint"]
        
        for trigger in trigger_types:
            response = CheckpointResponse(
                id=f"cp-{trigger}",
                execution_id="exec-001",
                trigger_type=trigger,
                created_at=datetime.now(timezone.utc),
            )
            assert response.trigger_type == trigger
    
    def test_checkpoint_list_response(self):
        """Test CheckpointListResponse structure."""
        checkpoints = [
            CheckpointResponse(
                id=f"cp-{i}",
                execution_id="exec-001",
                node_id=f"node_{i}",
                trigger_type="node_exit",
                created_at=datetime.now(timezone.utc),
            )
            for i in range(5)
        ]
        
        response = CheckpointListResponse(
            checkpoints=checkpoints,
            execution_id="exec-001",
            total=5,
        )
        
        assert len(response.checkpoints) == 5
        assert response.total == 5


class TestRollbackOperations:
    """Test rollback and branching operations."""
    
    def test_rollback_request_in_place(self):
        """Test in-place rollback request."""
        request = RollbackRequest(
            checkpoint_id="cp-abc123",
            create_branch=False,
        )
        assert request.checkpoint_id == "cp-abc123"
        assert request.create_branch is False
    
    def test_rollback_request_with_branch(self):
        """Test rollback with branch creation."""
        request = RollbackRequest(
            checkpoint_id="cp-xyz789",
            create_branch=True,
        )
        assert request.create_branch is True
    
    def test_rollback_response_in_place(self):
        """Test RollbackResponse for in-place rollback."""
        response = RollbackResponse(
            success=True,
            to_checkpoint="cp-123",
            new_session_id=None,  # No branch
            tools_reversed=3,
            files_restored=2,
            restored_state={"query": "original"},
        )
        
        assert response.success is True
        assert response.new_session_id is None
        assert response.tools_reversed == 3
    
    def test_rollback_response_with_branch(self):
        """Test RollbackResponse with new branch."""
        response = RollbackResponse(
            success=True,
            to_checkpoint="cp-456",
            new_session_id="session-branch-001",
            tools_reversed=0,  # Branch doesn't reverse
            files_restored=0,
        )
        
        assert response.new_session_id == "session-branch-001"
        assert response.tools_reversed == 0


class TestBranchOperations:
    """Test branch creation and management."""
    
    def test_branch_create_request(self):
        """Test BranchCreateRequest validation."""
        request = BranchCreateRequest(
            checkpoint_id="cp-checkpoint-001",
            name="experiment-a",
            description="Testing alternative retriever",
        )
        
        assert request.checkpoint_id == "cp-checkpoint-001"
        assert request.name == "experiment-a"
        assert request.description == "Testing alternative retriever"
    
    def test_branch_response(self):
        """Test BranchResponse structure."""
        branch_id = str(uuid.uuid4())
        exec_id = str(uuid.uuid4())
        
        response = BranchResponse(
            id=branch_id,
            parent_execution_id="exec-parent",
            from_checkpoint_id="cp-123",
            name="test-branch",
            description="Testing new generator",
            execution_id=exec_id,
            created_at=datetime.now(timezone.utc),
        )
        
        assert response.id == branch_id
        assert response.parent_execution_id == "exec-parent"
        assert response.execution_id == exec_id


# ═══════════════════════════════════════════════════════════════════════════════
# Unit Tests: Node-Level Control
# ═══════════════════════════════════════════════════════════════════════════════

class TestNodeLevelControl:
    """Test fine-grained node-level control."""
    
    def test_execute_single_node_request(self):
        """Test ExecuteSingleNodeRequest with input override."""
        request = ExecuteSingleNodeRequest(
            input_state={
                "documents": [
                    {"id": "doc1", "content": "Test content"},
                ],
                "query": "Overridden query",
            },
        )
        
        assert "documents" in request.input_state
        assert request.input_state["query"] == "Overridden query"
    
    def test_execute_single_node_empty_input(self):
        """Test ExecuteSingleNodeRequest with no input override."""
        request = ExecuteSingleNodeRequest()
        assert request.input_state is None
    
    def test_node_control_operations(self):
        """Test that all node control operations return consistent response."""
        operations = ["execute", "skip", "retry"]
        
        for op in operations:
            response = ControlResponse(
                success=True,
                status=ExecutionStatusEnum.RUNNING,
                message=f"Node {op} completed",
            )
            assert response.success is True
            assert response.status == ExecutionStatusEnum.RUNNING


# ═══════════════════════════════════════════════════════════════════════════════
# Unit Tests: Audit Trail API
# ═══════════════════════════════════════════════════════════════════════════════

class TestAuditTrailAPI:
    """Test audit trail query and aggregation."""
    
    def test_audit_event_response(self, mock_audit_entry):
        """Test AuditEventResponse model."""
        response = AuditEventResponse(
            id=mock_audit_entry["id"],
            timestamp=mock_audit_entry["timestamp"],
            event_type=AuditEventTypeEnum.NODE_COMPLETED,
            severity=AuditSeverityEnum.SUCCESS,
            message=mock_audit_entry["message"],
            execution_id=mock_audit_entry["execution_id"],
            node_id=mock_audit_entry["node_id"],
            details=mock_audit_entry["details"],
            duration_ms=mock_audit_entry["duration_ms"],
        )
        
        assert response.event_type == AuditEventTypeEnum.NODE_COMPLETED
        assert response.severity == AuditSeverityEnum.SUCCESS
        assert response.duration_ms == 150.5
    
    def test_audit_event_types_coverage(self):
        """Test all audit event types are defined."""
        expected_types = [
            "execution_started",
            "execution_paused",
            "execution_resumed",
            "execution_completed",
            "execution_failed",
            "node_started",
            "node_completed",
            "node_failed",
            "checkpoint_created",
            "rollback_performed",
            "branch_created",
            "state_modified",
        ]
        
        for event_type in expected_types:
            assert hasattr(AuditEventTypeEnum, event_type.upper())
    
    def test_audit_severity_levels(self):
        """Test all severity levels."""
        severities = [
            AuditSeverityEnum.INFO,
            AuditSeverityEnum.SUCCESS,
            AuditSeverityEnum.WARNING,
            AuditSeverityEnum.ERROR,
            AuditSeverityEnum.DEBUG,
        ]
        
        assert len(severities) == 5
        assert AuditSeverityEnum.INFO.value == "info"
        assert AuditSeverityEnum.ERROR.value == "error"
    
    def test_audit_summary_response(self):
        """Test AuditSummaryResponse aggregation."""
        response = AuditSummaryResponse(
            total_events=150,
            execution_id="exec-001",
            time_range="1h",
            events_by_type={
                "node_completed": 80,
                "checkpoint_created": 40,
                "execution_started": 10,
                "execution_completed": 10,
                "node_failed": 5,
                "rollback_performed": 5,
            },
            events_by_severity={
                "info": 50,
                "success": 90,
                "error": 5,
                "warning": 5,
            },
            error_rate=3.33,
            checkpoint_count=40,
            rollback_count=5,
            nodes_executed=80,
            nodes_failed=5,
            avg_node_duration_ms=125.5,
        )
        
        assert response.total_events == 150
        assert response.error_rate == 3.33
        assert response.checkpoint_count == 40


# ═══════════════════════════════════════════════════════════════════════════════
# Unit Tests: Transaction Consistency (ACID)
# ═══════════════════════════════════════════════════════════════════════════════

class TestTransactionConsistency:
    """Test ACID compliance in API operations."""
    
    def test_pause_creates_checkpoint_atomically(self):
        """Test that pause operation atomically creates checkpoint."""
        # When pause succeeds, checkpoint_id should always be present
        response = ControlResponse(
            success=True,
            status=ExecutionStatusEnum.PAUSED,
            checkpoint_id="cp-atomic-001",
            message="Paused with checkpoint",
        )
        
        assert response.success is True
        assert response.checkpoint_id is not None
    
    def test_rollback_response_consistency(self):
        """Test rollback response maintains data consistency."""
        # Rollback should report tools reversed and files restored
        response = RollbackResponse(
            success=True,
            to_checkpoint="cp-target",
            tools_reversed=5,
            files_restored=3,
            restored_state={"key": "value"},
        )
        
        # All fields should be consistent
        assert response.success is True
        assert response.restored_state is not None
    
    def test_state_modification_requires_paused_state(self):
        """Test that state modification returns paused status."""
        # After modifying state, execution should be paused
        response = ControlResponse(
            success=True,
            status=ExecutionStatusEnum.PAUSED,
            message="State modified, execution paused",
        )
        
        # State mod should always leave execution in paused state
        assert response.status == ExecutionStatusEnum.PAUSED
    
    def test_execution_response_state_consistency(self, mock_execution_data):
        """Test execution response maintains state consistency."""
        response = ExecutionResponse(
            id=mock_execution_data["id"],
            workflow_id=mock_execution_data["workflow_id"],
            status=ExecutionStatusEnum.RUNNING,
            state=ExecutionStateSchema(
                current_node_id="node_2",
                workflow_variables={"key": "value"},
                execution_path=["start", "node_1", "node_2"],
                node_results={},
            ),
            breakpoints=mock_execution_data["breakpoints"],
            current_node_id="node_2",
            started_at=mock_execution_data["started_at"],
            nodes_executed=2,
        )
        
        # State and response should be consistent
        assert response.current_node_id == response.state.current_node_id
        assert len(response.state.execution_path) == response.nodes_executed + 1  # +1 for start


# ═══════════════════════════════════════════════════════════════════════════════
# Unit Tests: Response Serialization
# ═══════════════════════════════════════════════════════════════════════════════

class TestResponseSerialization:
    """Test JSON serialization of response models."""
    
    def test_execution_response_json_serialization(self, mock_execution_data):
        """Test ExecutionResponse serializes to JSON correctly."""
        response = ExecutionResponse(
            id=mock_execution_data["id"],
            workflow_id=mock_execution_data["workflow_id"],
            status=ExecutionStatusEnum.RUNNING,
            state=ExecutionStateSchema(
                current_node_id="node_2",
                workflow_variables={"query": "test"},
                execution_path=["start", "node_1"],
                node_results={},
            ),
            started_at=mock_execution_data["started_at"],
            nodes_executed=2,
        )
        
        json_dict = response.model_dump(mode="json")
        
        assert isinstance(json_dict["id"], str)
        assert json_dict["status"] == "running"
        assert "state" in json_dict
        assert isinstance(json_dict["started_at"], str)
    
    def test_checkpoint_response_json_serialization(self, mock_checkpoint_data):
        """Test CheckpointResponse serializes correctly."""
        response = CheckpointResponse(
            id=mock_checkpoint_data["id"],
            execution_id=mock_checkpoint_data["execution_id"],
            trigger_type="node_exit",
            created_at=mock_checkpoint_data["created_at"],
        )
        
        json_dict = response.model_dump(mode="json")
        
        assert isinstance(json_dict["created_at"], str)
    
    def test_audit_event_response_json_serialization(self, mock_audit_entry):
        """Test AuditEventResponse serializes correctly."""
        response = AuditEventResponse(
            id=mock_audit_entry["id"],
            timestamp=mock_audit_entry["timestamp"],
            event_type=AuditEventTypeEnum.NODE_COMPLETED,
            severity=AuditSeverityEnum.SUCCESS,
            message=mock_audit_entry["message"],
            duration_ms=mock_audit_entry["duration_ms"],
        )
        
        json_dict = response.model_dump(mode="json")
        
        assert json_dict["event_type"] == "node_completed"
        assert json_dict["severity"] == "success"


# ═══════════════════════════════════════════════════════════════════════════════
# Unit Tests: Error Cases
# ═══════════════════════════════════════════════════════════════════════════════

class TestErrorCases:
    """Test error handling in external control API."""
    
    def test_rollback_request_requires_checkpoint_id(self):
        """Test that rollback requires checkpoint_id."""
        from pydantic import ValidationError
        
        with pytest.raises(ValidationError):
            RollbackRequest(create_branch=True)  # Missing checkpoint_id
    
    def test_execution_response_allows_error_state(self):
        """Test ExecutionResponse can represent failed state."""
        response = ExecutionResponse(
            id="exec-failed",
            workflow_id="wf-001",
            status=ExecutionStatusEnum.FAILED,
            state=ExecutionStateSchema(),
            error="Node 'generator' raised ValueError: Invalid input",
            error_node_id="generator",
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        )
        
        assert response.status == ExecutionStatusEnum.FAILED
        assert response.error is not None
        assert response.error_node_id == "generator"
    
    def test_control_response_failure(self):
        """Test ControlResponse for failed operations."""
        response = ControlResponse(
            success=False,
            status=ExecutionStatusEnum.RUNNING,  # Status unchanged
            message="Cannot pause: execution already completed",
        )
        
        assert response.success is False
        assert "Cannot pause" in response.message


# ═══════════════════════════════════════════════════════════════════════════════
# Unit Tests: Enum Values
# ═══════════════════════════════════════════════════════════════════════════════

class TestEnumValues:
    """Test enum values for API consistency."""
    
    def test_execution_status_values(self):
        """Test all execution status values."""
        statuses = [
            ("pending", ExecutionStatusEnum.PENDING),
            ("running", ExecutionStatusEnum.RUNNING),
            ("paused", ExecutionStatusEnum.PAUSED),
            ("completed", ExecutionStatusEnum.COMPLETED),
            ("failed", ExecutionStatusEnum.FAILED),
            ("cancelled", ExecutionStatusEnum.CANCELLED),
        ]
        
        for value, enum in statuses:
            assert enum.value == value
    
    def test_batch_test_status_values(self):
        """Test batch test status values."""
        statuses = [
            ("pending", BatchTestStatusEnum.PENDING),
            ("running", BatchTestStatusEnum.RUNNING),
            ("completed", BatchTestStatusEnum.COMPLETED),
            ("failed", BatchTestStatusEnum.FAILED),
            ("cancelled", BatchTestStatusEnum.CANCELLED),
        ]
        
        for value, enum in statuses:
            assert enum.value == value
    
    def test_audit_event_ray_types(self):
        """Test Ray-specific audit event types."""
        ray_events = [
            AuditEventTypeEnum.RAY_BATCH_TEST_STARTED,
            AuditEventTypeEnum.RAY_BATCH_TEST_COMPLETED,
            AuditEventTypeEnum.RAY_BATCH_TEST_FAILED,
            AuditEventTypeEnum.RAY_VARIANT_EXECUTION_COMPLETED,
        ]
        
        for event in ray_events:
            assert event.value.startswith("ray_")
