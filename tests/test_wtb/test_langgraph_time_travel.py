import pytest
from typing import TypedDict, Annotated, List, Dict, Any
import operator
from datetime import datetime

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from wtb.infrastructure.stores.langgraph_checkpoint_store import (
    LangGraphCheckpointStore,
    LangGraphCheckpointConfig
)
from wtb.domain.models.checkpoint import CheckpointId

# ═══════════════════════════════════════════════════════════════════════════════
# Test Definitions
# ═══════════════════════════════════════════════════════════════════════════════

class AgentState(TypedDict):
    messages: Annotated[list, operator.add]
    count: int
    path: Annotated[list, operator.add]
    run_ids: Annotated[list, operator.add]

def node_a(state):
    return {
        "messages": ["A"], 
        "count": state["count"] + 1,
        "path": ["node_a"],
        # Append current run_id from state (passed in input)
        # Note: If not present, we assume default
    }

def node_b(state):
    return {
        "messages": ["B"], 
        "count": state["count"] + 1,
        "path": ["node_b"]
    }

def node_c(state):
    return {
        "messages": ["C"], 
        "count": state["count"] + 1,
        "path": ["node_c"],
        "run_ids": state.get("run_ids", [])[-1:] if state.get("run_ids") else [] 
        # Just echo the last run_id to prove this node ran in that context?
        # Better: The node just adds its execution marker
    }

def node_d(state):
    return {
        "messages": ["D"],
        "count": state["count"] + 1,
        "path": ["node_d"]
    }

@pytest.fixture
def graph_def():
    workflow = StateGraph(AgentState)
    workflow.add_node("node_a", node_a)
    workflow.add_node("node_b", node_b)
    workflow.add_node("node_c", node_c)
    workflow.add_node("node_d", node_d)
    
    workflow.set_entry_point("node_a")
    workflow.add_edge("node_a", "node_b")
    
    # Simple routing
    def route_b(state):
        if "switch" in state and state["switch"]:
            return "node_d"
        return "node_c"
    
    workflow.add_conditional_edges("node_b", route_b)
    workflow.add_edge("node_c", END)
    workflow.add_edge("node_d", END)
    
    return workflow

# ═══════════════════════════════════════════════════════════════════════════════
# Time Travel Tests
# ═══════════════════════════════════════════════════════════════════════════════

def test_time_travel_no_future_leak_with_run_id(graph_def):
    """
    Test that rollback isolates future state by using unique run IDs.
    """
    checkpointer = MemorySaver()
    compiled_graph = graph_def.compile(checkpointer=checkpointer)
    store = LangGraphCheckpointStore(checkpointer=checkpointer, graph=compiled_graph)
    
    execution_id = "test-exec-leak-proof"
    thread_config = {"configurable": {"thread_id": f"wtb-{execution_id}"}}
    
    # 1. Run 1: A -> B -> C with run_id="RUN_1"
    # We pass run_ids as input. The nodes don't strictly use it but it's in the state.
    # We'll make Node C append "C-RUN_1" if we wanted, but checking the state is enough.
    
    initial_state = {
        "messages": [], 
        "count": 0, 
        "path": [], 
        "switch": False,
        "run_ids": ["RUN_1"]
    }
    
    result_1 = compiled_graph.invoke(initial_state, thread_config)
    
    assert "RUN_1" in result_1["run_ids"]
    assert result_1["messages"] == ["A", "B", "C"]
    
    # 2. Get Checkpoint at B
    history = list(compiled_graph.get_state_history(thread_config))
    checkpoint_b = next(snap for snap in history if snap.values.get("messages") and snap.values["messages"][-1] == "B")
    
    # 3. Rollback to B and start Run 2
    # We update state to add "RUN_2"
    config_at_b = checkpoint_b.config
    
    # We append RUN_2 to run_ids.
    # Note: run_ids is append-only.
    # At B, run_ids should be ["RUN_1"] (from start).
    # We add "RUN_2".
    compiled_graph.update_state(config_at_b, {"run_ids": ["RUN_2"]})
    
    # 4. Resume execution -> C
    result_2 = compiled_graph.invoke(None, thread_config)
    
    # 5. Verify Isolation
    # Result 2 should have ["RUN_1", "RUN_2"] in run_ids (Sequence: Start(1) -> B -> Update(2) -> C)
    assert "RUN_1" in result_2["run_ids"]
    assert "RUN_2" in result_2["run_ids"]
    
    # Messages should be A, B, C.
    assert result_2["messages"] == ["A", "B", "C"]
    
    # CRITICAL CHECK:
    # If state leaked from Run 1's future (the *first* C), 
    # and if that first C had modified something distinct (like adding a timestamp), we'd see it.
    # Since our nodes are deterministic, C looks the same.
    # But we can verify that we didn't get C twice.
    assert result_2["messages"].count("C") == 1
    
    # Verify path history of the new tip
    current_history = list(compiled_graph.get_state_history(thread_config))
    
    # We should see the state update event in history
    # Sequence (reverse): C (End) -> Update (RUN_2) -> B -> A -> Start
    
    has_run_2 = False
    for snap in current_history:
        if "RUN_2" in snap.values.get("run_ids", []):
            has_run_2 = True
            
    assert has_run_2, "Should have RUN_2 in history"

def test_branching_no_leak(graph_def):
    """
    Test branching to a different node ensures isolation.
    """
    checkpointer = MemorySaver()
    compiled_graph = graph_def.compile(checkpointer=checkpointer)
    
    execution_id = "test-exec-branch"
    thread_config = {"configurable": {"thread_id": f"wtb-{execution_id}"}}
    
    # 1. Run Path 1: A -> B -> C
    compiled_graph.invoke({
        "messages": [], "count": 0, "path": [], "switch": False, "run_ids": []
    }, thread_config)
    
    # Get Checkpoint at B
    history = list(compiled_graph.get_state_history(thread_config))
    checkpoint_b = next(snap for snap in history if snap.values.get("messages") and snap.values["messages"][-1] == "B")
    
    # 2. Branch: A -> B -> D
    config_at_b = checkpoint_b.config
    
    # Update state at B to set switch=True
    compiled_graph.update_state(config_at_b, {"switch": True})
    
    # Resume -> D
    result = compiled_graph.invoke(None, thread_config)
    
    # Should contain D, NOT C
    assert "D" in result["messages"]
    assert "C" not in result["messages"]
    assert "node_d" in result["path"]
    
    # Verify strict isolation
    assert result["messages"] == ["A", "B", "D"]
