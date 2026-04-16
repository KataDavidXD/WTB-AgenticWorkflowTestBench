"""
End-to-end integration test for full file tracking with rollback scenario.
Migrated from test_file_tracking_rollback.py.
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


# Use a closure or class for counter state in test to avoid global state issues across tests
class ProcessNode:
    def __init__(self):
        self.counter = 0

    def __call__(self, state):
        self.counter += 1
        query = state.get("query", "unknown")
        answer = f"Response #{self.counter} to: {query}"
        output_files = {
            "result.txt": f"Query: {query}\nAnswer: {answer}\nCounter: {self.counter}",
        }
        return {"answer": answer, "_output_files": output_files}


def test_file_tracking_rollback_full_scenario(tmp_path):
    """Test full rollback scenario including file deletion and restoration via SDK."""
    # 1. Setup bench
    bench = WTBTestBench.create(
        mode="development",
        data_dir=str(tmp_path),
        enable_file_tracking=True,
    )

    # 2. Setup graph with stateful node
    process_node = ProcessNode()
    builder = StateGraph(WorkflowState)
    builder.add_node("process", process_node)
    builder.add_edge("__start__", "process")
    builder.add_edge("process", END)

    project = WorkflowProject(
        name="test_rollback",
        graph_factory=lambda: builder,
    )
    bench.register_project(project)

    # 3. First execution
    result1 = bench.run(
        project="test_rollback",
        initial_state={"query": "First query", "answer": "", "_output_files": {}},
    )
    
    output_dir = tmp_path / "outputs"
    result_file = output_dir / "result.txt"
    assert result_file.exists()
    content_run1 = result_file.read_text()
    assert "Counter: 1" in content_run1

    # 4. Get checkpoints and target
    checkpoints = bench.get_checkpoints(result1.id)
    target_checkpoint = None
    for cp in checkpoints:
        if cp.step == 1:
            target_checkpoint = cp
            break
    
    assert target_checkpoint is not None

    # 5. Delete output file
    if result_file.exists():
        result_file.unlink()
    assert not result_file.exists()

    # 6. Rollback via SDK
    checkpoint_id = target_checkpoint.id.value
    rollback_result = bench.rollback(result1.id, checkpoint_id)
    
    assert rollback_result.success, f"Rollback failed: {rollback_result.error}"

    # 7. Check file restoration
    assert result_file.exists(), "File should be restored"
    content_restored = result_file.read_text()
    assert content_restored == content_run1
    
    # 8. Check execution state
    exec_state = bench.get_state(result1.id)
    restore_status = exec_state.workflow_variables.get("_file_restore_status", {})
    # Assert specific status details if known, otherwise just that it exists/ran
    
    # 9. Verify storage stats
    ft_service = bench._exec_ctrl._file_tracking
    stats = ft_service.get_storage_stats()
    assert stats
