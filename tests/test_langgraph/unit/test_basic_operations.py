"""
Unit tests for basic LangGraph operations.

Tests fundamental LangGraph functionality:
- Graph creation and compilation
- Simple execution flow
- State initialization and updates
- Entry point and edge configuration

SOLID Compliance:
- SRP: Tests focus only on basic operations
- DIP: Uses fixtures for dependencies

Run with: pytest tests/test_langgraph/unit/test_basic_operations.py -v
"""

import pytest
from typing import Dict, Any

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from tests.test_langgraph.helpers import (
    create_initial_simple_state,
    create_initial_advanced_state,
    create_initial_branch_state,
    SimpleState,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Graph Creation Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestGraphCreation:
    """Tests for graph creation and configuration."""
    
    def test_create_empty_graph(self):
        """Test creating an empty StateGraph."""
        workflow = StateGraph(SimpleState)
        
        assert workflow is not None
        assert workflow.nodes == {}
    
    def test_add_single_node(self):
        """Test adding a single node to graph."""
        workflow = StateGraph(SimpleState)
        workflow.add_node("test_node", lambda s: {"messages": ["test"]})
        
        assert "test_node" in workflow.nodes
    
    def test_add_multiple_nodes(self, simple_graph_def):
        """Test adding multiple nodes."""
        # simple_graph_def has node_a, node_b, node_c
        assert "node_a" in simple_graph_def.nodes
        assert "node_b" in simple_graph_def.nodes
        assert "node_c" in simple_graph_def.nodes
    
    def test_set_entry_point(self, simple_graph_def):
        """Test setting entry point is configured (verify via compilation)."""
        # Entry point is configured during set_entry_point, verify it compiles
        compiled = simple_graph_def.compile()
        assert compiled is not None
    
    def test_add_edge(self):
        """Test adding edges between nodes."""
        workflow = StateGraph(SimpleState)
        workflow.add_node("node_a", lambda s: {"messages": ["A"]})
        workflow.add_node("node_b", lambda s: {"messages": ["B"]})
        
        workflow.set_entry_point("node_a")
        workflow.add_edge("node_a", "node_b")
        workflow.add_edge("node_b", END)
        
        # Verify edges are configured
        assert workflow.edges is not None
    
    def test_add_conditional_edges(self, branching_graph_def):
        """Test adding conditional edges."""
        # branching_graph_def has conditional edges after node_b
        assert "node_b" in branching_graph_def.branches
    
    def test_graph_compilation_without_checkpointer(self, simple_graph_def):
        """Test compiling graph without checkpointer."""
        compiled = simple_graph_def.compile()
        
        assert compiled is not None
    
    def test_graph_compilation_with_checkpointer(self, simple_graph_def, memory_checkpointer):
        """Test compiling graph with checkpointer."""
        compiled = simple_graph_def.compile(checkpointer=memory_checkpointer)
        
        assert compiled is not None


# ═══════════════════════════════════════════════════════════════════════════════
# Simple Execution Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSimpleExecution:
    """Tests for simple graph execution."""
    
    def test_invoke_simple_graph(self, simple_compiled_graph):
        """Test invoking simple graph with thread_id (required when checkpointer is set)."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "test-invoke-simple"}}
        
        # Execute with checkpointer config
        result = simple_compiled_graph.invoke(initial_state, config)
        
        assert result["messages"] == ["A", "B", "C"]
        assert result["count"] == 3
    
    def test_invoke_with_thread_id(self, simple_compiled_graph):
        """Test invoking with thread_id for checkpointing."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "test-thread-1"}}
        
        result = simple_compiled_graph.invoke(initial_state, config)
        
        assert result["messages"] == ["A", "B", "C"]
        assert result["count"] == 3
    
    def test_invoke_preserves_state_type(self, simple_compiled_graph):
        """Test that result maintains state structure."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "test-thread-2"}}
        
        result = simple_compiled_graph.invoke(initial_state, config)
        
        assert "messages" in result
        assert "count" in result
        assert isinstance(result["messages"], list)
        assert isinstance(result["count"], int)
    
    def test_multiple_invocations_isolated(self, simple_compiled_graph):
        """Test that multiple invocations with different threads are isolated."""
        initial_state = create_initial_simple_state()
        
        config1 = {"configurable": {"thread_id": "thread-1"}}
        config2 = {"configurable": {"thread_id": "thread-2"}}
        
        result1 = simple_compiled_graph.invoke(initial_state, config1)
        result2 = simple_compiled_graph.invoke(initial_state, config2)
        
        # Both should have same results (independent execution)
        assert result1["messages"] == result2["messages"]
        assert result1["count"] == result2["count"]
    
    def test_branching_execution_default_path(self, branching_compiled_graph):
        """Test branching graph takes default path (switch=False)."""
        initial_state = create_initial_branch_state(switch=False)
        config = {"configurable": {"thread_id": "branch-test-1"}}
        
        result = branching_compiled_graph.invoke(initial_state, config)
        
        # Should go A -> B -> C (switch=False)
        assert "C" in result["messages"]
        assert "D" not in result["messages"]
        assert "node_c" in result["path"]
    
    def test_branching_execution_alternate_path(self, branching_compiled_graph):
        """Test branching graph takes alternate path (switch=True)."""
        initial_state = create_initial_branch_state(switch=True)
        config = {"configurable": {"thread_id": "branch-test-2"}}
        
        result = branching_compiled_graph.invoke(initial_state, config)
        
        # Should go A -> B -> D (switch=True)
        assert "D" in result["messages"]
        assert "C" not in result["messages"]
        assert "node_d" in result["path"]


# ═══════════════════════════════════════════════════════════════════════════════
# State Reducer Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestStateReducers:
    """Tests for state reducer functionality."""
    
    def test_list_reducer_accumulates(self, simple_compiled_graph):
        """Test that list reducer (operator.add) accumulates values."""
        initial_state = {"messages": ["INITIAL"], "count": 0}
        config = {"configurable": {"thread_id": "reducer-test-1"}}
        
        result = simple_compiled_graph.invoke(initial_state, config)
        
        # Messages should include INITIAL + A, B, C
        assert result["messages"] == ["INITIAL", "A", "B", "C"]
    
    def test_count_overwrites(self, simple_compiled_graph):
        """Test that count (no reducer) takes last value."""
        initial_state = {"messages": [], "count": 100}
        config = {"configurable": {"thread_id": "reducer-test-2"}}
        
        result = simple_compiled_graph.invoke(initial_state, config)
        
        # Count should be incremented from each node's perspective
        # Final count = 100 + 1 + 1 + 1 = 103
        assert result["count"] == 103
    
    def test_empty_initial_state(self, simple_compiled_graph):
        """Test execution with minimal initial state."""
        initial_state = {"messages": [], "count": 0}
        config = {"configurable": {"thread_id": "reducer-test-3"}}
        
        result = simple_compiled_graph.invoke(initial_state, config)
        
        assert result["messages"] == ["A", "B", "C"]
        assert result["count"] == 3


# ═══════════════════════════════════════════════════════════════════════════════
# Streaming Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestStreaming:
    """Tests for streaming execution."""
    
    def test_stream_returns_chunks(self, simple_compiled_graph):
        """Test that stream returns node outputs as chunks."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "stream-test-1"}}
        
        chunks = list(simple_compiled_graph.stream(initial_state, config))
        
        # Should have 3 chunks (one per node)
        assert len(chunks) == 3
    
    def test_stream_chunk_contains_node_output(self, simple_compiled_graph):
        """Test that each chunk contains node name and output."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "stream-test-2"}}
        
        chunks = list(simple_compiled_graph.stream(initial_state, config))
        
        # First chunk should be node_a
        assert "node_a" in chunks[0]
        assert chunks[0]["node_a"]["messages"] == ["A"]
    
    def test_stream_order_matches_execution(self, simple_compiled_graph):
        """Test that stream order matches execution order."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "stream-test-3"}}
        
        chunks = list(simple_compiled_graph.stream(initial_state, config))
        
        node_order = [list(chunk.keys())[0] for chunk in chunks]
        assert node_order == ["node_a", "node_b", "node_c"]


# ═══════════════════════════════════════════════════════════════════════════════
# Node Execution Order Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestExecutionOrder:
    """Tests for node execution order."""
    
    def test_linear_execution_order(self, simple_compiled_graph):
        """Test that linear graph executes in order."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "order-test-1"}}
        
        result = simple_compiled_graph.invoke(initial_state, config)
        
        # Messages should be in order
        assert result["messages"] == ["A", "B", "C"]
    
    def test_branching_execution_order(self, branching_compiled_graph):
        """Test branching graph execution order."""
        initial_state = create_initial_branch_state(switch=False)
        config = {"configurable": {"thread_id": "order-test-2"}}
        
        result = branching_compiled_graph.invoke(initial_state, config)
        
        # Path should be in order: A -> B -> C
        assert result["path"] == ["node_a", "node_b", "node_c"]
    
    def test_path_tracking_alternate_branch(self, branching_compiled_graph):
        """Test path tracking on alternate branch."""
        initial_state = create_initial_branch_state(switch=True)
        config = {"configurable": {"thread_id": "order-test-3"}}
        
        result = branching_compiled_graph.invoke(initial_state, config)
        
        # Path should be: A -> B -> D
        assert result["path"] == ["node_a", "node_b", "node_d"]


# ═══════════════════════════════════════════════════════════════════════════════
# Error Handling Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestErrorHandling:
    """Tests for error handling during execution."""
    
    def test_node_exception_propagates(self, failing_graph_def, memory_checkpointer):
        """Test that node exceptions propagate correctly."""
        compiled = failing_graph_def.compile(checkpointer=memory_checkpointer)
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "error-test-1"}}
        
        with pytest.raises(ValueError, match="Intentional failure"):
            compiled.invoke(initial_state, config)
    
    def test_partial_execution_on_failure(self, failing_graph_def, memory_checkpointer):
        """Test that partial state is preserved on failure."""
        compiled = failing_graph_def.compile(checkpointer=memory_checkpointer)
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "error-test-2"}}
        
        try:
            compiled.invoke(initial_state, config)
        except ValueError:
            pass
        
        # Check state was partially preserved (node_a completed)
        state = compiled.get_state(config)
        assert "A" in state.values["messages"]


# ═══════════════════════════════════════════════════════════════════════════════
# SOLID Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSOLIDCompliance:
    """Tests verifying SOLID principle compliance."""
    
    def test_single_responsibility_nodes(self, simple_compiled_graph):
        """Each node should have single responsibility."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "solid-test-1"}}
        
        chunks = list(simple_compiled_graph.stream(initial_state, config))
        
        # Each node should produce exactly one message
        for chunk in chunks:
            for node_name, output in chunk.items():
                assert len(output.get("messages", [])) == 1
    
    def test_open_closed_graph_extension(self, simple_graph_def, memory_checkpointer):
        """Graph should be extendable without modifying existing nodes."""
        # Clone the workflow pattern for extension test
        from typing import TypedDict, Annotated
        import operator
        
        class ExtendedState(TypedDict):
            messages: Annotated[list, operator.add]
            count: int
            extra: Annotated[list, operator.add]
        
        workflow = StateGraph(ExtendedState)
        
        # Add existing nodes
        workflow.add_node("node_a", lambda s: {"messages": ["A"], "count": s["count"] + 1})
        workflow.add_node("node_b", lambda s: {"messages": ["B"], "count": s["count"] + 1})
        # Add new node without modifying existing
        workflow.add_node("node_extra", lambda s: {"extra": ["EXTRA"]})
        
        workflow.set_entry_point("node_a")
        workflow.add_edge("node_a", "node_b")
        workflow.add_edge("node_b", "node_extra")
        workflow.add_edge("node_extra", END)
        
        compiled = workflow.compile(checkpointer=memory_checkpointer)
        result = compiled.invoke(
            {"messages": [], "count": 0, "extra": []},
            {"configurable": {"thread_id": "solid-test-2"}}
        )
        
        assert result["extra"] == ["EXTRA"]
        assert result["messages"] == ["A", "B"]
    
    def test_dependency_inversion_checkpointer(self, simple_graph_def):
        """Graph should work with any checkpointer implementation."""
        # Test with MemorySaver
        memory_checkpointer = MemorySaver()
        compiled_memory = simple_graph_def.compile(checkpointer=memory_checkpointer)
        
        initial_state = create_initial_simple_state()
        result = compiled_memory.invoke(
            initial_state, 
            {"configurable": {"thread_id": "dip-test"}}
        )
        
        assert result["messages"] == ["A", "B", "C"]


# ═══════════════════════════════════════════════════════════════════════════════
# ACID Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestACIDCompliance:
    """Tests verifying ACID property compliance."""
    
    def test_atomicity_node_execution(self, simple_compiled_graph):
        """Node execution should be atomic (all or nothing)."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "acid-atom-1"}}
        
        result = simple_compiled_graph.invoke(initial_state, config)
        
        # All nodes executed completely
        assert result["count"] == 3
        assert len(result["messages"]) == 3
    
    def test_consistency_state_invariants(self, simple_compiled_graph):
        """State should maintain consistency (count = message count)."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "acid-consist-1"}}
        
        result = simple_compiled_graph.invoke(initial_state, config)
        
        # Invariant: count should equal number of messages
        assert result["count"] == len(result["messages"])
    
    def test_isolation_thread_separation(self, simple_compiled_graph):
        """Different threads should be isolated."""
        initial_state1 = {"messages": ["THREAD1"], "count": 0}
        initial_state2 = {"messages": ["THREAD2"], "count": 0}
        
        config1 = {"configurable": {"thread_id": "isolated-thread-1"}}
        config2 = {"configurable": {"thread_id": "isolated-thread-2"}}
        
        result1 = simple_compiled_graph.invoke(initial_state1, config1)
        result2 = simple_compiled_graph.invoke(initial_state2, config2)
        
        # Thread 1's initial state shouldn't appear in Thread 2
        assert "THREAD1" not in result2["messages"]
        assert "THREAD2" not in result1["messages"]
    
    def test_durability_checkpoint_persistence(self, simple_compiled_graph):
        """State should persist in checkpoints."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "acid-durable-1"}}
        
        # Execute
        simple_compiled_graph.invoke(initial_state, config)
        
        # Retrieve persisted state
        state = simple_compiled_graph.get_state(config)
        
        assert state is not None
        assert state.values["messages"] == ["A", "B", "C"]
