"""Isolated probe executed against a built WTB wheel or sdist install."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import sys

import wtb
from wtb.domain.interfaces.workflow_runtime import RuntimeBindingMismatchError
from wtb.infrastructure.runtimes import (
    DeepSeekHarnessRuntime,
    DeepSeekHarnessWorkflow,
)
from wtb.infrastructure.database.async_unit_of_work import AsyncSQLAlchemyUnitOfWork
from wtb.sdk import WTBTestBench


assert importlib.metadata.version("wtb") == "0.4.0.dev0"
assert wtb.__version__ == importlib.metadata.version("wtb")
assert importlib.util.find_spec("langgraph") is None
assert importlib.util.find_spec("deepseek_harness") is None
assert not any(
    name == "langgraph" or name.startswith("langgraph.") for name in sys.modules
)
assert wtb is not None
assert WTBTestBench is not None
assert AsyncSQLAlchemyUnitOfWork is not None

expect_source_extra = os.environ.get("WTB_EXPECT_DSH_SOURCE_EXTRA") == "1"
assert (importlib.util.find_spec("pydantic") is not None) is expect_source_extra


class LegacyHarness:
    """The callable surface present in the published rc6 SDK."""

    def __init__(self) -> None:
        self.run_calls = 0

    def run(self, input: object, *, session_id: str) -> object:
        del input, session_id
        self.run_calls += 1
        raise AssertionError("legacy SDK must fail before dispatch")

    def close(self) -> None:
        pass


harness = LegacyHarness()
runtime = DeepSeekHarnessRuntime(
    harness=harness,
    session_namespace="installed-package-probe",
    runtime_config_sha256="a" * 64,
)
runtime.bind(DeepSeekHarnessWorkflow())
try:
    runtime.prepare_attempt(
        "installed-package-execution",
        "installed-package-attempt",
        input_sha256="b" * 64,
    )
except RuntimeBindingMismatchError as error:
    assert "process-generation binding" in str(error)
else:
    raise AssertionError("legacy SDK shape unexpectedly passed")

assert harness.run_calls == 0
