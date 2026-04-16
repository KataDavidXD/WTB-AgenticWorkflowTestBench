"""
End-to-end integration test for rollback state analysis.
Migrated from test_file_tracking_debug2.py.
"""

import pytest
from pathlib import Path
from typing import TypedDict
from langgraph.graph import StateGraph, END

from wtb.sdk import WTBTestBench, WorkflowProject
from wtb.domain.models import ExecutionState


class WorkflowState(TypedDict):
    query: str
    answer: str
    _output_files: dict


def process_node(state):
    query = state.get("query", "unknown")
    answer = f"Response to: {query}"
    output_files = {
        "result.txt": f"Query: {query}\nAnswer: {answer}",
    }
    return {"answer": answer, "_output_files": output_files}


def test_file_tracking_rollback_state_inspection(tmp_path):
    """Test inspecting state during rollback scenarios."""
    # 1. Setup bench
    bench = WTBTestBench.create(
        mode="development",
        data_dir=str(tmp_path),
        enable_file_tracking=True,
    )

    # 2. Register project
    builder = StateGraph(WorkflowState)
    builder.add_node("process", process_node)
    builder.add_edge("__start__", "process")
    builder.add_edge("process", END)

    project = WorkflowProject(
        name="debug",
        graph_factory=lambda: builder,
    )
    bench.register_project(project)

    # 3. Run execution
    result = bench.run(
        project="debug",
        initial_state={"query": "Test query", "answer": "", "_output_files": {}},
    )
    
    assert result.id, "Should have execution ID"

    # 4. Get checkpoints
    checkpoints = bench.get_checkpoints(result.id)
    assert len(checkpoints) > 0

    # 5. Find step=1 checkpoint
    target_cp = None
    for cp in checkpoints:
        if cp.step == 1:
            target_cp = cp
            break
            
    assert target_cp is not None, "Should find checkpoint for step 1"
    assert "_output_files" in target_cp.state_values

    # 6. Delete output file to simulate loss/change
    output_file = tmp_path / "outputs" / "result.txt"
    if output_file.exists():
        output_file.unlink()
    
    assert not output_file.exists()

    # 7. Perform rollback via internal state adapter (white-box testing)
    ctrl = bench._exec_ctrl
    state_adapter = ctrl._state_adapter

    restored_state = state_adapter.rollback(target_cp.id.value)

    # 8. Verify restored state structure
    output_files = None
    if isinstance(restored_state, ExecutionState):
        output_files = restored_state.workflow_variables.get("_output_files")
    elif isinstance(restored_state, dict):
        output_files = restored_state.get("_output_files")
        
    assert output_files is not None
    assert "result.txt" in output_files
