"""Runnable quick demo for WTB execution modes.

The demo intentionally uses a tiny LangGraph so each mode can prove the same
control-flow contract:

    real LLM -> variant -> _output_files -> CAS checkpoint -> rollback -> resume -> fork/resume

Run:
    python -m examples.modes_quick_demo --mode single
    python -m examples.modes_quick_demo --mode batch
    python -m examples.modes_quick_demo --mode ray
    python -m examples.modes_quick_demo --mode venv --grpc-url localhost:50051
"""

from __future__ import annotations

import argparse
import gc
import logging
import os
import shutil
import socket
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, TypedDict

from langgraph.graph import END, StateGraph
from openai import OpenAI

from wtb.sdk import ExecutionConfig, RayConfig, WTBTestBench, WorkflowProject

logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)


class DemoState(TypedDict, total=False):
    messages: list[str]
    count: int
    result: str
    llm_response: str
    _output_files: dict[str, str]
    _variant_config: dict[str, str]


INITIAL_STATE: Dict[str, Any] = {
    "messages": [],
    "count": 0,
    "result": "",
    "llm_response": "",
    "_output_files": {},
}
VARIANT = {"node_b": "marked"}
REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_local_env() -> None:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return

    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and not os.getenv(key):
            os.environ[key] = value


def _llm_settings() -> Dict[str, str]:
    _load_local_env()
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    model = os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL")
    base_url = os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or ""

    missing = [
        name
        for name, value in {
            "LLM_API_KEY or OPENAI_API_KEY": api_key,
            "LLM_MODEL or OPENAI_MODEL": model,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Real LLM configuration is required for this demo. Missing: "
            + ", ".join(missing)
        )
    return {"api_key": api_key, "model": model, "base_url": base_url}


def call_real_llm() -> str:
    """Call a real OpenAI-compatible LLM provider; no fake fallback."""
    settings = _llm_settings()
    kwargs: Dict[str, str] = {"api_key": settings["api_key"]}
    if settings["base_url"]:
        kwargs["base_url"] = settings["base_url"]
    client = OpenAI(**kwargs)
    response = client.chat.completions.create(
        model=settings["model"],
        messages=[
            {
                "role": "system",
                "content": "You are a concise health-check model.",
            },
            {
                "role": "user",
                "content": "Reply with exactly: WTB_LLM_OK",
            },
        ],
        temperature=0,
        max_tokens=256,
    )
    content = response.choices[0].message.content or ""
    if not content.strip():
        raise RuntimeError("Real LLM returned an empty response")
    return content.strip()


def create_demo_graph():
    """Create a small importable graph for local, threadpool, and Ray actors."""

    def node_a(state: DemoState) -> Dict[str, Any]:
        llm_response = state.get("llm_response") or call_real_llm()
        draft = f"draft:{llm_response}"
        return {
            "messages": state.get("messages", []) + ["A"],
            "count": state.get("count", 0) + 1,
            "llm_response": llm_response,
            "_output_files": {"demo_result.txt": draft},
        }

    def node_b(state: DemoState) -> Dict[str, Any]:
        variant = (state.get("_variant_config") or {}).get("node_b")
        marker = f"B:{variant}" if variant and variant != "default" else "B"
        return {
            "messages": state.get("messages", []) + [marker],
            "count": state.get("count", 0) + 1,
        }

    def node_c(state: DemoState) -> Dict[str, Any]:
        messages = state.get("messages", []) + ["C"]
        result = ",".join(messages)
        llm_response = state.get("llm_response", "")
        return {
            "messages": messages,
            "count": state.get("count", 0) + 1,
            "result": result,
            "_output_files": {"demo_result.txt": f"final:{result}:{llm_response}"},
        }

    graph = StateGraph(DemoState)
    graph.add_node("node_a", node_a)
    graph.add_node("node_b", node_b)
    graph.add_node("node_c", node_c)
    graph.set_entry_point("node_a")
    graph.add_edge("node_a", "node_b")
    graph.add_edge("node_b", "node_c")
    graph.add_edge("node_c", END)
    return graph


def node_b_marked(state: DemoState) -> Dict[str, Any]:
    """A real single-run node variant used by WorkflowProject.register_variant."""
    return {
        "messages": state.get("messages", []) + ["B:marked"],
        "count": state.get("count", 0) + 1,
    }


def _project(name: str, executor: str = "threadpool") -> WorkflowProject:
    return WorkflowProject(
        name=name,
        graph_factory=create_demo_graph,
        execution=ExecutionConfig(
            batch_executor=executor,
            ray_config=RayConfig(address="auto", max_retries=1),
        ),
    )


def _assert_completed(execution: Any) -> None:
    assert execution.status.value == "completed", (
        f"status={execution.status}, error={getattr(execution, 'error_message', None)}"
    )


def _assert_variant(bench: WTBTestBench, execution_id: str) -> None:
    execution = bench.get_execution(execution_id)
    state = execution.state.workflow_variables or {}
    messages = state.get("messages", [])
    assert "B:marked" in messages, f"variant marker missing from {messages!r}"
    assert state.get("llm_response"), "real LLM response missing from execution state"


def _assert_output_file(data_dir: str, expected: str) -> None:
    output_file = Path(data_dir) / "outputs" / "demo_result.txt"
    assert output_file.exists(), f"missing demo output file: {output_file}"
    actual = output_file.read_text(encoding="utf-8")
    assert actual == expected, f"unexpected file content: {actual!r}"


def _latest_output_text(bench: WTBTestBench, execution_id: str) -> str:
    execution = bench.get_execution(execution_id)
    output_files = execution.state.workflow_variables.get("_output_files", {})
    value = output_files.get("demo_result.txt")
    assert value, "execution did not expose demo_result.txt in _output_files"
    return value


def _checkpoint_with_output(
    bench: WTBTestBench,
    execution_id: str,
    prefix: str,
    batch_result: Optional[Any] = None,
) -> Any:
    checkpoints = (
        bench.get_batch_result_checkpoints(batch_result)
        if batch_result is not None
        else bench.get_checkpoints(execution_id)
    )
    assert checkpoints, "no checkpoints were created"
    for checkpoint in checkpoints:
        output_files = checkpoint.state_values.get("_output_files") or {}
        value = output_files.get("demo_result.txt")
        if isinstance(value, str) and value.startswith(prefix):
            return checkpoint
    raise AssertionError(f"no checkpoint output starts with {prefix!r}")


def _assert_checkpoint_file_commit(bench: WTBTestBench, checkpoint_id: str) -> None:
    ctrl = getattr(bench, "_exec_ctrl")
    inner = getattr(ctrl, "_inner", ctrl)
    file_tracking = getattr(inner, "_file_tracking", None)
    assert file_tracking is not None, "file tracking service is not wired"
    commit_id = file_tracking.get_commit_for_checkpoint(checkpoint_id)
    assert commit_id, f"checkpoint {checkpoint_id} has no file commit"


def _resume_and_fork(
    bench: WTBTestBench,
    execution_id: str,
    checkpoint_id: str,
    data_dir: str,
) -> str:
    resumed = bench.resume(execution_id)
    _assert_completed(resumed)
    _assert_output_file(data_dir, _latest_output_text(bench, execution_id))

    fork = bench.fork(
        execution_id,
        checkpoint_id=checkpoint_id,
        new_initial_state={"messages": ["forked"], "count": 42, "result": ""},
    )
    assert fork.fork_execution_id, f"fork error: {fork.error}"

    forked = bench.resume(fork.fork_execution_id)
    _assert_completed(forked)
    fork_output = _latest_output_text(bench, fork.fork_execution_id)
    assert "forked" in fork_output, f"fork output did not use fork state: {fork_output!r}"
    _assert_output_file(data_dir, fork_output)
    return fork.fork_execution_id


def run_single(data_dir: str) -> Dict[str, str]:
    """Run one workflow execution with variant, rollback, resume, and fork."""
    bench = WTBTestBench.create(
        mode="development",
        data_dir=data_dir,
        enable_file_tracking=True,
    )
    try:
        project = _project("demo_single")
        project.register_variant("node_b", "marked", node_b_marked)
        bench.register_project(project)

        execution = bench.run(
            project=project.name,
            initial_state=dict(INITIAL_STATE),
            variant_config=dict(VARIANT),
        )
        _assert_completed(execution)
        _assert_variant(bench, execution.id)
        _assert_output_file(data_dir, _latest_output_text(bench, execution.id))

        checkpoint = _checkpoint_with_output(bench, execution.id, "draft:")
        checkpoint_output = checkpoint.state_values["_output_files"]["demo_result.txt"]
        _assert_checkpoint_file_commit(bench, str(checkpoint.id))
        rollback = bench.rollback(execution.id, checkpoint_id=str(checkpoint.id))
        assert rollback.success, f"rollback error: {rollback.error}"
        _assert_output_file(data_dir, checkpoint_output)

        fork_id = _resume_and_fork(bench, execution.id, str(checkpoint.id), data_dir)
        return {"execution_id": execution.id, "fork_execution_id": fork_id}
    finally:
        bench.close()
        gc.collect()


def run_batch(data_dir: str) -> Dict[str, str]:
    """Run local threadpool batch mode with variant, rollback, resume, and fork."""
    bench = WTBTestBench.create(
        mode="development",
        data_dir=data_dir,
        enable_file_tracking=True,
    )
    try:
        project = _project("demo_batch", executor="threadpool")
        bench.register_project(project)

        batch = bench.run_batch_test(
            project=project.name,
            variant_matrix=[dict(VARIANT)],
            test_cases=[dict(INITIAL_STATE)],
        )
        result = batch.results[0]
        assert result.success, f"batch failed: {result.error_message}"
        assert result.execution_id, "batch result did not record execution_id"
        _assert_variant(bench, result.execution_id)
        _assert_output_file(data_dir, _latest_output_text(bench, result.execution_id))

        checkpoint = _checkpoint_with_output(
            bench,
            result.execution_id,
            "draft:",
            batch_result=result,
        )
        checkpoint_output = checkpoint.state_values["_output_files"]["demo_result.txt"]
        _assert_checkpoint_file_commit(bench, str(checkpoint.id))
        rollback = bench.rollback_batch_result(result, checkpoint_id=str(checkpoint.id))
        assert rollback.success, f"batch rollback error: {rollback.error}"
        _assert_output_file(data_dir, checkpoint_output)

        fork_id = _resume_and_fork(bench, result.execution_id, str(checkpoint.id), data_dir)
        return {"execution_id": result.execution_id, "fork_execution_id": fork_id}
    finally:
        bench.close()
        gc.collect()


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_ray(data_dir: str, grpc_url: Optional[str] = None) -> Dict[str, str]:
    """Run Ray batch mode; set grpc_url to also provision Docker UV venvs."""
    import ray

    os.environ["WTB_RAY_STORAGE_ROOT"] = os.path.join(data_dir, "ray_actors")
    if not ray.is_initialized():
        ray.init(
            num_cpus=2,
            ignore_reinit_error=True,
            include_dashboard=False,
            log_to_driver=False,
            _metrics_export_port=_find_free_port(),
        )

    bench = WTBTestBench.create(
        mode="development",
        data_dir=data_dir,
        enable_file_tracking=True,
        enable_ray=True,
        grpc_env_url=grpc_url,
    )
    try:
        project = _project("demo_venv" if grpc_url else "demo_ray", executor="ray")
        bench.register_project(project)

        batch = bench.run_batch_test(
            project=project.name,
            variant_matrix=[dict(VARIANT)],
            test_cases=[dict(INITIAL_STATE)],
        )
        result = batch.results[0]
        assert result.success, f"ray batch failed: {result.error_message}"
        assert result.execution_id, "ray result did not record execution_id"
        _assert_variant(bench, result.execution_id)
        _assert_output_file(data_dir, _latest_output_text(bench, result.execution_id))

        checkpoint = _checkpoint_with_output(
            bench,
            result.execution_id,
            "draft:",
            batch_result=result,
        )
        checkpoint_output = checkpoint.state_values["_output_files"]["demo_result.txt"]
        _assert_checkpoint_file_commit(bench, str(checkpoint.id))
        rollback = bench.rollback_batch_result(result, checkpoint_id=str(checkpoint.id))
        assert rollback.success, f"ray rollback error: {rollback.error}"
        _assert_output_file(data_dir, checkpoint_output)

        fork_id = _resume_and_fork(bench, result.execution_id, str(checkpoint.id), data_dir)
        output = {"execution_id": result.execution_id, "fork_execution_id": fork_id}

        if grpc_url:
            execution = bench.get_execution(result.execution_id)
            actor_id = (execution.metadata or {}).get("actor_id")
            assert actor_id, "venv-backed Ray run did not record actor_id"
            output["actor_id"] = actor_id

        return output
    finally:
        bench.close()
        gc.collect()
        os.environ.pop("WTB_RAY_STORAGE_ROOT", None)


def run_mode(mode: str, grpc_url: Optional[str] = None) -> Dict[str, Dict[str, str]]:
    """Run a selected mode in an isolated temporary data directory."""
    data_dir = tempfile.mkdtemp(prefix=f"wtb_{mode}_demo_")
    try:
        if mode == "single":
            _llm_settings()
            return {"single": run_single(data_dir)}
        if mode == "batch":
            _llm_settings()
            return {"batch": run_batch(data_dir)}
        if mode == "ray":
            _llm_settings()
            return {"ray": run_ray(data_dir)}
        if mode == "venv":
            if not grpc_url:
                raise ValueError("--grpc-url is required for venv mode")
            _llm_settings()
            return {"venv": run_ray(data_dir, grpc_url=grpc_url)}
        if mode == "all":
            _llm_settings()
            results = {
                "single": run_single(os.path.join(data_dir, "single")),
                "batch": run_batch(os.path.join(data_dir, "batch")),
                "ray": run_ray(os.path.join(data_dir, "ray")),
            }
            if grpc_url:
                results["venv"] = run_ray(os.path.join(data_dir, "venv"), grpc_url=grpc_url)
            return results
        raise ValueError(f"unknown mode: {mode}")
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run WTB quick mode demos")
    parser.add_argument(
        "--mode",
        choices=["single", "batch", "ray", "venv", "all"],
        default="single",
    )
    parser.add_argument(
        "--grpc-url",
        default=None,
        help="UV venv manager gRPC endpoint, for example localhost:50051",
    )
    args = parser.parse_args()

    results = run_mode(args.mode, grpc_url=args.grpc_url)
    for mode, info in results.items():
        suffix = ", ".join(f"{key}={value[:12]}..." for key, value in info.items())
        print(f"[PASS] {mode}: {suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
