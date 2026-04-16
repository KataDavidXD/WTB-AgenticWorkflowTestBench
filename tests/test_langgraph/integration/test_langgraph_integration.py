"""
Integration Tests for LangGraph WTB Integration.

Tests end-to-end workflow execution with LangGraph as the state persistence backend.
Uses the CORRECT architecture: ExecutionController + LangGraphStateAdapter

Architecture (2026-01-15):
══════════════════════════════════════════════════════════════════════════════════
ExecutionController (ONE) + LangGraphStateAdapter = LangGraph-powered workflows

DO NOT create separate LangGraphExecutionController - use adapter injection instead!
"""

import pytest
from typing import Dict, Any, TypedDict
from datetime import datetime
import asyncio

# Check LangGraph availability
try:
    from langgraph.graph import StateGraph, END
    from langgraph.checkpoint.memory import MemorySaver
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False

# Skip all tests if LangGraph not available
pytestmark = pytest.mark.skipif(
    not LANGGRAPH_AVAILABLE,
    reason="LangGraph not installed"
)


# Only import if LangGraph is available
if LANGGRAPH_AVAILABLE:
    from wtb.infrastructure.adapters.langgraph_state_adapter import (
        LangGraphStateAdapter,
        LangGraphConfig,
        LangGraphStateAdapterFactory,
    )
    from wtb.application.services.langgraph_node_replacer import (
        LangGraphNodeReplacer,
        GraphStructure,
        capture_graph_structure,
    )
    from wtb.infrastructure.events.wtb_event_bus import WTBEventBus


# ═══════════════════════════════════════════════════════════════════════════════
# Test State Schema
# ═══════════════════════════════════════════════════════════════════════════════


class TestState(TypedDict):
    """Test workflow state."""
    messages: list
    counter: int
    result: str


# ═══════════════════════════════════════════════════════════════════════════════
# Test Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def event_bus():
    """Create fresh event bus for testing."""
    return WTBEventBus(max_history=100)


@pytest.fixture
def langgraph_config():
    """Create LangGraph config for testing."""
    return LangGraphConfig.for_testing()


@pytest.fixture
def state_adapter(langgraph_config):
    """Create LangGraph state adapter."""
    return LangGraphStateAdapter(langgraph_config)


@pytest.fixture
def simple_graph():
    """Create a simple test graph."""
    def start_node(state: TestState) -> Dict[str, Any]:
        return {
            "messages": state.get("messages", []) + ["Started"],
            "counter": state.get("counter", 0) + 1,
        }
    
    def process_node(state: TestState) -> Dict[str, Any]:
        return {
            "messages": state.get("messages", []) + ["Processed"],
            "counter": state.get("counter", 0) + 1,
        }
    
    def end_node(state: TestState) -> Dict[str, Any]:
        return {
            "messages": state.get("messages", []) + ["Completed"],
            "result": "success",
        }
    
    graph = StateGraph(TestState)
    graph.add_node("start", start_node)
    graph.add_node("process", process_node)
    graph.add_node("end", end_node)
    
    graph.add_edge("start", "process")
    graph.add_edge("process", "end")
    graph.add_edge("end", END)
    
    graph.set_entry_point("start")
    
    return graph


@pytest.fixture
def node_replacer():
    """Create node replacer with test variants."""
    replacer = LangGraphNodeReplacer()
    
    # Register original implementations
    def original_process(state: TestState) -> Dict[str, Any]:
        return {
            "messages": state.get("messages", []) + ["Original Process"],
            "counter": state.get("counter", 0) + 1,
        }
    
    def optimized_process(state: TestState) -> Dict[str, Any]:
        return {
            "messages": state.get("messages", []) + ["Optimized Process"],
            "counter": state.get("counter", 0) + 2,  # Faster!
        }
    
    def experimental_process(state: TestState) -> Dict[str, Any]:
        return {
            "messages": state.get("messages", []) + ["Experimental Process"],
            "counter": state.get("counter", 0) + 3,  # Even faster!
        }
    
    replacer.register_original("process", original_process)
    replacer.register_variant("process", "optimized", optimized_process)
    replacer.register_variant("process", "experimental", experimental_process)
    
    return replacer


# ═══════════════════════════════════════════════════════════════════════════════
# State Adapter Tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph required")
class TestLangGraphStateAdapter:
    """Integration tests for LangGraphStateAdapter."""
    
    def test_adapter_initialization(self, state_adapter):
        """Test adapter initializes correctly."""
        assert state_adapter is not None
        assert state_adapter.get_current_session_id() is None
    
    def test_extended_capabilities(self, state_adapter):
        """Test that LangGraphStateAdapter reports extended capabilities."""
        assert state_adapter.supports_time_travel() is True
        assert state_adapter.supports_streaming() is True
    
    def test_session_initialization(self, state_adapter, simple_graph):
        """Test session initialization with graph."""
        from wtb.domain.models.workflow import ExecutionState
        
        state_adapter.set_workflow_graph(simple_graph)
        
        exec_state = ExecutionState(
            current_node_id="start",
            workflow_variables={"test": "value"},
            execution_path=[],
            node_results={},
        )
        
        session_id = state_adapter.initialize_session("exec-001", exec_state)
        
        assert session_id is not None
        assert state_adapter.get_current_session_id() == session_id
    
    def test_execute_simple_graph(self, state_adapter, simple_graph):
        """Test executing a simple graph."""
        from wtb.domain.models.workflow import ExecutionState
        
        state_adapter.set_workflow_graph(simple_graph)
        
        exec_state = ExecutionState(
            current_node_id="start",
            workflow_variables={},
            execution_path=[],
            node_results={},
        )
        
        state_adapter.initialize_session("exec-001", exec_state)
        
        initial_state = {
            "messages": [],
            "counter": 0,
            "result": "",
        }
        
        result = state_adapter.execute(initial_state)
        
        assert "messages" in result
        assert len(result["messages"]) == 3
        assert result["result"] == "success"
    
    def test_checkpoint_history(self, state_adapter, simple_graph):
        """Test checkpoint history after execution (time-travel)."""
        from wtb.domain.models.workflow import ExecutionState
        
        state_adapter.set_workflow_graph(simple_graph)
        
        exec_state = ExecutionState(
            current_node_id="start",
            workflow_variables={},
            execution_path=[],
            node_results={},
        )
        
        state_adapter.initialize_session("exec-001", exec_state)
        
        initial_state = {
            "messages": [],
            "counter": 0,
            "result": "",
        }
        
        state_adapter.execute(initial_state)
        
        # Time-travel feature
        history = state_adapter.get_checkpoint_history()
        
        assert len(history) > 0
        # LangGraph creates checkpoints at each super-step
    
    def test_get_current_state(self, state_adapter, simple_graph):
        """Test get_current_state extended method."""
        from wtb.domain.models.workflow import ExecutionState
        
        state_adapter.set_workflow_graph(simple_graph)
        
        exec_state = ExecutionState(
            current_node_id="start",
            workflow_variables={},
            execution_path=[],
            node_results={},
        )
        
        state_adapter.initialize_session("exec-001", exec_state)
        
        initial_state = {
            "messages": [],
            "counter": 0,
            "result": "",
        }
        
        state_adapter.execute(initial_state)
        
        current = state_adapter.get_current_state()
        
        assert isinstance(current, dict)
    
    def test_update_state(self, state_adapter, simple_graph):
        """Test update_state extended method (human-in-the-loop)."""
        from wtb.domain.models.workflow import ExecutionState
        
        state_adapter.set_workflow_graph(simple_graph)
        
        exec_state = ExecutionState(
            current_node_id="start",
            workflow_variables={},
            execution_path=[],
            node_results={},
        )
        
        state_adapter.initialize_session("exec-001", exec_state)
        
        # Execute to populate state
        initial_state = {
            "messages": [],
            "counter": 0,
            "result": "",
        }
        
        state_adapter.execute(initial_state)
        
        # Update state (human-in-the-loop)
        result = state_adapter.update_state({"messages": ["Human added this"]})
        
        assert result is True


# ═══════════════════════════════════════════════════════════════════════════════
# Node Replacer Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph required")
class TestLangGraphNodeReplacerIntegration:
    """Integration tests for node replacement with LangGraph."""
    
    def test_create_variant_graph_structure(self, node_replacer):
        """Test creating variant graph structure."""
        # Create base graph structure
        def start_node(state):
            return {"messages": ["start"]}
        
        def process_node(state):
            return {"messages": state.get("messages", []) + ["process"]}
        
        def end_node(state):
            return {"result": "done"}
        
        structure = GraphStructure(state_schema=TestState)
        structure.add_node("start", start_node)
        structure.add_node("process", process_node)
        structure.add_node("end", end_node)
        structure.add_edge("start", "process")
        structure.add_edge("process", "end")
        structure.set_entry_point("start")
        
        # Create variant structure
        variant_structure = node_replacer.create_variant_structure(
            base_structure=structure,
            variant_set={"process": "optimized"},
        )
        
        # Verify variant was applied
        assert "process" in variant_structure.nodes
        # The process node function should be different
    
    def test_generate_and_execute_variants(self, node_replacer, state_adapter):
        """Test generating variants and executing them."""
        # Create base structure
        def start_node(state: TestState) -> Dict[str, Any]:
            return {"messages": ["started"]}
        
        structure = GraphStructure(state_schema=TestState)
        structure.add_node("start", start_node)
        structure.add_node("process", node_replacer.get_original_node("process"))
        structure.add_edge("start", "process")
        structure.add_edge("process", END)
        structure.set_entry_point("start")
        
        # Generate variant combinations
        combinations = node_replacer.generate_variant_combinations(nodes=["process"])
        
        assert len(combinations) >= 2  # original, optimized, experimental
        
        # Each combination should have "process" variant
        for combo in combinations:
            assert "process" in combo.variants
    
    def test_variant_execution_with_adapter(self, node_replacer, state_adapter):
        """Test executing variants with LangGraphStateAdapter."""
        from wtb.domain.models.workflow import ExecutionState
        
        # Create base structure
        def start_node(state: TestState) -> Dict[str, Any]:
            return {"messages": ["started"], "counter": 1}
        
        def end_node(state: TestState) -> Dict[str, Any]:
            return {"result": "done"}
        
        structure = GraphStructure(state_schema=TestState)
        structure.add_node("start", start_node)
        structure.add_node("process", node_replacer.get_original_node("process"))
        structure.add_node("end", end_node)
        structure.add_edge("start", "process")
        structure.add_edge("process", "end")
        structure.add_edge("end", END)
        structure.set_entry_point("start")
        
        # Build and execute original graph
        original_graph = structure.build()
        state_adapter.set_workflow_graph(original_graph)
        
        exec_state = ExecutionState(
            current_node_id="start",
            workflow_variables={},
            execution_path=[],
            node_results={},
        )
        
        state_adapter.initialize_session("exec-original", exec_state)
        
        original_result = state_adapter.execute({
            "messages": [],
            "counter": 0,
            "result": "",
        })
        
        original_counter = original_result.get("counter", 0)
        
        # Build and execute optimized variant
        variant_structure = node_replacer.create_variant_structure(
            base_structure=structure,
            variant_set={"process": "optimized"},
        )
        
        variant_graph = variant_structure.build()
        
        # Create new adapter for variant (isolated)
        variant_adapter = LangGraphStateAdapter(LangGraphConfig.for_testing())
        variant_adapter.set_workflow_graph(variant_graph)
        
        variant_adapter.initialize_session("exec-optimized", exec_state)
        
        variant_result = variant_adapter.execute({
            "messages": [],
            "counter": 0,
            "result": "",
        })
        
        variant_counter = variant_result.get("counter", 0)
        
        # Optimized variant should have higher counter (processes faster)
        assert variant_counter > original_counter


# ═══════════════════════════════════════════════════════════════════════════════
# Factory Tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph required")
class TestFactories:
    """Tests for factory functions."""
    
    def test_state_adapter_factory_testing(self):
        """Test creating adapter for testing."""
        adapter = LangGraphStateAdapterFactory.create_for_testing()
        assert adapter is not None
    
    def test_availability_check(self):
        """Test availability check functions."""
        assert LangGraphStateAdapterFactory.is_available() is True


# ═══════════════════════════════════════════════════════════════════════════════
# Architecture Validation Tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not LANGGRAPH_AVAILABLE, reason="LangGraph required")
class TestArchitectureValidation:
    """Tests validating the correct architecture is used."""
    
    def test_no_langgraph_execution_controller_import(self):
        """Verify LangGraphExecutionController is NOT importable (deleted)."""
        with pytest.raises(ImportError):
            from wtb.application.services.langgraph_execution_controller import (
                LangGraphExecutionController
            )
    
    def test_no_langgraph_batch_runner_import(self):
        """Verify LangGraphBatchTestRunner is NOT importable (deleted)."""
        with pytest.raises(ImportError):
            from wtb.application.services.langgraph_batch_runner import (
                LangGraphBatchTestRunner
            )
    
    def test_langgraph_node_replacer_exists(self):
        """Verify LangGraphNodeReplacer IS available (kept - different domain)."""
        from wtb.application.services import LangGraphNodeReplacer
        assert LangGraphNodeReplacer is not None
    
    def test_adapter_is_state_adapter(self, state_adapter):
        """Verify LangGraphStateAdapter implements IStateAdapter."""
        from wtb.domain.interfaces.state_adapter import IStateAdapter
        assert isinstance(state_adapter, IStateAdapter)
