"""DeepSeek adapter tests for exact runtime/store binding admission."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any

import pytest

from wtb.domain.interfaces.workflow_runtime import (
    RuntimeBindingEnvelope,
    RuntimeBindingMismatchError,
    RuntimeOutcomeUnknownError,
    RuntimePreDispatchError,
    RuntimeTerminalOutcome,
)
from wtb.infrastructure.runtimes.deepseek_harness import (
    DeepSeekHarnessRuntime,
    DeepSeekHarnessWorkflow,
)


_CONFIG_SHA256 = "c" * 64
_ROOT_CONFIG_SHA256 = "d" * 64


@dataclass(frozen=True)
class _SdkBinding:
    protocol_version: int = 1
    schema_version: int = 2
    persistence_instance_id: str = "store-01HX"
    root_config_sha256: str = _ROOT_CONFIG_SHA256


@dataclass(frozen=True)
class _SdkProcessBinding:
    runtime_binding: _SdkBinding
    process_generation: str


class RuntimeGenerationMismatchError(RuntimeError):
    pass


@dataclass
class _Result:
    session_id: str
    attempt_id: str
    input_sha256: str
    runtime_binding: _SdkBinding
    message_id: str = "message-1"
    status: str = "terminal"
    final_response: str = "done"
    finish_reason: dict[str, str] | str = None
    events: list[dict[str, Any]] | None = None
    session_root: str | None = None

    def __post_init__(self) -> None:
        if self.finish_reason is None:
            self.finish_reason = {"kind": "completed"}


class _Harness:
    def __init__(self) -> None:
        self.binding = _SdkBinding()
        self.binding_calls = 0
        self.get_attempt_calls: list[tuple[str, str, str]] = []
        self.run_calls: list[tuple[str, str, str]] = []
        self.cancel_calls: list[tuple[str, str, str]] = []
        self.attempts: dict[tuple[str, str], _Result] = {}
        self.process_generation = "generation-1"
        self.expected_process_generations: list[str | None] = []

    def get_runtime_binding(self):
        self.binding_calls += 1
        return self.binding

    def get_runtime_process_binding(self):
        self.binding_calls += 1
        return _SdkProcessBinding(
            runtime_binding=self.binding,
            process_generation=self.process_generation,
        )

    def run(
        self,
        prompt,
        *,
        session_id,
        attempt_id=None,
        input_sha256=None,
        expected_process_generation=None,
    ):
        del prompt
        self.expected_process_generations.append(expected_process_generation)
        if (
            expected_process_generation is not None
            and expected_process_generation != self.process_generation
        ):
            raise RuntimeGenerationMismatchError
        self.run_calls.append((session_id, attempt_id, input_sha256))
        result = _Result(
            session_id=session_id,
            attempt_id=attempt_id,
            input_sha256=input_sha256,
            runtime_binding=self.binding,
            events=[
                {
                    "type": "turn/end",
                    "seq": 1,
                    "data": {"reason": {"kind": "completed"}},
                }
            ],
        )
        self.attempts[(session_id, attempt_id)] = result
        return result

    def get_attempt(self, *, session_id, attempt_id, input_sha256):
        self.get_attempt_calls.append((session_id, attempt_id, input_sha256))
        return self.attempts.get((session_id, attempt_id))

    def cancel_attempt(
        self,
        *,
        session_id,
        attempt_id,
        input_sha256,
        expected_process_generation=None,
    ):
        if (
            expected_process_generation is not None
            and expected_process_generation != self.process_generation
        ):
            raise RuntimeGenerationMismatchError
        self.cancel_calls.append((session_id, attempt_id, input_sha256))
        return _Result(
            session_id=session_id,
            attempt_id=attempt_id,
            input_sha256=input_sha256,
            runtime_binding=self.binding,
            finish_reason={"kind": "canceled"},
            final_response="",
            events=None,
        )

    def close(self):
        return None


def _runtime(harness: _Harness, *, digest: str = _CONFIG_SHA256):
    runtime = DeepSeekHarnessRuntime(
        harness=harness,
        session_namespace="tenant",
        runtime_config_sha256=digest,
    )
    runtime.bind(DeepSeekHarnessWorkflow())
    return runtime


def _plan(runtime, state=None):
    state = state or {"prompt": "hello"}
    input_sha256 = runtime._state_sha256(state)
    return runtime.prepare_attempt(
        "exec-1",
        "attempt-1",
        input_sha256=input_sha256,
    )


def test_prepare_acquires_store_binding_without_prompt_dispatch() -> None:
    harness = _Harness()
    runtime = _runtime(harness)

    plan = _plan(runtime)

    assert plan.binding == RuntimeBindingEnvelope(
        exact_session_id="wtb-tenant-exec-1",
        persistence_instance_id="store-01HX",
        runtime_config_sha256=runtime._composite_runtime_config_sha256(
            operator_config_sha256=_CONFIG_SHA256,
            root_config_sha256=_ROOT_CONFIG_SHA256,
        ),
        protocol_version=1,
        schema_version=2,
        input_sha256=runtime._state_sha256({"prompt": "hello"}),
        attempt_id="attempt-1",
    )
    assert harness.binding_calls == 1
    assert harness.run_calls == []
    assert harness.get_attempt_calls == []
    assert plan.metadata["process_generation"] == "generation-1"
    assert plan.metadata["root_config_sha256"] == _ROOT_CONFIG_SHA256
    assert plan.metadata["operator_config_sha256"] == _CONFIG_SHA256


def test_prepare_rejects_config_change_while_child_initializes(monkeypatch) -> None:
    harness = _Harness()
    runtime = _runtime(harness)
    observed = iter(("a" * 64, "b" * 64))
    monkeypatch.setattr(
        runtime,
        "_runtime_configuration_sha256",
        lambda: next(observed),
    )

    with pytest.raises(RuntimeBindingMismatchError, match="while.*initialized"):
        _plan(runtime)

    assert harness.run_calls == []


@pytest.mark.parametrize(
    "binding",
    [
        _SdkBinding(protocol_version=2),
        _SdkBinding(schema_version=1),
        _SdkBinding(root_config_sha256="missing"),
    ],
)
def test_prepare_requires_supported_protocol_and_schema_two_attestation(binding) -> None:
    harness = _Harness()
    harness.binding = binding
    runtime = _runtime(harness)

    with pytest.raises(
        RuntimeBindingMismatchError,
        match="protocol|schema|root config",
    ):
        _plan(runtime)

    assert harness.run_calls == []


def test_initial_invoke_pins_prepared_process_generation_before_admission() -> None:
    harness = _Harness()
    runtime = _runtime(harness)
    state = {"prompt": "hello"}
    plan = _plan(runtime, state)
    harness.process_generation = "generation-2"

    with pytest.raises(
        RuntimePreDispatchError,
        match="process generation changed",
    ) as caught:
        runtime.invoke(
            state,
            execution_id="exec-1",
            attempt_id="attempt-1",
            binding=plan.binding,
            attempt_metadata=plan.metadata,
        )

    assert type(caught.value.__cause__).__name__ == (
        "RuntimeGenerationMismatchError"
    )
    assert harness.expected_process_generations == ["generation-1"]
    assert harness.run_calls == []


def test_initial_invoke_requires_persisted_root_config_attestation() -> None:
    harness = _Harness()
    runtime = _runtime(harness)
    state = {"prompt": "hello"}
    plan = _plan(runtime, state)
    incomplete = dict(plan.metadata)
    incomplete.pop("root_config_sha256")

    with pytest.raises(RuntimeBindingMismatchError, match="root_config_sha256"):
        runtime.invoke(
            state,
            execution_id="exec-1",
            attempt_id="attempt-1",
            binding=plan.binding,
            attempt_metadata=incomplete,
        )

    assert harness.run_calls == []


def test_initial_invoke_rejects_process_drift_before_attempt_admission() -> None:
    harness = _Harness()
    runtime = _runtime(harness)
    state = {"prompt": "hello"}
    plan = _plan(runtime, state)
    harness.binding = _SdkBinding(persistence_instance_id="store-02HX")
    harness.process_generation = "generation-2"

    with pytest.raises(RuntimePreDispatchError, match="process generation"):
        runtime.invoke(
            state,
            execution_id="exec-1",
            attempt_id="attempt-1",
            binding=plan.binding,
            attempt_metadata=plan.metadata,
        )

    assert harness.get_attempt_calls == []
    assert harness.run_calls == []


def test_reconcile_rejects_store_drift_before_lookup_or_redrive() -> None:
    harness = _Harness()
    runtime = _runtime(harness)
    state = {"prompt": "hello"}
    plan = _plan(runtime, state)
    harness.binding = _SdkBinding(persistence_instance_id="store-02HX")

    with pytest.raises(RuntimeBindingMismatchError, match="binding"):
        runtime.reconcile(
            "exec-1",
            "attempt-1",
            binding=plan.binding,
            input_state=state,
        )

    assert harness.get_attempt_calls == []
    assert harness.run_calls == []


def test_cancel_rejects_config_drift_before_external_cancel() -> None:
    harness = _Harness()
    first = _runtime(harness)
    plan = _plan(first)
    restarted = _runtime(harness, digest="d" * 64)

    with pytest.raises(RuntimeBindingMismatchError, match="config"):
        restarted.cancel(
            "exec-1",
            "attempt-1",
            binding=plan.binding,
            input_sha256=plan.binding.input_sha256,
            attempt_metadata=plan.metadata,
        )

    assert harness.cancel_calls == []


@pytest.mark.parametrize("operation", ["reconcile", "cancel"])
def test_recovery_mutations_reject_loader_root_drift(operation) -> None:
    harness = _Harness()
    runtime = _runtime(harness)
    state = {"prompt": "hello"}
    plan = _plan(runtime, state)
    harness.binding = _SdkBinding(root_config_sha256="e" * 64)
    if operation == "cancel":
        # A different Loader root cannot belong to the same initialized SDK
        # generation. Cancellation must reject that restarted owner without an
        # identity lookup or cancel write.
        harness.process_generation = "generation-2"

    expected_error = (
        RuntimeOutcomeUnknownError
        if operation == "cancel"
        else RuntimeBindingMismatchError
    )
    expected_message = (
        "process generation" if operation == "cancel" else "config binding"
    )
    with pytest.raises(expected_error, match=expected_message):
        if operation == "reconcile":
            runtime.reconcile(
                "exec-1",
                "attempt-1",
                binding=plan.binding,
                input_state=state,
            )
        else:
            runtime.cancel(
                "exec-1",
                "attempt-1",
                binding=plan.binding,
                input_sha256=plan.binding.input_sha256,
                attempt_metadata=plan.metadata,
            )

    assert harness.get_attempt_calls == []
    assert harness.cancel_calls == []
    assert harness.run_calls == []


def test_verified_store_not_found_never_redrives_without_freshness_proof() -> None:
    harness = _Harness()
    runtime = _runtime(harness)
    state = {"prompt": "hello"}
    plan = _plan(runtime, state)

    with pytest.raises(RuntimeOutcomeUnknownError, match="not found"):
        runtime.reconcile(
            "exec-1",
            "attempt-1",
            binding=plan.binding,
            input_state=state,
        )
    assert harness.get_attempt_calls == [
        (
            plan.binding.exact_session_id,
            plan.binding.attempt_id,
            plan.binding.input_sha256,
        )
    ]
    assert harness.run_calls == []


@pytest.mark.parametrize("status", ["accepted", "pending", "queued", "running"])
def test_nonterminal_store_receipt_never_wakes_without_monotonic_proof(
    status,
) -> None:
    harness = _Harness()
    runtime = _runtime(harness)
    state = {"prompt": "hello"}
    plan = _plan(runtime, state)
    harness.attempts[(plan.binding.exact_session_id, plan.binding.attempt_id)] = (
        _Result(
            session_id=plan.binding.exact_session_id,
            attempt_id=plan.binding.attempt_id,
            input_sha256=plan.binding.input_sha256,
            runtime_binding=harness.binding,
            status=status,
            events=[],
        )
    )

    with pytest.raises(RuntimeOutcomeUnknownError, match="non-terminal"):
        runtime.reconcile(
            "exec-1",
            "attempt-1",
            binding=plan.binding,
            input_state=state,
        )

    assert len(harness.get_attempt_calls) == 1
    assert harness.run_calls == []


@pytest.mark.parametrize("operation", ["invoke", "reconcile", "cancel"])
def test_durable_operations_consume_persisted_session_without_rederiving_it(
    operation,
    monkeypatch,
) -> None:
    harness = _Harness()
    runtime = _runtime(harness)
    state = {"prompt": "hello"}
    plan = _plan(runtime, state)

    def forbidden_session_derivation(*_args, **_kwargs):
        raise AssertionError("durable operation re-derived the persisted session")

    monkeypatch.setattr(runtime, "_session_id", forbidden_session_derivation)

    if operation == "invoke":
        runtime.invoke(
            state,
            execution_id="exec-1",
            attempt_id="attempt-1",
            binding=plan.binding,
            attempt_metadata=plan.metadata,
        )
        observed = harness.run_calls[-1][0]
    elif operation == "reconcile":
        with pytest.raises(RuntimeOutcomeUnknownError, match="not found"):
            runtime.reconcile(
                "exec-1",
                "attempt-1",
                binding=plan.binding,
                input_state=state,
            )
        observed = harness.get_attempt_calls[-1][0]
    else:
        runtime.cancel(
            "exec-1",
            "attempt-1",
            binding=plan.binding,
            input_sha256=plan.binding.input_sha256,
            attempt_metadata=plan.metadata,
        )
        observed = harness.cancel_calls[-1][0]

    assert observed == plan.binding.exact_session_id


def test_post_dispatch_foreign_binding_never_becomes_terminal_proof() -> None:
    harness = _Harness()
    runtime = _runtime(harness)
    state = {"prompt": "hello"}
    plan = _plan(runtime, state)

    original_run = harness.run

    def foreign_run(*args, **kwargs):
        result = original_run(*args, **kwargs)
        result.runtime_binding = _SdkBinding(
            persistence_instance_id="store-foreign"
        )
        return result

    harness.run = foreign_run

    with pytest.raises(RuntimeOutcomeUnknownError, match="binding"):
        runtime.invoke(
            state,
            execution_id="exec-1",
            attempt_id="attempt-1",
            binding=plan.binding,
            attempt_metadata=plan.metadata,
        )


def _owned_config_runtime(
    *,
    cordis: Path,
    provider: str = "provider-a",
    model: str = "model-a",
    command: tuple[str, ...] = ("node", "runtime.js"),
    workflow: DeepSeekHarnessWorkflow | None = None,
    api_key: str = "secret-a",
    base_url: str = "https://model-a.invalid",
    session_root: str = "C:/private/store-a",
    environment: dict[str, str] | None = None,
) -> DeepSeekHarnessRuntime:
    runtime = DeepSeekHarnessRuntime(
        harness_options={
            "provider": provider,
            "model": model,
            "max_tokens": 1024,
            "cordis": str(cordis),
            "api_key": api_key,
            "base_url": base_url,
            "session_root": session_root,
            "env": {
                "DEEPSEEK_API_KEY": api_key,
                "DEEPSEEK_BASE_URL": base_url,
                **(environment or {}),
            },
        },
        runtime_command=command,
        session_namespace="tenant",
        runtime_config_sha256="e" * 64,
    )
    runtime.bind(workflow or DeepSeekHarnessWorkflow())
    return runtime


def test_loader_root_attestation_detects_config_aba_during_prepare(
    tmp_path,
) -> None:
    cordis = tmp_path / "cordis.yml"
    config_a = b"plugins: [a]\n"
    config_b = b"plugins: [b]\n"
    cordis.write_bytes(config_a)

    class AbaHarness(_Harness):
        def get_runtime_process_binding(self):
            cordis.write_bytes(config_b)
            self.binding = _SdkBinding(
                root_config_sha256=hashlib.sha256(config_b).hexdigest()
            )
            observed = super().get_runtime_process_binding()
            cordis.write_bytes(config_a)
            return observed

    harness = AbaHarness()
    runtime = _owned_config_runtime(cordis=cordis)
    runtime._harness = harness

    with pytest.raises(
        RuntimeBindingMismatchError,
        match="Loader root config bytes differ",
    ):
        _plan(runtime)

    assert cordis.read_bytes() == config_a
    assert harness.run_calls == []


def test_composite_binding_changes_with_loader_root_attestation() -> None:
    first = DeepSeekHarnessRuntime._composite_runtime_config_sha256(
        operator_config_sha256="a" * 64,
        root_config_sha256="b" * 64,
    )
    second = DeepSeekHarnessRuntime._composite_runtime_config_sha256(
        operator_config_sha256="a" * 64,
        root_config_sha256="c" * 64,
    )

    assert first != second


def test_runtime_config_fingerprint_tracks_semantics_without_secrets_or_paths(
    tmp_path,
) -> None:
    first_cordis = tmp_path / "a.yml"
    second_cordis = tmp_path / "nested" / "b.yml"
    second_cordis.parent.mkdir()
    first_cordis.write_text("plugins: []\n", encoding="utf-8")
    second_cordis.write_text("plugins: []\n", encoding="utf-8")

    baseline = _owned_config_runtime(cordis=first_cordis)
    equivalent = _owned_config_runtime(
        cordis=second_cordis,
        api_key="secret-b",
    )
    baseline_hash = baseline._runtime_configuration_sha256()

    assert equivalent._runtime_configuration_sha256() == baseline_hash
    assert _owned_config_runtime(
        cordis=first_cordis,
        session_root="D:/different/private/store",
    )._runtime_configuration_sha256() != baseline_hash
    assert _owned_config_runtime(
        cordis=first_cordis,
        provider="provider-b",
    )._runtime_configuration_sha256() != baseline_hash
    assert _owned_config_runtime(
        cordis=first_cordis,
        model="model-b",
    )._runtime_configuration_sha256() != baseline_hash
    assert _owned_config_runtime(
        cordis=first_cordis,
        command=("node", "other.js"),
    )._runtime_configuration_sha256() != baseline_hash
    assert _owned_config_runtime(
        cordis=first_cordis,
        workflow=DeepSeekHarnessWorkflow(result_key="other"),
    )._runtime_configuration_sha256() != baseline_hash
    assert _owned_config_runtime(
        cordis=first_cordis,
        base_url="https://model-b.invalid",
    )._runtime_configuration_sha256() != baseline_hash

    first_cordis.write_text("plugins: [changed]\n", encoding="utf-8")
    assert baseline._runtime_configuration_sha256() != baseline_hash


@pytest.mark.parametrize(
    ("name", "first", "second"),
    [
        ("DSH_SYSTEM_PROMPT", "persona-a", "persona-b"),
        ("DSH_MAX_TOKENS_AS_SUCCESS", "true", "false"),
        ("CUSTOM_PLUGIN_MODE", "safe", "fast"),
        ("DSH_CWD", "C:/workspace-a", "C:/workspace-b"),
        ("DSH_SESSION_ROOT", "C:/store-a", "C:/store-b"),
        ("DSH_SNAPSHOT", "1", "replay"),
    ],
)
def test_explicit_runtime_environment_semantics_change_config_fingerprint(
    tmp_path,
    name,
    first,
    second,
) -> None:
    cordis = tmp_path / "cordis.yml"
    cordis.write_text("plugins: []\n", encoding="utf-8")

    left = _owned_config_runtime(
        cordis=cordis,
        environment={name: first},
    )
    right = _owned_config_runtime(
        cordis=cordis,
        environment={name: second},
    )

    assert left._runtime_configuration_sha256() != right._runtime_configuration_sha256()


def test_explicit_environment_drift_blocks_lookup_and_redrive(tmp_path) -> None:
    cordis = tmp_path / "cordis.yml"
    cordis.write_text("plugins: []\n", encoding="utf-8")
    harness = _Harness()
    initial = _owned_config_runtime(
        cordis=cordis,
        environment={"DSH_SYSTEM_PROMPT": "persona-a"},
    )
    # Use the real owned-config digest with a controllable injected transport.
    prepared_runtime = _runtime(
        harness,
        digest=initial._runtime_configuration_sha256(),
    )
    state = {"prompt": "hello"}
    plan = _plan(prepared_runtime, state)
    drifted = _owned_config_runtime(
        cordis=cordis,
        environment={"DSH_SYSTEM_PROMPT": "persona-b"},
    )
    restarted = _runtime(
        harness,
        digest=drifted._runtime_configuration_sha256(),
    )

    with pytest.raises(RuntimeBindingMismatchError, match="config"):
        restarted.reconcile(
            "exec-1",
            "attempt-1",
            binding=plan.binding,
            input_state=state,
        )

    assert harness.get_attempt_calls == []
    assert harness.run_calls == []


def test_cloned_store_lineage_at_different_root_blocks_before_lookup_or_redrive(
    tmp_path,
) -> None:
    cordis = tmp_path / "cordis.yml"
    cordis.write_text("plugins: []\n", encoding="utf-8")
    harness = _Harness()
    original = _owned_config_runtime(
        cordis=cordis,
        session_root=str(tmp_path / "store-original"),
    )
    cloned = _owned_config_runtime(
        cordis=cordis,
        session_root=str(tmp_path / "store-clone"),
    )
    prepared_runtime = _runtime(
        harness,
        digest=original._runtime_configuration_sha256(),
    )
    state = {"prompt": "hello"}
    plan = _plan(prepared_runtime, state)
    restarted = _runtime(
        harness,
        digest=cloned._runtime_configuration_sha256(),
    )

    with pytest.raises(RuntimeBindingMismatchError, match="config"):
        restarted.reconcile(
            "exec-1",
            "attempt-1",
            binding=plan.binding,
            input_state=state,
        )

    assert harness.get_attempt_calls == []
    assert harness.run_calls == []


def test_snapshot_mode_is_semantic_value_not_file_path(tmp_path) -> None:
    cordis = tmp_path / "cordis.yml"
    cordis.write_text("plugins: []\n", encoding="utf-8")
    owned = _owned_config_runtime(
        cordis=cordis,
        environment={"DSH_SNAPSHOT": "1"},
    )

    digest = owned._runtime_configuration_sha256()

    assert len(digest) == 64


def test_runtime_command_cordis_content_is_fingerprinted_without_option(
    tmp_path,
) -> None:
    cordis = tmp_path / "runtime-cordis.yml"
    cordis.write_text("plugins: []\n", encoding="utf-8")
    runtime = DeepSeekHarnessRuntime(
        harness_options={
            "provider": "provider-a",
            "model": "model-a",
            "session_root": str(tmp_path / "sessions"),
            "env": {},
        },
        runtime_command=("node", "runtime.js", str(cordis)),
        session_namespace="tenant",
        runtime_config_sha256="e" * 64,
    )
    runtime.bind(DeepSeekHarnessWorkflow())
    before = runtime._runtime_configuration_sha256()

    cordis.write_text("plugins: [changed]\n", encoding="utf-8")

    assert runtime._runtime_configuration_sha256() != before


def test_owned_sdk_requires_explicit_authority_for_inherited_environment(
    tmp_path,
    monkeypatch,
) -> None:
    cordis = tmp_path / "cordis.yml"
    cordis.write_text("plugins: []\n", encoding="utf-8")
    monkeypatch.setenv("DSH_SYSTEM_PROMPT", "host-persona-a")
    runtime = DeepSeekHarnessRuntime(
        harness_options={"cordis": str(cordis), "env": {}},
        runtime_command=("node", "runtime.js", str(cordis)),
        session_namespace="tenant",
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    with pytest.raises(RuntimeBindingMismatchError, match="deployment fingerprint"):
        runtime._runtime_configuration_sha256()


def test_relative_runtime_working_directories_resolve_into_config_identity(
    tmp_path,
    monkeypatch,
) -> None:
    first_root = tmp_path / "host-a"
    second_root = tmp_path / "host-b"
    first_root.mkdir()
    second_root.mkdir()
    cordis = tmp_path / "cordis.yml"
    cordis.write_text("plugins: []\n", encoding="utf-8")

    monkeypatch.chdir(first_root)
    first = _owned_config_runtime(cordis=cordis)
    first._harness_options["cwd"] = "workspace"
    first._harness_options["runtime_cwd"] = "runtime"
    first_digest = first._runtime_configuration_sha256()

    monkeypatch.chdir(second_root)
    second = _owned_config_runtime(cordis=cordis)
    second._harness_options["cwd"] = "workspace"
    second._harness_options["runtime_cwd"] = "runtime"

    assert second._runtime_configuration_sha256() != first_digest


def test_injected_harness_requires_explicit_runtime_config_digest() -> None:
    runtime = DeepSeekHarnessRuntime(
        harness=_Harness(),
        session_namespace="tenant",
    )
    runtime.bind(DeepSeekHarnessWorkflow())

    with pytest.raises(RuntimeBindingMismatchError, match="fingerprint"):
        _plan(runtime)
