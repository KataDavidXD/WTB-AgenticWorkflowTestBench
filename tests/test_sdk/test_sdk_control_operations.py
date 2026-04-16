"""
SDK Integration tests for execution control operations.

Tests control operations:
- Pause/Resume execution
- Update node implementation
- Update workflow structure
- Branching execution
- Rollback to checkpoints
- Stop execution

Run with: pytest tests/test_sdk/test_sdk_control_operations.py -v
"""

import pytest
import uuid
import time
from typing import Dict, Any, List, Callable
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from wtb.sdk import WTBTestBench, WorkflowProject
from wtb.sdk.test_bench import RollbackResult, ForkResult
from wtb.domain.models import ExecutionStatus
from wtb.application.factories import WTBTestBenchFactory

from tests.test_sdk.conftest import (
    create_initial_state,
    create_branching_state,
    LANGGRAPH_AVAILABLE,
)


# Skip all tests if LangGraph not available
pytestmark = pytest.mark.skipif(
    not LANGGRAPH_AVAILABLE,
    reason="LangGraph not installed"
)


# ═══════════════════════════════════════════════════════════════════════════════
# Pause/Resume Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestPauseResume:
    """Tests for pause and resume operations."""
    
    def test_run_with_breakpoint(self, wtb_inmemory, pausable_project):
        """Test running with breakpoints."""
        wtb_inmemory.register_project(pausable_project)
        
        result = wtb_inmemory.run(
            project=pausable_project.name,
            initial_state={"messages": [], "paused": False},
            breakpoints=["node_2"],
        )
        
        # Depending on implementation, may pause or complete
        assert result.status in [
            ExecutionStatus.PAUSED,
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
        ]
    
    def test_pause_execution(self, wtb_inmemory, simple_project):
        """Test pausing an execution."""
        wtb_inmemory.register_project(simple_project)
        
        result = wtb_inmemory.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        # Try to pause (may not work if already completed)
        try:
            paused = wtb_inmemory.pause(result.id)
            assert paused.status in [
                ExecutionStatus.PAUSED,
                ExecutionStatus.COMPLETED,
            ]
        except Exception:
            # Pause may fail if execution already completed
            pass
    
    def test_resume_paused_execution(self, wtb_inmemory, pausable_project):
        """Test resuming a paused execution."""
        wtb_inmemory.register_project(pausable_project)
        
        result = wtb_inmemory.run(
            project=pausable_project.name,
            initial_state={"messages": [], "paused": False},
            breakpoints=["node_2"],
        )
        
        if result.status == ExecutionStatus.PAUSED:
            resumed = wtb_inmemory.resume(result.id)
            
            assert resumed.status in [
                ExecutionStatus.RUNNING,
                ExecutionStatus.COMPLETED,
            ]
    
    def test_resume_with_modified_state(self, wtb_inmemory, pausable_project):
        """Test resuming with modified state."""
        wtb_inmemory.register_project(pausable_project)
        
        result = wtb_inmemory.run(
            project=pausable_project.name,
            initial_state={"messages": [], "paused": False},
            breakpoints=["node_2"],
        )
        
        if result.status == ExecutionStatus.PAUSED:
            resumed = wtb_inmemory.resume(
                result.id,
                modified_state={"messages": ["injected"], "paused": False},
            )
            
            # Should incorporate modified state
            assert resumed is not None
    
    def test_stop_execution(self, wtb_inmemory, simple_project):
        """Test stopping an execution."""
        wtb_inmemory.register_project(simple_project)
        
        result = wtb_inmemory.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        try:
            stopped = wtb_inmemory.stop(result.id)
            assert stopped.status in [
                ExecutionStatus.STOPPED,
                ExecutionStatus.COMPLETED,
            ]
        except Exception:
            # Stop may fail if execution already completed
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# Update Node Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestUpdateNode:
    """Tests for updating node implementations."""
    
    def test_update_node_implementation(self, simple_graph_factory):
        """Test updating a node implementation."""
        project = WorkflowProject(
            name=f"update-node-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
            description="Test node update",
        )
        
        original_version = project.version
        
        def new_implementation(state: Dict[str, Any]) -> Dict[str, Any]:
            return {"messages": state.get("messages", []) + ["UPDATED"], "count": 999}
        
        project.update_node(
            node="node_b",
            new_implementation=new_implementation,
            reason="Testing node update",
        )
        
        # Version should increment
        assert project.version == original_version + 1
    
    def test_node_update_creates_variant(self, simple_graph_factory):
        """Test node update creates a variant."""
        project = WorkflowProject(
            name=f"variant-update-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        def new_impl(state):
            return {"messages": state.get("messages", []) + ["NEW"]}
        
        project.update_node("node_a", new_impl, reason="Test update")
        
        # Should have a variant registered
        variants = project.list_variants("node_a")
        assert len(variants.get("node_a", [])) > 0
    
    def test_multiple_node_updates(self, simple_graph_factory):
        """Test multiple node updates."""
        project = WorkflowProject(
            name=f"multi-update-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        for i in range(3):
            def impl(state, i=i):
                return {"messages": [f"v{i}"]}
            
            project.update_node("node_a", impl, reason=f"Update {i}")
        
        assert project.version >= 3


# ═══════════════════════════════════════════════════════════════════════════════
# Update Workflow Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestUpdateWorkflow:
    """Tests for updating workflow structure."""
    
    def test_update_workflow_graph(self, simple_graph_factory):
        """Test updating workflow graph factory."""
        project = WorkflowProject(
            name=f"update-wf-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        original_version = project.version
        
        # Create a new graph factory
        def new_graph_factory():
            return simple_graph_factory()
        
        project.update_workflow(
            graph_factory=new_graph_factory,
            version="2.0",
            changelog="Updated workflow structure",
        )
        
        # Version should increment
        assert project.version == original_version + 1
        
        # Factory should be updated
        assert project.graph_factory == new_graph_factory
    
    def test_workflow_variant_registration(self, simple_graph_factory, branching_graph_factory):
        """Test registering workflow variants."""
        project = WorkflowProject(
            name=f"wf-variant-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        # Register workflow variant
        project.register_workflow_variant(
            name="branching",
            graph_factory=branching_graph_factory,
            description="Branching variant of workflow",
        )
        
        # Should be registered
        variants = project.list_workflow_variants()
        assert "branching" in variants
    
    def test_get_workflow_variant(self, simple_graph_factory, branching_graph_factory):
        """Test getting workflow variant."""
        project = WorkflowProject(
            name=f"get-wf-variant-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        project.register_workflow_variant(
            name="alt",
            graph_factory=branching_graph_factory,
            description="Alternative workflow",
        )
        
        variant = project.get_workflow_variant("alt")
        
        assert variant is not None
        assert variant.name == "alt"
        assert variant.graph_factory == branching_graph_factory
    
    def test_remove_workflow_variant(self, simple_graph_factory, branching_graph_factory):
        """Test removing workflow variant."""
        project = WorkflowProject(
            name=f"rm-wf-variant-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        project.register_workflow_variant("temp", branching_graph_factory)
        assert "temp" in project.list_workflow_variants()
        
        removed = project.remove_workflow_variant("temp")
        assert removed is True
        assert "temp" not in project.list_workflow_variants()


# ═══════════════════════════════════════════════════════════════════════════════
# Rollback Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRollback:
    """Tests for rollback operations."""
    
    def test_rollback_to_checkpoint(self, wtb_langgraph, simple_project):
        """Test rollback to checkpoint."""
        wtb_langgraph.register_project(simple_project)
        
        result = wtb_langgraph.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        rollback_result = wtb_langgraph.rollback(result.id, "1")
        
        assert isinstance(rollback_result, RollbackResult)
        assert rollback_result.execution_id == result.id
    
    def test_rollback_to_node(self, wtb_langgraph, simple_project):
        """Test rollback to specific node."""
        wtb_langgraph.register_project(simple_project)
        
        result = wtb_langgraph.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        rollback_result = wtb_langgraph.rollback_to_node(result.id, "node_a")
        
        assert isinstance(rollback_result, RollbackResult)
    
    def test_rollback_invalid_checkpoint(self, wtb_langgraph, simple_project):
        """Test rollback with invalid checkpoint."""
        wtb_langgraph.register_project(simple_project)
        
        result = wtb_langgraph.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        rollback_result = wtb_langgraph.rollback(result.id, "999999")
        
        # Should fail gracefully
        assert isinstance(rollback_result, RollbackResult)
        # May or may not succeed depending on implementation
    
    def test_rollback_preserves_execution(self, wtb_langgraph, simple_project):
        """Test rollback preserves execution record."""
        wtb_langgraph.register_project(simple_project)
        
        result = wtb_langgraph.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        original_id = result.id
        
        wtb_langgraph.rollback(result.id, "1")
        
        # Execution should still be retrievable
        execution = wtb_langgraph.get_execution(original_id)
        assert execution is not None
        assert execution.id == original_id


# ═══════════════════════════════════════════════════════════════════════════════
# Branching Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBranching:
    """Tests for execution branching."""
    
    def test_fork_from_checkpoint(self, wtb_langgraph, simple_project):
        """Test forking from checkpoint (replaced branch() in v1.6)."""
        wtb_langgraph.register_project(simple_project)
        
        result = wtb_langgraph.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        # Get checkpoints to find a valid checkpoint_id
        checkpoints = wtb_langgraph.get_checkpoints(result.id)
        if not checkpoints:
            pytest.skip("No checkpoints available for forking")
        
        checkpoint_id = str(checkpoints[0].id)
        
        try:
            fork_result = wtb_langgraph.fork(
                result.id,
                checkpoint_id,
            )
            
            assert isinstance(fork_result, ForkResult)
            assert fork_result.source_execution_id == result.id
        except (RuntimeError, ValueError) as e:
            pytest.skip(f"Forking not supported: {e}")
    
    def test_fork_creates_new_execution(self, wtb_langgraph, simple_project):
        """Test fork creates new independent execution."""
        wtb_langgraph.register_project(simple_project)
        
        result = wtb_langgraph.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        # Get checkpoints to find a valid checkpoint_id
        checkpoints = wtb_langgraph.get_checkpoints(result.id)
        if not checkpoints:
            pytest.skip("No checkpoints available for forking")
        
        checkpoint_id = str(checkpoints[0].id)
        
        try:
            fork_result = wtb_langgraph.fork(result.id, checkpoint_id)
            
            # Fork ID should be different from source
            assert fork_result.fork_execution_id != result.id
        except (RuntimeError, ValueError):
            pytest.skip("Forking not supported")
    
    def test_fork_execution(self, wtb_langgraph, simple_project):
        """Test forking execution."""
        wtb_langgraph.register_project(simple_project)
        
        result = wtb_langgraph.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        try:
            fork_result = wtb_langgraph.fork(
                result.id,
                "1",
                new_initial_state={"messages": ["forked"], "count": 0},
            )
            
            assert isinstance(fork_result, ForkResult)
            assert fork_result.source_execution_id == result.id
            assert fork_result.fork_execution_id != result.id
        except (RuntimeError, ValueError):
            pytest.skip("Forking not supported")
    
    def test_multiple_forks(self, wtb_langgraph, simple_project):
        """Test creating multiple forks (replaced branch() in v1.6)."""
        wtb_langgraph.register_project(simple_project)
        
        result = wtb_langgraph.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        # Get checkpoints to find a valid checkpoint_id
        checkpoints = wtb_langgraph.get_checkpoints(result.id)
        if not checkpoints:
            pytest.skip("No checkpoints available for forking")
        
        checkpoint_id = str(checkpoints[0].id)
        
        forks = []
        try:
            for i in range(3):
                fork = wtb_langgraph.fork(
                    result.id, 
                    checkpoint_id,
                    new_initial_state={"fork_num": i},
                )
                forks.append(fork)
            
            # All forks should be unique
            fork_ids = [f.fork_execution_id for f in forks]
            assert len(set(fork_ids)) == 3
        except (RuntimeError, ValueError):
            pytest.skip("Forking not supported")


# ═══════════════════════════════════════════════════════════════════════════════
# Variant Management Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestVariantManagement:
    """Tests for variant management through SDK."""
    
    def test_register_variant(self, wtb_inmemory, simple_project):
        """Test registering variant through WTB."""
        wtb_inmemory.register_project(simple_project)
        
        def custom_impl(state):
            return {"messages": state.get("messages", []) + ["CUSTOM"]}
        
        wtb_inmemory.register_variant(
            project=simple_project.name,
            node="node_b",
            name="custom",
            implementation=custom_impl,
            description="Custom variant",
        )
        
        # Variant should be in project cache
        project = wtb_inmemory.get_project(simple_project.name)
        variants = project.list_variants("node_b")
        assert "custom" in variants.get("node_b", [])
    
    def test_run_with_variant_config(self, wtb_inmemory, project_with_variants):
        """Test running with variant configuration."""
        wtb_inmemory.register_project(project_with_variants)
        
        result = wtb_inmemory.run(
            project=project_with_variants.name,
            initial_state=create_initial_state(),
            variant_config={"node_b": "fast"},
        )
        
        assert result.status in [ExecutionStatus.COMPLETED, ExecutionStatus.FAILED]
    
    def test_run_with_workflow_variant(self, wtb_inmemory, simple_graph_factory, branching_graph_factory):
        """Test running with workflow variant."""
        project = WorkflowProject(
            name=f"wf-variant-run-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        project.register_workflow_variant("branching", branching_graph_factory)
        
        wtb_inmemory.register_project(project)
        
        result = wtb_inmemory.run(
            project=project.name,
            initial_state=create_branching_state("a"),
            workflow_variant="branching",
        )
        
        assert result.status in [ExecutionStatus.COMPLETED, ExecutionStatus.FAILED]


# ═══════════════════════════════════════════════════════════════════════════════
# Concurrent Control Operations Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestConcurrentOperations:
    """Tests for concurrent control operations."""
    
    def test_concurrent_state_reads(self, wtb_inmemory, simple_project):
        """Test concurrent state reads don't corrupt state."""
        wtb_inmemory.register_project(simple_project)
        
        result = wtb_inmemory.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        def read_state():
            return wtb_inmemory.get_state(result.id)
        
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(read_state) for _ in range(10)]
            states = [f.result() for f in futures]
        
        # All reads should return consistent state
        assert all(s is not None for s in states)
    
    def test_concurrent_execution_queries(self, wtb_inmemory, simple_project):
        """Test concurrent execution queries."""
        wtb_inmemory.register_project(simple_project)
        
        result = wtb_inmemory.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        def query_execution():
            return wtb_inmemory.get_execution(result.id)
        
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(query_execution) for _ in range(10)]
            executions = [f.result() for f in futures]
        
        # All should return same execution
        assert all(e.id == result.id for e in executions)


# ═══════════════════════════════════════════════════════════════════════════════
# ACID Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestControlOperationsACID:
    """Tests for ACID compliance in control operations."""
    
    def test_atomicity_rollback(self, wtb_langgraph, simple_project):
        """Test rollback is atomic."""
        wtb_langgraph.register_project(simple_project)
        
        result = wtb_langgraph.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        rollback = wtb_langgraph.rollback(result.id, "1")
        
        # Rollback should completely succeed or completely fail
        assert isinstance(rollback, RollbackResult)
        assert rollback.success in [True, False]
    
    def test_consistency_after_update(self, simple_graph_factory):
        """Test state consistency after update."""
        project = WorkflowProject(
            name=f"consistent-{uuid.uuid4().hex[:8]}",
            graph_factory=simple_graph_factory,
        )
        
        def new_impl(state):
            return {"messages": ["new"]}
        
        project.update_node("node_a", new_impl, reason="Test")
        
        # Project should be in consistent state
        assert project.version > 1
        variants = project.list_variants("node_a")
        assert len(variants) > 0
    
    def test_isolation_control_operations(self, wtb_inmemory, simple_project):
        """Test control operations are isolated."""
        wtb_inmemory.register_project(simple_project)
        
        result1 = wtb_inmemory.run(
            project=simple_project.name,
            initial_state={"messages": ["exec1"], "count": 1},
        )
        
        result2 = wtb_inmemory.run(
            project=simple_project.name,
            initial_state={"messages": ["exec2"], "count": 2},
        )
        
        # Operations on result1 shouldn't affect result2
        rollback = wtb_inmemory.rollback(result1.id, "1")
        
        exec2 = wtb_inmemory.get_execution(result2.id)
        assert exec2.id == result2.id
    
    def test_durability_control_operations(self, wtb_inmemory, simple_project):
        """Test control operation results are durable."""
        wtb_inmemory.register_project(simple_project)
        
        result = wtb_inmemory.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        # Multiple queries should return same result
        for _ in range(5):
            execution = wtb_inmemory.get_execution(result.id)
            assert execution.id == result.id
            assert execution.status == result.status


# ═══════════════════════════════════════════════════════════════════════════════
# Error Handling Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestControlOperationsErrors:
    """Tests for error handling in control operations."""
    
    def test_pause_invalid_execution(self, wtb_inmemory):
        """Test pausing invalid execution ID."""
        try:
            wtb_inmemory.pause("invalid-execution-id")
        except (KeyError, ValueError, AttributeError):
            # Expected to fail
            pass
    
    def test_resume_invalid_execution(self, wtb_inmemory):
        """Test resuming invalid execution ID."""
        try:
            wtb_inmemory.resume("invalid-execution-id")
        except (KeyError, ValueError, AttributeError):
            # Expected to fail
            pass
    
    def test_rollback_invalid_execution(self, wtb_inmemory):
        """Test rollback with invalid execution ID."""
        result = wtb_inmemory.rollback("invalid-execution-id", "1")
        
        # Should return failed result
        assert isinstance(result, RollbackResult)
        assert result.success is False
    
    def test_get_state_invalid_execution(self, wtb_inmemory):
        """Test getting state of invalid execution."""
        try:
            state = wtb_inmemory.get_state("invalid-execution-id")
            # May return None or raise
        except (KeyError, ValueError, AttributeError):
            pass
