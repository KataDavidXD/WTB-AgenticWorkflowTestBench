"""Executable contract for WTB's LangGraph-independent default package."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from wtb.application.factories import (
    ExecutionControllerFactory,
    WTBTestBenchFactory,
)
from wtb.application.services.ray_batch_runner import RayBatchTestRunner
from wtb.config import WTBConfig
from wtb.infrastructure.adapters.sqlite_state_adapter import SqliteStateAdapter
from wtb.sdk import ExecutionConfig, WTBTestBench, WorkflowProject
from wtb.sdk.test_bench import WTBTestBenchBuilder


ROOT = Path(__file__).resolve().parents[2]


def test_default_durable_presets_select_wtb_native_state(tmp_path: Path) -> None:
    """Ordinary durable presets must not silently select an optional runtime."""
    development = WTBConfig.for_development(str(tmp_path / "development"))
    standalone = WTBConfig.for_standalone(str(tmp_path / "standalone"))
    production = WTBConfig.for_production(
        "postgresql://user:pass@localhost/wtb",
        data_dir=str(tmp_path / "production"),
    )

    for config in (development, standalone, production):
        assert config.state_adapter_mode == "sqlite"
        assert config.state_checkpoint_path == str(
            Path(config.data_dir) / "wtb_checkpoints.db"
        )
        assert config.langgraph_event_config is None

        adapter = ExecutionControllerFactory._create_state_adapter(config, None)
        try:
            assert isinstance(adapter, SqliteStateAdapter)
            assert adapter.storage_path == Path(
                config.state_checkpoint_path
            ).resolve()
        finally:
            adapter.close()


def test_public_development_lifecycle_never_imports_langgraph() -> None:
    """A base install can persist and reattach executable native nodes."""
    script = r'''
import importlib.abc
import sys
import tempfile
from pathlib import Path

class RejectLangGraph(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "langgraph" or fullname.startswith("langgraph."):
            raise AssertionError(
                f"default WTB lifecycle attempted optional import: {fullname}"
            )
        return None

sys.meta_path.insert(0, RejectLangGraph())

from wtb.domain.models.workflow import ExecutionStatus
from wtb.sdk import WTBTestBench, WorkflowProject

class Edge:
    def __init__(self, source, target):
        self.source = source
        self.target = target

def start(state):
    return {"trace": [*state.get("trace", []), "start"]}

def action(state):
    return {"trace": [*state["trace"], "action"]}

def finish(state):
    return {"trace": [*state["trace"], "finish"]}

class PortableGraph:
    nodes = {"start": start, "action": action, "finish": finish}
    edges = [
        Edge("__start__", "start"),
        Edge("start", "action"),
        Edge("action", "finish"),
        Edge("finish", "__end__"),
    ]
    entry_point = "start"

with tempfile.TemporaryDirectory() as directory:
    project = WorkflowProject(
        name="framework-neutral-default",
        graph_factory=PortableGraph,
    )

    first = WTBTestBench.create(mode="development", data_dir=directory)
    try:
        first.register_project(project)
        paused = first.run(
            project.name,
            {"request": "independent"},
            breakpoints=["action"],
        )
        assert paused.status is ExecutionStatus.PAUSED
        execution_id = paused.id
        assert first.supports_time_travel() is True
        assert first.get_checkpoints(execution_id)
    finally:
        first.close()

    second = WTBTestBench.create(mode="development", data_dir=directory)
    try:
        reopened = second.get_execution(execution_id)
        assert reopened.status is ExecutionStatus.PAUSED
        # Durable state intentionally stores no executable Python code. Restore
        # the reviewed project binding before resuming a real callable graph.
        second.attach_project(project)
        completed = second.resume(execution_id)
        assert completed.status is ExecutionStatus.COMPLETED
        assert completed.state.execution_path[:3] == [
            "start",
            "action",
            "finish",
        ], completed.state.execution_path
        assert completed.state.workflow_variables["trace"] == [
            "start",
            "action",
            "finish",
        ]
        checkpoints = second.get_checkpoints(execution_id)
        assert checkpoints
        fork_source = next(
            checkpoint
            for checkpoint in checkpoints
            if checkpoint.state_values.get("current_node_id") == "action"
        )
        fork = second.fork(
            execution_id,
            str(fork_source.id),
            new_initial_state={"branch": "fork"},
        )
        forked = second.resume(fork.fork_execution_id)
        assert forked.status is ExecutionStatus.COMPLETED
        assert forked.state.workflow_variables["branch"] == "fork"
    finally:
        second.close()

    assert {path.name for path in Path(directory).glob("*.db")} >= {
        "wtb.db",
        "wtb_checkpoints.db",
    }

loaded_langgraph = sorted(
    name
    for name in sys.modules
    if name == "langgraph" or name.startswith("langgraph.")
)
assert not loaded_langgraph, loaded_langgraph
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_native_graph_executes_real_python_nodes_without_langgraph() -> None:
    script = r'''
import importlib.abc
import sys
import tempfile

class RejectLangGraph(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "langgraph" or fullname.startswith("langgraph."):
            raise AssertionError(f"native execution imported {fullname}")
        return None

sys.meta_path.insert(0, RejectLangGraph())

from wtb.domain.models.workflow import ExecutionStatus
from wtb.sdk import WTBTestBench, WorkflowProject

class Edge:
    def __init__(self, source, target):
        self.source = source
        self.target = target

def prepare(state):
    return {"trace": [*state.get("trace", []), "prepare"], "value": 20}

def compute(state):
    return {
        "trace": [*state["trace"], "compute"],
        "answer": state["value"] * 2 + 2,
    }

def compute_variant(state):
    return {
        "trace": [*state["trace"], "compute-variant"],
        "answer": state["value"] * 4 + 4,
    }

class NativeGraph:
    nodes = {"prepare": prepare, "compute": compute}
    edges = [
        Edge("__start__", "prepare"),
        Edge("prepare", "compute"),
        Edge("compute", "__end__"),
    ]
    entry_point = "prepare"

with tempfile.TemporaryDirectory() as directory:
    project = WorkflowProject(
        name="real-native-functions",
        graph_factory=NativeGraph,
    )
    with WTBTestBench.create(mode="development", data_dir=directory) as bench:
        bench.register_project(project)
        result = bench.run(project.name, {"trace": []})
        assert result.status is ExecutionStatus.COMPLETED
        assert result.state.workflow_variables["answer"] == 42
        assert result.state.workflow_variables["trace"] == [
            "prepare",
            "compute",
        ]
        project.register_variant(
            node="compute",
            name="double",
            implementation=compute_variant,
        )
        variant = bench.run(
            project.name,
            {"trace": []},
            variant_config={"compute": "double"},
        )
        assert variant.status is ExecutionStatus.COMPLETED
        assert variant.state.workflow_variables["answer"] == 84
        assert variant.state.workflow_variables["trace"] == [
            "prepare",
            "compute-variant",
        ]

assert not any(
    name == "langgraph" or name.startswith("langgraph.")
    for name in sys.modules
)
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_native_threadpool_batch_executes_real_nodes_without_langgraph() -> None:
    """The default batch executor must retain native callable graph code."""
    script = r'''
import importlib.abc
import sys
import tempfile

class RejectLangGraph(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "langgraph" or fullname.startswith("langgraph."):
            raise AssertionError(f"native batch imported {fullname}")
        return None

sys.meta_path.insert(0, RejectLangGraph())

from wtb.domain.models.batch_test import BatchTestStatus
from wtb.sdk import WTBTestBench, WorkflowProject

class Edge:
    def __init__(self, source, target):
        self.source = source
        self.target = target

def prepare(state):
    return {"trace": [*state.get("trace", []), "prepare"], "value": 20}

def compute(state):
    return {
        "trace": [*state["trace"], "compute"],
        "answer": state["value"] * 2 + 2,
    }

def compute_variant(state):
    return {
        "trace": [*state["trace"], "compute-variant"],
        "answer": state["value"] * 4 + 4,
    }

class NativeGraph:
    nodes = {"prepare": prepare, "compute": compute}
    edges = [
        Edge("__start__", "prepare"),
        Edge("prepare", "compute"),
        Edge("compute", "__end__"),
    ]
    entry_point = "prepare"

with tempfile.TemporaryDirectory() as directory:
    project = WorkflowProject(
        name="real-native-threadpool-batch",
        graph_factory=NativeGraph,
    )
    project.register_variant(
        node="compute",
        name="double",
        implementation=compute_variant,
    )
    with WTBTestBench.create(mode="development", data_dir=directory) as bench:
        bench.register_project(project)
        batch = bench.run_batch_test(
            project.name,
            [{}, {"compute": "double"}],
            [{"trace": []}],
        )
        assert batch.status is BatchTestStatus.COMPLETED, batch.metadata
        assert len(batch.results) == 2
        assert all(item.success for item in batch.results), [
            item.error_message for item in batch.results
        ]
        answers = {
            bench.get_execution(item.execution_id).state.workflow_variables["answer"]
            for item in batch.results
        }
        assert answers == {42, 84}, answers

loaded_langgraph = sorted(
    name
    for name in sys.modules
    if name == "langgraph" or name.startswith("langgraph.")
)
assert not loaded_langgraph, loaded_langgraph
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_native_file_checkpoint_rollback_never_imports_langgraph() -> None:
    """WTB-native execution owns file materialization and CAS restoration."""
    script = r'''
import importlib.abc
import sys
import tempfile
from pathlib import Path

class RejectLangGraph(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "langgraph" or fullname.startswith("langgraph."):
            raise AssertionError(f"native file lifecycle imported {fullname}")
        return None

sys.meta_path.insert(0, RejectLangGraph())

from wtb.domain.models.workflow import ExecutionStatus
from wtb.sdk import WTBTestBench, WorkflowProject

class Edge:
    def __init__(self, source, target):
        self.source = source
        self.target = target

def draft(state):
    return {
        "trace": [*state.get("trace", []), "draft"],
        "_output_files": {"result.txt": "draft"},
    }

def finalize(state):
    return {
        "trace": [*state["trace"], "finalize"],
        "_output_files": {"result.txt": "final"},
    }

class NativeFileGraph:
    nodes = {"draft": draft, "finalize": finalize}
    edges = [
        Edge("__start__", "draft"),
        Edge("draft", "finalize"),
        Edge("finalize", "__end__"),
    ]
    entry_point = "draft"

with tempfile.TemporaryDirectory() as directory:
    project = WorkflowProject(
        name="native-file-control",
        graph_factory=NativeFileGraph,
    )
    with WTBTestBench.create(
        mode="development",
        data_dir=directory,
        enable_file_tracking=True,
    ) as bench:
        bench.register_project(project)
        completed = bench.run(project.name, {"trace": []})
        assert completed.status is ExecutionStatus.COMPLETED
        output = Path(directory) / "outputs" / "result.txt"
        assert output.read_text(encoding="utf-8") == "final"

        checkpoints = bench.get_checkpoints(completed.id)
        draft_checkpoint = next(
            checkpoint
            for checkpoint in checkpoints
            if (
                checkpoint.state_values.get("current_node_id") == "finalize"
                and checkpoint.state_values.get("workflow_variables", {})
                .get("_output_files", {})
                .get("result.txt") == "draft"
            )
        )
        output.write_text("corrupted", encoding="utf-8")
        rolled_back = bench.rollback(completed.id, str(draft_checkpoint.id))
        assert rolled_back.success, rolled_back.error
        assert output.read_text(encoding="utf-8") == "draft"

        resumed = bench.resume(completed.id)
        assert resumed.status is ExecutionStatus.COMPLETED, (
            resumed.status,
            resumed.error_message,
            resumed.state.workflow_variables,
        )
        assert output.read_text(encoding="utf-8") == "final"

    assert (Path(directory) / ".filetrack" / "filetrack.db").is_file()

assert not any(
    name == "langgraph" or name.startswith("langgraph.")
    for name in sys.modules
)
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_explicit_langgraph_mode_remains_opt_in() -> None:
    """The optional adapter is still selectable, but never as a hidden default."""
    config = WTBConfig.for_development()
    config.state_adapter_mode = "langgraph"

    assert config.state_adapter_mode == "langgraph"


def test_public_explicit_langgraph_ray_options_reach_optional_factory() -> None:
    bench = MagicMock()
    with patch.object(
        WTBTestBenchFactory,
        "create_with_langgraph",
        return_value=bench,
    ) as create:
        result = WTBTestBench.create(
            mode="development",
            data_dir="data-dir",
            state_adapter_mode="langgraph",
            enable_ray=True,
            grpc_env_url="localhost:50051",
        )

    assert result is bench
    assert create.call_args.kwargs["enable_ray"] is True
    assert create.call_args.kwargs["grpc_env_url"] == "localhost:50051"


def test_production_builder_selects_native_composition_by_default() -> None:
    bench = MagicMock()
    with patch.object(
        WTBTestBenchFactory,
        "create",
        return_value=bench,
    ) as create:
        result = WTBTestBenchBuilder().for_production(
            connection_string="postgresql://user:pass@localhost/wtb",
        ).build()

    assert result is bench
    config = create.call_args.args[0]
    assert config.state_adapter_mode == "sqlite"
    assert config.langgraph_event_config is None


def test_persistent_external_runtime_bench_uses_native_state(tmp_path: Path) -> None:
    runtime = MagicMock()
    bench = WTBTestBench.create_with_runtime(
        runtime,
        data_dir=str(tmp_path),
    )
    try:
        inner = getattr(bench._exec_ctrl, "_inner", bench._exec_ctrl)
        assert isinstance(inner._state_adapter, SqliteStateAdapter)
        assert inner._state_adapter.storage_path == (
            tmp_path / "wtb_checkpoints.db"
        ).resolve()
    finally:
        bench.close()


def test_public_runtime_factory_accepts_framework_neutral_state_adapter(
    tmp_path: Path,
) -> None:
    runtime = MagicMock()
    adapter = SqliteStateAdapter(tmp_path / "injected-state.db")
    bench = WTBTestBench.create_with_runtime(
        runtime,
        state_adapter=adapter,
    )
    try:
        inner = getattr(bench._exec_ctrl, "_inner", bench._exec_ctrl)
        assert inner._state_adapter is adapter
    finally:
        bench.close()


def test_native_project_keeps_thread_graph_out_of_public_batch_payload() -> None:
    class PortableGraph:
        nodes = {"start": object()}
        edges: list[object] = []
        entry_point = "start"

    project = WorkflowProject(
        name="native-batch",
        graph_factory=PortableGraph,
        execution=ExecutionConfig(batch_executor="threadpool"),
    )
    bench = WTBTestBenchFactory.create_for_testing()
    runner = MagicMock()
    runner.run_batch_test.side_effect = lambda batch: batch
    bench._replace_batch_runner(runner, owns_batch_runner=False)
    try:
        bench.register_project(project)
        bench.run_batch_test(project.name, [{}], [{}])
    finally:
        bench.close()

    batch = runner.run_batch_test.call_args.args[0]
    assert project.runtime_backend == "native"
    assert "_graph_factory_pickled" not in batch.metadata
    assert "_graph_pickled" not in batch.variant_combinations[0].metadata
    assert batch.variant_combinations[0].graph_factory_module is None
    assert batch.variant_combinations[0].graph_factory_name is None
    assert batch.variant_combinations[0].runtime_backend == "native"
    assert batch.variant_combinations[0]._runtime_graph is not None


@pytest.mark.skipif(
    not RayBatchTestRunner.is_available(),
    reason="Ray is not installed",
)
def test_native_ray_batch_executes_real_nodes_with_native_checkpoints() -> None:
    """A transported native graph must not be routed into LangGraph state."""
    script = r'''
import sys
import tempfile

from wtb.domain.models.batch_test import BatchTestStatus
from wtb.sdk import ExecutionConfig, WTBTestBench, WorkflowProject

class Edge:
    def __init__(self, source, target):
        self.source = source
        self.target = target

def prepare(state):
    return {"value": 20}

def compute(state):
    return {"answer": state["value"] * 2 + 2}

class NativeGraph:
    nodes = {"prepare": prepare, "compute": compute}
    edges = [
        Edge("__start__", "prepare"),
        Edge("prepare", "compute"),
        Edge("compute", "__end__"),
    ]
    entry_point = "prepare"

with tempfile.TemporaryDirectory() as directory:
    project = WorkflowProject(
        name="real-native-ray-batch",
        graph_factory=NativeGraph,
        execution=ExecutionConfig(batch_executor="ray"),
    )
    with WTBTestBench.create(
        mode="development",
        data_dir=directory,
        enable_ray=True,
    ) as bench:
        bench.register_project(project)
        batch = bench.run_batch_test(project.name, [{}], [{}])
        assert batch.status is BatchTestStatus.COMPLETED, batch.metadata
        assert len(batch.results) == 1
        assert batch.results[0].success, batch.results[0].error_message
        execution = bench.get_execution(batch.results[0].execution_id)
        assert execution.state.workflow_variables["answer"] == 42
        assert execution.metadata["state_adapter_backend"] == "node_sqlite"

assert not any(
    name == "langgraph" or name.startswith("langgraph.")
    for name in sys.modules
)
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_ray_rollback_coordinator_defaults_to_native_state(tmp_path: Path) -> None:
    runner = object.__new__(RayBatchTestRunner)
    runner._wtb_db_url = f"sqlite:///{tmp_path / 'wtb.db'}"
    runner._state_checkpoint_path = str(tmp_path / "native-state.db")

    adapter = runner._create_shared_state_adapter()
    try:
        assert isinstance(adapter, SqliteStateAdapter)
        assert adapter.storage_path == (tmp_path / "native-state.db").resolve()
    finally:
        adapter.close()
