from __future__ import annotations

import builtins
import hashlib
import importlib.util
import json
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Never, get_type_hints

import pytest

from wtb.domain.interfaces.workflow_runtime import (
    RuntimeBindingEnvelope,
    RuntimeBindingMismatchError,
    RuntimeOutcomeUnknownError,
    RuntimePreDispatchError,
    RuntimeTerminalError,
    RuntimeTerminalOutcome,
)
from wtb.application.services.execution_controller import ExecutionController

from wtb.infrastructure.runtimes.deepseek_harness import (
    DeepSeekHarnessRuntime,
    DeepSeekHarnessWorkflow,
)


_TEST_NAMESPACE = "test"
_TEST_CONFIG_SHA256 = "c" * 64
_TEST_ROOT_CONFIG_SHA256 = "d" * 64


@dataclass(frozen=True)
class _RuntimeBinding:
    protocol_version: int = 1
    schema_version: int = 2
    persistence_instance_id: str = "store-test-1"
    root_config_sha256: str = _TEST_ROOT_CONFIG_SHA256


@dataclass(frozen=True)
class _RuntimeProcessBinding:
    runtime_binding: _RuntimeBinding
    process_generation: str


class RuntimeGenerationMismatchError(RuntimeError):
    pass


@dataclass
class _RunResult:
    session_id: str
    attempt_id: str | None = None
    input_sha256: str | None = None
    message_id: str | None = None
    status: str | None = None
    final_response: str = "done"
    finish_reason: str | dict[str, str] | None = "completed"
    events: list[dict[str, Any]] | None = None
    session_root: str | None = "sessions"
    runtime_binding: object | None = None
    reference_snapshot: object | None = None
    reference_snapshots: object | None = None
    memory_mode: str | None = None


class _Harness:
    def __init__(
        self,
        result: _RunResult | None = None,
        *,
        attempts: dict[tuple[str, str], _RunResult] | None = None,
    ) -> None:
        self.calls: list[tuple[object, str]] = []
        self.identity_calls: list[tuple[object, str, str | None, str | None]] = []
        self.get_attempt_calls: list[tuple[str, str, str]] = []
        self.cancel_attempt_calls: list[tuple[str, str, str]] = []
        self.result = result
        self.attempts = attempts if attempts is not None else {}
        self.closed = False
        self.close_calls = 0
        self.binding = _RuntimeBinding()
        self.binding_calls = 0
        self.process_generation = "generation-1"
        self.expected_process_generations: list[str | None] = []
        self.reference_calls: list[list[dict[str, object]] | None] = []
        self.memory_mode_calls: list[str | None] = []

    def get_runtime_binding(self) -> _RuntimeBinding:
        self.binding_calls += 1
        return self.binding

    def get_runtime_process_binding(self) -> _RuntimeProcessBinding:
        self.binding_calls += 1
        return _RuntimeProcessBinding(
            runtime_binding=self.binding,
            process_generation=self.process_generation,
        )

    def run(
        self,
        prompt: object,
        *,
        session_id: str,
        attempt_id: str | None = None,
        input_sha256: str | None = None,
        references: list[dict[str, object]] | None = None,
        memory_mode: str | None = None,
        expected_process_generation: str | None = None,
    ) -> _RunResult:
        self.expected_process_generations.append(expected_process_generation)
        self.reference_calls.append(references)
        self.memory_mode_calls.append(memory_mode)
        if (
            expected_process_generation is not None
            and expected_process_generation != self.process_generation
        ):
            raise RuntimeGenerationMismatchError
        self.calls.append((prompt, session_id))
        self.identity_calls.append(
            (prompt, session_id, attempt_id, input_sha256)
        )
        if attempt_id is not None:
            existing = self.attempts.get((session_id, attempt_id))
            if existing is not None:
                if existing.input_sha256 != input_sha256:
                    raise ValueError("attempt identity conflict")
                existing_reason = existing.finish_reason
                if isinstance(existing_reason, dict):
                    existing_reason = existing_reason.get("kind")
                return _RunResult(
                    session_id=existing.session_id,
                    attempt_id=existing.attempt_id,
                    input_sha256=existing.input_sha256,
                    message_id=existing.message_id,
                    final_response=existing.final_response,
                    finish_reason=existing_reason,
                    events=[],
                    session_root=existing.session_root,
                    runtime_binding=existing.runtime_binding or self.binding,
                )
        if self.result is None:
            result = _RunResult(
                session_id=session_id,
                events=[
                    {"type": "assistant/message", "seq": 7},
                    {
                        "type": "turn/end",
                        "seq": 8,
                        "data": {"reason": {"kind": "completed"}},
                    },
                ],
            )
        else:
            result = self.result
        if attempt_id is not None:
            result.attempt_id = attempt_id
            result.input_sha256 = input_sha256
            result.message_id = result.message_id or f"message-{attempt_id}"
            finish_reason = result.finish_reason
            if isinstance(finish_reason, dict):
                finish_reason = finish_reason.get("kind")
            self.attempts[(session_id, attempt_id)] = _RunResult(
                session_id=session_id,
                attempt_id=attempt_id,
                input_sha256=input_sha256,
                message_id=result.message_id,
                status="terminal",
                final_response=result.final_response,
                finish_reason={"kind": finish_reason or "unknown"},
                events=None,
                session_root=result.session_root,
                runtime_binding=self.binding,
            )
            result.runtime_binding = self.binding
        return result

    def get_attempt(
        self,
        *,
        session_id: str,
        attempt_id: str,
        input_sha256: str,
    ) -> _RunResult | None:
        self.get_attempt_calls.append((session_id, attempt_id, input_sha256))
        result = self.attempts.get((session_id, attempt_id))
        if result is not None and result.input_sha256 != input_sha256:
            raise ValueError("attempt identity conflict")
        if result is not None and result.runtime_binding is None:
            result.runtime_binding = self.binding
        return result

    def cancel_attempt(
        self,
        *,
        session_id: str,
        attempt_id: str,
        input_sha256: str,
        expected_process_generation: str | None = None,
    ) -> _RunResult:
        if (
            expected_process_generation is not None
            and expected_process_generation != self.process_generation
        ):
            raise RuntimeGenerationMismatchError
        self.cancel_attempt_calls.append((session_id, attempt_id, input_sha256))
        existing = self.get_attempt(
            session_id=session_id,
            attempt_id=attempt_id,
            input_sha256=input_sha256,
        )
        if existing is not None and existing.status == "terminal":
            return existing
        result = _RunResult(
            session_id=session_id,
            attempt_id=attempt_id,
            input_sha256=input_sha256,
            message_id=f"message-{attempt_id}",
            status="terminal",
            final_response="",
            finish_reason={"kind": "canceled"},
            events=None,
            runtime_binding=self.binding,
        )
        self.attempts[(session_id, attempt_id)] = result
        return result

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


def _prepare(
    runtime: DeepSeekHarnessRuntime,
    state: dict[str, Any],
    *,
    execution_id: str,
    attempt_id: str,
):
    return runtime.prepare_attempt(
        execution_id,
        attempt_id,
        input_sha256=runtime._state_sha256(state),
    )


def test_runtime_invokes_dsh_in_execution_scoped_session_and_returns_receipt() -> None:
    harness = _Harness()
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    runtime.bind(DeepSeekHarnessWorkflow(include_events=True))
    input_state = {"prompt": "fix the bug", "case": "needle"}

    output = runtime.invoke(input_state, execution_id="exec-42")

    assert harness.calls == [("fix the bug", "wtb-test-exec-42")]
    assert input_state == {"prompt": "fix the bug", "case": "needle"}
    assert output["answer"] == "done"
    assert output["case"] == "needle"
    receipt = output["_dsh"]
    assert receipt["protocol"] == "deepseek-harness-sdk-jsonrpc"
    assert receipt["session_id"] == "wtb-test-exec-42"
    assert receipt["session_namespace"] == "test"
    assert receipt["execution_id"] == "exec-42"
    assert receipt["attempt_id"] is None
    assert len(receipt["input_sha256"]) == 64
    assert receipt["finish_reason"] == "completed"
    assert receipt["session_root_configured"] is True
    assert "session_root" not in receipt
    assert receipt["event_count"] == 2
    assert receipt["last_event_seq"] == 8
    assert receipt["event_types"] == ["assistant/message", "turn/end"]
    assert receipt["events"] == [
        {"type": "assistant/message", "seq": 7},
        {
            "type": "turn/end",
            "seq": 8,
            "data": {"reason": {"kind": "completed"}},
        },
    ]
    assert receipt["event_bytes"] > 0

    snapshot = runtime.snapshot("exec-42")
    assert snapshot is not None
    assert snapshot.checkpoint_id is None
    assert snapshot.metadata["recoverable"] is False
    assert runtime.supports_checkpoint_recovery() is False
    assert runtime.supports_forking() is False
    assert runtime.snapshot("exec-42", "event-8") is None
    assert runtime.history("exec-42") == (snapshot,)


def test_durable_runtime_passes_structured_references_and_records_capture() -> None:
    reference_snapshot = {
        "schemaVersion": 1,
        "contentSha256": "e" * 64,
        "references": [
            {
                "sessionId": "source-session",
                "label": "source",
                "capturedThroughSeq": 17,
                "compacted": True,
                "originalMessages": 5,
                "retainedMessages": 2,
                "omittedMessages": 3,
                "omittedBytes": 144,
                "truncated": False,
                "inputIndex": 0,
            }
        ],
    }
    harness = _Harness(
        _RunResult(
            session_id="wtb-test-exec-ref",
            reference_snapshot=reference_snapshot,
            events=[
                {
                    "type": "turn/end",
                    "seq": 1,
                    "data": {"reason": {"kind": "completed"}},
                }
            ],
        )
    )
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    runtime.bind(
        DeepSeekHarnessWorkflow(
            reference_key="memory_references",
            memory_mode="S0",
        )
    )
    state = {
        "prompt": "use the source",
        "memory_references": [
            {"sessionId": "source-session", "label": "source"}
        ],
    }
    plan = _prepare(runtime, state, execution_id="exec-ref", attempt_id="attempt-ref")

    output = runtime.invoke(
        state,
        execution_id="exec-ref",
        attempt_id="attempt-ref",
        binding=plan.binding,
        attempt_metadata=plan.metadata,
    )

    assert harness.reference_calls == [state["memory_references"]]
    assert output["_dsh"]["memory_mode"] == "S0"
    assert output["_dsh"]["reference_snapshot"] == reference_snapshot
    assert output["_dsh"]["reference_snapshot"]["references"][0][
        "capturedThroughSeq"
    ] == 17


def test_durable_runtime_passes_m0_and_records_ordered_step_captures() -> None:
    latest = {
        "schemaVersion": 1,
        "contentSha256": "f" * 64,
        "references": [{
            "sessionId": "source-session", "label": "source",
            "capturedThroughSeq": 22, "compacted": False,
            "originalMessages": 1, "retainedMessages": 1,
            "omittedMessages": 0, "omittedBytes": 0,
            "truncated": False, "inputIndex": 0,
        }],
    }
    captures = [
        {**latest, "contentSha256": "e" * 64, "turn": 1, "step": 1, "referenceEventSeq": 8},
        {**latest, "turn": 1, "step": 2, "referenceEventSeq": 15},
    ]
    harness = _Harness(_RunResult(
        session_id="wtb-test-exec-m0",
        reference_snapshot=latest,
        reference_snapshots=captures,
        memory_mode="M0",
        events=[{"type": "turn/end", "seq": 16, "data": {"reason": {"kind": "completed"}}}],
    ))
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    runtime.bind(DeepSeekHarnessWorkflow(reference_key="memory_references", memory_mode="M0"))
    state = {
        "prompt": "track source",
        "memory_references": [{"sessionId": "source-session", "label": "source"}],
    }
    plan = _prepare(runtime, state, execution_id="exec-m0", attempt_id="attempt-m0")

    output = runtime.invoke(
        state,
        execution_id="exec-m0",
        attempt_id="attempt-m0",
        binding=plan.binding,
        attempt_metadata=plan.metadata,
    )

    assert harness.memory_mode_calls == ["M0"]
    assert output["_dsh"]["memory_mode"] == "M0"
    assert output["_dsh"]["reference_snapshot"] == latest
    assert output["_dsh"]["reference_snapshots"] == captures


def test_runtime_receipt_omits_event_payloads_by_default() -> None:
    runtime = DeepSeekHarnessRuntime(harness=_Harness())
    runtime.bind(DeepSeekHarnessWorkflow())

    output = runtime.invoke({"prompt": "run"}, execution_id="exec-1")

    assert "events" not in output["_dsh"]
    assert output["_dsh"]["event_types"] == [
        "assistant/message",
        "turn/end",
    ]


def test_runtime_reuses_stable_session_for_same_execution() -> None:
    harness = _Harness()
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    runtime.invoke({"prompt": "first"}, execution_id="exec-1")
    runtime.invoke({"prompt": "second"}, execution_id="exec-1")

    assert [session_id for _, session_id in harness.calls] == [
        "wtb-test-exec-1",
        "wtb-test-exec-1",
    ]


def test_transport_failure_after_dispatch_is_outcome_unknown_and_not_retried() -> None:
    from wtb.domain.interfaces.workflow_runtime import RuntimeOutcomeUnknownError

    class CommitThenDisconnectHarness(_Harness):
        def run(self, prompt: object, *, session_id: str) -> _RunResult:
            self.calls.append((prompt, session_id))
            raise ConnectionError("response lost after durable turn")

    harness = CommitThenDisconnectHarness()
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    with pytest.raises(RuntimeOutcomeUnknownError) as caught:
        runtime.invoke({"prompt": "one attempt"}, execution_id="exec-unknown")

    assert caught.value.session_id == "wtb-test-exec-unknown"
    assert "must be reconciled before retry" in str(caught.value)
    assert harness.calls == [("one attempt", "wtb-test-exec-unknown")]


def test_runtime_rejects_checkpoint_resume_before_calling_harness() -> None:
    harness = _Harness()
    runtime = DeepSeekHarnessRuntime(harness=harness)
    runtime.bind(DeepSeekHarnessWorkflow())

    with pytest.raises(RuntimePreDispatchError) as caught:
        runtime.invoke(
            {"prompt": "resume"},
            execution_id="exec-1",
            checkpoint_id="cp-9",
        )

    assert isinstance(caught.value.__cause__, NotImplementedError)
    assert harness.calls == []


def test_runtime_does_not_report_missing_terminal_as_success() -> None:
    harness = _Harness(
        _RunResult(
            session_id="wtb-test-exec-1",
            finish_reason=None,
            events=[],
        )
    )
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    with pytest.raises(RuntimeOutcomeUnknownError, match="trusted terminal"):
        runtime.invoke({"prompt": "run"}, execution_id="exec-1")
    assert runtime.history("exec-1") == ()


@pytest.mark.parametrize(
    "finish_reason",
    [
        "error",
        "max-tokens",
        "aborted",
        "canceled",
        "cancelled",
        "stopped",
    ],
)
def test_runtime_reports_matching_non_success_terminal_as_known(
    finish_reason: str,
) -> None:
    harness = _Harness(
        _RunResult(
            session_id="wtb-test-exec-1",
            finish_reason=finish_reason,
            events=[
                {
                    "type": "turn/end",
                    "seq": 4,
                    "data": {"reason": {"kind": finish_reason}},
                },
            ],
        )
    )
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    state = {"prompt": "run"}
    plan = _prepare(
        runtime,
        state,
        execution_id="exec-1",
        attempt_id="attempt-1",
    )
    with pytest.raises(RuntimeTerminalError) as caught:
        runtime.invoke(
            state,
            execution_id="exec-1",
            attempt_id="attempt-1",
            binding=plan.binding,
            attempt_metadata=plan.metadata,
        )

    assert caught.value.finish_reason == finish_reason
    assert caught.value.session_id == "wtb-test-exec-1"
    assert caught.value.receipt is not None
    assert caught.value.receipt["finish_reason"] == finish_reason
    assert caught.value.result_state is not None
    history = runtime.history("exec-1")
    assert len(history) == 1
    assert history[0].metadata["recoverable"] is False
    assert history[0].metadata["attempt_id"] == "attempt-1"
    reconciled = runtime.reconcile(
        "exec-1",
        "attempt-1",
        binding=plan.binding,
    )
    assert reconciled is not None
    expected = (
        RuntimeTerminalOutcome.CANCELLED
        if finish_reason
        in {"aborted", "canceled", "cancelled", "stopped"}
        else RuntimeTerminalOutcome.FAILED
    )
    assert reconciled.outcome is expected


def test_runtime_keeps_crash_repaired_interrupted_turn_outcome_unknown() -> None:
    harness = _Harness(
        _RunResult(
            session_id="wtb-test-exec-1",
            finish_reason="interrupted",
            events=[
                {
                    "type": "turn/end",
                    "seq": 4,
                    "data": {"reason": {"kind": "interrupted"}},
                },
            ],
        )
    )
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    state = {"prompt": "run"}
    plan = _prepare(
        runtime,
        state,
        execution_id="exec-1",
        attempt_id="attempt-1",
    )
    with pytest.raises(RuntimeOutcomeUnknownError, match="interrupted"):
        runtime.invoke(
            state,
            execution_id="exec-1",
            attempt_id="attempt-1",
            binding=plan.binding,
            attempt_metadata=plan.metadata,
        )

    assert runtime.history("exec-1") == ()


def test_runtime_rejects_cross_session_result() -> None:
    harness = _Harness(_RunResult(session_id="another-session", events=[]))
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    from wtb.domain.interfaces.workflow_runtime import RuntimeOutcomeUnknownError

    with pytest.raises(RuntimeOutcomeUnknownError, match="trusted terminal"):
        runtime.invoke({"prompt": "run"}, execution_id="exec-1")


def test_runtime_rejects_completed_result_without_terminal_event() -> None:
    harness = _Harness(
        _RunResult(
            session_id="wtb-test-exec-1",
            finish_reason="completed",
            events=[{"type": "assistant/message", "seq": 7}],
        )
    )
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    from wtb.domain.interfaces.workflow_runtime import RuntimeOutcomeUnknownError

    with pytest.raises(RuntimeOutcomeUnknownError, match="trusted terminal"):
        runtime.invoke({"prompt": "run"}, execution_id="exec-1")


def test_malformed_post_dispatch_response_is_outcome_unknown() -> None:
    from wtb.domain.interfaces.workflow_runtime import RuntimeOutcomeUnknownError

    harness = _Harness(
        _RunResult(
            session_id="wtb-test-exec-1",
            events=["not-an-event"],
        )
    )
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    with pytest.raises(RuntimeOutcomeUnknownError) as caught:
        runtime.invoke({"prompt": "run"}, execution_id="exec-1")

    assert caught.value.session_id == "wtb-test-exec-1"


def test_runtime_requires_explicit_workflow_and_prompt() -> None:
    runtime = DeepSeekHarnessRuntime(harness=_Harness())

    with pytest.raises(RuntimePreDispatchError) as unbound:
        runtime.invoke({"prompt": "run"}, execution_id="exec-1")
    assert isinstance(unbound.value.__cause__, RuntimeError)
    assert "No DeepSeek Harness workflow" in str(unbound.value.__cause__)

    runtime.bind(DeepSeekHarnessWorkflow(prompt_key="task"))
    with pytest.raises(RuntimePreDispatchError) as missing_prompt:
        runtime.invoke({"prompt": "run"}, execution_id="exec-1")
    assert isinstance(missing_prompt.value.__cause__, ValueError)
    assert "requires input key 'task'" in str(missing_prompt.value.__cause__)


def test_runtime_rejects_arbitrary_workflow_binding() -> None:
    runtime = DeepSeekHarnessRuntime(harness=_Harness())

    with pytest.raises(TypeError, match="DeepSeekHarnessWorkflow"):
        runtime.bind(object())


def test_runtime_does_not_close_injected_harness_by_default() -> None:
    harness = _Harness()
    runtime = DeepSeekHarnessRuntime(harness=harness)

    runtime.close()

    assert harness.closed is False


def test_runtime_closes_factory_owned_harness() -> None:
    harness = _Harness()
    runtime = DeepSeekHarnessRuntime(
        harness_factory=lambda: harness,
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    runtime.bind(DeepSeekHarnessWorkflow())
    runtime.invoke({"prompt": "run"}, execution_id="exec-1")

    runtime.close()

    assert harness.closed is True


def test_runtime_satisfies_framework_neutral_protocol() -> None:
    from wtb.domain.interfaces.workflow_runtime import WorkflowRuntime

    runtime = DeepSeekHarnessRuntime(harness=_Harness())

    assert isinstance(runtime, WorkflowRuntime)


def test_keyless_sdk_factory_runs_project_and_closes_owned_harness() -> None:
    from wtb.domain.models.workflow import ExecutionStatus
    from wtb.sdk import WTBTestBench
    from wtb.sdk.workflow_project import WorkflowProject

    harness = _Harness()
    runtime = DeepSeekHarnessRuntime(
        harness_factory=lambda: harness,
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    bench = WTBTestBench.create_with_runtime(runtime)
    project = WorkflowProject(
        name="deepseek-runtime",
        graph_factory=lambda: DeepSeekHarnessWorkflow(),
    )

    try:
        bench.register_project(project)
        result = bench.run(
            project=project.name,
            initial_state={"prompt": "keyless smoke"},
        )

        assert result.status is ExecutionStatus.COMPLETED
        assert result.state.workflow_variables["answer"] == "done"
        assert harness.calls == [
            ("keyless smoke", f"wtb-test-{result.id}"),
        ]
        assert bench.supports_time_travel() is False
        assert bench.supports_forking() is False
    finally:
        bench.close()

    assert harness.closed is True


def test_default_factory_reports_missing_optional_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DeepSeekHarnessRuntime()
    runtime.bind(DeepSeekHarnessWorkflow())
    real_import = builtins.__import__

    def reject_sdk(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "deepseek_harness":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_sdk)

    with pytest.raises(RuntimePreDispatchError) as caught:
        runtime.invoke({"prompt": "run"}, execution_id="exec-1")
    assert caught.value.__cause__ is not None
    assert "sdk_source_path" in str(caught.value.__cause__)


def test_local_sdk_source_receives_explicit_runtime_command_and_config(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "deepseek-harness"
    package = checkout / "python" / "sdk" / "src" / "deepseek_harness"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        """
class _Result:
    def __init__(self, session_id, answer):
        self.session_id = session_id
        self.final_response = answer
        self.finish_reason = "completed"
        self.events = [{"type": "turn/end", "seq": 3, "data": {"reason": {"kind": "completed"}}}]
        self.session_root = "local-sessions"

class DeepSeekHarness:
    def __init__(self, **options):
        self.options = options

    def run(self, prompt, *, session_id):
        command = "|".join(self.options["launch_args_override"])
        answer = f"{command};{self.options['cordis']};{prompt}"
        return _Result(session_id, answer)

    def close(self):
        pass
""".lstrip(),
        encoding="utf-8",
    )
    original_sys_path = tuple(sys.path)
    installed_sdk_module = sys.modules.get("deepseek_harness")
    runtime = DeepSeekHarnessRuntime(
        sdk_source_path=checkout,
        runtime_command=("node.exe", "runtime/packaged-bin.js"),
        harness_options={"cordis": "runtime/cordis.yml"},
        session_namespace=_TEST_NAMESPACE,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    output = runtime.invoke({"prompt": "run locally"}, execution_id="exec-local")

    assert output["answer"] == (
        "node.exe|runtime/packaged-bin.js;runtime/cordis.yml;run locally"
    )
    assert tuple(sys.path) == original_sys_path
    assert sys.modules.get("deepseek_harness") is installed_sdk_module


def test_local_sdk_source_path_fails_before_runtime_creation(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="python/sdk/src"):
        DeepSeekHarnessRuntime(
            sdk_source_path=tmp_path,
            runtime_command=("node", "runtime.js"),
        )


@pytest.mark.parametrize(
    "command, error",
    [
        ("node runtime.js", TypeError),
        ((), ValueError),
        (("node", ""), ValueError),
    ],
)
def test_runtime_command_requires_explicit_argv(
    command: Any,
    error: type[Exception],
) -> None:
    with pytest.raises(error, match="runtime_command"):
        DeepSeekHarnessRuntime(runtime_command=command)


def test_runtime_command_rejects_ambiguous_sdk_launch_options() -> None:
    with pytest.raises(TypeError, match="launch_args_override"):
        DeepSeekHarnessRuntime(
            runtime_command=("node", "runtime.js"),
            harness_options={"launch_args_override": ("other",)},
        )


def test_completed_turn_end_need_not_be_the_last_event() -> None:
    harness = _Harness(
        _RunResult(
            session_id="wtb-test-exec-tail",
            events=[
                {
                    "type": "turn/end",
                    "seq": 8,
                    "data": {"reason": {"kind": "completed"}},
                },
                {"type": "feedback/record", "seq": 9, "data": {"ok": True}},
            ],
        )
    )
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    output = runtime.invoke({"prompt": "run"}, execution_id="exec-tail")

    assert output["answer"] == "done"
    assert output["_dsh"]["finish_reason"] == "completed"
    assert output["_dsh"]["last_event_seq"] == 9
    assert output["_dsh"]["event_types"] == ["turn/end", "feedback/record"]


def test_rc6_wire_events_without_seq_keep_receipt_truthful() -> None:
    harness = _Harness(
        _RunResult(
            session_id="wtb-test-exec-no-seq",
            events=[
                {"type": "assistant/message", "data": {"content": []}},
                {
                    "type": "turn/end",
                    "data": {"reason": {"kind": "completed"}},
                },
            ],
        )
    )
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    output = runtime.invoke({"prompt": "run"}, execution_id="exec-no-seq")

    assert output["_dsh"]["last_event_seq"] is None


@pytest.mark.parametrize(
    "events",
    [
        [
            {"type": "assistant/message", "seq": 1},
            {
                "type": "turn/end",
                "data": {"reason": {"kind": "completed"}},
            },
        ],
        [
            {
                "type": "turn/end",
                "seq": True,
                "data": {"reason": {"kind": "completed"}},
            },
        ],
        [
            {"type": "assistant/message", "seq": 2},
            {
                "type": "turn/end",
                "seq": 2,
                "data": {"reason": {"kind": "completed"}},
            },
        ],
        [
            {
                "type": "turn/end",
                "seq": -1,
                "data": {"reason": {"kind": "completed"}},
            },
        ],
    ],
    ids=["partial", "bool", "duplicate", "negative"],
)
def test_untrustworthy_event_sequences_are_outcome_unknown(
    events: list[dict[str, Any]],
) -> None:
    harness = _Harness(
        _RunResult(
            session_id="wtb-test-exec-seq",
            events=events,
        )
    )
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    with pytest.raises(RuntimeOutcomeUnknownError):
        runtime.invoke({"prompt": "run"}, execution_id="exec-seq")

    assert runtime.history("exec-seq") == ()


@pytest.mark.parametrize(
    "result",
    [
        _RunResult(
            session_id="wtb-test-exec-schema",
            final_response=123,  # type: ignore[arg-type]
            events=[
                {
                    "type": "turn/end",
                    "seq": 1,
                    "data": {"reason": {"kind": "completed"}},
                }
            ],
        ),
        _RunResult(
            session_id="wtb-test-exec-schema",
            finish_reason="completed",
            events=[
                {
                    "type": "turn/end",
                    "seq": 1,
                    "data": {"reason": {"kind": "error"}},
                }
            ],
        ),
        _RunResult(
            session_id="wtb-test-exec-schema",
            events=[
                {
                    "type": 7,
                    "seq": 1,
                    "data": {"reason": {"kind": "completed"}},
                }
            ],
        ),
        _RunResult(
            session_id="wtb-test-exec-schema",
            session_root=5,  # type: ignore[arg-type]
            events=[
                {
                    "type": "turn/end",
                    "seq": 1,
                    "data": {"reason": {"kind": "completed"}},
                }
            ],
        ),
    ],
    ids=["final-response", "reason-mismatch", "event-type", "session-root"],
)
def test_malformed_post_dispatch_schema_is_unknown_without_history(
    result: _RunResult,
) -> None:
    runtime = DeepSeekHarnessRuntime(
        harness=_Harness(result),
        session_namespace=_TEST_NAMESPACE,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    with pytest.raises(RuntimeOutcomeUnknownError):
        runtime.invoke({"prompt": "run"}, execution_id="exec-schema")

    assert runtime.history("exec-schema") == ()


def test_start_failure_is_typed_pre_dispatch_and_never_calls_run() -> None:
    class StartFailureHarness(_Harness):
        def start(self) -> None:
            raise OSError("initialize failed")

    harness = StartFailureHarness()
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    state = {"prompt": "run"}
    with pytest.raises(RuntimePreDispatchError) as caught:
        runtime.invoke(
            state,
            execution_id="exec-start",
        )

    assert isinstance(caught.value.__cause__, OSError)
    assert harness.calls == []
    assert runtime.history("exec-start") == ()


@pytest.mark.parametrize(
    "prompt",
    [
        [{"type": "text", "text": b"bytes"}],
        [{"type": "text", "score": float("nan")}],
        [{"type": "text", "score": float("inf")}],
        [{1: "non-string-key"}],
        ["not-an-object"],
        ("not", "a", "list"),
    ],
    ids=["bytes", "nan", "infinity", "key", "block", "tuple"],
)
def test_invalid_content_blocks_fail_before_dispatch(prompt: Any) -> None:
    harness = _Harness()
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    with pytest.raises(RuntimePreDispatchError):
        runtime.invoke(
            {"prompt": prompt},
            execution_id="exec-input",
            attempt_id="attempt-input",
        )

    assert harness.calls == []


def test_cyclic_content_block_fails_before_dispatch() -> None:
    block: dict[str, Any] = {"type": "text"}
    block["cycle"] = block
    harness = _Harness()
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    with pytest.raises(RuntimePreDispatchError):
        runtime.invoke({"prompt": [block]}, execution_id="exec-cycle")

    assert harness.calls == []


@pytest.mark.parametrize(
    "execution_id",
    ["../escape", "tenant/id", "has space", "租户", "x" * 129],
)
def test_execution_id_is_safe_and_bounded_before_dispatch(
    execution_id: str,
) -> None:
    harness = _Harness()
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    with pytest.raises(RuntimePreDispatchError):
        runtime.invoke({"prompt": "run"}, execution_id=execution_id)

    assert harness.calls == []


@pytest.mark.parametrize("namespace", ["", "../tenant", "tenant/id", "x" * 65])
def test_session_namespace_is_safe_and_bounded(namespace: str) -> None:
    with pytest.raises(ValueError, match="session_namespace"):
        DeepSeekHarnessRuntime(harness=_Harness(), session_namespace=namespace)


def test_default_namespaces_isolate_equal_execution_ids() -> None:
    first_harness = _Harness()
    second_harness = _Harness()
    first = DeepSeekHarnessRuntime(harness=first_harness)
    second = DeepSeekHarnessRuntime(harness=second_harness)
    first.bind(DeepSeekHarnessWorkflow())
    second.bind(DeepSeekHarnessWorkflow())

    first_output = first.invoke({"prompt": "run"}, execution_id="same")
    second_output = second.invoke({"prompt": "run"}, execution_id="same")

    first_session = first_output["_dsh"]["session_id"]
    second_session = second_output["_dsh"]["session_id"]
    assert first_session != second_session
    assert first_output["_dsh"]["execution_id"] == "same"
    assert second_output["_dsh"]["execution_id"] == "same"


def test_prepare_attempt_requires_explicit_namespace_and_is_side_effect_free() -> None:
    digest = "a" * 64
    harness = _Harness()
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace="tenant-a",
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    runtime.bind(DeepSeekHarnessWorkflow(session_prefix="wtb-"))

    plan = runtime.prepare_attempt(
        "exec-1",
        "attempt-1",
        input_sha256=digest,
    )

    assert plan.session_id == "wtb-tenant-a-exec-1"
    assert plan.metadata["input_sha256"] == digest
    assert plan.metadata["durable_reconcile_supported"] is True
    assert harness.calls == []

    unsafe_default = DeepSeekHarnessRuntime(harness=_Harness())
    unsafe_default.bind(DeepSeekHarnessWorkflow())
    with pytest.raises(RuntimePreDispatchError) as caught:
        unsafe_default.prepare_attempt(
            "exec-1",
            "attempt-1",
            input_sha256=digest,
        )
    assert isinstance(caught.value.__cause__, ValueError)


def test_fixed_session_id_is_exactly_planned_without_changing_default() -> None:
    digest = "a" * 64
    fixed = DeepSeekHarnessRuntime(
        harness=_Harness(),
        session_namespace="tenant-a",
        fixed_session_id="source-session-1",
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    fixed.bind(DeepSeekHarnessWorkflow(session_prefix="wtb-"))
    default = DeepSeekHarnessRuntime(
        harness=_Harness(),
        session_namespace="tenant-a",
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    default.bind(DeepSeekHarnessWorkflow(session_prefix="wtb-"))

    fixed_plan = fixed.prepare_attempt(
        "exec-2",
        "attempt-2",
        input_sha256=digest,
    )
    default_plan = default.prepare_attempt(
        "exec-2",
        "attempt-2",
        input_sha256=digest,
    )

    assert fixed_plan.session_id == "source-session-1"
    assert fixed_plan.metadata["fixed_session_id"] == "source-session-1"
    assert default_plan.session_id == "wtb-tenant-a-exec-2"
    assert default_plan.metadata["fixed_session_id"] is None


@pytest.mark.parametrize("session_id", ["", "../source", "source/id", "x" * 129])
def test_fixed_session_id_is_safe_and_bounded(session_id: str) -> None:
    with pytest.raises(ValueError, match="fixed_session_id"):
        DeepSeekHarnessRuntime(
            harness=_Harness(),
            session_namespace="tenant-a",
            fixed_session_id=session_id,
        )


def test_attempt_receipt_reconcile_and_same_process_idempotency() -> None:
    state = {"prompt": "run", "case": "stable"}
    expected_hash = hashlib.sha256(
        json.dumps(
            state,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=repr,
        ).encode("utf-8")
    ).hexdigest()
    harness = _Harness()
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    plan = _prepare(
        runtime,
        state,
        execution_id="exec-attempt",
        attempt_id="attempt-1",
    )
    first = runtime.invoke(
        state,
        execution_id="exec-attempt",
        attempt_id="attempt-1",
        binding=plan.binding,
        attempt_metadata=plan.metadata,
    )
    replay = runtime.invoke(
        state,
        execution_id="exec-attempt",
        attempt_id="attempt-1",
        binding=plan.binding,
        attempt_metadata=plan.metadata,
    )

    assert first == replay
    assert len(harness.calls) == 1
    assert first["_dsh"]["attempt_id"] == "attempt-1"
    assert first["_dsh"]["input_sha256"] == expected_hash
    reconciled = runtime.reconcile(
        "exec-attempt",
        "attempt-1",
        binding=plan.binding,
    )
    assert reconciled is not None
    assert reconciled.outcome is RuntimeTerminalOutcome.COMPLETED
    assert reconciled.input_sha256 == expected_hash
    assert reconciled.receipt["input_sha256"] == expected_hash

    with pytest.raises(RuntimePreDispatchError) as changed:
        runtime.invoke(
            {"prompt": "changed", "case": "stable"},
            execution_id="exec-attempt",
            attempt_id="attempt-1",
            binding=plan.binding,
            attempt_metadata=plan.metadata,
        )
    assert "input" in str(changed.value)
    assert len(harness.calls) == 1


def test_attempt_identity_is_forwarded_to_public_sdk_and_receipt() -> None:
    state = {"prompt": "run", "case": "durable"}
    expected_hash = hashlib.sha256(
        json.dumps(
            state,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=repr,
        ).encode("utf-8")
    ).hexdigest()
    harness = _Harness()
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    plan = _prepare(
        runtime,
        state,
        execution_id="exec-identity",
        attempt_id="attempt-identity",
    )
    output = runtime.invoke(
        state,
        execution_id="exec-identity",
        attempt_id="attempt-identity",
        binding=plan.binding,
        attempt_metadata=plan.metadata,
    )

    assert harness.identity_calls == [
        (
            "run",
            "wtb-test-exec-identity",
            "attempt-identity",
            expected_hash,
        )
    ]
    assert output["_dsh"]["attempt_id"] == "attempt-identity"
    assert output["_dsh"]["input_sha256"] == expected_hash
    assert output["_dsh"]["message_id"] == "message-attempt-identity"
    assert output["_dsh"]["attempt_status"] == "terminal"


def test_controller_fence_and_adapter_use_one_order_independent_input_hash() -> None:
    controller_state = {
        "prompt": "same",
        "nested": {"b": [2, {"z": None}], "a": 1},
    }
    adapter_state = {
        "nested": {"a": 1, "b": [2, {"z": None}]},
        "prompt": "same",
    }
    expected_hash = ExecutionController._runtime_input_sha256(controller_state)
    harness = _Harness()
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    plan = _prepare(
        runtime,
        adapter_state,
        execution_id="exec-canonical-hash",
        attempt_id="attempt-canonical-hash",
    )
    runtime.invoke(
        adapter_state,
        execution_id="exec-canonical-hash",
        attempt_id="attempt-canonical-hash",
        binding=plan.binding,
        attempt_metadata=plan.metadata,
    )

    assert harness.identity_calls[0][3] == expected_hash


def test_non_json_state_is_rejected_before_deepseek_dispatch() -> None:
    harness = _Harness()
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    with pytest.raises(RuntimePreDispatchError) as error:
        runtime.invoke(
            {"prompt": "reject", "bad": object()},
            execution_id="exec-non-json",
            attempt_id="attempt-non-json",
        )

    assert isinstance(error.value.__cause__, TypeError)
    assert harness.calls == []


def test_legacy_sdk_without_durable_attempt_api_fails_before_dispatch() -> None:
    class LegacyHarness:
        def __init__(self) -> None:
            self.run_calls = 0

        def run(self, prompt: object, *, session_id: str) -> _RunResult:
            del prompt
            self.run_calls += 1
            return _RunResult(session_id=session_id, events=[])

        def get_runtime_binding(self) -> _RuntimeBinding:
            return _RuntimeBinding()

        def close(self) -> None:
            return None

    harness = LegacyHarness()
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    state = {"prompt": "must not dispatch"}
    with pytest.raises(RuntimePreDispatchError, match="process-generation"):
        _prepare(
            runtime,
            state,
            execution_id="exec-legacy-sdk",
            attempt_id="attempt-legacy-sdk",
        )

    assert harness.run_calls == 0


def test_restart_reconcile_looks_up_terminal_attempt_without_second_turn() -> None:
    state = {"prompt": "only once", "case": "response-lost"}
    expected_hash = DeepSeekHarnessRuntime._state_sha256(state)
    attempts: dict[tuple[str, str], _RunResult] = {}

    class CommitThenDisconnectHarness(_Harness):
        def run(
            self,
            prompt: object,
            *,
            session_id: str,
            attempt_id: str | None = None,
            input_sha256: str | None = None,
            expected_process_generation: str | None = None,
        ) -> _RunResult:
            result = super().run(
                prompt,
                session_id=session_id,
                attempt_id=attempt_id,
                input_sha256=input_sha256,
                expected_process_generation=expected_process_generation,
            )
            assert result.status is None
            raise ConnectionError("WTB response lost after durable terminal")

    first_harness = CommitThenDisconnectHarness(attempts=attempts)
    first = DeepSeekHarnessRuntime(
        harness=first_harness,
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    first.bind(DeepSeekHarnessWorkflow())

    plan = _prepare(
        first,
        state,
        execution_id="exec-restart",
        attempt_id="attempt-restart",
    )
    with pytest.raises(RuntimeOutcomeUnknownError):
        first.invoke(
            state,
            execution_id="exec-restart",
            attempt_id="attempt-restart",
            binding=plan.binding,
            attempt_metadata=plan.metadata,
        )

    restarted_harness = _Harness(attempts=attempts)
    restarted = DeepSeekHarnessRuntime(
        harness=restarted_harness,
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    restarted.bind(DeepSeekHarnessWorkflow())

    receipt = restarted.reconcile(
        "exec-restart",
        "attempt-restart",
        binding=plan.binding,
        input_state=state,
        attempt_metadata=plan.metadata,
    )

    assert receipt is not None
    assert receipt.outcome is RuntimeTerminalOutcome.COMPLETED
    assert receipt.input_sha256 == expected_hash
    assert receipt.values["answer"] == "done"
    assert (
        receipt.receipt["operator_config_sha256"]
        == plan.metadata["operator_config_sha256"]
    )
    assert (
        receipt.receipt["root_config_sha256"]
        == plan.metadata["root_config_sha256"]
    )
    assert restarted_harness.get_attempt_calls == [
        (
            "wtb-test-exec-restart",
            "attempt-restart",
            expected_hash,
        )
    ]
    assert restarted_harness.calls == []
    assert first_harness.calls == [
        ("only once", "wtb-test-exec-restart"),
    ]


def test_restart_reconcile_not_found_stays_unknown_without_freshness_proof() -> None:
    state = {"prompt": "safe redrive", "case": "pre-accept-crash"}
    expected_hash = DeepSeekHarnessRuntime._state_sha256(state)
    harness = _Harness(attempts={})
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    plan = _prepare(
        runtime,
        state,
        execution_id="exec-redrive",
        attempt_id="attempt-redrive",
    )
    with pytest.raises(RuntimeOutcomeUnknownError, match="not found"):
        runtime.reconcile(
            "exec-redrive",
            "attempt-redrive",
            binding=plan.binding,
            input_state=state,
        )
    assert harness.get_attempt_calls == [
        ("wtb-test-exec-redrive", "attempt-redrive", expected_hash),
    ]
    assert harness.identity_calls == []


def test_durable_lookup_identity_conflict_is_never_materialized() -> None:
    state = {"prompt": "owned", "case": "identity"}
    expected_hash = DeepSeekHarnessRuntime._state_sha256(state)
    harness = _Harness(
        attempts={
            ("wtb-test-exec-conflict", "attempt-conflict"): _RunResult(
                session_id="wtb-test-exec-conflict",
                attempt_id="attempt-conflict",
                input_sha256="f" * 64,
                message_id="message-conflict",
                status="terminal",
                final_response="foreign",
                finish_reason={"kind": "completed"},
                events=None,
            )
        }
    )
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    plan = _prepare(
        runtime,
        state,
        execution_id="exec-conflict",
        attempt_id="attempt-conflict",
    )
    with pytest.raises(RuntimeError, match="identity|input"):
        runtime.reconcile(
            "exec-conflict",
            "attempt-conflict",
            binding=plan.binding,
            input_state=state,
        )

    assert harness.get_attempt_calls == [
        ("wtb-test-exec-conflict", "attempt-conflict", expected_hash),
    ]
    assert runtime.history("exec-conflict") == ()


def test_cancel_uses_exact_durable_attempt_identity() -> None:
    input_sha256 = "a" * 64
    harness = _Harness(attempts={})
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    plan = runtime.prepare_attempt(
        "exec-cancel",
        "attempt-cancel",
        input_sha256=input_sha256,
    )
    receipt = runtime.cancel(
        "exec-cancel",
        "attempt-cancel",
        binding=plan.binding,
        input_sha256=input_sha256,
        attempt_metadata=plan.metadata,
    )

    assert receipt.outcome is RuntimeTerminalOutcome.CANCELLED
    assert receipt.session_id == "wtb-test-exec-cancel"
    assert receipt.input_sha256 == input_sha256
    assert harness.cancel_attempt_calls == [
        ("wtb-test-exec-cancel", "attempt-cancel", input_sha256),
    ]


def test_cancel_reference_workflow_preserves_validated_reference_receipt() -> None:
    reference_snapshot = {
        "schemaVersion": 1,
        "contentSha256": "e" * 64,
        "references": [
            {
                "sessionId": "source-session",
                "label": "source",
                "capturedThroughSeq": 17,
                "compacted": False,
                "originalMessages": 1,
                "retainedMessages": 1,
                "omittedMessages": 0,
                "omittedBytes": 0,
                "truncated": False,
                "inputIndex": 0,
            }
        ],
    }

    class ReferenceCancelHarness(_Harness):
        def cancel_attempt(self, **kwargs) -> _RunResult:
            result = super().cancel_attempt(**kwargs)
            result.reference_snapshot = reference_snapshot
            result.memory_mode = "S0"
            return result

    harness = ReferenceCancelHarness(attempts={})
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    runtime.bind(
        DeepSeekHarnessWorkflow(
            reference_key="memory_references",
            memory_mode="S0",
        )
    )
    state = {
        "prompt": "use the source",
        "memory_references": [
            {"sessionId": "source-session", "label": "source"}
        ],
    }
    input_sha256 = runtime._state_sha256(state)
    plan = runtime.prepare_attempt(
        "exec-reference-cancel",
        "attempt-reference-cancel",
        input_sha256=input_sha256,
    )

    receipt = runtime.cancel(
        "exec-reference-cancel",
        "attempt-reference-cancel",
        binding=plan.binding,
        input_state=state,
        input_sha256=input_sha256,
        attempt_metadata=plan.metadata,
    )

    assert receipt.outcome is RuntimeTerminalOutcome.CANCELLED
    assert receipt.receipt["memory_mode"] == "S0"
    assert receipt.receipt["reference_snapshot"] == reference_snapshot


def test_cancel_rejects_input_state_drift_before_cancel_io() -> None:
    state = {"prompt": "original"}
    harness = _Harness(attempts={})
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    runtime.bind(DeepSeekHarnessWorkflow())
    plan = _prepare(
        runtime,
        state,
        execution_id="exec-drifted-cancel",
        attempt_id="attempt-drifted-cancel",
    )

    with pytest.raises(RuntimeBindingMismatchError, match="input"):
        runtime.cancel(
            "exec-drifted-cancel",
            "attempt-drifted-cancel",
            binding=plan.binding,
            input_state={"prompt": "changed"},
            input_sha256=plan.binding.input_sha256,
            attempt_metadata=plan.metadata,
        )

    assert harness.cancel_attempt_calls == []


def test_cancel_rejects_restarted_process_generation_before_cancel_io() -> None:
    input_sha256 = "e" * 64
    harness = _Harness(attempts={})
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    runtime.bind(DeepSeekHarnessWorkflow())
    plan = runtime.prepare_attempt(
        "exec-restarted-cancel",
        "attempt-restarted-cancel",
        input_sha256=input_sha256,
    )
    assert plan.metadata["process_generation"] == "generation-1"
    harness.process_generation = "generation-2"

    with pytest.raises(RuntimeOutcomeUnknownError, match="process generation"):
        runtime.cancel(
            "exec-restarted-cancel",
            "attempt-restarted-cancel",
            binding=plan.binding,
            input_sha256=input_sha256,
            attempt_metadata=plan.metadata,
        )

    assert harness.cancel_attempt_calls == []
    assert harness.get_attempt_calls == []


def test_reconcile_keeps_crash_repaired_interrupted_attempt_unknown() -> None:
    state = {"prompt": "resume after crash"}
    input_sha256 = DeepSeekHarnessRuntime._state_sha256(state)
    attempt = _RunResult(
        session_id="wtb-test-exec-interrupted",
        attempt_id="attempt-interrupted",
        input_sha256=input_sha256,
        message_id="message-interrupted",
        status="terminal",
        final_response="",
        finish_reason={"kind": "interrupted"},
        events=None,
    )
    harness = _Harness(
        attempts={(attempt.session_id, "attempt-interrupted"): attempt}
    )
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    plan = _prepare(
        runtime,
        state,
        execution_id="exec-interrupted",
        attempt_id="attempt-interrupted",
    )
    with pytest.raises(RuntimeOutcomeUnknownError, match="interrupted"):
        runtime.reconcile(
            "exec-interrupted",
            "attempt-interrupted",
            binding=plan.binding,
            input_state=state,
        )

    assert runtime.history("exec-interrupted") == ()


def test_cancel_does_not_acknowledge_crash_repaired_interrupted_attempt() -> None:
    input_sha256 = "b" * 64
    attempt = _RunResult(
        session_id="wtb-test-exec-interrupted-cancel",
        attempt_id="attempt-interrupted-cancel",
        input_sha256=input_sha256,
        message_id="message-interrupted-cancel",
        status="terminal",
        final_response="",
        finish_reason={"kind": "interrupted"},
        events=None,
    )
    harness = _Harness(
        attempts={(attempt.session_id, "attempt-interrupted-cancel"): attempt}
    )
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    plan = runtime.prepare_attempt(
        "exec-interrupted-cancel",
        "attempt-interrupted-cancel",
        input_sha256=input_sha256,
    )
    with pytest.raises(RuntimeOutcomeUnknownError, match="interrupted"):
        runtime.cancel(
            "exec-interrupted-cancel",
            "attempt-interrupted-cancel",
            binding=plan.binding,
            input_sha256=input_sha256,
            attempt_metadata=plan.metadata,
        )


def test_sqlite_fence_restart_not_found_stays_recovery_required(
    tmp_path: Path,
) -> None:
    from wtb.domain.models.workflow import ExecutionStatus
    from wtb.sdk import WTBTestBench
    from wtb.sdk.workflow_project import WorkflowProject

    attempts: dict[tuple[str, str], _RunResult] = {}

    class RequestNeverArrivedHarness(_Harness):
        def run(
            self,
            prompt: object,
            *,
            session_id: str,
            attempt_id: str | None = None,
            input_sha256: str | None = None,
            expected_process_generation: str | None = None,
        ) -> _RunResult:
            if expected_process_generation != self.process_generation:
                raise RuntimeGenerationMismatchError
            self.calls.append((prompt, session_id))
            self.identity_calls.append(
                (prompt, session_id, attempt_id, input_sha256)
            )
            raise ConnectionError("request never reached DSH")

    first_harness = RequestNeverArrivedHarness(attempts=attempts)
    first_runtime = DeepSeekHarnessRuntime(
        harness=first_harness,
        session_namespace="restart-redrive",
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    first_runtime.bind(DeepSeekHarnessWorkflow())
    first_bench = WTBTestBench.create_with_runtime(
        first_runtime,
        data_dir=str(tmp_path / "wtb-redrive"),
    )
    project = WorkflowProject(
        name="dsh-redrive",
        graph_factory=DeepSeekHarnessWorkflow,
    )
    first_bench.register_project(project)
    try:
        fenced = first_bench.run(
            project=project.name,
            initial_state={"prompt": "redrive exactly once"},
        )
        assert fenced.status is ExecutionStatus.RUNNING
        attempt_id = fenced.metadata["runtime_attempt"]["attempt_id"]
        assert attempts == {}
    finally:
        first_bench.close()

    restarted_harness = _Harness(attempts=attempts)
    restarted_runtime = DeepSeekHarnessRuntime(
        harness=restarted_harness,
        session_namespace="restart-redrive",
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    restarted_runtime.bind(DeepSeekHarnessWorkflow())
    restarted_bench = WTBTestBench.create_with_runtime(
        restarted_runtime,
        data_dir=str(tmp_path / "wtb-redrive"),
    )
    try:
        with pytest.raises(RuntimeOutcomeUnknownError, match="not found"):
            restarted_bench.reconcile_runtime_outcome(
                fenced.id,
                attempt_id=attempt_id,
            )
        unresolved = restarted_bench.get_execution(fenced.id)
        assert unresolved.status is ExecutionStatus.RUNNING
        assert unresolved.metadata["recovery_required"] is True
        assert restarted_harness.identity_calls == []
        assert attempts == {}
    finally:
        restarted_bench.close()


def test_sqlite_restart_looks_up_terminal_after_wtb_final_commit_loss(
    tmp_path: Path,
) -> None:
    from unittest.mock import MagicMock

    from wtb.domain.models.workflow import ExecutionStatus
    from wtb.sdk import WTBTestBench
    from wtb.sdk.workflow_project import WorkflowProject

    attempts: dict[tuple[str, str], _RunResult] = {}
    first_harness = _Harness(attempts=attempts)
    first_runtime = DeepSeekHarnessRuntime(
        harness=first_harness,
        session_namespace="restart-terminal",
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    first_runtime.bind(DeepSeekHarnessWorkflow())
    data_dir = tmp_path / "wtb-terminal"
    first_bench = WTBTestBench.create_with_runtime(
        first_runtime,
        data_dir=str(data_dir),
    )
    project = WorkflowProject(
        name="dsh-terminal",
        graph_factory=DeepSeekHarnessWorkflow,
    )
    first_bench.register_project(project)
    workflow = first_bench._project_service.get_workflow_by_name(project.name)
    assert workflow is not None
    created = first_bench._exec_ctrl.create_execution(
        workflow,
        initial_state={"prompt": "commit once"},
    )
    first_bench._exec_ctrl._commit_fn = MagicMock(
        side_effect=RuntimeError("WTB final commit lost")
    )
    try:
        with pytest.raises(RuntimeError, match="final commit lost"):
            first_bench._exec_ctrl.run(
                created.id,
                workflow=DeepSeekHarnessWorkflow(),
            )
        assert len(attempts) == 1
        attempt_id = next(iter(attempts))[1]
    finally:
        first_bench.close()

    restarted_harness = _Harness(attempts=attempts)
    restarted_runtime = DeepSeekHarnessRuntime(
        harness=restarted_harness,
        session_namespace="restart-terminal",
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    restarted_runtime.bind(DeepSeekHarnessWorkflow())
    restarted_bench = WTBTestBench.create_with_runtime(
        restarted_runtime,
        data_dir=str(data_dir),
    )
    try:
        recovered = restarted_bench.reconcile_runtime_outcome(
            created.id,
            attempt_id=attempt_id,
        )
        assert recovered.status is ExecutionStatus.COMPLETED
        assert recovered.state.workflow_variables["answer"] == "done"
        assert restarted_harness.calls == []
        assert len(attempts) == 1
    finally:
        restarted_bench.close()


def test_two_sqlite_reconcilers_commit_one_terminal_transition_and_outbox(
    tmp_path: Path,
) -> None:
    from wtb.domain.models.outbox import OutboxEventType
    from wtb.domain.models.workflow import ExecutionStatus
    from wtb.sdk import WTBTestBench
    from wtb.sdk.workflow_project import WorkflowProject

    attempts: dict[tuple[str, str], _RunResult] = {}

    class CommitThenDisconnectHarness(_Harness):
        def run(
            self,
            prompt: object,
            *,
            session_id: str,
            attempt_id: str | None = None,
            input_sha256: str | None = None,
            expected_process_generation: str | None = None,
        ) -> _RunResult:
            super().run(
                prompt,
                session_id=session_id,
                attempt_id=attempt_id,
                input_sha256=input_sha256,
                expected_process_generation=expected_process_generation,
            )
            raise ConnectionError("terminal response lost")

    data_dir = tmp_path / "wtb-double-reconcile"
    initial_runtime = DeepSeekHarnessRuntime(
        harness=CommitThenDisconnectHarness(attempts=attempts),
        session_namespace="double-reconcile",
        runtime_config_sha256=_TEST_CONFIG_SHA256,
    )
    initial_runtime.bind(DeepSeekHarnessWorkflow())
    initial_bench = WTBTestBench.create_with_runtime(
        initial_runtime,
        data_dir=str(data_dir),
    )
    project = WorkflowProject(
        name="double-reconcile",
        graph_factory=DeepSeekHarnessWorkflow,
    )
    initial_bench.register_project(project)
    try:
        fenced = initial_bench.run(
            project=project.name,
            initial_state={"prompt": "one terminal transition"},
        )
        assert fenced.status is ExecutionStatus.RUNNING
        attempt_id = fenced.metadata["runtime_attempt"]["attempt_id"]
    finally:
        initial_bench.close()

    lookup_barrier = threading.Barrier(2)

    class BarrierLookupHarness(_Harness):
        def get_attempt(
            self,
            *,
            session_id: str,
            attempt_id: str,
            input_sha256: str,
        ) -> _RunResult | None:
            lookup_barrier.wait(timeout=3)
            return super().get_attempt(
                session_id=session_id,
                attempt_id=attempt_id,
                input_sha256=input_sha256,
            )

    benches = []
    for _ in range(2):
        runtime = DeepSeekHarnessRuntime(
            harness=BarrierLookupHarness(attempts=attempts),
            session_namespace="double-reconcile",
            runtime_config_sha256=_TEST_CONFIG_SHA256,
        )
        runtime.bind(DeepSeekHarnessWorkflow())
        benches.append(
            WTBTestBench.create_with_runtime(runtime, data_dir=str(data_dir))
        )

    results: list[Any] = []
    errors: list[BaseException] = []

    def reconcile(bench: Any) -> None:
        try:
            results.append(
                bench.reconcile_runtime_outcome(
                    fenced.id,
                    attempt_id=attempt_id,
                )
            )
        except BaseException as error:  # noqa: BLE001 - asserted below
            errors.append(error)

    workers = [
        threading.Thread(target=reconcile, args=(bench,)) for bench in benches
    ]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        assert all(not worker.is_alive() for worker in workers)
        assert errors == []
        assert len(results) == 2
        assert all(result.status is ExecutionStatus.COMPLETED for result in results)

        outbox = benches[0]._exec_ctrl._inner._uow.outbox.list_all(limit=100)
        assert sum(
            event.event_type is OutboxEventType.EXECUTION_RECONCILED
            for event in outbox
        ) == 1
        assert sum(
            event.event_type is OutboxEventType.EXECUTION_COMPLETED
            for event in outbox
        ) == 1
    finally:
        for bench in benches:
            bench.close()


def test_receipt_event_count_limit_omits_evidence_without_changing_success() -> None:
    result = _RunResult(
        session_id="wtb-test-exec-count",
        events=[
            {
                "type": "turn/end",
                "seq": 1,
                "data": {"reason": {"kind": "completed"}},
            },
            {"type": "feedback/record", "seq": 2},
        ],
    )
    runtime = DeepSeekHarnessRuntime(
        harness=_Harness(result),
        session_namespace=_TEST_NAMESPACE,
    )
    runtime.bind(DeepSeekHarnessWorkflow(include_events=True, max_event_count=1))

    output = runtime.invoke({"prompt": "run"}, execution_id="exec-count")
    receipt = output["_dsh"]

    assert output["answer"] == "done"
    assert receipt["event_count"] == 2
    assert receipt["event_evidence_omitted_reason"] == "event_count_limit"
    assert "event_types" not in receipt
    assert "events" not in receipt


def test_receipt_event_byte_limit_omits_evidence_without_changing_success() -> None:
    result = _RunResult(
        session_id="wtb-test-exec-bytes",
        events=[
            {
                "type": "turn/end",
                "seq": 1,
                "data": {
                    "reason": {"kind": "completed"},
                    "padding": "sensitive" * 100,
                },
            }
        ],
    )
    runtime = DeepSeekHarnessRuntime(
        harness=_Harness(result),
        session_namespace=_TEST_NAMESPACE,
    )
    runtime.bind(DeepSeekHarnessWorkflow(include_events=True, max_event_bytes=16))

    output = runtime.invoke({"prompt": "run"}, execution_id="exec-bytes")
    receipt = output["_dsh"]

    assert output["answer"] == "done"
    assert receipt["event_bytes"] > 16
    assert receipt["event_evidence_omitted_reason"] == "event_bytes_limit"
    assert "event_types" not in receipt
    assert "events" not in receipt


def test_included_event_evidence_is_a_bounded_deep_copy() -> None:
    events = [
        {
            "type": "turn/end",
            "seq": 1,
            "data": {"reason": {"kind": "completed"}},
        }
    ]
    result = _RunResult(
        session_id="wtb-test-exec-copy",
        events=events,
    )
    runtime = DeepSeekHarnessRuntime(
        harness=_Harness(result),
        session_namespace=_TEST_NAMESPACE,
    )
    runtime.bind(DeepSeekHarnessWorkflow(include_events=True))

    output = runtime.invoke({"prompt": "run"}, execution_id="exec-copy")
    events[0]["data"]["reason"]["kind"] = "mutated"

    assert output["_dsh"]["events"][0]["data"]["reason"]["kind"] == "completed"


def test_session_root_requires_explicit_receipt_opt_in() -> None:
    secret_root = r"C:\tenants\private\sessions"
    hidden_result = _RunResult(
        session_id="wtb-test-exec-hidden",
        session_root=secret_root,
        events=[
            {
                "type": "turn/end",
                "seq": 1,
                "data": {"reason": {"kind": "completed"}},
            }
        ],
    )
    hidden = DeepSeekHarnessRuntime(
        harness=_Harness(hidden_result),
        session_namespace=_TEST_NAMESPACE,
    )
    hidden.bind(DeepSeekHarnessWorkflow())

    hidden_receipt = hidden.invoke(
        {"prompt": "run"},
        execution_id="exec-hidden",
    )["_dsh"]
    assert hidden_receipt["session_root_configured"] is True
    assert "session_root" not in hidden_receipt

    visible_result = _RunResult(
        session_id="wtb-test-exec-visible",
        session_root=secret_root,
        events=hidden_result.events,
    )
    visible = DeepSeekHarnessRuntime(
        harness=_Harness(visible_result),
        session_namespace=_TEST_NAMESPACE,
    )
    visible.bind(DeepSeekHarnessWorkflow(include_session_root=True))
    visible_receipt = visible.invoke(
        {"prompt": "run"},
        execution_id="exec-visible",
    )["_dsh"]
    assert visible_receipt["session_root"] == secret_root


def test_close_is_terminal_and_idempotent_for_injected_harness() -> None:
    harness = _Harness()
    runtime = DeepSeekHarnessRuntime(harness=harness)
    runtime.bind(DeepSeekHarnessWorkflow())

    runtime.close()
    runtime.close()

    assert harness.close_calls == 0
    with pytest.raises(RuntimePreDispatchError):
        runtime.invoke({"prompt": "must not restart"}, execution_id="exec-closed")
    assert harness.calls == []


def test_owned_close_can_interrupt_a_blocked_run_without_lifecycle_deadlock() -> None:
    started = threading.Event()
    released = threading.Event()
    invoke_done = threading.Event()
    close_done = threading.Event()
    failures: list[BaseException] = []

    class BlockingHarness(_Harness):
        def run(self, prompt: object, *, session_id: str) -> _RunResult:
            self.calls.append((prompt, session_id))
            started.set()
            released.wait()
            raise ConnectionError("closed while prompt was in flight")

        def close(self) -> None:
            super().close()
            released.set()

    harness = BlockingHarness()
    runtime = DeepSeekHarnessRuntime(
        harness_factory=lambda: harness,
        session_namespace=_TEST_NAMESPACE,
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    def invoke() -> None:
        try:
            runtime.invoke({"prompt": "block"}, execution_id="exec-block")
        except BaseException as error:
            failures.append(error)
        finally:
            invoke_done.set()

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    assert started.wait(1)

    def close() -> None:
        try:
            runtime.close()
        finally:
            close_done.set()

    closer = threading.Thread(target=close, daemon=True)
    closer.start()

    assert close_done.wait(1), "close deadlocked behind the external run call"
    assert invoke_done.wait(1), "owned close did not release the blocked run"
    assert harness.close_calls == 1
    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeOutcomeUnknownError)


def test_patch_checkpoint_has_never_return_annotation() -> None:
    annotation = get_type_hints(DeepSeekHarnessRuntime.patch_checkpoint)["return"]

    assert annotation is Never


def test_official_sdk_jsonrpc_wire_allows_post_terminal_event_without_seq(
    tmp_path: Path,
) -> None:
    explicit = os.environ.get("WTB_DSH_SOURCE_PATH")
    checkout = (
        Path(explicit).expanduser().resolve()
        if explicit
        else Path(__file__).resolve().parents[4] / "deepseek-harness"
    )
    sdk_package = checkout / "python" / "sdk" / "src" / "deepseek_harness"
    installed_sdk_available = importlib.util.find_spec("deepseek_harness") is not None
    source_sdk_available = (sdk_package / "__init__.py").is_file()
    if not installed_sdk_available and not source_sdk_available:
        pytest.skip(
            "official DeepSeek Harness SDK is neither installed nor adjacent to WTB"
        )

    runtime_script = tmp_path / "fake_dsh_runtime.py"
    runtime_script.write_text(
        """
import json
import sys

attempts = {}
runtime_binding = {
    "protocolVersion": 1,
    "schemaVersion": 2,
    "persistenceInstanceId": "12345678-1234-4123-8123-123456789abc",
    "rootConfigSha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
}
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "serverInfo": {"name": "durable-wire-fake"},
            },
        }), flush=True)
    elif method == "session/attempt.binding":
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"runtimeBinding": runtime_binding},
        }), flush=True)
    elif method == "session/attempt.start":
        params = request["params"]
        session_id = params["sessionId"]
        attempt_id = params["attemptId"]
        input_sha256 = params["inputSha256"]
        message_id = "message-1"
        identity = {
            "sessionId": session_id,
            "attemptId": attempt_id,
            "inputSha256": input_sha256,
            "messageId": message_id,
            "runtimeBinding": runtime_binding,
        }
        attempts[(session_id, attempt_id)] = {
            **identity,
            "status": "terminal",
            "turn": 0,
            "terminalEventSeq": 2,
            "finishReason": {"kind": "completed"},
            "finalResponse": "durable-wire",
        }
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "attempt": {**identity, "status": "queued", "turn": 0},
                "deduplicated": False,
            },
        }), flush=True)
        for event in [
            {
                "type": "assistant/message",
                "data": {"content": [{"type": "text", "text": "rc6-wire"}]},
            },
            {
                "type": "turn/end",
                "data": {"reason": {"kind": "completed"}},
            },
            {"type": "feedback/record", "data": {"accepted": True}},
        ]:
            print(json.dumps({
                "jsonrpc": "2.0",
                "method": "session.event",
                "params": {"sessionId": session_id, "event": event},
            }), flush=True)
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "session.status",
            "params": {"sessionId": session_id, "status": "idle"},
        }), flush=True)
    elif method == "session/attempt.get":
        params = request["params"]
        attempt = attempts.get((params["sessionId"], params["attemptId"]))
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"attempt": attempt},
        }), flush=True)
    elif method == "shutdown":
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {},
        }), flush=True)
        break
""".lstrip(),
        encoding="utf-8",
    )
    runtime_options: dict[str, Any] = {}
    if not installed_sdk_available:
        runtime_options["sdk_source_path"] = checkout
    runtime = DeepSeekHarnessRuntime(
        runtime_command=(sys.executable, str(runtime_script)),
        harness_options={
            "request_timeout_seconds": 2.0,
            "shutdown_timeout_seconds": 1.0,
        },
        session_namespace=_TEST_NAMESPACE,
        runtime_config_sha256=_TEST_CONFIG_SHA256,
        **runtime_options,
    )
    runtime.bind(DeepSeekHarnessWorkflow(include_events=True))

    try:
        state = {"prompt": "wire contract"}
        plan = _prepare(
            runtime,
            state,
            execution_id="exec-wire",
            attempt_id="attempt-wire",
        )
        output = runtime.invoke(
            state,
            execution_id="exec-wire",
            attempt_id="attempt-wire",
            binding=plan.binding,
            attempt_metadata=plan.metadata,
        )
    finally:
        runtime.close()

    assert output["answer"] == "durable-wire"
    assert output["_dsh"]["last_event_seq"] is None
    assert output["_dsh"]["event_types"] == [
        "assistant/message",
        "turn/end",
        "feedback/record",
    ]
