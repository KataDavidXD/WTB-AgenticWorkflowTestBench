"""
Integration Tests for WTB.

Tests end-to-end scenarios using all components together.

Quality Improvements (2026-01-16):
- Added contextual assertion helpers
- Improved error messages for debugging
- One-shot breakpoint semantic support
"""

import pytest
from typing import Optional, List, Dict, Any

from wtb.domain.models import (
    Execution,
    ExecutionStatus,
    ExecutionState,
    TestWorkflow,
    WorkflowNode,
    WorkflowEdge,
    NodeVariant,
)
from wtb.domain.models.batch_test import BatchTest, BatchTestStatus, VariantCombination, BatchTestResult
from wtb.domain.models.evaluation import EvaluationResult
from wtb.domain.interfaces.repositories import IExecutionRepository, IWorkflowRepository, INodeVariantRepository
from wtb.domain.interfaces.state_adapter import CheckpointTrigger
from wtb.infrastructure.adapters.inmemory_state_adapter import InMemoryStateAdapter
from wtb.application.services.execution_controller import ExecutionController
from wtb.application.services.node_replacer import NodeReplacer

# Test helpers for better assertions
from tests.helpers.assertions import (
    assert_execution_completed,
    assert_execution_paused,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Mock Repositories
# ═══════════════════════════════════════════════════════════════════════════════

class InMemoryExecutionRepository(IExecutionRepository):
    """In-memory execution repository."""
    
    def __init__(self):
        self._storage: Dict[str, Execution] = {}
    
    def get(self, id: str) -> Optional[Execution]:
        return self._storage.get(id)
    
    def list(self, limit: int = 100, offset: int = 0) -> List[Execution]:
        return list(self._storage.values())[offset:offset + limit]
    
    def add(self, entity: Execution) -> Execution:
        self._storage[entity.id] = entity
        return entity
    
    def update(self, entity: Execution) -> Execution:
        self._storage[entity.id] = entity
        return entity
    
    def delete(self, id: str) -> bool:
        if id in self._storage:
            del self._storage[id]
            return True
        return False
    
    def exists(self, id: str) -> bool:
        return id in self._storage
    
    def find_by_workflow(self, workflow_id: str, status: Optional[str] = None) -> List[Execution]:
        results = [e for e in self._storage.values() if e.workflow_id == workflow_id]
        if status:
            results = [e for e in results if e.status.value == status]
        return results
    
    def find_by_status(self, status: str) -> List[Execution]:
        return [e for e in self._storage.values() if e.status.value == status]
    
    def count_by_status(self) -> Dict[str, int]:
        counts = {}
        for e in self._storage.values():
            counts[e.status.value] = counts.get(e.status.value, 0) + 1
        return counts
    
    def find_running(self) -> List[Execution]:
        return [e for e in self._storage.values() if e.status == ExecutionStatus.RUNNING]


class InMemoryWorkflowRepository(IWorkflowRepository):
    """In-memory workflow repository."""
    
    def __init__(self):
        self._storage: Dict[str, TestWorkflow] = {}
    
    def get(self, id: str) -> Optional[TestWorkflow]:
        return self._storage.get(id)
    
    def list(self, limit: int = 100, offset: int = 0) -> List[TestWorkflow]:
        return list(self._storage.values())[offset:offset + limit]
    
    def add(self, entity: TestWorkflow) -> TestWorkflow:
        self._storage[entity.id] = entity
        return entity
    
    def update(self, entity: TestWorkflow) -> TestWorkflow:
        self._storage[entity.id] = entity
        return entity
    
    def delete(self, id: str) -> bool:
        if id in self._storage:
            del self._storage[id]
            return True
        return False
    
    def exists(self, id: str) -> bool:
        return id in self._storage
    
    def find_by_name(self, name: str) -> Optional[TestWorkflow]:
        for w in self._storage.values():
            if w.name == name:
                return w
        return None
    
    def find_by_version(self, name: str, version: str) -> Optional[TestWorkflow]:
        for w in self._storage.values():
            if w.name == name and w.version == version:
                return w
        return None
    
    def list_all(self) -> List[TestWorkflow]:
        """List all workflows without pagination."""
        return list(self._storage.values())


class InMemoryNodeVariantRepository(INodeVariantRepository):
    """In-memory node variant repository."""
    
    def __init__(self):
        self._storage: Dict[str, NodeVariant] = {}
    
    def get(self, id: str) -> Optional[NodeVariant]:
        return self._storage.get(id)
    
    def list(self, limit: int = 100, offset: int = 0) -> List[NodeVariant]:
        return list(self._storage.values())[offset:offset + limit]
    
    def add(self, entity: NodeVariant) -> NodeVariant:
        self._storage[entity.id] = entity
        return entity
    
    def update(self, entity: NodeVariant) -> NodeVariant:
        self._storage[entity.id] = entity
        return entity
    
    def delete(self, id: str) -> bool:
        if id in self._storage:
            del self._storage[id]
            return True
        return False
    
    def exists(self, id: str) -> bool:
        return id in self._storage
    
    def find_by_workflow(self, workflow_id: str) -> List[NodeVariant]:
        return [v for v in self._storage.values() if v.workflow_id == workflow_id]
    
    def find_by_node(self, workflow_id: str, node_id: str) -> List[NodeVariant]:
        return [
            v for v in self._storage.values()
            if v.workflow_id == workflow_id and v.original_node_id == node_id
        ]
    
    def find_active(self, workflow_id: str) -> List[NodeVariant]:
        return [
            v for v in self._storage.values()
            if v.workflow_id == workflow_id and v.is_active
        ]


# ═══════════════════════════════════════════════════════════════════════════════
# Integration Test: Simple Workflow Execution
# ═══════════════════════════════════════════════════════════════════════════════

class TestSimpleWorkflowExecution:
    """
    Integration test for a simple workflow execution scenario.
    
    Scenario:
    1. Create a data processing workflow with 4 nodes:
       - start: Initialize
       - fetch: Fetch data from source
       - transform: Transform data
       - end: Complete
    
    2. Execute the workflow and verify:
       - All nodes execute in order
       - State is properly tracked
       - Checkpoints are created
       - Execution completes successfully
    
    3. Test rollback to a mid-point checkpoint
    
    4. Test A/B testing with a variant node
    """
    
    @pytest.fixture
    def setup(self):
        """Set up all components for integration testing."""
        # Repositories
        exec_repo = InMemoryExecutionRepository()
        workflow_repo = InMemoryWorkflowRepository()
        variant_repo = InMemoryNodeVariantRepository()
        
        # State adapter
        state_adapter = InMemoryStateAdapter()
        
        # Services
        controller = ExecutionController(
            execution_repository=exec_repo,
            workflow_repository=workflow_repo,
            state_adapter=state_adapter,
        )
        
        replacer = NodeReplacer(variant_repository=variant_repo)
        
        return {
            "exec_repo": exec_repo,
            "workflow_repo": workflow_repo,
            "variant_repo": variant_repo,
            "state_adapter": state_adapter,
            "controller": controller,
            "replacer": replacer,
        }
    
    def create_data_processing_workflow(self) -> TestWorkflow:
        """Create the test workflow."""
        workflow = TestWorkflow(
            name="Data Processing Pipeline",
            description="Fetches, transforms, and stores data"
        )
        
        # Define nodes
        start = WorkflowNode(
            id="start",
            name="Initialize",
            type="start",
            config={"source": "api"}
        )
        
        fetch = WorkflowNode(
            id="fetch",
            name="Fetch Data",
            type="action",
            tool_name="data_fetcher",
            config={"url": "https://api.example.com/data", "timeout": 30}
        )
        
        transform = WorkflowNode(
            id="transform",
            name="Transform Data",
            type="action",
            tool_name="data_transformer",
            config={"format": "json", "schema_version": "v1"}
        )
        
        end = WorkflowNode(
            id="end",
            name="Complete",
            type="end"
        )
        
        # Build workflow
        workflow.add_node(start)
        workflow.add_node(fetch)
        workflow.add_node(transform)
        workflow.add_node(end)
        
        workflow.add_edge(WorkflowEdge(source_id="start", target_id="fetch"))
        workflow.add_edge(WorkflowEdge(source_id="fetch", target_id="transform"))
        workflow.add_edge(WorkflowEdge(source_id="transform", target_id="end"))
        
        return workflow
    
    def test_complete_workflow_execution(self, setup):
        """Test executing a workflow from start to finish."""
        workflow_repo = setup["workflow_repo"]
        controller = setup["controller"]
        state_adapter = setup["state_adapter"]
        
        # Create and register workflow
        workflow = self.create_data_processing_workflow()
        workflow_repo.add(workflow)
        
        # Create execution with initial variables
        execution = controller.create_execution(
            workflow,
            initial_state={"run_id": "test_001", "mode": "full"}
        )
        
        # Verify initial state
        assert execution.status == ExecutionStatus.PENDING, \
            f"New execution should be PENDING, got {execution.status}"
        assert execution.state.current_node_id == "start", \
            f"Should start at 'start' node, got {execution.state.current_node_id}"
        assert execution.state.workflow_variables["run_id"] == "test_001"
        
        # Run the workflow
        completed = controller.run(execution.id)
        
        # Verify completion with contextual helper
        assert_execution_completed(
            completed,
            expected_path=["start", "fetch", "transform", "end"],
        )
        assert completed.completed_at is not None, \
            "completed_at timestamp should be set"
        
        # Verify node results exist
        expected_nodes = {"start", "fetch", "transform", "end"}
        actual_nodes = set(completed.state.node_results.keys())
        missing = expected_nodes - actual_nodes
        assert not missing, f"Missing node results: {missing}"
        
        # Verify checkpoints were created
        session_id = completed.session_id
        checkpoints = state_adapter.get_checkpoints(session_id)
        assert len(checkpoints) >= 8  # At least 2 per node (entry + exit)
        
        # Verify node boundaries
        boundaries = state_adapter.get_node_boundaries(session_id)
        assert len(boundaries) == 4  # One per node
        
        for boundary in boundaries:
            assert boundary.node_status == "completed"
    
    def test_breakpoint_and_resume(self, setup):
        """Test pausing at breakpoint and resuming."""
        workflow_repo = setup["workflow_repo"]
        controller = setup["controller"]
        
        workflow = self.create_data_processing_workflow()
        workflow_repo.add(workflow)
        
        # Create execution with breakpoint at transform
        execution = controller.create_execution(
            workflow,
            initial_state={"data": []},
            breakpoints=["transform"]
        )
        
        # Run - should pause at transform
        paused = controller.run(execution.id)
        
        assert paused.status == ExecutionStatus.PAUSED
        assert paused.state.current_node_id == "transform"
        assert "fetch" in paused.state.execution_path
        assert "transform" not in paused.state.execution_path
        
        # Breakpoint is auto-removed after triggering (one-shot semantic)
        assert "transform" not in paused.breakpoints, "Breakpoint should be removed after triggering"
        
        # Resume with modified variables
        completed = controller.resume(
            execution.id,
            modified_state={"extra_config": "added_during_pause"}
        )
        
        assert completed.status == ExecutionStatus.COMPLETED
        assert "transform" in completed.state.execution_path
        assert completed.state.workflow_variables.get("extra_config") == "added_during_pause"
    
    def test_rollback_and_retry(self, setup):
        """Test rolling back to a checkpoint and retrying."""
        workflow_repo = setup["workflow_repo"]
        exec_repo = setup["exec_repo"]
        controller = setup["controller"]
        state_adapter = setup["state_adapter"]
        
        workflow = self.create_data_processing_workflow()
        workflow_repo.add(workflow)
        
        # Run workflow with breakpoint to get a checkpoint
        execution = controller.create_execution(
            workflow,
            initial_state={"attempt": 1},
            breakpoints=["transform"]
        )
        
        # Run to breakpoint
        paused = controller.run(execution.id)
        session_id = paused.session_id
        
        # Get checkpoint at the breakpoint
        checkpoints = state_adapter.get_checkpoints(session_id)
        breakpoint_cp = checkpoints[-1]  # Most recent
        
        # Remove breakpoint and complete
        paused.breakpoints.clear()
        completed = controller.run(execution.id)
        
        assert completed.status == ExecutionStatus.COMPLETED
        
        # Now rollback to the checkpoint
        rolled_back = controller.rollback(execution.id, breakpoint_cp.id)
        
        assert rolled_back.status == ExecutionStatus.PAUSED
        
        # Modify and retry
        rolled_back.state.workflow_variables["attempt"] = 2
        exec_repo.update(rolled_back)
        
        retried = controller.run(execution.id)
        
        assert retried.status == ExecutionStatus.COMPLETED
        assert retried.state.workflow_variables.get("attempt") == 2
    
    def test_ab_testing_with_variants(self, setup):
        """Test A/B testing using node variants."""
        workflow_repo = setup["workflow_repo"]
        controller = setup["controller"]
        replacer = setup["replacer"]
        
        workflow = self.create_data_processing_workflow()
        workflow_repo.add(workflow)
        
        # Create a variant for the transform node (simulating a faster algorithm)
        variant = replacer.create_variant_from_node(
            workflow,
            "transform",
            "fast_transform",
            modifications={
                "algorithm": "optimized_v2",
                "parallel": True
            }
        )
        
        # Run original workflow
        exec_original = controller.create_execution(
            workflow,
            initial_state={"variant": "original"}
        )
        result_original = controller.run(exec_original.id)
        
        assert result_original.status == ExecutionStatus.COMPLETED
        
        # Apply variant and run
        modified_workflow = replacer.apply_variant_set(
            workflow,
            {"transform": variant.id}
        )
        workflow_repo.add(modified_workflow)  # Add as new workflow
        
        exec_variant = controller.create_execution(
            modified_workflow,
            initial_state={"variant": "fast_transform"}
        )
        result_variant = controller.run(exec_variant.id)
        
        assert result_variant.status == ExecutionStatus.COMPLETED
        
        # Verify different node configs were used
        original_transform_result = result_original.state.node_results.get("transform", {})
        variant_transform_result = result_variant.state.node_results.get("transform", {})
        
        # The variant should have the optimized config
        assert variant_transform_result.get("algorithm") == "optimized_v2"
        assert variant_transform_result.get("parallel") is True


# ═══════════════════════════════════════════════════════════════════════════════
# Integration Test: Batch Test Scenario
# ═══════════════════════════════════════════════════════════════════════════════

class TestBatchTestScenario:
    """
    Integration test for batch A/B testing scenario.
    
    Scenario:
    1. Create a workflow with a configurable processing node
    2. Create multiple variants with different configurations
    3. Simulate running batch tests with different variants
    4. Collect and compare results
    """
    
    @pytest.fixture
    def setup(self):
        """Set up components for batch testing."""
        exec_repo = InMemoryExecutionRepository()
        workflow_repo = InMemoryWorkflowRepository()
        variant_repo = InMemoryNodeVariantRepository()
        state_adapter = InMemoryStateAdapter()
        
        controller = ExecutionController(
            execution_repository=exec_repo,
            workflow_repository=workflow_repo,
            state_adapter=state_adapter,
        )
        replacer = NodeReplacer(variant_repository=variant_repo)
        
        return {
            "exec_repo": exec_repo,
            "workflow_repo": workflow_repo,
            "variant_repo": variant_repo,
            "state_adapter": state_adapter,
            "controller": controller,
            "replacer": replacer,
        }
    
    def create_prompt_workflow(self) -> TestWorkflow:
        """Create a workflow for testing different LLM prompts."""
        workflow = TestWorkflow(name="LLM Response Generator")
        
        start = WorkflowNode(id="start", name="Start", type="start")
        generate = WorkflowNode(
            id="generate",
            name="Generate Response",
            type="action",
            tool_name="llm_generate",
            config={"model": "gpt-4", "temperature": 0.7, "prompt_template": "default"}
        )
        end = WorkflowNode(id="end", name="End", type="end")
        
        workflow.add_node(start)
        workflow.add_node(generate)
        workflow.add_node(end)
        
        workflow.add_edge(WorkflowEdge(source_id="start", target_id="generate"))
        workflow.add_edge(WorkflowEdge(source_id="generate", target_id="end"))
        
        return workflow
    
    def test_batch_comparison(self, setup):
        """Test running multiple variants and comparing results."""
        workflow_repo = setup["workflow_repo"]
        controller = setup["controller"]
        replacer = setup["replacer"]
        
        workflow = self.create_prompt_workflow()
        workflow_repo.add(workflow)
        
        # Create variants for different prompt strategies
        variant_concise = replacer.create_variant_from_node(
            workflow,
            "generate",
            "concise",
            modifications={"prompt_template": "concise", "max_tokens": 100}
        )
        
        variant_detailed = replacer.create_variant_from_node(
            workflow,
            "generate",
            "detailed",
            modifications={"prompt_template": "detailed", "max_tokens": 500}
        )
        
        variant_creative = replacer.create_variant_from_node(
            workflow,
            "generate",
            "creative",
            modifications={"prompt_template": "creative", "temperature": 0.9}
        )
        
        # Define batch test
        batch_test = BatchTest(
            name="Prompt Template Comparison",
            workflow_id=workflow.id,
            variant_combinations=[
                VariantCombination(name="Original", variants={}),
                VariantCombination(name="Concise", variants={"generate": variant_concise.id}),
                VariantCombination(name="Detailed", variants={"generate": variant_detailed.id}),
                VariantCombination(name="Creative", variants={"generate": variant_creative.id}),
            ]
        )
        
        batch_test.start()
        
        # Run each combination
        for combo in batch_test.variant_combinations:
            # Apply variants
            if combo.variants:
                test_workflow = replacer.apply_variant_set(workflow, combo.variants)
            else:
                test_workflow = workflow
            
            # Ensure workflow is in repo
            if not workflow_repo.exists(test_workflow.id):
                workflow_repo.add(test_workflow)
            
            # Run execution
            execution = controller.create_execution(
                test_workflow,
                initial_state={"input": "Test prompt"}
            )
            result = controller.run(execution.id)
            
            # Evaluate (simplified - would use real evaluator)
            success = result.status == ExecutionStatus.COMPLETED
            score = 0.85 if success else 0.0
            
            batch_test.add_result(BatchTestResult(
                combination_name=combo.name,
                execution_id=execution.id,
                success=success,
                overall_score=score,
                metrics={"latency_ms": 150, "token_count": 50}
            ))
        
        batch_test.complete()
        
        # Verify batch test results
        assert batch_test.status == BatchTestStatus.COMPLETED
        assert len(batch_test.results) == 4
        assert batch_test.get_success_rate() == 1.0
        
        # Build and verify comparison matrix
        matrix = batch_test.build_comparison_matrix()
        assert "combinations" in matrix
        assert len(matrix["combinations"]) == 4
    
    def test_evaluation_collection(self, setup):
        """Test collecting evaluation results from executions."""
        workflow_repo = setup["workflow_repo"]
        controller = setup["controller"]
        
        workflow = self.create_prompt_workflow()
        workflow_repo.add(workflow)
        
        # Run execution
        execution = controller.create_execution(workflow)
        result = controller.run(execution.id)
        
        # Create evaluation results (simulating multiple evaluators)
        accuracy_eval = EvaluationResult(
            execution_id=execution.id,
            evaluator_name="accuracy",
            overall_score=0.92
        )
        accuracy_eval.add_metric("exact_match", 0.85)
        accuracy_eval.add_metric("semantic_similarity", 0.95)
        
        latency_eval = EvaluationResult(
            execution_id=execution.id,
            evaluator_name="latency",
            overall_score=0.88
        )
        latency_eval.add_metric("response_time_ms", 150)
        latency_eval.add_metric("tokens_per_second", 45)
        
        # Verify evaluations
        assert accuracy_eval.get_metric_value("exact_match") == 0.85
        assert latency_eval.get_metric_value("response_time_ms") == 150
        
        # Calculate combined score
        combined_score = (accuracy_eval.overall_score + latency_eval.overall_score) / 2
        assert combined_score == pytest.approx(0.9)


# ═══════════════════════════════════════════════════════════════════════════════
# Integration Test: Error Handling and Recovery
# ═══════════════════════════════════════════════════════════════════════════════

class TestErrorHandlingAndRecovery:
    """Integration tests for error scenarios and recovery."""
    
    @pytest.fixture
    def setup(self):
        """Set up components."""
        exec_repo = InMemoryExecutionRepository()
        workflow_repo = InMemoryWorkflowRepository()
        state_adapter = InMemoryStateAdapter()
        
        controller = ExecutionController(
            execution_repository=exec_repo,
            workflow_repository=workflow_repo,
            state_adapter=state_adapter,
        )
        
        return {
            "exec_repo": exec_repo,
            "workflow_repo": workflow_repo,
            "state_adapter": state_adapter,
            "controller": controller,
        }
    
    def test_workflow_with_invalid_node_reference(self, setup):
        """Test handling workflow with bad edge references."""
        workflow_repo = setup["workflow_repo"]
        
        workflow = TestWorkflow(name="Bad Workflow")
        start = WorkflowNode(id="start", name="Start", type="start")
        workflow.add_node(start)
        workflow.add_edge(WorkflowEdge(source_id="start", target_id="nonexistent"))
        
        workflow_repo.add(workflow)
        
        errors = workflow.validate()
        assert len(errors) > 0
        assert any("nonexistent" in e or "not found" in e for e in errors)
    
    def test_execution_cancellation(self, setup):
        """Test cancelling an execution."""
        workflow_repo = setup["workflow_repo"]
        exec_repo = setup["exec_repo"]
        controller = setup["controller"]
        
        # Create a simple workflow
        workflow = TestWorkflow(name="Test")
        workflow.add_node(WorkflowNode(id="start", name="Start", type="start"))
        workflow.add_node(WorkflowNode(id="end", name="End", type="end"))
        workflow.add_edge(WorkflowEdge(source_id="start", target_id="end"))
        workflow_repo.add(workflow)
        
        # Create execution
        execution = controller.create_execution(workflow)
        
        # Cancel before running
        cancelled = controller.stop(execution.id)
        
        assert cancelled.status == ExecutionStatus.CANCELLED
        assert cancelled.is_terminal()
        
        # Verify it's persisted
        stored = exec_repo.get(execution.id)
        assert stored.status == ExecutionStatus.CANCELLED
    
    def test_checkpoint_persistence_after_failure(self, setup):
        """Test that checkpoints are preserved after failure."""
        workflow_repo = setup["workflow_repo"]
        controller = setup["controller"]
        state_adapter = setup["state_adapter"]
        
        # Create workflow
        workflow = TestWorkflow(name="Test")
        workflow.add_node(WorkflowNode(id="start", name="Start", type="start"))
        workflow.add_node(WorkflowNode(id="action", name="Action", type="action"))
        workflow.add_node(WorkflowNode(id="end", name="End", type="end"))
        workflow.add_edge(WorkflowEdge(source_id="start", target_id="action"))
        workflow.add_edge(WorkflowEdge(source_id="action", target_id="end"))
        workflow_repo.add(workflow)
        
        # Run with breakpoint
        execution = controller.create_execution(workflow, breakpoints=["action"])
        paused = controller.run(execution.id)
        
        session_id = paused.session_id
        
        # Verify checkpoints exist
        checkpoints = state_adapter.get_checkpoints(session_id)
        assert len(checkpoints) > 0
        
        # Cancel the execution
        controller.stop(execution.id)
        
        # Checkpoints should still exist
        checkpoints_after = state_adapter.get_checkpoints(session_id)
        assert len(checkpoints_after) == len(checkpoints)

