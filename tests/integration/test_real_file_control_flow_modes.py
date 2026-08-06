"""Real file rollback/fork/resume coverage across WTB execution modes."""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any, TypedDict

import pytest
from langgraph.graph import END, StateGraph

from wtb.sdk import ExecutionConfig, RayConfig, WorkflowProject, WTBTestBench


class FileState(TypedDict, total=False):
    messages: list[str]
    suffix: str
    result: str
    _output_files: dict[str, str]
    _variant_config: dict[str, str]


def create_file_control_graph():
    def draft(state: FileState) -> dict[str, Any]:
        suffix = state.get("suffix", "base")
        output = f"version-1:{suffix}"
        return {
            "messages": state.get("messages", []) + ["draft"],
            "result": output,
            "_output_files": {"result.txt": output},
        }

    def finalize(state: FileState) -> dict[str, Any]:
        suffix = state.get("suffix", "base")
        variant = (state.get("_variant_config") or {}).get("finalize", "default")
        output = f"version-2:{suffix}:{variant}"
        return {
            "messages": state.get("messages", []) + ["finalize"],
            "result": output,
            "_output_files": {"result.txt": output},
        }

    graph = StateGraph(FileState)
    graph.add_node("draft", draft)
    graph.add_node("finalize", finalize)
    graph.set_entry_point("draft")
    graph.add_edge("draft", "finalize")
    graph.add_edge("finalize", END)
    return graph


def _initial_state(suffix: str = "base") -> dict[str, Any]:
    return {"messages": [], "suffix": suffix, "result": "", "_output_files": {}}


def _project(name: str, executor: str = "threadpool") -> WorkflowProject:
    return WorkflowProject(
        name=name,
        graph_factory=create_file_control_graph,
        execution=ExecutionConfig(
            batch_executor=executor,
            ray_config=RayConfig(address="auto", max_retries=1),
        ),
    )


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _checkpoint_with_result(
    bench: WTBTestBench,
    execution_id: str,
    expected: str,
    batch_result: Any | None = None,
):
    checkpoints = (
        bench.get_batch_result_checkpoints(batch_result)
        if batch_result is not None
        else bench.get_checkpoints(execution_id)
    )
    assert checkpoints, "expected LangGraph checkpoints"
    for checkpoint in checkpoints:
        output_files = checkpoint.state_values.get("_output_files") or {}
        if output_files.get("result.txt") == expected:
            return checkpoint
    details = [
        (cp.step, (cp.state_values.get("_output_files") or {}).get("result.txt"))
        for cp in checkpoints
    ]
    raise AssertionError(f"checkpoint with {expected!r} not found: {details!r}")


def _assert_file(path: Path, expected: str) -> None:
    assert path.exists(), f"expected real output file at {path}"
    assert path.read_text(encoding="utf-8") == expected


def _assert_checkpoint_has_file_commit(bench: WTBTestBench, checkpoint_id: str) -> None:
    ctrl = getattr(bench, "_exec_ctrl")
    inner = getattr(ctrl, "_inner", ctrl)
    file_tracking = getattr(inner, "_file_tracking", None)
    assert file_tracking is not None, "file tracking service is not wired"
    commit_id = file_tracking.get_commit_for_checkpoint(checkpoint_id)
    assert commit_id, f"checkpoint {checkpoint_id} is not linked to a file commit"


def _exercise_single_like_file_flow(
    bench: WTBTestBench,
    project_name: str,
    output_file: Path,
    batch: bool,
) -> None:
    if batch:
        batch_result = bench.run_batch_test(
            project=project_name,
            variant_matrix=[{"finalize": "marked"}],
            test_cases=[_initial_state()],
        )
        result = batch_result.results[0]
        assert result.success, result.error_message
        execution_id = result.execution_id
    else:
        execution = bench.run(
            project=project_name,
            initial_state=_initial_state(),
            variant_config={"finalize": "marked"},
        )
        assert execution.status.value == "completed", execution.error_message
        execution_id = execution.id

    _assert_file(output_file, "version-2:base:marked")

    checkpoint = _checkpoint_with_result(
        bench,
        execution_id,
        "version-1:base",
        batch_result=result if batch else None,
    )
    checkpoint_id = str(checkpoint.id)
    _assert_checkpoint_has_file_commit(bench, checkpoint_id)

    output_file.write_text("corrupted-after-run", encoding="utf-8")
    if batch:
        rollback = bench.rollback_batch_result(result, checkpoint_id=checkpoint_id)
    else:
        rollback = bench.rollback(execution_id, checkpoint_id=checkpoint_id)
    assert rollback.success, rollback.error
    _assert_file(output_file, "version-1:base")

    resumed = bench.resume(execution_id)
    assert resumed.status.value == "completed", resumed.error_message
    _assert_file(output_file, "version-2:base:marked")

    output_file.write_text("corrupted-before-fork", encoding="utf-8")
    if batch:
        fork = bench.fork_batch_result(
            result,
            checkpoint_id=checkpoint_id,
            new_state={"suffix": "forked"},
        )
        fork_execution_id = fork.fork_execution_id
        assert fork_execution_id, fork.error
    else:
        fork = bench.fork(
            execution_id,
            checkpoint_id=checkpoint_id,
            new_initial_state={"suffix": "forked"},
        )
        fork_execution_id = fork.fork_execution_id
        assert fork_execution_id, fork.error

    forked = bench.resume(fork_execution_id)
    assert forked.status.value == "completed", forked.error_message
    _assert_file(output_file, "version-2:forked:marked")


def test_single_mode_real_file_rollback_resume_fork(tmp_path):
    bench = WTBTestBench.create(
        mode="development",
        data_dir=str(tmp_path),
        enable_file_tracking=True,
    )
    try:
        project = _project("single_files")
        bench.register_project(project)
        _exercise_single_like_file_flow(
            bench=bench,
            project_name=project.name,
            output_file=tmp_path / "outputs" / "result.txt",
            batch=False,
        )
    finally:
        bench.close()


def test_batch_mode_real_file_rollback_resume_fork(tmp_path):
    bench = WTBTestBench.create(
        mode="development",
        data_dir=str(tmp_path),
        enable_file_tracking=True,
    )
    try:
        project = _project("batch_files", executor="threadpool")
        bench.register_project(project)
        _exercise_single_like_file_flow(
            bench=bench,
            project_name=project.name,
            output_file=tmp_path / "outputs" / "result.txt",
            batch=True,
        )
    finally:
        bench.close()


@pytest.mark.parametrize("grpc_url", [None, "localhost:50051"])
def test_ray_modes_real_file_rollback_resume_fork(tmp_path, grpc_url: str | None):
    if grpc_url is not None:
        pytest.importorskip("grpc")

    ray = pytest.importorskip("ray")
    ray.shutdown()
    ray.init(
        num_cpus=2,
        ignore_reinit_error=True,
        include_dashboard=False,
        log_to_driver=False,
        _metrics_export_port=_find_free_port(),
    )

    os.environ["WTB_RAY_STORAGE_ROOT"] = str(tmp_path / "ray_actors")
    bench = WTBTestBench.create(
        mode="development",
        data_dir=str(tmp_path),
        enable_file_tracking=True,
        enable_ray=True,
        grpc_env_url=grpc_url,
    )
    try:
        project = _project("venv_files" if grpc_url else "ray_files", executor="ray")
        bench.register_project(project)
        _exercise_single_like_file_flow(
            bench=bench,
            project_name=project.name,
            output_file=tmp_path / "outputs" / "result.txt",
            batch=True,
        )
    finally:
        bench.close()
        os.environ.pop("WTB_RAY_STORAGE_ROOT", None)
        ray.shutdown()
