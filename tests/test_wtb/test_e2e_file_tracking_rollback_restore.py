"""
End-to-end integration test for rollback file restore functionality.
Migrated from test_file_tracking_debug3.py.
"""

import pytest
from pathlib import Path
from typing import TypedDict
from langgraph.graph import StateGraph, END

from wtb.sdk import WTBTestBench, WorkflowProject


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


def test_file_tracking_rollback_restore_direct(tmp_path):
    """Test file restoration when calling rollback directly on controller."""
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

    # 4. Find checkpoint
    checkpoints = bench.get_checkpoints(result.id)
    target_cp = None
    for cp in checkpoints:
        if cp.step == 1:
            target_cp = cp
            break
            
    assert target_cp is not None

    # 5. Delete output file
    output_file = tmp_path / "outputs" / "result.txt"
    if output_file.exists():
        output_file.unlink()
        
    assert not output_file.exists()

    # 6. Check controller state
    ctrl = bench._exec_ctrl
    assert ctrl._file_tracking.is_available()

    # 7. Call rollback on controller
    execution = ctrl.rollback(result.id, target_cp.id.value)
    
    # 8. Verify success
    # Status might be paused after rollback depending on where it stopped
    assert execution.status.value in ["completed", "paused"]
    # Actually rollback returns the execution in its new state. If it was completed, rollback makes it effectively completed at that point unless resumed?
    # WTBTestBench.rollback usually returns RollbackResult, but ctrl.rollback returns execution.
    
    # Check if file restored
    assert output_file.exists(), "Output file should be restored"
    content = output_file.read_text()
    assert "Query: Test query" in content
    assert "Answer: Response to: Test query" in content
    
    # Check restore status metadata
    restore_status = execution.state.workflow_variables.get('_file_restore_status')
    # Depending on implementation, this might be present
    # The original debug script printed it, assuming it exists
