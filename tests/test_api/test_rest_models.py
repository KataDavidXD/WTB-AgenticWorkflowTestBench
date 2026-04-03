"""
Unit Tests for REST API Models (Pydantic Schemas).

Tests validation, serialization, and model behavior.
"""

import pytest
from datetime import datetime
from pydantic import ValidationError

from wtb.api.rest.models import (
    # Enums
    ExecutionStatusEnum,
    AuditEventTypeEnum,
    AuditSeverityEnum,
    BatchTestStatusEnum,
    # Common models
    ErrorResponse,
    HealthResponse,
    PaginationMeta,
    # Workflow models
    WorkflowNodeSchema,
    WorkflowEdgeSchema,
    WorkflowCreateRequest,
    WorkflowResponse,
    # Execution models
    ExecutionCreateRequest,
    ExecutionResponse,
    ExecutionStateSchema,
    PauseRequest,
    ResumeRequest,
    RollbackRequest,
    StateModifyRequest,
    ControlResponse,
    # Checkpoint models
    CheckpointResponse,
    # Audit models
    AuditEventResponse,
    AuditSummaryResponse,
    # Batch test models
    BatchTestCreateRequest,
    BatchTestResponse,
    VariantCombinationRequest,
)


class TestEnums:
    """Test enum models."""
    
    def test_execution_status_enum_values(self):
        """Test all execution status values exist."""
        assert ExecutionStatusEnum.PENDING == "pending"
        assert ExecutionStatusEnum.RUNNING == "running"
        assert ExecutionStatusEnum.PAUSED == "paused"
        assert ExecutionStatusEnum.COMPLETED == "completed"
        assert ExecutionStatusEnum.FAILED == "failed"
        assert ExecutionStatusEnum.CANCELLED == "cancelled"
    
    def test_audit_event_type_enum_values(self):
        """Test audit event type values."""
        assert AuditEventTypeEnum.EXECUTION_STARTED == "execution_started"
        assert AuditEventTypeEnum.NODE_COMPLETED == "node_completed"
        assert AuditEventTypeEnum.CHECKPOINT_CREATED == "checkpoint_created"
    
    def test_audit_severity_enum_values(self):
        """Test audit severity values."""
        assert AuditSeverityEnum.INFO == "info"
        assert AuditSeverityEnum.ERROR == "error"
        assert AuditSeverityEnum.WARNING == "warning"


class TestCommonModels:
    """Test common/shared models."""
    
    def test_error_response_creation(self):
        """Test ErrorResponse model."""
        error = ErrorResponse(
            error="NOT_FOUND",
            message="Resource not found",
        )
        assert error.error == "NOT_FOUND"
        assert error.message == "Resource not found"
        assert error.details is None
        assert isinstance(error.timestamp, datetime)
    
    def test_error_response_with_details(self):
        """Test ErrorResponse with details."""
        error = ErrorResponse(
            error="VALIDATION_ERROR",
            message="Invalid input",
            details={"field": "name", "reason": "too_short"},
        )
        assert error.details == {"field": "name", "reason": "too_short"}
    
    def test_health_response(self):
        """Test HealthResponse model."""
        health = HealthResponse(
            status="healthy",
            version="1.0.0",
            components={"api": "healthy", "db": "healthy"},
        )
        assert health.status == "healthy"
        assert health.version == "1.0.0"
        assert health.components["api"] == "healthy"
    
    def test_pagination_meta(self):
        """Test PaginationMeta model."""
        pagination = PaginationMeta(
            total=100,
            limit=20,
            offset=40,
            has_more=True,
        )
        assert pagination.total == 100
        assert pagination.limit == 20
        assert pagination.offset == 40
        assert pagination.has_more is True


class TestWorkflowModels:
    """Test workflow-related models."""
    
    def test_workflow_node_schema(self):
        """Test WorkflowNodeSchema model."""
        node = WorkflowNodeSchema(
            id="node1",
            name="Test Node",
            type="action",
            tool_name="test_tool",
            config={"key": "value"},
        )
        assert node.id == "node1"
        assert node.name == "Test Node"
        assert node.type == "action"
        assert node.tool_name == "test_tool"
    
    def test_workflow_edge_schema(self):
        """Test WorkflowEdgeSchema model."""
        edge = WorkflowEdgeSchema(
            source_id="node1",
            target_id="node2",
            condition="result == True",
        )
        assert edge.source_id == "node1"
        assert edge.target_id == "node2"
        assert edge.condition == "result == True"
    
    def test_workflow_create_request_validation(self):
        """Test WorkflowCreateRequest validation."""
        # Valid request
        request = WorkflowCreateRequest(
            name="test_workflow",
            nodes=[
                WorkflowNodeSchema(id="start", name="Start", type="start"),
                WorkflowNodeSchema(id="end", name="End", type="end"),
            ],
            entry_point="start",
        )
        assert request.name == "test_workflow"
        assert len(request.nodes) == 2
        assert request.entry_point == "start"
    
    def test_workflow_create_request_name_validation(self):
        """Test name length validation."""
        # Empty name should fail
        with pytest.raises(ValidationError):
            WorkflowCreateRequest(
                name="",
                nodes=[WorkflowNodeSchema(id="n1", name="N1", type="action")],
                entry_point="n1",
            )
    
    def test_workflow_create_request_nodes_required(self):
        """Test that nodes are required."""
        with pytest.raises(ValidationError):
            WorkflowCreateRequest(
                name="test",
                nodes=[],  # Empty list should fail
                entry_point="n1",
            )


class TestExecutionModels:
    """Test execution-related models."""
    
    def test_execution_create_request(self):
        """Test ExecutionCreateRequest model."""
        request = ExecutionCreateRequest(
            workflow_id="wf-123",
            initial_state={"query": "test"},
            breakpoints=["node1", "node2"],
        )
        assert request.workflow_id == "wf-123"
        assert request.initial_state == {"query": "test"}
        assert request.breakpoints == ["node1", "node2"]
    
    def test_execution_state_schema(self):
        """Test ExecutionStateSchema model."""
        state = ExecutionStateSchema(
            current_node_id="node1",
            workflow_variables={"key": "value"},
            execution_path=["start", "node1"],
            node_results={"start": {"done": True}},
        )
        assert state.current_node_id == "node1"
        assert state.workflow_variables["key"] == "value"
    
    def test_execution_response(self):
        """Test ExecutionResponse model."""
        response = ExecutionResponse(
            id="exec-123",
            workflow_id="wf-456",
            status=ExecutionStatusEnum.RUNNING,
            state=ExecutionStateSchema(),
            started_at=datetime.utcnow(),
        )
        assert response.id == "exec-123"
        assert response.status == ExecutionStatusEnum.RUNNING
    
    def test_pause_request(self):
        """Test PauseRequest model."""
        request = PauseRequest(
            reason="User requested pause",
            at_node="node3",
        )
        assert request.reason == "User requested pause"
        assert request.at_node == "node3"
    
    def test_resume_request(self):
        """Test ResumeRequest model."""
        request = ResumeRequest(
            modified_state={"key": "new_value"},
            from_node="node2",
        )
        assert request.modified_state == {"key": "new_value"}
        assert request.from_node == "node2"
    
    def test_rollback_request(self):
        """Test RollbackRequest model."""
        request = RollbackRequest(
            checkpoint_id="cp-123",
            create_branch=True,
        )
        assert request.checkpoint_id == "cp-123"
        assert request.create_branch is True
    
    def test_state_modify_request(self):
        """Test StateModifyRequest model."""
        request = StateModifyRequest(
            changes={"key1": "value1", "key2": 42},
            reason="Fixing incorrect value",
        )
        assert request.changes["key1"] == "value1"
        assert request.reason == "Fixing incorrect value"


class TestCheckpointModels:
    """Test checkpoint-related models."""
    
    def test_checkpoint_response(self):
        """Test CheckpointResponse model."""
        response = CheckpointResponse(
            id="cp-123",
            execution_id="exec-456",
            node_id="node1",
            trigger_type="node_exit",
            created_at=datetime.utcnow(),
            has_file_commit=True,
            file_commit_id="fc-789",
        )
        assert response.id == "cp-123"
        assert response.trigger_type == "node_exit"
        assert response.has_file_commit is True


class TestAuditModels:
    """Test audit-related models."""
    
    def test_audit_event_response(self):
        """Test AuditEventResponse model."""
        response = AuditEventResponse(
            timestamp=datetime.utcnow(),
            event_type=AuditEventTypeEnum.NODE_COMPLETED,
            severity=AuditSeverityEnum.SUCCESS,
            message="Node completed successfully",
            execution_id="exec-123",
            node_id="node1",
            duration_ms=150.5,
        )
        assert response.event_type == AuditEventTypeEnum.NODE_COMPLETED
        assert response.severity == AuditSeverityEnum.SUCCESS
        assert response.duration_ms == 150.5
    
    def test_audit_summary_response(self):
        """Test AuditSummaryResponse model."""
        response = AuditSummaryResponse(
            total_events=100,
            time_range="1h",
            events_by_type={"node_completed": 50, "checkpoint_created": 30},
            error_rate=5.0,
            checkpoint_count=30,
            rollback_count=2,
        )
        assert response.total_events == 100
        assert response.error_rate == 5.0


class TestBatchTestModels:
    """Test batch test-related models."""
    
    def test_variant_combination_request(self):
        """Test VariantCombinationRequest model."""
        request = VariantCombinationRequest(
            name="test_combo",
            node_variants={"retriever": "bm25", "generator": "gpt4"},
        )
        assert request.name == "test_combo"
        assert request.node_variants["retriever"] == "bm25"
    
    def test_batch_test_create_request(self):
        """Test BatchTestCreateRequest model."""
        request = BatchTestCreateRequest(
            workflow_id="wf-123",
            variants=[
                VariantCombinationRequest(
                    name="default",
                    node_variants={"retriever": "default"},
                ),
                VariantCombinationRequest(
                    name="bm25",
                    node_variants={"retriever": "bm25"},
                ),
            ],
            initial_state={"query": "test"},
            use_ray=True,
        )
        assert request.workflow_id == "wf-123"
        assert len(request.variants) == 2
        assert request.use_ray is True
    
    def test_batch_test_response(self):
        """Test BatchTestResponse model."""
        response = BatchTestResponse(
            id="bt-123",
            workflow_id="wf-456",
            status=BatchTestStatusEnum.COMPLETED,
            variant_count=3,
            variants_completed=3,
            variants_failed=0,
            created_at=datetime.utcnow(),
            best_variant="bm25",
            use_ray=True,
        )
        assert response.status == BatchTestStatusEnum.COMPLETED
        assert response.best_variant == "bm25"


class TestModelSerialization:
    """Test model JSON serialization."""
    
    def test_execution_response_serialization(self):
        """Test ExecutionResponse can be serialized to JSON."""
        response = ExecutionResponse(
            id="exec-123",
            workflow_id="wf-456",
            status=ExecutionStatusEnum.COMPLETED,
            state=ExecutionStateSchema(
                current_node_id=None,
                workflow_variables={"result": "success"},
            ),
            started_at=datetime(2024, 1, 15, 10, 0, 0),
            completed_at=datetime(2024, 1, 15, 10, 5, 0),
            duration_ms=300000.0,
            nodes_executed=5,
        )
        
        # Should not raise
        json_dict = response.model_dump(mode="json")
        assert json_dict["id"] == "exec-123"
        assert json_dict["status"] == "completed"
        assert json_dict["nodes_executed"] == 5
    
    def test_audit_event_serialization(self):
        """Test AuditEventResponse serialization."""
        response = AuditEventResponse(
            timestamp=datetime(2024, 1, 15, 10, 0, 0),
            event_type=AuditEventTypeEnum.EXECUTION_STARTED,
            severity=AuditSeverityEnum.INFO,
            message="Execution started",
            details={"workflow": "test"},
        )
        
        json_dict = response.model_dump(mode="json")
        assert json_dict["event_type"] == "execution_started"
        assert json_dict["severity"] == "info"
