"""
Shared helper functions and type definitions for LangGraph tests.

This module contains non-fixture utilities that can be directly imported.
Pytest fixtures remain in conftest.py for auto-discovery.
"""

import operator
from typing import TypedDict, Annotated, Dict, Any, List


# ═══════════════════════════════════════════════════════════════════════════════
# State Definitions
# ═══════════════════════════════════════════════════════════════════════════════


class SimpleState(TypedDict):
    """Simple state for basic tests."""
    messages: Annotated[list, operator.add]
    count: int


class AdvancedState(TypedDict):
    """Advanced state for complex tests."""
    messages: Annotated[list, operator.add]
    count: int
    path: Annotated[list, operator.add]
    variables: Dict[str, Any]
    execution_id: str
    checkpoint_ids: Annotated[list, operator.add]


class ParallelState(TypedDict):
    """State for parallel execution tests."""
    results: Annotated[list, operator.add]
    node_order: Annotated[list, operator.add]
    parallel_count: int


class BranchState(TypedDict):
    """State for branching tests."""
    messages: Annotated[list, operator.add]
    count: int
    path: Annotated[list, operator.add]
    switch: bool
    run_ids: Annotated[list, operator.add]


class FileTrackingState(TypedDict):
    """State for file tracking integration tests."""
    messages: Annotated[list, operator.add]
    count: int
    files_processed: Annotated[list, operator.add]
    commit_ids: Annotated[list, operator.add]


# ═══════════════════════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════════════════════


def create_initial_simple_state() -> SimpleState:
    """Create initial simple state."""
    return {"messages": [], "count": 0}


def create_initial_advanced_state(execution_id: str = "test-exec") -> AdvancedState:
    """Create initial advanced state."""
    return {
        "messages": [],
        "count": 0,
        "path": [],
        "variables": {},
        "execution_id": execution_id,
        "checkpoint_ids": [],
    }


def create_initial_branch_state(switch: bool = False) -> BranchState:
    """Create initial branch state."""
    return {
        "messages": [],
        "count": 0,
        "path": [],
        "switch": switch,
        "run_ids": [],
    }


def create_initial_file_tracking_state() -> FileTrackingState:
    """Create initial file tracking state."""
    return {
        "messages": [],
        "count": 0,
        "files_processed": [],
        "commit_ids": [],
    }


def create_initial_parallel_state() -> ParallelState:
    """Create initial parallel state."""
    return {
        "results": [],
        "node_order": [],
        "parallel_count": 0,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Node Functions (for tests that need to define custom graphs)
# ═══════════════════════════════════════════════════════════════════════════════


def simple_node_a(state: SimpleState) -> Dict[str, Any]:
    """Simple node A."""
    return {"messages": ["A"], "count": state["count"] + 1}


def simple_node_b(state: SimpleState) -> Dict[str, Any]:
    """Simple node B."""
    return {"messages": ["B"], "count": state["count"] + 1}


def simple_node_c(state: SimpleState) -> Dict[str, Any]:
    """Simple node C."""
    return {"messages": ["C"], "count": state["count"] + 1}


def advanced_node_a(state: AdvancedState) -> Dict[str, Any]:
    """Advanced node A with path tracking."""
    return {
        "messages": ["A"],
        "count": state["count"] + 1,
        "path": ["node_a"],
    }


def advanced_node_b(state: AdvancedState) -> Dict[str, Any]:
    """Advanced node B with path tracking."""
    return {
        "messages": ["B"],
        "count": state["count"] + 1,
        "path": ["node_b"],
    }


def advanced_node_c(state: AdvancedState) -> Dict[str, Any]:
    """Advanced node C with path tracking."""
    return {
        "messages": ["C"],
        "count": state["count"] + 1,
        "path": ["node_c"],
    }


def advanced_node_d(state: AdvancedState) -> Dict[str, Any]:
    """Advanced node D (alternate branch)."""
    return {
        "messages": ["D"],
        "count": state["count"] + 1,
        "path": ["node_d"],
    }


def failing_node(state: SimpleState) -> Dict[str, Any]:
    """Node that always fails."""
    raise ValueError("Intentional failure for testing")


def file_processing_node(state: FileTrackingState) -> Dict[str, Any]:
    """Node that simulates file processing."""
    import uuid
    file_id = str(uuid.uuid4())[:8]
    return {
        "messages": [f"Processed file {file_id}"],
        "count": state["count"] + 1,
        "files_processed": [f"file_{file_id}.txt"],
    }


def conditional_fail_node(state: AdvancedState) -> Dict[str, Any]:
    """Node that fails based on state."""
    if state.get("variables", {}).get("should_fail", False):
        raise ValueError("Conditional failure triggered")
    return {
        "messages": ["Conditional"],
        "count": state["count"] + 1,
        "path": ["conditional_node"],
    }


def parallel_node_1(state: ParallelState) -> Dict[str, Any]:
    """Parallel node 1."""
    return {
        "results": [{"node": "parallel_1", "value": 10}],
        "node_order": ["parallel_1"],
    }


def parallel_node_2(state: ParallelState) -> Dict[str, Any]:
    """Parallel node 2."""
    return {
        "results": [{"node": "parallel_2", "value": 20}],
        "node_order": ["parallel_2"],
    }


def aggregator_node(state: ParallelState) -> Dict[str, Any]:
    """Aggregator node after parallel execution."""
    total = sum(r.get("value", 0) for r in state["results"])
    return {
        "results": [{"node": "aggregator", "total": total}],
        "node_order": ["aggregator"],
        "parallel_count": len([r for r in state["results"] if "value" in r]),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Routing Functions
# ═══════════════════════════════════════════════════════════════════════════════


def route_by_switch(state: BranchState) -> str:
    """Route based on switch flag."""
    from langgraph.graph import END
    if state.get("switch", False):
        return "node_d"
    return "node_c"


def route_by_count(state: AdvancedState) -> str:
    """Route based on count value."""
    from langgraph.graph import END
    if state["count"] >= 3:
        return END
    return "node_b"
