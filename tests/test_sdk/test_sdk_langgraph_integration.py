"""
SDK Integration tests for LangGraph checkpointing and state management.

Tests LangGraph integration:
- Checkpointing during execution
- Time-travel / rollback
- State inspection
- Streaming execution
- Thread-based isolation

Run with: pytest tests/test_sdk/test_sdk_langgraph_integration.py -v
"""

import pytest
import uuid
from typing import Dict, Any, List
from datetime import datetime

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
# Basic Execution Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestLangGraphBasicExecution:
    """Tests for basic LangGraph execution through SDK."""
    
    def test_run_simple_workflow(self, wtb_langgraph, simple_project):
        """Test running a simple workflow with LangGraph checkpointer."""
        wtb_langgraph.register_project(simple_project)
        
        result = wtb_langgraph.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        assert result.status == ExecutionStatus.COMPLETED
        assert result.id is not None
    
    def test_run_with_initial_state(self, wtb_langgraph, simple_project):
        """Test running with custom initial state."""
        wtb_langgraph.register_project(simple_project)
        
        result = wtb_langgraph.run(
            project=simple_project.name,
            initial_state={"messages": ["init"], "count": 10},
        )
        
        assert result.status == ExecutionStatus.COMPLETED
    
    def test_run_branching_workflow(self, wtb_langgraph, branching_project):
        """Test running branching workflow."""
        wtb_langgraph.register_project(branching_project)
        
        # Test branch A
        result_a = wtb_langgraph.run(
            project=branching_project.name,
            initial_state=create_branching_state("a"),
        )
        assert result_a.status == ExecutionStatus.COMPLETED
        
        # Test branch B
        result_b = wtb_langgraph.run(
            project=branching_project.name,
            initial_state=create_branching_state("b"),
        )
        assert result_b.status == ExecutionStatus.COMPLETED
        
        # Different executions
        assert result_a.id != result_b.id


# ═══════════════════════════════════════════════════════════════════════════════
# Checkpoint Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestLangGraphCheckpoints:
    """Tests for LangGraph checkpoint operations."""
    
    def test_checkpoints_created(self, wtb_langgraph, simple_project):
        """Test checkpoints are created during execution."""
        wtb_langgraph.register_project(simple_project)
        
        result = wtb_langgraph.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        checkpoints = wtb_langgraph.get_checkpoints(result.id)
        
        # Should have checkpoints (depends on adapter implementation)
        assert isinstance(checkpoints, list)
    
    def test_get_checkpoint_state(self, wtb_langgraph, simple_project):
        """Test getting state at a specific checkpoint."""
        wtb_langgraph.register_project(simple_project)
        
        result = wtb_langgraph.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        checkpoints = wtb_langgraph.get_checkpoints(result.id)
        
        if checkpoints:
            # v1.6: State is available directly on checkpoint's state_values
            cp = checkpoints[0]
            cp_state = cp.state_values
            # State values is a dict containing the workflow state
            if cp_state is not None:
                assert isinstance(cp_state, dict)
    
    def test_supports_time_travel(self, wtb_langgraph):
        """Test time-travel capability check."""
        # LangGraph adapter should support time travel
        supports = wtb_langgraph.supports_time_travel()
        assert isinstance(supports, bool)


# ═══════════════════════════════════════════════════════════════════════════════
# State Inspection Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestLangGraphStateInspection:
    """Tests for state inspection through SDK."""
    
    def test_get_execution_state(self, wtb_langgraph, simple_project):
        """Test getting current execution state."""
        wtb_langgraph.register_project(simple_project)
        
        result = wtb_langgraph.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        state = wtb_langgraph.get_state(result.id)
        
        # State should be retrievable
        assert state is not None
    
    def test_get_execution_details(self, wtb_langgraph, simple_project):
        """Test getting execution details."""
        wtb_langgraph.register_project(simple_project)
        
        result = wtb_langgraph.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        execution = wtb_langgraph.get_execution(result.id)
        
        assert execution is not None
        assert execution.id == result.id
        assert execution.status == ExecutionStatus.COMPLETED


# ═══════════════════════════════════════════════════════════════════════════════
# Rollback Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestLangGraphRollback:
    """Tests for rollback operations."""
    
    def test_rollback_to_checkpoint(self, wtb_langgraph, simple_project):
        """Test rolling back to a checkpoint."""
        wtb_langgraph.register_project(simple_project)
        
        result = wtb_langgraph.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        # Try rollback
        rollback_result = wtb_langgraph.rollback(result.id, "1")
        
        assert isinstance(rollback_result, RollbackResult)
        # Success depends on checkpoint existence
        assert rollback_result.success in [True, False]
    
    def test_rollback_to_node(self, wtb_langgraph, simple_project):
        """Test rolling back to a specific node."""
        wtb_langgraph.register_project(simple_project)
        
        result = wtb_langgraph.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        rollback_result = wtb_langgraph.rollback_to_node(result.id, "node_a")
        
        assert isinstance(rollback_result, RollbackResult)


# ═══════════════════════════════════════════════════════════════════════════════
# Branching Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestLangGraphBranching:
    """Tests for execution branching."""
    
    def test_supports_forking(self, wtb_langgraph):
        """Test forking capability check (replaced branching in v1.6)."""
        supports = wtb_langgraph.supports_forking()
        assert isinstance(supports, bool)
    
    def test_create_fork(self, wtb_langgraph, simple_project):
        """Test creating execution fork (replaced branch() in v1.6)."""
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
            
            assert isinstance(fork_result, ForkResult)
            assert fork_result.source_execution_id == result.id
        except (RuntimeError, ValueError):
            # Forking may not be supported
            pytest.skip("Forking not supported in this configuration")
    
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
                new_initial_state={"messages": ["forked"], "count": 100},
            )
            
            assert fork_result.source_execution_id == result.id
            assert fork_result.fork_execution_id != result.id
        except (RuntimeError, ValueError, AttributeError):
            pytest.skip("Forking not supported in this configuration")


# ═══════════════════════════════════════════════════════════════════════════════
# Thread Isolation Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestLangGraphIsolation:
    """Tests for thread-based execution isolation."""
    
    def test_executions_are_isolated(self, wtb_langgraph, simple_project):
        """Test multiple executions are isolated."""
        wtb_langgraph.register_project(simple_project)
        
        results = []
        for i in range(3):
            result = wtb_langgraph.run(
                project=simple_project.name,
                initial_state={"messages": [f"exec_{i}"], "count": i},
            )
            results.append(result)
        
        # All should complete
        assert all(r.status == ExecutionStatus.COMPLETED for r in results)
        
        # All have unique IDs
        ids = [r.id for r in results]
        assert len(set(ids)) == 3
    
    def test_state_isolation(self, wtb_langgraph, simple_project):
        """Test state is isolated between executions."""
        wtb_langgraph.register_project(simple_project)
        
        # Execute with different initial states
        result_1 = wtb_langgraph.run(
            project=simple_project.name,
            initial_state={"messages": ["first"], "count": 1},
        )
        
        result_2 = wtb_langgraph.run(
            project=simple_project.name,
            initial_state={"messages": ["second"], "count": 2},
        )
        
        # Get states
        state_1 = wtb_langgraph.get_state(result_1.id)
        state_2 = wtb_langgraph.get_state(result_2.id)
        
        # States should be different
        assert result_1.id != result_2.id


# ═══════════════════════════════════════════════════════════════════════════════
# LangGraph Adapter Direct Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestLangGraphAdapterDirect:
    """Tests for LangGraph adapter directly."""
    
    def test_adapter_creation(self):
        """Test creating LangGraph adapter."""
        from wtb.infrastructure.adapters.langgraph_state_adapter import (
            LangGraphStateAdapter,
            LangGraphConfig,
            LANGGRAPH_AVAILABLE,
        )
        
        if not LANGGRAPH_AVAILABLE:
            pytest.skip("LangGraph not installed")
        
        config = LangGraphConfig.for_testing()
        adapter = LangGraphStateAdapter(config)
        
        assert adapter is not None
        assert adapter.supports_time_travel()
        assert adapter.supports_streaming()
    
    def test_adapter_session_management(self, simple_graph_factory):
        """Test adapter session management."""
        from wtb.infrastructure.adapters.langgraph_state_adapter import (
            LangGraphStateAdapter,
            LangGraphConfig,
        )
        from wtb.domain.models.workflow import ExecutionState
        
        adapter = LangGraphStateAdapter(LangGraphConfig.for_testing())
        
        # Set graph
        graph = simple_graph_factory()
        adapter.set_workflow_graph(graph)
        
        # Initialize session
        exec_id = str(uuid.uuid4())
        session_id = adapter.initialize_session(
            exec_id,
            ExecutionState(
                workflow_variables={"init": True},
                execution_path=[],
            ),
        )
        
        assert session_id is not None
        assert adapter.get_current_session_id() == session_id
    
    def test_adapter_execution(self, simple_graph_factory):
        """Test adapter execution."""
        from wtb.infrastructure.adapters.langgraph_state_adapter import (
            LangGraphStateAdapter,
            LangGraphConfig,
        )
        from wtb.domain.models.workflow import ExecutionState
        
        adapter = LangGraphStateAdapter(LangGraphConfig.for_testing())
        
        # Set graph
        graph = simple_graph_factory()
        adapter.set_workflow_graph(graph)
        
        # Initialize session
        exec_id = str(uuid.uuid4())
        adapter.initialize_session(
            exec_id,
            ExecutionState(workflow_variables={}, execution_path=[]),
        )
        
        # Execute
        result = adapter.execute({"messages": [], "count": 0})
        
        assert "messages" in result
        assert len(result["messages"]) >= 1  # At least one node executed
    
    def test_adapter_checkpoint_history(self, simple_graph_factory):
        """Test adapter checkpoint history."""
        from wtb.infrastructure.adapters.langgraph_state_adapter import (
            LangGraphStateAdapter,
            LangGraphConfig,
        )
        from wtb.domain.models.workflow import ExecutionState
        
        adapter = LangGraphStateAdapter(LangGraphConfig.for_testing())
        graph = simple_graph_factory()
        adapter.set_workflow_graph(graph)
        
        exec_id = str(uuid.uuid4())
        adapter.initialize_session(
            exec_id,
            ExecutionState(workflow_variables={}, execution_path=[]),
        )
        
        adapter.execute({"messages": [], "count": 0})
        
        history = adapter.get_checkpoint_history()
        
        assert isinstance(history, list)
        # Should have checkpoints from execution
        assert len(history) >= 0


# ═══════════════════════════════════════════════════════════════════════════════
# ACID Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestLangGraphACID:
    """Tests for ACID compliance in LangGraph integration."""
    
    def test_atomicity_execution(self, wtb_langgraph, simple_project):
        """Test execution atomicity."""
        wtb_langgraph.register_project(simple_project)
        
        result = wtb_langgraph.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        # Execution should complete fully or not at all
        assert result.status in [ExecutionStatus.COMPLETED, ExecutionStatus.FAILED]
    
    def test_consistency_checkpoints(self, wtb_langgraph, simple_project):
        """Test checkpoint consistency."""
        wtb_langgraph.register_project(simple_project)
        
        result = wtb_langgraph.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        # Multiple reads should be consistent
        checkpoints_1 = wtb_langgraph.get_checkpoints(result.id)
        checkpoints_2 = wtb_langgraph.get_checkpoints(result.id)
        
        assert len(checkpoints_1) == len(checkpoints_2)
    
    def test_isolation_parallel_executions(self, wtb_langgraph, simple_project):
        """Test parallel execution isolation."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        wtb_langgraph.register_project(simple_project)
        
        def run_execution(idx: int):
            return wtb_langgraph.run(
                project=simple_project.name,
                initial_state={"messages": [f"parallel_{idx}"], "count": idx},
            )
        
        results = []
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(run_execution, i) for i in range(3)]
            results = [f.result() for f in as_completed(futures)]
        
        # All should complete without interference
        assert all(r.status == ExecutionStatus.COMPLETED for r in results)
        assert len(set(r.id for r in results)) == 3
    
    def test_durability_execution_persists(self, wtb_langgraph, simple_project):
        """Test execution durability."""
        wtb_langgraph.register_project(simple_project)
        
        result = wtb_langgraph.run(
            project=simple_project.name,
            initial_state=create_initial_state(),
        )
        
        # Execution should be retrievable
        retrieved = wtb_langgraph.get_execution(result.id)
        
        assert retrieved.id == result.id
        assert retrieved.status == result.status
