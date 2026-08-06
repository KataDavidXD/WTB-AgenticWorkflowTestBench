"""
WTB Testing Fixtures

Provides common test fixtures for testing adapters and services.

Architecture Decision:
- These helpers are part of WTB (not just tests) because:
  1. Users need them to test their own workflows
  2. Ensures consistent testing patterns across all adapter implementations
  3. Follows DIP: tests depend on abstractions from wtb.testing
"""

import operator
from typing import Annotated, TypedDict

from wtb.domain.interfaces.state_adapter import (
    CheckpointInfo,
    CheckpointTrigger,
    IStateAdapter,
    NodeBoundaryInfo,
)
from wtb.domain.models.workflow import ExecutionState

# ═══════════════════════════════════════════════════════════════════════════════
# Minimal Graph for Testing LangGraph Adapter
# ═══════════════════════════════════════════════════════════════════════════════


class MinimalState(TypedDict):
    """
    Minimal LangGraph state for testing.
    
    Contains only the essential fields needed for checkpoint testing.
    """
    value: int
    messages: Annotated[list, operator.add]
    route: str | None  # For conditional routing


def _node_a(state: MinimalState) -> dict:
    """Test node A - increments value."""
    return {"messages": ["a_executed"], "value": state["value"] + 1}


def _node_b(state: MinimalState) -> dict:
    """Test node B - increments value."""
    return {"messages": ["b_executed"], "value": state["value"] + 1}


def _node_c(state: MinimalState) -> dict:
    """Test node C - for conditional branch."""
    return {"messages": ["c_executed"], "value": state["value"] + 10}


def _route_decision(state: MinimalState) -> str:
    """Router function for conditional edge."""
    if state.get("route") == "c":
        return "node_c"
    return "node_b"


def create_minimal_graph():
    """
    Create a minimal StateGraph for testing LangGraph adapter.
    
    Graph structure:
        START -> node_a -> node_b -> END
    
    Returns:
        StateGraph: Uncompiled graph ready to be set on adapter
        
    Example:
        >>> from wtb.testing import create_minimal_graph
        >>> from wtb.infrastructure.adapters import LangGraphStateAdapter
        >>> 
        >>> adapter = LangGraphStateAdapter(config)
        >>> adapter.set_workflow_graph(create_minimal_graph())
    """
    try:
        from langgraph.graph import END, StateGraph
    except ImportError:
        raise ImportError(
            "langgraph is required for create_minimal_graph(). "
            "Install with: pip install langgraph"
        )
    
    workflow = StateGraph(MinimalState)
    workflow.add_node("node_a", _node_a)
    workflow.add_node("node_b", _node_b)
    workflow.set_entry_point("node_a")
    workflow.add_edge("node_a", "node_b")
    workflow.add_edge("node_b", END)
    
    return workflow


def create_conditional_graph():
    """
    Create a StateGraph with conditional edges for testing branching.
    
    Graph structure:
        START -> node_a -> [conditional] -> node_b -> END
                                         -> node_c -> END
    
    The routing is based on state["route"]:
        - route == "c" -> node_c
        - otherwise    -> node_b
    
    Returns:
        StateGraph: Uncompiled graph with conditional edges
        
    Example:
        >>> from wtb.testing import create_conditional_graph
        >>> graph = create_conditional_graph()
        >>> # Route to node_c
        >>> result = compiled.invoke({"value": 0, "messages": [], "route": "c"})
        >>> assert "c_executed" in result["messages"]
    """
    try:
        from langgraph.graph import END, StateGraph
    except ImportError:
        raise ImportError(
            "langgraph is required for create_conditional_graph(). "
            "Install with: pip install langgraph"
        )
    
    workflow = StateGraph(MinimalState)
    workflow.add_node("node_a", _node_a)
    workflow.add_node("node_b", _node_b)
    workflow.add_node("node_c", _node_c)
    
    workflow.set_entry_point("node_a")
    workflow.add_conditional_edges(
        "node_a",
        _route_decision,
        {
            "node_b": "node_b",
            "node_c": "node_c",
        }
    )
    workflow.add_edge("node_b", END)
    workflow.add_edge("node_c", END)
    
    return workflow


# ═══════════════════════════════════════════════════════════════════════════════
# Unified State Adapter Test Mixin
# ═══════════════════════════════════════════════════════════════════════════════


class StateAdapterTestMixin:
    """
    Mixin providing standard tests for IStateAdapter implementations.
    
    Ensures all adapters conform to the same interface and behavior.
    Follows LSP: any adapter should pass these tests.
    
    Usage:
        class TestMyAdapter(StateAdapterTestMixin, unittest.TestCase):
            def create_adapter(self) -> IStateAdapter:
                return MyAdapter()
    """
    
    def create_adapter(self) -> IStateAdapter:
        """Override this to create your adapter instance."""
        raise NotImplementedError("Subclass must implement create_adapter()")
    
    # ═══════════════════════════════════════════════════════════════════════
    # Session Tests
    # ═══════════════════════════════════════════════════════════════════════
    
    def test_initialize_session_returns_string(self):
        """Session ID should be a string."""
        adapter = self.create_adapter()
        session_id = adapter.initialize_session("test-exec", ExecutionState())
        
        assert isinstance(session_id, str), f"Expected str, got {type(session_id)}"
        assert len(session_id) > 0, "Session ID should not be empty"
    
    def test_get_current_session_id_returns_string_or_none(self):
        """Current session ID should be str or None."""
        adapter = self.create_adapter()
        
        # Before initialization
        assert adapter.get_current_session_id() is None
        
        # After initialization
        adapter.initialize_session("test-exec", ExecutionState())
        session_id = adapter.get_current_session_id()
        
        assert isinstance(session_id, str), f"Expected str, got {type(session_id)}"
    
    def test_set_current_session_returns_bool(self):
        """set_current_session should return bool."""
        adapter = self.create_adapter()
        session_id = adapter.initialize_session("test-exec", ExecutionState())
        
        result = adapter.set_current_session(session_id)
        
        assert isinstance(result, bool)
        assert result is True
    
    # ═══════════════════════════════════════════════════════════════════════
    # Checkpoint Tests - These may need graph setup for LangGraph
    # ═══════════════════════════════════════════════════════════════════════
    
    def test_save_checkpoint_returns_string(self):
        """Checkpoint ID should be a string."""
        adapter = self.create_adapter()
        adapter.initialize_session("test-exec", ExecutionState())
        
        # Note: LangGraph adapter needs graph set before this works
        try:
            checkpoint_id = adapter.save_checkpoint(
                state=ExecutionState(),
                node_id="node_a",
                trigger=CheckpointTrigger.NODE_START,
            )
            
            assert isinstance(checkpoint_id, str), f"Expected str, got {type(checkpoint_id)}"
            assert len(checkpoint_id) > 0, "Checkpoint ID should not be empty"
        except RuntimeError as e:
            if "Graph not set" in str(e):
                # LangGraph adapter needs graph - skip this test
                import pytest
                pytest.skip("LangGraph adapter requires graph setup")
            raise
    
    # ═══════════════════════════════════════════════════════════════════════
    # Node Boundary Tests
    # ═══════════════════════════════════════════════════════════════════════
    
    def test_mark_node_started_returns_string(self):
        """Boundary ID should be a string."""
        adapter = self.create_adapter()
        adapter.initialize_session("test-exec", ExecutionState())
        
        try:
            checkpoint_id = adapter.save_checkpoint(
                state=ExecutionState(),
                node_id="node_a",
                trigger=CheckpointTrigger.NODE_START,
            )
            
            boundary_id = adapter.mark_node_started(
                node_id="node_a",
                entry_checkpoint_id=checkpoint_id,
            )
            
            assert isinstance(boundary_id, str), f"Expected str, got {type(boundary_id)}"
        except RuntimeError as e:
            if "Graph not set" in str(e):
                import pytest
                pytest.skip("LangGraph adapter requires graph setup")
            raise
    
    def test_mark_node_completed_accepts_string(self):
        """mark_node_completed should accept string IDs."""
        adapter = self.create_adapter()
        adapter.initialize_session("test-exec", ExecutionState())
        
        try:
            entry_cp = adapter.save_checkpoint(
                state=ExecutionState(),
                node_id="node_a",
                trigger=CheckpointTrigger.NODE_START,
            )
            
            boundary_id = adapter.mark_node_started(
                node_id="node_a",
                entry_checkpoint_id=entry_cp,
            )
            
            exit_cp = adapter.save_checkpoint(
                state=ExecutionState(),
                node_id="node_a",
                trigger=CheckpointTrigger.NODE_END,
            )
            
            # Should not raise
            adapter.mark_node_completed(
                node_id="node_a",
                exit_checkpoint_id=exit_cp,
            )
        except RuntimeError as e:
            if "Graph not set" in str(e):
                import pytest
                pytest.skip("LangGraph adapter requires graph setup")
            raise
    
    # ═══════════════════════════════════════════════════════════════════════
    # CheckpointInfo Format Tests
    # ═══════════════════════════════════════════════════════════════════════
    
    def test_checkpoint_info_has_string_id(self):
        """CheckpointInfo.id should be a string."""
        adapter = self.create_adapter()
        session_id = adapter.initialize_session("test-exec", ExecutionState())
        
        try:
            adapter.save_checkpoint(
                state=ExecutionState(),
                node_id="node_a",
                trigger=CheckpointTrigger.NODE_START,
            )
            
            checkpoints = adapter.get_checkpoints(session_id)
            
            for cp in checkpoints:
                assert isinstance(cp, CheckpointInfo)
                assert isinstance(cp.id, str)
                assert isinstance(cp.step, int)
                assert isinstance(cp.trigger_type, CheckpointTrigger)
        except RuntimeError as e:
            if "Graph not set" in str(e):
                import pytest
                pytest.skip("LangGraph adapter requires graph setup")
            raise
    
    # ═══════════════════════════════════════════════════════════════════════
    # NodeBoundaryInfo Format Tests
    # ═══════════════════════════════════════════════════════════════════════
    
    def test_node_boundary_info_has_string_ids(self):
        """NodeBoundaryInfo should have string IDs."""
        adapter = self.create_adapter()
        session_id = adapter.initialize_session("test-exec", ExecutionState())
        
        try:
            entry_cp = adapter.save_checkpoint(
                state=ExecutionState(),
                node_id="node_a",
                trigger=CheckpointTrigger.NODE_START,
            )
            
            boundary_id = adapter.mark_node_started(
                node_id="node_a",
                entry_checkpoint_id=entry_cp,
            )
            
            boundaries = adapter.get_node_boundaries(session_id)
            
            for b in boundaries:
                assert isinstance(b, NodeBoundaryInfo)
                assert isinstance(b.id, str)
                assert isinstance(b.node_id, str)
                assert isinstance(b.entry_checkpoint_id, str)
        except RuntimeError as e:
            if "Graph not set" in str(e):
                import pytest
                pytest.skip("LangGraph adapter requires graph setup")
            raise
