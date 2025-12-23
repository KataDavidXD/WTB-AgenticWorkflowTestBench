"""
Unit tests for WTB Domain Models.

Tests the core domain entities, value objects, and aggregates.
"""

import pytest
from datetime import datetime

from wtb.domain.models import (
    WorkflowNode,
    WorkflowEdge,
    TestWorkflow,
    ExecutionState,
    Execution,
    ExecutionStatus,
    NodeVariant,
)
from wtb.domain.models.node_boundary import NodeBoundary
from wtb.domain.models.batch_test import BatchTest, BatchTestStatus, VariantCombination, BatchTestResult
from wtb.domain.models.evaluation import EvaluationResult, MetricValue


# ═══════════════════════════════════════════════════════════════════════════════
# WorkflowNode Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestWorkflowNode:
    """Tests for WorkflowNode value object."""
    
    def test_create_basic_node(self):
        """Test creating a basic workflow node."""
        node = WorkflowNode(id="node1", name="Test Node", type="action")
        
        assert node.id == "node1"
        assert node.name == "Test Node"
        assert node.type == "action"
        assert node.tool_name is None
        assert node.config == {}
    
    def test_create_node_with_tool(self):
        """Test creating a node with tool reference."""
        node = WorkflowNode(
            id="node1",
            name="Tool Node",
            type="action",
            tool_name="my_tool",
            config={"param1": "value1"}
        )
        
        assert node.tool_name == "my_tool"
        assert node.config == {"param1": "value1"}
    
    def test_node_is_frozen(self):
        """Test that WorkflowNode is immutable (frozen dataclass)."""
        node = WorkflowNode(id="node1", name="Test", type="action")
        
        with pytest.raises(Exception):  # FrozenInstanceError
            node.name = "Changed"
    
    def test_node_with_config(self):
        """Test with_config creates new node with merged config."""
        node = WorkflowNode(
            id="node1",
            name="Test",
            type="action",
            config={"a": 1}
        )
        
        new_node = node.with_config(b=2, c=3)
        
        assert new_node.config == {"a": 1, "b": 2, "c": 3}
        assert node.config == {"a": 1}  # Original unchanged
    
    def test_node_hashable(self):
        """Test that nodes are hashable (can be used in sets/dicts)."""
        node1 = WorkflowNode(id="node1", name="Test", type="action")
        node2 = WorkflowNode(id="node1", name="Test", type="action")
        
        assert hash(node1) == hash(node2)
        assert {node1, node2}  # Can be added to set


# ═══════════════════════════════════════════════════════════════════════════════
# WorkflowEdge Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestWorkflowEdge:
    """Tests for WorkflowEdge value object."""
    
    def test_create_basic_edge(self):
        """Test creating a basic edge."""
        edge = WorkflowEdge(source_id="node1", target_id="node2")
        
        assert edge.source_id == "node1"
        assert edge.target_id == "node2"
        assert edge.condition is None
        assert edge.priority == 0
    
    def test_create_conditional_edge(self):
        """Test creating an edge with condition."""
        edge = WorkflowEdge(
            source_id="node1",
            target_id="node2",
            condition="x > 5",
            priority=10
        )
        
        assert edge.condition == "x > 5"
        assert edge.priority == 10
    
    def test_edge_is_frozen(self):
        """Test that WorkflowEdge is immutable."""
        edge = WorkflowEdge(source_id="node1", target_id="node2")
        
        with pytest.raises(Exception):
            edge.source_id = "changed"


# ═══════════════════════════════════════════════════════════════════════════════
# TestWorkflow Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestTestWorkflow:
    """Tests for TestWorkflow aggregate root."""
    
    def test_create_empty_workflow(self):
        """Test creating an empty workflow."""
        workflow = TestWorkflow(name="Test Workflow")
        
        assert workflow.name == "Test Workflow"
        assert workflow.nodes == {}
        assert workflow.edges == []
        assert workflow.entry_point is None
    
    def test_add_nodes(self):
        """Test adding nodes to workflow."""
        workflow = TestWorkflow(name="Test")
        start = WorkflowNode(id="start", name="Start", type="start")
        action = WorkflowNode(id="action", name="Action", type="action")
        
        workflow.add_node(start)
        workflow.add_node(action)
        
        assert len(workflow.nodes) == 2
        assert "start" in workflow.nodes
        assert "action" in workflow.nodes
        assert workflow.entry_point == "start"  # Auto-set for start type
    
    def test_add_edges(self):
        """Test adding edges to workflow."""
        workflow = TestWorkflow(name="Test")
        edge = WorkflowEdge(source_id="start", target_id="end")
        
        workflow.add_edge(edge)
        
        assert len(workflow.edges) == 1
        assert workflow.edges[0] == edge
    
    def test_get_outgoing_edges(self):
        """Test getting outgoing edges from a node."""
        workflow = TestWorkflow(name="Test")
        edge1 = WorkflowEdge(source_id="node1", target_id="node2", priority=1)
        edge2 = WorkflowEdge(source_id="node1", target_id="node3", priority=2)
        edge3 = WorkflowEdge(source_id="node2", target_id="node3")
        
        workflow.add_edge(edge1)
        workflow.add_edge(edge2)
        workflow.add_edge(edge3)
        
        outgoing = workflow.get_outgoing_edges("node1")
        
        assert len(outgoing) == 2
        assert outgoing[0].priority == 2  # Higher priority first
        assert outgoing[1].priority == 1
    
    def test_validate_empty_workflow(self):
        """Test validation of empty workflow."""
        workflow = TestWorkflow(name="Test")
        
        errors = workflow.validate()
        
        assert "No entry point defined" in errors
    
    def test_validate_invalid_entry_point(self):
        """Test validation with invalid entry point."""
        workflow = TestWorkflow(name="Test", entry_point="nonexistent")
        
        errors = workflow.validate()
        
        assert any("Entry point" in e or "not found" in e for e in errors)
    
    def test_validate_valid_workflow(self):
        """Test validation of valid workflow."""
        workflow = TestWorkflow(name="Test")
        start = WorkflowNode(id="start", name="Start", type="start")
        end = WorkflowNode(id="end", name="End", type="end")
        
        workflow.add_node(start)
        workflow.add_node(end)
        workflow.add_edge(WorkflowEdge(source_id="start", target_id="end"))
        
        errors = workflow.validate()
        
        assert errors == []
    
    def test_to_dict_and_from_dict(self):
        """Test serialization and deserialization."""
        workflow = TestWorkflow(name="Test", description="A test workflow")
        start = WorkflowNode(id="start", name="Start", type="start")
        end = WorkflowNode(id="end", name="End", type="end")
        
        workflow.add_node(start)
        workflow.add_node(end)
        workflow.add_edge(WorkflowEdge(source_id="start", target_id="end"))
        
        data = workflow.to_dict()
        restored = TestWorkflow.from_dict(data)
        
        assert restored.name == workflow.name
        assert restored.description == workflow.description
        assert len(restored.nodes) == 2
        assert len(restored.edges) == 1
        assert restored.entry_point == "start"


# ═══════════════════════════════════════════════════════════════════════════════
# ExecutionState Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestExecutionState:
    """Tests for ExecutionState value object."""
    
    def test_create_empty_state(self):
        """Test creating empty execution state."""
        state = ExecutionState()
        
        assert state.current_node_id is None
        assert state.workflow_variables == {}
        assert state.execution_path == []
        assert state.node_results == {}
    
    def test_clone_state(self):
        """Test cloning execution state."""
        state = ExecutionState(
            current_node_id="node1",
            workflow_variables={"x": 1},
            execution_path=["start", "node1"],
            node_results={"start": {"ok": True}}
        )
        
        cloned = state.clone()
        
        assert cloned.current_node_id == "node1"
        assert cloned.workflow_variables == {"x": 1}
        
        # Verify it's a deep copy
        cloned.workflow_variables["x"] = 2
        assert state.workflow_variables["x"] == 1
    
    def test_to_dict_and_from_dict(self):
        """Test serialization."""
        state = ExecutionState(
            current_node_id="node1",
            workflow_variables={"key": "value"},
            execution_path=["start"],
            node_results={"start": {}}
        )
        
        data = state.to_dict()
        restored = ExecutionState.from_dict(data)
        
        assert restored.current_node_id == state.current_node_id
        assert restored.workflow_variables == state.workflow_variables


# ═══════════════════════════════════════════════════════════════════════════════
# Execution Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestExecution:
    """Tests for Execution aggregate root."""
    
    def test_create_execution(self):
        """Test creating an execution."""
        execution = Execution(workflow_id="wf1")
        
        assert execution.workflow_id == "wf1"
        assert execution.status == ExecutionStatus.PENDING
        assert execution.id is not None
    
    def test_execution_lifecycle_pending_to_running(self):
        """Test transition from PENDING to RUNNING."""
        execution = Execution(workflow_id="wf1")
        
        assert execution.can_run()
        
        execution.start()
        
        assert execution.status == ExecutionStatus.RUNNING
        assert execution.started_at is not None
    
    def test_execution_lifecycle_running_to_paused(self):
        """Test transition from RUNNING to PAUSED."""
        execution = Execution(workflow_id="wf1")
        execution.start()
        
        assert execution.can_pause()
        
        execution.pause()
        
        assert execution.status == ExecutionStatus.PAUSED
    
    def test_execution_lifecycle_paused_to_running(self):
        """Test transition from PAUSED to RUNNING."""
        execution = Execution(workflow_id="wf1")
        execution.start()
        execution.pause()
        
        assert execution.can_resume()
        
        execution.resume()
        
        assert execution.status == ExecutionStatus.RUNNING
    
    def test_execution_complete(self):
        """Test completing an execution."""
        execution = Execution(workflow_id="wf1")
        execution.start()
        
        execution.complete()
        
        assert execution.status == ExecutionStatus.COMPLETED
        assert execution.completed_at is not None
        assert execution.is_terminal()
    
    def test_execution_fail(self):
        """Test failing an execution."""
        execution = Execution(workflow_id="wf1")
        execution.start()
        
        execution.fail("Something went wrong", "node1")
        
        assert execution.status == ExecutionStatus.FAILED
        assert execution.error_message == "Something went wrong"
        assert execution.error_node_id == "node1"
        assert execution.is_terminal()
    
    def test_execution_cancel(self):
        """Test cancelling an execution."""
        execution = Execution(workflow_id="wf1")
        
        execution.cancel()
        
        assert execution.status == ExecutionStatus.CANCELLED
        assert execution.is_terminal()
    
    def test_cannot_start_from_running(self):
        """Test that we cannot start from RUNNING state."""
        from wtb.domain.models import InvalidStateTransition
        
        execution = Execution(workflow_id="wf1")
        execution.start()
        
        assert not execution.can_run()
        
        with pytest.raises(InvalidStateTransition):
            execution.start()
    
    def test_can_rollback(self):
        """Test rollback eligibility."""
        execution = Execution(workflow_id="wf1")
        
        # Cannot rollback from PENDING
        assert not execution.can_rollback()
        
        execution.start()
        
        # Cannot rollback from RUNNING
        assert not execution.can_rollback()
        
        execution.pause()
        
        # Can rollback from PAUSED
        assert execution.can_rollback()
    
    def test_get_duration(self):
        """Test duration calculation."""
        execution = Execution(workflow_id="wf1")
        execution.start()
        
        import time
        time.sleep(0.05)  # Short sleep for test speed
        
        duration = execution.get_duration_seconds()
        
        assert duration is not None
        assert duration >= 0.05


# ═══════════════════════════════════════════════════════════════════════════════
# NodeBoundary Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestNodeBoundary:
    """Tests for NodeBoundary entity."""
    
    def test_create_boundary(self):
        """Test creating a node boundary."""
        boundary = NodeBoundary(
            execution_id="exec1",
            internal_session_id=1,
            node_id="node1",
            entry_checkpoint_id=5
        )
        
        assert boundary.node_id == "node1"
        assert boundary.entry_checkpoint_id == 5
        assert boundary.node_status == "started"
        assert boundary.exit_checkpoint_id is None
    
    def test_complete_boundary(self):
        """Test completing a node boundary."""
        boundary = NodeBoundary(
            execution_id="exec1",
            internal_session_id=1,
            node_id="node1",
            entry_checkpoint_id=5
        )
        
        boundary.complete(exit_checkpoint_id=10, tool_count=3, checkpoint_count=2)
        
        assert boundary.node_status == "completed"
        assert boundary.exit_checkpoint_id == 10
        assert boundary.tool_count == 3
        assert boundary.checkpoint_count == 2
        assert boundary.completed_at is not None
        assert boundary.duration_ms is not None
    
    def test_fail_boundary(self):
        """Test failing a node boundary."""
        boundary = NodeBoundary(
            execution_id="exec1",
            internal_session_id=1,
            node_id="node1",
            entry_checkpoint_id=5
        )
        
        boundary.fail("Error occurred", {"detail": "more info"})
        
        assert boundary.node_status == "failed"
        assert boundary.error_message == "Error occurred"
        assert boundary.error_details == {"detail": "more info"}
    
    def test_is_rollback_target(self):
        """Test rollback target eligibility."""
        boundary = NodeBoundary(
            execution_id="exec1",
            internal_session_id=1,
            node_id="node1",
            entry_checkpoint_id=5
        )
        
        assert not boundary.is_rollback_target()
        
        boundary.complete(exit_checkpoint_id=10)
        
        assert boundary.is_rollback_target()
    
    def test_to_dict_and_from_dict(self):
        """Test serialization."""
        boundary = NodeBoundary(
            execution_id="exec1",
            internal_session_id=1,
            node_id="node1",
            entry_checkpoint_id=5
        )
        boundary.complete(exit_checkpoint_id=10)
        
        data = boundary.to_dict()
        restored = NodeBoundary.from_dict(data)
        
        assert restored.node_id == boundary.node_id
        assert restored.exit_checkpoint_id == 10


# ═══════════════════════════════════════════════════════════════════════════════
# BatchTest Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestBatchTest:
    """Tests for BatchTest aggregate root."""
    
    def test_create_batch_test(self):
        """Test creating a batch test."""
        batch = BatchTest(
            name="Test Batch",
            workflow_id="wf1",
            variant_combinations=[
                VariantCombination(name="Config A", variants={"node1": "v1"}),
                VariantCombination(name="Config B", variants={"node1": "v2"}),
            ]
        )
        
        assert batch.name == "Test Batch"
        assert len(batch.variant_combinations) == 2
        assert batch.status == BatchTestStatus.PENDING
    
    def test_batch_test_lifecycle(self):
        """Test batch test lifecycle."""
        batch = BatchTest(name="Test", workflow_id="wf1")
        
        batch.start()
        assert batch.status == BatchTestStatus.RUNNING
        assert batch.started_at is not None
        
        batch.complete()
        assert batch.status == BatchTestStatus.COMPLETED
        assert batch.completed_at is not None
    
    def test_add_results(self):
        """Test adding results to batch test."""
        batch = BatchTest(name="Test", workflow_id="wf1")
        batch.start()
        
        result = BatchTestResult(
            combination_name="Config A",
            execution_id="exec1",
            success=True,
            overall_score=0.95
        )
        
        batch.add_result(result)
        
        assert len(batch.results) == 1
        assert "exec1" in batch.execution_ids
    
    def test_determine_best(self):
        """Test determining best variant."""
        batch = BatchTest(
            name="Test",
            workflow_id="wf1",
            variant_combinations=[
                VariantCombination(name="A"),
                VariantCombination(name="B"),
            ]
        )
        batch.start()
        
        batch.add_result(BatchTestResult(
            combination_name="A",
            execution_id="exec1",
            success=True,
            overall_score=0.80
        ))
        batch.add_result(BatchTestResult(
            combination_name="B",
            execution_id="exec2",
            success=True,
            overall_score=0.95
        ))
        
        batch.complete()
        
        assert batch.best_combination_name == "B"
    
    def test_success_rate(self):
        """Test success rate calculation."""
        batch = BatchTest(name="Test", workflow_id="wf1")
        
        batch.add_result(BatchTestResult(
            combination_name="A", execution_id="1", success=True, overall_score=0.9
        ))
        batch.add_result(BatchTestResult(
            combination_name="B", execution_id="2", success=False, overall_score=0.0
        ))
        batch.add_result(BatchTestResult(
            combination_name="C", execution_id="3", success=True, overall_score=0.8
        ))
        
        assert batch.get_success_rate() == pytest.approx(2/3)


# ═══════════════════════════════════════════════════════════════════════════════
# EvaluationResult Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestEvaluationResult:
    """Tests for EvaluationResult entity."""
    
    def test_create_result(self):
        """Test creating an evaluation result."""
        result = EvaluationResult(
            execution_id="exec1",
            evaluator_name="accuracy",
            overall_score=0.95
        )
        
        assert result.execution_id == "exec1"
        assert result.evaluator_name == "accuracy"
        assert result.overall_score == 0.95
        assert result.passed
    
    def test_add_metrics(self):
        """Test adding metrics to result."""
        result = EvaluationResult(
            execution_id="exec1",
            evaluator_name="combined"
        )
        
        result.add_metric("accuracy", 0.95, unit="%")
        result.add_metric("latency", 150, unit="ms")
        
        assert len(result.metrics) == 2
        assert result.get_metric("accuracy").value == 0.95
        assert result.get_metric_value("latency") == 150
        assert result.get_metric_value("unknown", default=0.0) == 0.0
    
    def test_to_dict_and_from_dict(self):
        """Test serialization."""
        result = EvaluationResult(
            execution_id="exec1",
            evaluator_name="test",
            overall_score=0.85
        )
        result.add_metric("score", 85.0)
        
        data = result.to_dict()
        restored = EvaluationResult.from_dict(data)
        
        assert restored.execution_id == result.execution_id
        assert restored.overall_score == result.overall_score
        assert len(restored.metrics) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# NodeVariant Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestNodeVariant:
    """Tests for NodeVariant aggregate root."""
    
    def test_create_variant(self):
        """Test creating a node variant."""
        variant_node = WorkflowNode(id="var1", name="Variant Node", type="action")
        variant = NodeVariant(
            workflow_id="wf1",
            original_node_id="node1",
            variant_name="fast_version",
            variant_node=variant_node,
            description="A faster implementation"
        )
        
        assert variant.workflow_id == "wf1"
        assert variant.original_node_id == "node1"
        assert variant.variant_name == "fast_version"
        assert variant.is_active
    
    def test_variant_to_dict(self):
        """Test variant serialization."""
        variant_node = WorkflowNode(id="var1", name="Variant", type="action")
        variant = NodeVariant(
            workflow_id="wf1",
            original_node_id="node1",
            variant_name="test",
            variant_node=variant_node
        )
        
        data = variant.to_dict()
        
        assert data["workflow_id"] == "wf1"
        assert data["original_node_id"] == "node1"
        assert data["variant_name"] == "test"
        assert "variant_node" in data
