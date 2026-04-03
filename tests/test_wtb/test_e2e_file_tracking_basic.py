"""
End-to-end integration test for basic file tracking using WTBTestBench.
Migrated from test_file_tracking_debug.py.
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
    # Also add explicit output files
    output_files = {
        "result.txt": f"Query: {query}\nAnswer: {answer}",
        "metadata.json": '{"processed": true}',
    }
    return {"answer": answer, "_output_files": output_files}


def test_file_tracking_basic(tmp_path):
    """Test basic file tracking functionality in WTBTestBench."""
    # 1. Create bench with file tracking enabled
    bench = WTBTestBench.create(
        mode="development",
        data_dir=str(tmp_path),
        enable_file_tracking=True,
    )

    # 2. Create and register project
    builder = StateGraph(WorkflowState)
    builder.add_node("process", process_node)
    builder.add_edge("__start__", "process")
    builder.add_edge("process", END)

    project = WorkflowProject(
        name="test_file_tracking",
        graph_factory=lambda: builder,
    )
    bench.register_project(project)

    # 3. Run execution
    result = bench.run(
        project="test_file_tracking",
        initial_state={"query": "What is WTB?", "answer": "", "_output_files": {}},
    )

    assert result.status.value == "completed"
    assert result.state.workflow_variables.get("answer") == "Response to: What is WTB?"

    # 4. Check if files were tracked
    ft_service = bench._exec_ctrl._file_tracking
    assert ft_service is not None, "File tracking service should be available"
    
    stats = ft_service.get_storage_stats()
    # Expect at least some files tracked
    # stats usually returns a dict like {'total_files': N, 'total_size': M, ...}
    # Adjust assertions based on actual return structure if needed, but assuming dict or object
    # If stats is an object or dict, checking basic truthiness or properties
    assert stats, "Should have file tracking stats"

    # 5. Check output directory
    output_dir = tmp_path / "outputs"
    assert output_dir.exists()
    output_files = list(output_dir.iterdir())
    assert len(output_files) >= 2  # result.txt and metadata.json
    
    file_names = [f.name for f in output_files]
    assert "result.txt" in file_names
    assert "metadata.json" in file_names

    # 6. Check filetrack directory
    ft_dir = tmp_path / ".filetrack"
    assert ft_dir.exists()
    assert any(ft_dir.iterdir()), "FileTrack directory should not be empty"

    # 7. Check for blobs
    blobs_dir = ft_dir / "blobs"
    assert blobs_dir.exists()
    blob_files = list(blobs_dir.iterdir())
    assert len(blob_files) > 0, "Should have blob files stored"
