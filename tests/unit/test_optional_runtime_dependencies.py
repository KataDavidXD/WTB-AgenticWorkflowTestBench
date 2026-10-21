"""Packaging regressions for optional workflow runtimes."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from wtb.domain.interfaces.workflow_runtime import RuntimeBindingMismatchError
from wtb.infrastructure.runtimes.deepseek_harness import (
    DeepSeekHarnessRuntime,
    DeepSeekHarnessWorkflow,
)


ROOT = Path(__file__).resolve().parents[2]


def test_package_metadata_bounds_python_and_uses_pep639_license() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]

    assert project["requires-python"] == ">=3.11,<3.14"
    assert project["license"] == "Apache-2.0"
    assert project["license-files"] == ["LICENSE"]
    assert {
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
    } <= set(project["classifiers"])


def test_langgraph_is_not_a_core_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    core_names = {
        item.split(">", 1)[0].split("=", 1)[0] for item in project["dependencies"]
    }
    extras = project["optional-dependencies"]

    assert "langgraph" not in core_names
    assert "langgraph-checkpoint" not in core_names
    assert any(item.startswith("langgraph>=") for item in extras["langgraph"])
    assert any(
        item.startswith("langgraph-checkpoint>=") for item in extras["langgraph"]
    )
    assert any(
        item.startswith("langgraph-checkpoint-sqlite>=") for item in extras["dev"]
    )


def test_sqlite_and_postgres_extras_use_one_compatible_runtime_family() -> None:
    """Checkpoint v3 providers require the modern checkpoint-v4 runtime."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    extras = project["optional-dependencies"]

    expected = {
        "langgraph>=1.0,<1.1",
        "langgraph-checkpoint>=4.1,<5",
    }
    for extra_name in ("langgraph-sqlite", "langgraph-postgres", "dev", "test"):
        assert expected <= set(extras[extra_name])

    assert "langgraph-checkpoint-sqlite>=3.1,<4" in extras["langgraph-sqlite"]
    assert "langgraph-checkpoint-postgres>=3.1,<4" in extras["langgraph-postgres"]


def test_unpublished_deepseek_contract_is_source_only_and_release_gated() -> None:
    document = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    project = document["project"]
    extras = project["optional-dependencies"]
    release = document["tool"]["wtb"]["release"]

    assert project["version"] == "0.4.0.dev0"
    assert release == {
        "phase": "development",
        "deepseek-harness-contract": "durable-attempt-v2",
        "deepseek-harness-distribution": "source-only",
    }
    assert "deepseek-harness" not in extras
    source_requirements = [
        Requirement(item) for item in extras["deepseek-harness-source"]
    ]
    assert {item.name for item in source_requirements} == {"pydantic"}
    assert all(item.marker is None for item in source_requirements)
    assert not any(
        Requirement(item).name == "deepseek-harness-sdk"
        for requirements in extras.values()
        for item in requirements
        if not item.startswith("wtb[")
    )
    assert not any("deepseek-harness-source" in item for item in extras["all"])


def test_rc6_shape_fails_before_dispatch_without_durable_identity() -> None:
    class LegacyHarness:
        run_calls = 0

        def run(self, input: object, *, session_id: str) -> object:
            del input, session_id
            self.run_calls += 1
            raise AssertionError("legacy SDK must fail before dispatch")

        def close(self) -> None:
            pass

    harness = LegacyHarness()
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace="rc6-contract-probe",
        runtime_config_sha256="a" * 64,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    with pytest.raises(
        RuntimeBindingMismatchError,
        match="process-generation binding",
    ):
        runtime.prepare_attempt(
            "execution-rc6",
            "attempt-rc6",
            input_sha256="b" * 64,
        )

    assert harness.run_calls == 0


def test_missing_sdk_error_never_recommends_the_removed_binary_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_sdk() -> object:
        raise ImportError("contract probe")

    monkeypatch.setattr(
        DeepSeekHarnessRuntime,
        "_load_installed_sdk",
        staticmethod(missing_sdk),
    )
    runtime = DeepSeekHarnessRuntime()

    with pytest.raises(RuntimeError) as captured:
        runtime._default_harness_factory()

    message = str(captured.value)
    assert "'deepseek-harness-source' extra" in message
    assert "sdk_source_path='<deepseek-harness checkout>'" in message
    assert "runtime_command=" in message
    assert "'deepseek-harness' extra" not in message


def test_base_imports_work_when_langgraph_is_unavailable() -> None:
    script = r"""
import importlib.abc
import sys

class BlockLangGraph(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "langgraph" or fullname.startswith("langgraph."):
            raise ModuleNotFoundError(
                "blocked optional dependency",
                name=fullname,
            )
        return None

sys.meta_path.insert(0, BlockLangGraph())

import wtb
from wtb.application.services.execution_controller import ExecutionController
from wtb.domain.interfaces.workflow_runtime import WorkflowRuntime
from wtb.infrastructure import InMemoryCheckpointStore, InMemoryStateAdapter
from wtb.infrastructure import SqliteStateAdapter
from wtb.infrastructure.adapters import *
from wtb.infrastructure.runtimes.deepseek_harness import DeepSeekHarnessRuntime

assert wtb.InMemoryStateAdapter is InMemoryStateAdapter
assert InMemoryCheckpointStore is not None
assert SqliteStateAdapter is not None
assert ExecutionController is not None
assert WorkflowRuntime is not None
assert DeepSeekHarnessRuntime is not None
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_top_level_import_does_not_hide_broken_core_modules() -> None:
    script = r'''
import importlib.abc
import sys

class BreakCoreModule(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "wtb.application.services.execution_controller":
            raise ImportError("synthetic core import failure")
        return None

sys.meta_path.insert(0, BreakCoreModule())

try:
    import wtb
except ImportError as error:
    assert "synthetic core import failure" in str(error), error
else:
    raise AssertionError("wtb silently hid a broken core module")
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_deepseek_runtime_path_never_attempts_to_import_langgraph() -> None:
    script = r"""
import importlib.abc
import sys
from types import SimpleNamespace

class RejectLangGraph(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "langgraph" or fullname.startswith("langgraph."):
            raise AssertionError(
                f"DeepSeek runtime path attempted optional import: {fullname}"
            )
        return None

sys.meta_path.insert(0, RejectLangGraph())

import wtb
from wtb.domain.models.workflow import ExecutionStatus
from wtb.infrastructure.runtimes.deepseek_harness import (
    DeepSeekHarnessRuntime,
    DeepSeekHarnessWorkflow,
)
from wtb.sdk import WTBTestBench, WorkflowProject

class FakeHarness:
    def __init__(self):
        self.attempts = {}
        self.generation = "import-contract-generation"
        self.binding = SimpleNamespace(
            protocol_version=1,
            schema_version=2,
            persistence_instance_id="import-contract-store",
            root_config_sha256="d" * 64,
        )

    def start(self):
        pass

    def get_runtime_binding(self):
        return self.binding

    def get_runtime_process_binding(self):
        return SimpleNamespace(
            runtime_binding=self.binding,
            process_generation=self.generation,
        )

    def run(
        self,
        prompt,
        *,
        session_id,
        attempt_id,
        input_sha256,
        expected_process_generation,
    ):
        assert expected_process_generation == self.generation
        result = SimpleNamespace(
            session_id=session_id,
            attempt_id=attempt_id,
            input_sha256=input_sha256,
            message_id=f"message-{attempt_id}",
            final_response=f"deepseek:{prompt}",
            finish_reason="completed",
            events=[{
                "type": "turn/end",
                "seq": 1,
                "data": {"reason": {"kind": "completed"}},
            }],
            session_root=None,
            runtime_binding=self.binding,
        )
        self.attempts[(session_id, attempt_id)] = SimpleNamespace(
            session_id=session_id,
            attempt_id=attempt_id,
            input_sha256=input_sha256,
            message_id=result.message_id,
            status="terminal",
            final_response=result.final_response,
            finish_reason={"kind": "completed"},
            events=None,
            session_root=None,
            runtime_binding=self.binding,
        )
        return result

    def get_attempt(self, *, session_id, attempt_id, input_sha256):
        result = self.attempts.get((session_id, attempt_id))
        if result is not None and result.input_sha256 != input_sha256:
            raise ValueError("attempt identity conflict")
        return result

    def cancel_attempt(self, *, session_id, attempt_id, input_sha256):
        raise AssertionError("cancel is not expected in this import contract")

    def close(self):
        pass

runtime = DeepSeekHarnessRuntime(
    harness=FakeHarness(),
    session_namespace="import-contract",
    runtime_config_sha256="c" * 64,
)
bench = WTBTestBench.create_with_runtime(runtime)
project = WorkflowProject(
    name="deepseek-import-contract",
    graph_factory=DeepSeekHarnessWorkflow,
)
try:
    bench.register_project(project)
    result = bench.run(
        project=project.name,
        initial_state={"prompt": "hello"},
    )
    assert result.status is ExecutionStatus.COMPLETED
    assert result.state.workflow_variables["answer"] == "deepseek:hello"
    assert not any(
        name == "langgraph" or name.startswith("langgraph.")
        for name in sys.modules
    )
finally:
    bench.close()

assert wtb.InMemoryStateAdapter is not None
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
