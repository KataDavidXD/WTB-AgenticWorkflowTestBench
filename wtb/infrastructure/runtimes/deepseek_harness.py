"""DeepSeek Harness runtime adapter over its public Python SDK/JSON-RPC seam."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import math
import os
import re
import secrets
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Never, Protocol, cast

from wtb.domain.interfaces.workflow_runtime import (
    RuntimeAttemptPlan,
    RuntimeBindingEnvelope,
    RuntimeBindingMismatchError,
    RuntimeOutcomeUnknownError,
    RuntimeOutcomeReceipt,
    RuntimePreDispatchError,
    RuntimeSnapshot,
    RuntimeTerminalError,
    RuntimeTerminalOutcome,
)
from wtb.domain.runtime_identity import canonical_json_sha256


_LOCAL_SDK_IMPORT_LOCK = threading.Lock()
_RUNTIME_BINDING_PROTOCOL_VERSION = 1
_RUNTIME_BINDING_SCHEMA_VERSION = 2


class _RunResult(Protocol):
    """Result fields consumed from ``deepseek_harness.RunResult``."""

    session_id: str
    attempt_id: str | None
    input_sha256: str | None
    message_id: str | None
    status: str | None
    final_response: str
    finish_reason: str | Mapping[str, Any] | None
    events: list[dict[str, Any]]
    session_root: str | None
    runtime_binding: object


class _Harness(Protocol):
    """Small public SDK face used by the adapter."""

    def run(
        self,
        input: object,
        *,
        session_id: str,
        attempt_id: str | None = None,
        input_sha256: str | None = None,
        expected_process_generation: str | None = None,
    ) -> _RunResult:
        """Run one turn in an execution-scoped session."""

    def get_attempt(
        self,
        *,
        session_id: str,
        attempt_id: str,
        input_sha256: str,
    ) -> _RunResult | None:
        """Return the durable attempt record, or ``None`` when not accepted."""

    def cancel_attempt(
        self,
        *,
        session_id: str,
        attempt_id: str,
        input_sha256: str,
        expected_process_generation: str | None = None,
    ) -> _RunResult:
        """Cancel one exact attempt and return its durable terminal record."""

    def get_runtime_binding(self) -> object:
        """Initialize without a prompt and return the pinned store binding."""

    def get_runtime_process_binding(self) -> object:
        """Return the initialized store binding plus opaque process generation."""

    def start(self) -> None:
        """Start and initialize the SDK before a turn is dispatched."""

    def close(self) -> None:
        """Release the owned runtime process."""


@dataclass(frozen=True, slots=True)
class DeepSeekHarnessWorkflow:
    """Mapping between WTB state and one DeepSeek Harness turn.

    ``prompt_key`` may contain a string or the public SDK's list of content
    blocks. The adapter writes the committed assistant text to ``result_key``.
    """

    prompt_key: str = "prompt"
    result_key: str = "answer"
    session_prefix: str = "wtb-"
    include_events: bool = False
    include_session_root: bool = False
    max_event_count: int = 1_024
    max_event_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        for name, value in (
            ("prompt_key", self.prompt_key),
            ("result_key", self.result_key),
            ("session_prefix", self.session_prefix),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", self.session_prefix):
            raise ValueError(
                "session_prefix must contain only safe identifier characters"
            )
        if len(self.session_prefix) > 32:
            raise ValueError("session_prefix must contain at most 32 characters")
        for name, value in (
            ("include_events", self.include_events),
            ("include_session_root", self.include_session_root),
        ):
            if not isinstance(value, bool):
                raise TypeError(f"{name} must be a bool")
        for name, value in (
            ("max_event_count", self.max_event_count),
            ("max_event_bytes", self.max_event_bytes),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


class DeepSeekHarnessRuntime:
    """Invoke DeepSeek Harness without coupling WTB to Cordis internals.

    The official Python SDK owns the JSON-RPC subprocess and durable session
    log. WTB supplies an execution-scoped session identity and records an
    evidence receipt. DeepSeek Harness currently exposes no public checkpoint
    rewind method, so checkpoint continuation fails before a model call rather
    than pretending that a session event position is a restorable checkpoint.
    """

    def __init__(
        self,
        *,
        harness: _Harness | None = None,
        harness_factory: Callable[[], _Harness] | None = None,
        harness_options: Mapping[str, Any] | None = None,
        sdk_source_path: str | Path | None = None,
        runtime_command: Sequence[str] | None = None,
        session_namespace: str | None = None,
        fixed_session_id: str | None = None,
        runtime_config_sha256: str | None = None,
    ) -> None:
        if harness is not None and harness_factory is not None:
            raise TypeError("pass either harness or harness_factory, not both")
        owns_factory = harness is None and harness_factory is None
        if not owns_factory and (
            harness_options
            or sdk_source_path is not None
            or runtime_command is not None
        ):
            raise TypeError(
                "harness_options, sdk_source_path, and runtime_command apply only "
                "to the default Harness factory"
            )
        options = dict(harness_options or {})
        if runtime_command is not None:
            command = self._validate_runtime_command(runtime_command)
            conflicting = {
                "launch_args_override",
                "runtime_bin",
                "bridge_bin",
            }.intersection(options)
            if conflicting:
                names = ", ".join(sorted(conflicting))
                raise TypeError(
                    f"runtime_command conflicts with Harness options: {names}"
                )
            options["launch_args_override"] = command
        self._harness = harness
        self._harness_factory = harness_factory
        self._harness_options = options
        self._owns_default_configuration = owns_factory
        self._explicit_runtime_config_sha256 = (
            self._validate_sha256(
                runtime_config_sha256,
                name="runtime_config_sha256",
            )
            if runtime_config_sha256 is not None
            else None
        )
        self._sdk_source_path = (
            self._resolve_sdk_source_path(sdk_source_path)
            if sdk_source_path is not None
            else None
        )
        self._owns_harness = harness is None
        self._session_namespace_configured = session_namespace is not None
        self._session_namespace = self._validate_session_component(
            secrets.token_hex(8) if session_namespace is None else session_namespace,
            name="session_namespace",
            max_length=64,
        )
        self._fixed_session_id = (
            None
            if fixed_session_id is None
            else self._validate_session_component(
                fixed_session_id,
                name="fixed_session_id",
                max_length=128,
            )
        )
        self._workflow: DeepSeekHarnessWorkflow | None = None
        self._history: dict[str, list[RuntimeSnapshot]] = {}
        self._lifecycle_lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._history_lock = threading.Lock()
        self._closed = False

    def supports_checkpoint_recovery(self) -> bool:
        """The public SDK exposes receipts and sessions, but no rewind API."""
        return False

    def supports_forking(self) -> bool:
        """The public SDK cannot clone a checkpoint into another execution."""
        return False

    def shares_checkpoint_store(self, state_adapter: Any) -> bool:
        """Harness receipts are evidence, never state-adapter checkpoints."""
        del state_adapter
        return False

    def is_bound(self) -> bool:
        """Return whether a DeepSeek Harness state mapping is configured."""
        return self._workflow is not None

    def bind(self, workflow: Any, *, force_rebind: bool = True) -> None:
        """Bind an explicit state mapping; arbitrary graph objects are rejected."""
        with self._lifecycle_lock:
            self._require_open()
            if not isinstance(workflow, DeepSeekHarnessWorkflow):
                raise TypeError("workflow must be a DeepSeekHarnessWorkflow")
            if self._workflow is not None and not force_rebind:
                raise RuntimeError("DeepSeek Harness runtime is already bound")
            self._workflow = workflow

    def prepare_attempt(
        self,
        execution_id: str,
        attempt_id: str,
        *,
        input_sha256: str,
        checkpoint_id: str | None = None,
    ) -> RuntimeAttemptPlan:
        """Plan the exact DSH session before WTB commits its dispatch fence."""
        try:
            execution_id = self._require_execution_id(execution_id)
            attempt_id = self._validate_session_component(
                attempt_id,
                name="attempt_id",
                max_length=128,
            )
            input_sha256 = self._validate_sha256(input_sha256)
            if checkpoint_id is not None:
                raise NotImplementedError(
                    "DeepSeek Harness SDK does not expose checkpoint rewind"
                )
            if not self._session_namespace_configured:
                raise ValueError(
                    "session_namespace must be explicitly configured for "
                    "durable WTB dispatch"
                )
            with self._lifecycle_lock:
                self._require_open()
                workflow = self._require_workflow()
                session_id = self._session_id(workflow, execution_id)
                harness = self._require_harness()
            pre_start_config_sha256 = self._runtime_configuration_sha256()
            pre_start_root_sha256 = self._configured_root_config_sha256()
            (
                protocol_version,
                schema_version,
                persistence_instance_id,
                root_config_sha256,
                process_generation,
            ) = self._current_sdk_process_binding(harness)
            operator_config_sha256 = self._runtime_configuration_sha256()
            post_start_root_sha256 = self._configured_root_config_sha256()
            if pre_start_config_sha256 != operator_config_sha256:
                raise RuntimeBindingMismatchError(
                    "runtime config changed while DeepSeek Harness initialized"
                )
            if pre_start_root_sha256 != post_start_root_sha256:
                raise RuntimeBindingMismatchError(
                    "root config changed while DeepSeek Harness initialized"
                )
            self._validate_operator_root_config(
                root_config_sha256,
                configured_root_sha256=post_start_root_sha256,
            )
            runtime_config_sha256 = self._composite_runtime_config_sha256(
                operator_config_sha256=operator_config_sha256,
                root_config_sha256=root_config_sha256,
            )
            binding = RuntimeBindingEnvelope(
                exact_session_id=session_id,
                persistence_instance_id=persistence_instance_id,
                runtime_config_sha256=runtime_config_sha256,
                protocol_version=protocol_version,
                schema_version=schema_version,
                input_sha256=input_sha256,
                attempt_id=attempt_id,
            )
        except RuntimeBindingMismatchError:
            raise
        except Exception as error:
            raise RuntimePreDispatchError(
                "DeepSeek Harness attempt planning failed before dispatch"
            ) from error
        return RuntimeAttemptPlan(
            execution_id=execution_id,
            attempt_id=attempt_id,
            session_id=session_id,
            binding=binding,
            metadata={
                "protocol": "deepseek-harness-sdk-jsonrpc",
                "session_namespace": self._session_namespace,
                "fixed_session_id": self._fixed_session_id,
                "input_sha256": input_sha256,
                "durable_reconcile_supported": True,
                "operator_config_sha256": operator_config_sha256,
                "root_config_sha256": root_config_sha256,
                "process_generation": process_generation,
            },
        )

    def invoke(
        self,
        input_state: Mapping[str, Any] | None,
        *,
        execution_id: str,
        checkpoint_id: str | None = None,
        attempt_id: str | None = None,
        binding: RuntimeBindingEnvelope | None = None,
        attempt_metadata: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Run one durable Harness turn and return state plus an evidence receipt."""
        with self._run_lock:
            return self._invoke(
                input_state,
                execution_id=execution_id,
                checkpoint_id=checkpoint_id,
                attempt_id=attempt_id,
                binding=binding,
                attempt_metadata=attempt_metadata,
            )

    def _invoke(
        self,
        input_state: Mapping[str, Any] | None,
        *,
        execution_id: str,
        checkpoint_id: str | None = None,
        attempt_id: str | None = None,
        binding: RuntimeBindingEnvelope | None = None,
        attempt_metadata: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        expected_process_generation: str | None = None
        persisted_operator_config_sha256: str | None = None
        persisted_root_config_sha256: str | None = None
        try:
            workflow = self._require_workflow()
            execution_id = self._require_execution_id(execution_id)
            if checkpoint_id is not None:
                raise NotImplementedError(
                    "DeepSeek Harness SDK does not expose checkpoint rewind; "
                    "resume must be coordinated from a WTB composite recovery point"
                )
            if input_state is None:
                raise ValueError("DeepSeek Harness invocation requires input state")
            if workflow.prompt_key not in input_state:
                raise ValueError(
                    "DeepSeek Harness workflow requires input key "
                    f"'{workflow.prompt_key}'"
                )
            if "_dsh" in input_state:
                raise ValueError("input state reserves '_dsh' for the Harness receipt")

            prompt = self._validated_prompt(
                input_state[workflow.prompt_key],
                prompt_key=workflow.prompt_key,
            )
            base_state = deepcopy(dict(input_state))
            normalized_attempt_id = (
                None
                if attempt_id is None
                else self._validate_session_component(
                    attempt_id,
                    name="attempt_id",
                    max_length=128,
                )
            )
            if (
                normalized_attempt_id is not None
                and not self._session_namespace_configured
            ):
                raise ValueError(
                    "session_namespace must be explicitly configured when "
                    "attempt_id is supplied"
                )
            input_sha256 = self._state_sha256(input_state)
            with self._lifecycle_lock:
                self._require_open()
                harness = self._require_harness()
            if normalized_attempt_id is not None:
                if not isinstance(binding, RuntimeBindingEnvelope):
                    raise RuntimeBindingMismatchError(
                        "durable DeepSeek Harness invoke requires its binding envelope"
                    )
                (
                    persisted_operator_config_sha256,
                    persisted_root_config_sha256,
                    expected_process_generation,
                ) = self._binding_metadata_from_attempt_metadata(attempt_metadata)
                self._validate_binding_envelope(
                    binding,
                    attempt_id=normalized_attempt_id,
                    input_sha256=input_sha256,
                    harness=harness,
                    verify_current_binding=False,
                    operator_config_sha256=persisted_operator_config_sha256,
                    root_config_sha256=persisted_root_config_sha256,
                )
                prior = self._snapshot_for_attempt(
                    execution_id,
                    normalized_attempt_id,
                )
                if prior is not None:
                    return self._replay_attempt(
                        prior,
                        input_sha256=input_sha256,
                    )
                self._require_process_generation_api(harness)
            session_id = (
                binding.exact_session_id
                if normalized_attempt_id is not None and binding is not None
                else self._session_id(workflow, execution_id)
            )
            if normalized_attempt_id is not None:
                self._require_identity_api(harness)
        except RuntimeBindingMismatchError:
            raise
        except RuntimeTerminalError:
            raise
        except Exception as error:
            raise RuntimePreDispatchError(
                "DeepSeek Harness invocation failed before prompt dispatch"
            ) from error

        if normalized_attempt_id is None:
            start = getattr(harness, "start", None)
            try:
                if callable(start):
                    start()
            except Exception as error:
                raise RuntimePreDispatchError(
                    "DeepSeek Harness startup failed before prompt dispatch"
                ) from error
        with self._lifecycle_lock:
            closed_before_dispatch = self._closed
        if closed_before_dispatch:
            if self._owns_harness:
                harness.close()
            raise RuntimePreDispatchError(
                "DeepSeek Harness runtime closed before prompt dispatch"
            )
        try:
            run_kwargs: dict[str, Any] = {"session_id": session_id}
            if normalized_attempt_id is not None:
                run_kwargs.update(
                    attempt_id=normalized_attempt_id,
                    input_sha256=input_sha256,
                    expected_process_generation=expected_process_generation,
                )
            result = harness.run(prompt, **run_kwargs)
        except Exception as error:
            if self._is_process_generation_mismatch(error):
                raise RuntimePreDispatchError(
                    "DeepSeek Harness process generation changed before dispatch"
                ) from error
            raise RuntimeOutcomeUnknownError(
                "DeepSeek Harness transport failed after dispatch; the durable "
                f"outcome for session {session_id!r} is unknown and must be "
                "reconciled before retry",
                session_id=session_id,
            ) from error
        try:
            if result.session_id != session_id:
                raise RuntimeError(
                    "DeepSeek Harness returned an unexpected session: "
                    f"expected {session_id!r}, got {result.session_id!r}"
                )
            message_id: str | None = None
            attempt_status: str | None = None
            if normalized_attempt_id is not None:
                observed_attempt_id = getattr(result, "attempt_id", None)
                observed_input_sha256 = getattr(result, "input_sha256", None)
                message_id = getattr(result, "message_id", None)
                attempt_status = getattr(result, "status", None)
                if observed_attempt_id != normalized_attempt_id:
                    raise RuntimeError(
                        "DeepSeek Harness result belongs to another attempt"
                    )
                if observed_input_sha256 != input_sha256:
                    raise RuntimeError(
                        "DeepSeek Harness result belongs to another input"
                    )
                if not isinstance(message_id, str) or not message_id:
                    raise RuntimeError(
                        "DeepSeek Harness result has no durable message identity"
                    )
                if attempt_status not in {
                    None,
                    "terminal",
                    # Retain compatibility with pre-durable SDK test doubles.
                    "completed",
                    "failed",
                    "cancelled",
                    "canceled",
                }:
                    raise RuntimeError(
                        "DeepSeek Harness result is not durably terminal"
                    )
                self._validate_result_binding(
                    result,
                    binding,
                    operator_config_sha256=persisted_operator_config_sha256,
                    root_config_sha256=persisted_root_config_sha256,
                )
            raw_events = getattr(result, "events", None)
            if not isinstance(raw_events, list) or not all(
                isinstance(event, dict) for event in raw_events
            ):
                raise RuntimeError("DeepSeek Harness returned invalid event evidence")
            finish_reason = self._finish_reason_kind(result.finish_reason)
            if raw_events or normalized_attempt_id is None:
                finish_reason = self._validate_terminal_evidence(
                    raw_events,
                    finish_reason,
                )
            self._reject_crash_repaired_terminal(
                finish_reason,
                session_id=session_id,
            )
            if attempt_status not in {None, "terminal"} and (
                self._terminal_outcome(attempt_status)
                is not self._terminal_outcome(finish_reason)
            ):
                raise RuntimeError(
                    "DeepSeek Harness attempt status disagrees with finish reason"
                )
            final_response = getattr(result, "final_response", None)
            if not isinstance(final_response, str):
                raise RuntimeError(
                    "DeepSeek Harness returned a non-string final response"
                )
            session_root = getattr(result, "session_root", None)
            if session_root is not None and (
                not isinstance(session_root, str) or not session_root
            ):
                raise RuntimeError("DeepSeek Harness returned an invalid session root")

            receipt: dict[str, Any] = {
                "protocol": "deepseek-harness-sdk-jsonrpc",
                "session_id": result.session_id,
                "session_namespace": self._session_namespace,
                "execution_id": execution_id,
                "attempt_id": normalized_attempt_id,
                "input_sha256": input_sha256,
                "finish_reason": finish_reason,
                "session_root_configured": session_root is not None,
                "event_count": len(raw_events),
                "last_event_seq": self._last_event_seq(raw_events),
            }
            if message_id is not None:
                receipt["message_id"] = message_id
            if normalized_attempt_id is not None:
                # ``run`` is terminal-only. Durable lookup/cancel use the
                # explicit typed receipt status ``terminal``.
                receipt["attempt_status"] = attempt_status or "terminal"
                receipt["binding"] = binding.to_dict()
                receipt["operator_config_sha256"] = (
                    persisted_operator_config_sha256
                )
                receipt["root_config_sha256"] = persisted_root_config_sha256
            if workflow.include_session_root and session_root is not None:
                receipt["session_root"] = session_root

            evidence_omitted_reason: str | None = None
            if len(raw_events) > workflow.max_event_count:
                evidence_omitted_reason = "event_count_limit"
            elif workflow.include_events:
                event_bytes = self._json_size(raw_events)
                receipt["event_bytes"] = event_bytes
                if event_bytes > workflow.max_event_bytes:
                    evidence_omitted_reason = "event_bytes_limit"

            if evidence_omitted_reason is None:
                receipt["event_types"] = [
                    cast(str, event["type"]) for event in raw_events
                ]
                if workflow.include_events:
                    receipt["events"] = deepcopy(raw_events)
            else:
                receipt["event_evidence_omitted_reason"] = evidence_omitted_reason

            output = base_state
            output[workflow.result_key] = final_response
            output["_dsh"] = receipt
        except RuntimeOutcomeUnknownError:
            raise
        except Exception as error:
            raise RuntimeOutcomeUnknownError(
                "DeepSeek Harness response after dispatch could not establish "
                f"a trusted terminal receipt/binding for session {session_id!r}; "
                "reconcile before retry",
                session_id=session_id,
            ) from error
        snapshot = RuntimeSnapshot(
            execution_id=execution_id,
            checkpoint_id=None,
            values=deepcopy(output),
            metadata={
                "runtime": "deepseek-harness",
                "recoverable": False,
                "session_id": result.session_id,
                "attempt_id": normalized_attempt_id,
                "input_sha256": input_sha256,
                "finish_reason": receipt["finish_reason"],
                "last_event_seq": receipt["last_event_seq"],
            },
        )
        with self._history_lock:
            self._history.setdefault(execution_id, []).append(snapshot)
        if receipt["finish_reason"] != "completed":
            raise RuntimeTerminalError(
                "DeepSeek Harness returned a trusted non-success terminal outcome: "
                f"finish_reason={receipt['finish_reason']!r}",
                finish_reason=cast(str, receipt["finish_reason"]),
                session_id=cast(str, receipt["session_id"]),
                result_state=deepcopy(output),
                receipt=deepcopy(receipt),
            )
        return output

    def snapshot(
        self,
        execution_id: str,
        checkpoint_id: str | None = None,
    ) -> RuntimeSnapshot | None:
        """Return the latest evidence snapshot, never a rewindable checkpoint."""
        execution_id = self._require_execution_id(execution_id)
        if checkpoint_id is not None:
            return None
        with self._history_lock:
            history = self._history.get(execution_id, ())
            return deepcopy(history[-1]) if history else None

    def history(self, execution_id: str) -> Sequence[RuntimeSnapshot]:
        """Return invocation receipts recorded by this adapter instance."""
        execution_id = self._require_execution_id(execution_id)
        with self._history_lock:
            return tuple(deepcopy(self._history.get(execution_id, ())))

    def patch_checkpoint(
        self,
        execution_id: str,
        checkpoint_id: str,
        values: Mapping[str, Any],
        *,
        as_node: str | None = None,
    ) -> Never:
        """Fail closed because the public SDK has no checkpoint mutation API."""
        del execution_id, checkpoint_id, values, as_node
        raise NotImplementedError(
            "DeepSeek Harness SDK does not expose checkpoint mutation"
        )

    def reconcile(
        self,
        execution_id: str,
        attempt_id: str,
        *,
        binding: RuntimeBindingEnvelope,
        input_state: Mapping[str, Any] | None = None,
        attempt_metadata: Mapping[str, Any] | None = None,
    ) -> RuntimeOutcomeReceipt | None:
        """Look up one attempt; never infer freshness from store-local absence."""
        execution_id = self._require_execution_id(execution_id)
        attempt_id = self._validate_session_component(
            attempt_id,
            name="attempt_id",
            max_length=128,
        )
        if input_state is not None and not isinstance(input_state, Mapping):
            raise TypeError("input_state must be a mapping for durable reconcile")
        input_sha256 = (
            self._state_sha256(input_state)
            if input_state is not None
            else binding.input_sha256
        )
        workflow = self._require_workflow()
        with self._lifecycle_lock:
            self._require_open()
            harness = self._require_harness()
        persisted_operator_config_sha256: str | None = None
        persisted_root_config_sha256: str | None = None
        if attempt_metadata is not None:
            (
                persisted_operator_config_sha256,
                persisted_root_config_sha256,
                _process_generation,
            ) = self._binding_metadata_from_attempt_metadata(attempt_metadata)
        self._validate_binding_envelope(
            binding,
            attempt_id=attempt_id,
            input_sha256=input_sha256,
            harness=harness,
            operator_config_sha256=persisted_operator_config_sha256,
            root_config_sha256=persisted_root_config_sha256,
        )
        session_id = binding.exact_session_id
        snapshot = self._snapshot_for_attempt(execution_id, attempt_id)
        if snapshot is not None:
            receipt = snapshot.values.get("_dsh")
            if isinstance(receipt, Mapping):
                finish_reason = receipt.get("finish_reason")
                input_sha256 = receipt.get("input_sha256")
                session_id = receipt.get("session_id")
                if (
                    isinstance(finish_reason, str)
                    and isinstance(input_sha256, str)
                    and isinstance(session_id, str)
                ):
                    if (
                        input_state is not None
                        and self._state_sha256(input_state) != input_sha256
                    ):
                        raise RuntimeError(
                            "durable reconcile input conflicts with the stored attempt"
                        )
                    self._reject_crash_repaired_terminal(
                        finish_reason,
                        session_id=session_id,
                    )
                    try:
                        stored_binding = RuntimeBindingEnvelope.from_mapping(
                            receipt.get("binding")
                        )
                    except (TypeError, ValueError) as error:
                        raise RuntimeBindingMismatchError(
                            "stored attempt has no valid runtime binding"
                        ) from error
                    if stored_binding != binding:
                        raise RuntimeBindingMismatchError(
                            "stored attempt runtime binding disagrees with its fence"
                        )
                    return RuntimeOutcomeReceipt(
                        execution_id=execution_id,
                        attempt_id=attempt_id,
                        input_sha256=input_sha256,
                        outcome=self._terminal_outcome(finish_reason),
                        binding=binding,
                        values=deepcopy(snapshot.values),
                        receipt=deepcopy(receipt),
                        session_id=session_id,
                        detail=(
                            "DeepSeek Harness finish_reason="
                            f"{finish_reason}"
                        ),
                    )

        # The in-process cache is only an optimization. Recovery after a WTB
        # restart must use the public durable attempt API and the exact input
        # committed in WTB's pre-dispatch fence.
        if input_state is None:
            return None
        if attempt_metadata is None:
            persisted_operator_config_sha256 = (
                self._runtime_configuration_sha256()
            )
            persisted_root_config_sha256 = self._current_sdk_binding(harness)[3]
            self._validate_operator_root_config(persisted_root_config_sha256)
            if binding.runtime_config_sha256 != self._composite_runtime_config_sha256(
                operator_config_sha256=persisted_operator_config_sha256,
                root_config_sha256=persisted_root_config_sha256,
            ):
                raise RuntimeBindingMismatchError(
                    "current runtime config differs from the dispatch fence"
                )
        self._require_identity_api(harness)
        try:
            result = harness.get_attempt(
                session_id=session_id,
                attempt_id=attempt_id,
                input_sha256=input_sha256,
            )
            status = getattr(result, "status", None) if result is not None else None
            if result is None or status == "not_found":
                raise RuntimeOutcomeUnknownError(
                    "DeepSeek Harness durable attempt was not found; store-local "
                    "absence cannot prove that redrive is safe without a "
                    "rollback-resistant admission authority",
                    session_id=session_id,
                )
            if status != "terminal":
                raise RuntimeOutcomeUnknownError(
                    "DeepSeek Harness durable attempt is non-terminal; a "
                    "store-local queued/running state may be a rolled-back "
                    "copy and cannot be woken without a monotonic authority",
                    session_id=session_id,
                )
            return self._durable_result_receipt(
                result,
                workflow=workflow,
                execution_id=execution_id,
                attempt_id=attempt_id,
                input_sha256=input_sha256,
                session_id=session_id,
                input_state=input_state,
                binding=binding,
                record_history=True,
                terminal_run_result=False,
                operator_config_sha256=persisted_operator_config_sha256,
                root_config_sha256=persisted_root_config_sha256,
            )
        except (RuntimeOutcomeUnknownError, RuntimeTerminalError):
            raise
        except Exception as error:
            raise RuntimeError(
                "DeepSeek Harness durable attempt reconciliation failed: "
                f"{error}"
            ) from error

    def cancel(
        self,
        execution_id: str,
        attempt_id: str,
        *,
        binding: RuntimeBindingEnvelope,
        input_state: Mapping[str, Any] | None = None,
        input_sha256: str | None = None,
        attempt_metadata: Mapping[str, Any] | None = None,
    ) -> RuntimeOutcomeReceipt:
        """Cancel one exact durable attempt and verify its terminal receipt."""
        execution_id = self._require_execution_id(execution_id)
        attempt_id = self._validate_session_component(
            attempt_id,
            name="attempt_id",
            max_length=128,
        )
        if input_sha256 is None:
            raise ValueError("input_sha256 is required for durable cancellation")
        input_sha256 = self._validate_sha256(input_sha256)
        receipt_input_state: Mapping[str, Any] = input_state or {}
        if (
            input_state is not None
            and self._state_sha256(input_state) != input_sha256
        ):
            raise RuntimeBindingMismatchError(
                "DeepSeek Harness cancel input differs from the persisted fence"
            )
        workflow = self._require_workflow()
        with self._lifecycle_lock:
            self._require_open()
            harness = self._require_harness()
        (
            persisted_operator_config_sha256,
            persisted_root_config_sha256,
            expected_process_generation,
        ) = self._binding_metadata_from_attempt_metadata(attempt_metadata)
        self._validate_binding_envelope(
            binding,
            attempt_id=attempt_id,
            input_sha256=input_sha256,
            harness=harness,
            verify_current_binding=False,
            operator_config_sha256=persisted_operator_config_sha256,
            root_config_sha256=persisted_root_config_sha256,
        )
        session_id = binding.exact_session_id
        self._require_identity_api(harness)
        self._require_cancel_process_generation_api(harness)
        try:
            result = harness.cancel_attempt(
                session_id=session_id,
                attempt_id=attempt_id,
                input_sha256=input_sha256,
                expected_process_generation=expected_process_generation,
            )
            receipt = self._durable_result_receipt(
                result,
                workflow=workflow,
                execution_id=execution_id,
                attempt_id=attempt_id,
                input_sha256=input_sha256,
                session_id=session_id,
                input_state=receipt_input_state,
                binding=binding,
                record_history=False,
                terminal_run_result=False,
                operator_config_sha256=persisted_operator_config_sha256,
                root_config_sha256=persisted_root_config_sha256,
            )
        except RuntimeOutcomeUnknownError:
            raise
        except Exception as error:
            if self._is_process_generation_mismatch(error):
                raise RuntimeOutcomeUnknownError(
                    "DeepSeek Harness process generation changed before "
                    "cancellation; the durable attempt outcome remains unknown",
                    session_id=session_id,
                ) from error
            raise RuntimeError(
                "DeepSeek Harness durable attempt cancellation failed"
            ) from error
        return RuntimeOutcomeReceipt(
            execution_id=receipt.execution_id,
            attempt_id=receipt.attempt_id,
            input_sha256=receipt.input_sha256,
            outcome=receipt.outcome,
            binding=receipt.binding,
            values={},
            receipt=receipt.receipt,
            session_id=receipt.session_id,
            detail=receipt.detail,
        )

    def close(self) -> None:
        """Close an owned Harness exactly once and make this runtime terminal."""
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            harness = self._harness if self._owns_harness else None
            self._harness = None
        if harness is not None:
            harness.close()

    def _require_workflow(self) -> DeepSeekHarnessWorkflow:
        if self._workflow is None:
            raise RuntimeError("No DeepSeek Harness workflow is bound")
        return self._workflow

    def _require_harness(self) -> _Harness:
        if self._harness is not None:
            return self._harness
        factory = self._harness_factory or self._default_harness_factory
        self._harness = factory()
        return self._harness

    @staticmethod
    def _require_identity_api(harness: _Harness) -> None:
        """Fail before dispatch when the SDK cannot bind durable identity."""
        run = getattr(harness, "run", None)
        get_attempt = getattr(harness, "get_attempt", None)
        cancel_attempt = getattr(harness, "cancel_attempt", None)
        get_runtime_binding = getattr(harness, "get_runtime_binding", None)
        if not all(
            callable(method)
            for method in (
                run,
                get_attempt,
                cancel_attempt,
                get_runtime_binding,
            )
        ):
            raise RuntimeError(
                "DeepSeek Harness SDK lacks durable attempt identity APIs"
            )
        try:
            parameters = inspect.signature(run).parameters.values()
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "DeepSeek Harness SDK run signature cannot be verified"
            ) from error
        names = {parameter.name for parameter in parameters}
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        if not accepts_kwargs and not {"attempt_id", "input_sha256"}.issubset(names):
            raise RuntimeError(
                "DeepSeek Harness SDK run does not accept attempt identity"
            )


    def _runtime_configuration_sha256(self) -> str:
        """Hash semantic adapter configuration without persisting secrets/paths."""
        explicit = self._explicit_runtime_config_sha256
        if not self._owns_default_configuration:
            if explicit is not None:
                # An injected transport is opaque to WTB. Its caller-owned
                # deployment manifest must cover every semantic input,
                # including namespace/workflow mapping.
                return explicit
            raise RuntimeBindingMismatchError(
                "injected Harness requires an explicit deployment fingerprint"
            )
        if explicit is None:
            # HarnessClient inherits the host process environment before
            # applying its explicit overrides. An arbitrary Cordis plugin can
            # read any inherited key, so WTB cannot prove the composition from
            # harness_options alone.
            raise RuntimeBindingMismatchError(
                "adapter-owned Harness requires an explicit deployment "
                "fingerprint because inherited environment semantics are opaque"
            )

        workflow = self._require_workflow()
        options = dict(self._harness_options)
        raw_env = options.get("env", {})
        env = dict(raw_env) if isinstance(raw_env, Mapping) else {}
        command = options.get("launch_args_override")
        if command is not None:
            command = list(command)
        cordis = (
            options.get("cordis")
            or env.get("DSH_CORDIS_CONFIG")
            or self._configuration_path_from_command(command)
        )
        cordis_sha256 = self._configuration_file_sha256(
            cordis,
            name="Cordis configuration",
        )
        snapshot_sha256 = self._configuration_file_sha256(
            env.get("DSH_SNAPSHOT_FILE"),
            name="DSH snapshot",
        )
        payload = {
            "adapter": "deepseek-harness-sdk-jsonrpc",
            "deployment_fingerprint": explicit,
            "session_namespace": self._session_namespace,
            "fixed_session_id": self._fixed_session_id,
            "provider": options.get("provider", "deepseek-official"),
            "model": options.get("model", "deepseek-v4-flash"),
            "max_tokens": options.get("max_tokens"),
            "base_url": options.get("base_url") or env.get("DEEPSEEK_BASE_URL"),
            "cwd_location_sha256": self._path_location_sha256(
                options.get("cwd"),
                name="Harness cwd",
            ),
            "runtime_cwd_location_sha256": self._path_location_sha256(
                options.get("runtime_cwd"),
                name="Harness runtime cwd",
            ),
            "runtime_command": command,
            "session_root_location_sha256": self._path_location_sha256(
                options.get("session_root"),
                name="Harness session root",
            ),
            "cordis_sha256": cordis_sha256,
            "snapshot_sha256": snapshot_sha256,
            "explicit_env_sha256": self._explicit_environment_fingerprints(env),
            "sdk_source_identity": self._sdk_source_identity(),
            "workflow": {
                "prompt_key": workflow.prompt_key,
                "result_key": workflow.result_key,
                "session_prefix": workflow.session_prefix,
                "include_events": workflow.include_events,
                "include_session_root": workflow.include_session_root,
                "max_event_count": workflow.max_event_count,
                "max_event_bytes": workflow.max_event_bytes,
            },
        }
        return canonical_json_sha256(payload)

    @staticmethod
    def _composite_runtime_config_sha256(
        *,
        operator_config_sha256: str,
        root_config_sha256: str,
    ) -> str:
        """Bind operator evidence to the exact bytes read by Loader."""
        return canonical_json_sha256(
            {
                "operator_config_sha256": operator_config_sha256,
                "root_config_sha256": root_config_sha256,
            }
        )

    def _configured_root_config_sha256(self) -> str | None:
        """Hash the operator-visible root config when its path is explicit."""
        if not self._owns_default_configuration:
            return None
        options = dict(self._harness_options)
        raw_env = options.get("env", {})
        env = dict(raw_env) if isinstance(raw_env, Mapping) else {}
        command = options.get("launch_args_override")
        if command is not None:
            command = list(command)
        cordis = (
            options.get("cordis")
            or env.get("DSH_CORDIS_CONFIG")
            or self._configuration_path_from_command(command)
        )
        return self._configuration_file_sha256(
            cordis,
            name="Cordis configuration",
        )

    def _validate_operator_root_config(
        self,
        root_config_sha256: str,
        *,
        configured_root_sha256: str | None = None,
    ) -> None:
        root_config_sha256 = self._validate_sha256(
            root_config_sha256,
            name="root_config_sha256",
        )
        configured = (
            self._configured_root_config_sha256()
            if configured_root_sha256 is None
            else configured_root_sha256
        )
        if configured is not None and configured != root_config_sha256:
            raise RuntimeBindingMismatchError(
                "Loader root config bytes differ from the operator-visible config"
            )

    @staticmethod
    def _configuration_path_from_command(
        command: Sequence[Any] | None,
    ) -> str | None:
        """Find the explicit Cordis-like config argument in runtime argv."""
        if command is None:
            return None
        candidates: list[str] = []
        for value in command:
            if not isinstance(value, str) or not value:
                continue
            path = Path(value).expanduser()
            if path.suffix.lower() in {".yml", ".yaml", ".json", ".json5"}:
                candidates.append(value)
        if len(candidates) > 1:
            raise RuntimeBindingMismatchError(
                "runtime command has ambiguous configuration file arguments"
            )
        return candidates[0] if candidates else None

    @staticmethod
    def _explicit_environment_fingerprints(
        env: Mapping[str, Any],
    ) -> dict[str, str]:
        """Hash explicit non-credential environment semantics by key.

        Values are never copied into the durable envelope. Credential values
        are deliberately excluded; Cordis/snapshot path channels are covered
        by file-content digests instead, so relocating identical config does
        not manufacture drift.
        """
        excluded_channels = {
            "DSH_CORDIS_CONFIG",
            "DSH_SNAPSHOT_FILE",
        }
        path_channels = {"DSH_CWD", "DSH_SESSION_ROOT"}
        fingerprints: dict[str, str] = {}
        for key, value in sorted(env.items(), key=lambda item: str(item[0])):
            if not isinstance(key, str) or not key:
                raise RuntimeBindingMismatchError(
                    "Harness environment keys must be non-empty strings"
                )
            normalized_key = key.upper()
            if normalized_key in excluded_channels or normalized_key.endswith(
                ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")
            ):
                continue
            if not isinstance(value, str):
                raise RuntimeBindingMismatchError(
                    "Harness environment values must be strings"
                )
            if normalized_key in path_channels:
                fingerprints[key] = DeepSeekHarnessRuntime._path_location_sha256(
                    value,
                    name=key,
                )
            else:
                fingerprints[key] = hashlib.sha256(
                    value.encode("utf-8")
                ).hexdigest()
        return fingerprints

    @staticmethod
    def _path_location_sha256(value: Any, *, name: str) -> str | None:
        """Hash one configured location after absolute platform normalization."""
        if value is None:
            return None
        if not isinstance(value, (str, Path)) or not str(value):
            raise RuntimeBindingMismatchError(f"{name} path is invalid")
        normalized = os.path.normcase(
            os.path.normpath(
                os.path.abspath(os.path.expanduser(os.fspath(value)))
            )
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _configuration_file_sha256(value: Any, *, name: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, (str, Path)) or not str(value):
            raise RuntimeBindingMismatchError(f"{name} path is invalid")
        path = Path(value).expanduser()
        try:
            content = path.read_bytes()
        except OSError as error:
            raise RuntimeBindingMismatchError(
                f"{name} content cannot be fingerprinted"
            ) from error
        return hashlib.sha256(content).hexdigest()

    def _sdk_source_identity(self) -> str:
        source = self._sdk_source_path
        if source is None:
            spec = importlib.util.find_spec("deepseek_harness")
            origin = getattr(spec, "origin", None) if spec is not None else None
            if isinstance(origin, str) and Path(origin).is_file():
                return "installed:" + hashlib.sha256(
                    Path(origin).read_bytes()
                ).hexdigest()
            return "installed:unresolved"
        package = source / "deepseek_harness"
        digest = hashlib.sha256()
        for path in sorted(package.rglob("*.py")):
            digest.update(path.relative_to(package).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return "source:" + digest.hexdigest()

    @classmethod
    def _normalize_sdk_binding(cls, raw: object) -> tuple[int, int, str, str]:
        def field(snake: str, camel: str) -> Any:
            if isinstance(raw, Mapping):
                return raw.get(snake, raw.get(camel))
            return getattr(raw, snake, getattr(raw, camel, None))

        protocol_version = field("protocol_version", "protocolVersion")
        schema_version = field("schema_version", "schemaVersion")
        persistence_instance_id = field(
            "persistence_instance_id",
            "persistenceInstanceId",
        )
        root_config_sha256 = field(
            "root_config_sha256",
            "rootConfigSha256",
        )
        for name, value in (
            ("protocol_version", protocol_version),
            ("schema_version", schema_version),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise RuntimeBindingMismatchError(
                    f"DeepSeek Harness {name} is invalid"
                )
        if protocol_version != _RUNTIME_BINDING_PROTOCOL_VERSION:
            raise RuntimeBindingMismatchError(
                "DeepSeek Harness binding protocol is unsupported"
            )
        if schema_version != _RUNTIME_BINDING_SCHEMA_VERSION:
            raise RuntimeBindingMismatchError(
                "DeepSeek Harness binding schema does not attest root config"
            )
        try:
            persistence_instance_id = cls._validate_session_component(
                persistence_instance_id,
                name="persistence_instance_id",
                max_length=128,
            )
        except (TypeError, ValueError) as error:
            raise RuntimeBindingMismatchError(
                "DeepSeek Harness persistence binding is invalid"
            ) from error
        try:
            root_config_sha256 = cls._validate_sha256(
                root_config_sha256,
                name="root_config_sha256",
            )
        except (TypeError, ValueError) as error:
            raise RuntimeBindingMismatchError(
                "DeepSeek Harness root config binding is invalid"
            ) from error
        return (
            protocol_version,
            schema_version,
            persistence_instance_id,
            root_config_sha256,
        )

    def _current_sdk_binding(self, harness: _Harness) -> tuple[int, int, str, str]:
        get_binding = getattr(harness, "get_runtime_binding", None)
        if not callable(get_binding):
            raise RuntimeBindingMismatchError(
                "DeepSeek Harness SDK exposes no durable runtime binding"
            )
        try:
            raw = get_binding()
        except Exception as error:
            raise RuntimeBindingMismatchError(
                "DeepSeek Harness runtime binding could not be established"
            ) from error
        return self._normalize_sdk_binding(raw)

    def _current_sdk_process_binding(
        self,
        harness: _Harness,
    ) -> tuple[int, int, str, str, str]:
        """Initialize and capture one process generation with its store binding."""
        get_process_binding = getattr(
            harness,
            "get_runtime_process_binding",
            None,
        )
        if not callable(get_process_binding):
            raise RuntimeBindingMismatchError(
                "DeepSeek Harness SDK exposes no process-generation binding"
            )
        try:
            raw = get_process_binding()
        except Exception as error:
            raise RuntimeBindingMismatchError(
                "DeepSeek Harness process binding could not be established"
            ) from error

        def field(snake: str, camel: str) -> Any:
            if isinstance(raw, Mapping):
                return raw.get(snake, raw.get(camel))
            return getattr(raw, snake, getattr(raw, camel, None))

        runtime_binding = field("runtime_binding", "runtimeBinding")
        process_generation = field("process_generation", "processGeneration")
        if not isinstance(process_generation, str) or not process_generation:
            raise RuntimeBindingMismatchError(
                "DeepSeek Harness process generation is invalid"
            )
        return (*self._normalize_sdk_binding(runtime_binding), process_generation)

    def _validate_binding_envelope(
        self,
        binding: RuntimeBindingEnvelope,
        *,
        attempt_id: str,
        input_sha256: str,
        harness: _Harness,
        verify_current_binding: bool = True,
        operator_config_sha256: str | None = None,
        root_config_sha256: str | None = None,
    ) -> None:
        if not isinstance(binding, RuntimeBindingEnvelope):
            raise RuntimeBindingMismatchError(
                "durable operation requires a runtime binding envelope"
            )
        if binding.attempt_id != attempt_id:
            raise RuntimeBindingMismatchError(
                "runtime binding attempt identity disagrees with the request"
            )
        if binding.input_sha256 != input_sha256:
            raise RuntimeBindingMismatchError(
                "runtime binding input identity disagrees with the request"
            )
        pre_binding_config_sha256 = self._runtime_configuration_sha256()
        if operator_config_sha256 is not None:
            operator_config_sha256 = self._validate_sha256(
                operator_config_sha256,
                name="operator_config_sha256",
            )
            if pre_binding_config_sha256 != operator_config_sha256:
                raise RuntimeBindingMismatchError(
                    "operator config fingerprint drifted from the dispatch fence"
                )
        else:
            operator_config_sha256 = pre_binding_config_sha256
        if root_config_sha256 is not None:
            root_config_sha256 = self._validate_sha256(
                root_config_sha256,
                name="root_config_sha256",
            )
            self._validate_operator_root_config(root_config_sha256)
        if (
            root_config_sha256 is not None
            and binding.runtime_config_sha256
            != self._composite_runtime_config_sha256(
                operator_config_sha256=operator_config_sha256,
                root_config_sha256=root_config_sha256,
            )
        ):
            raise RuntimeBindingMismatchError(
                "runtime config fingerprint drifted from the dispatch fence"
            )
        if not verify_current_binding:
            if root_config_sha256 is None:
                raise RuntimeBindingMismatchError(
                    "initial dispatch has no persisted root config binding"
                )
            # Initial dispatch must not call an SDK lookup that can lazily
            # restart a dead process. The process generation captured during
            # prepare binds this envelope to the exact initialized process;
            # SDK run verifies that generation before admission, and the
            # terminal result re-attests the complete store binding.
            return
        current = self._current_sdk_binding(harness)
        post_binding_config_sha256 = self._runtime_configuration_sha256()
        if post_binding_config_sha256 != pre_binding_config_sha256:
            raise RuntimeBindingMismatchError(
                "runtime config changed while DeepSeek Harness binding was checked"
            )
        current_root_config_sha256 = current[3]
        self._validate_operator_root_config(current_root_config_sha256)
        current_composite = self._composite_runtime_config_sha256(
            operator_config_sha256=post_binding_config_sha256,
            root_config_sha256=current_root_config_sha256,
        )
        if binding.runtime_config_sha256 != current_composite:
            raise RuntimeBindingMismatchError(
                "runtime config binding differs from the dispatch fence"
            )
        expected = (
            binding.protocol_version,
            binding.schema_version,
            binding.persistence_instance_id,
        )
        if current[:3] != expected:
            raise RuntimeBindingMismatchError(
                "current DeepSeek Harness binding differs from the dispatch fence"
            )

    @staticmethod
    def _binding_metadata_from_attempt_metadata(
        attempt_metadata: Mapping[str, Any] | None,
    ) -> tuple[str, str, str]:
        if not isinstance(attempt_metadata, Mapping):
            raise RuntimeBindingMismatchError(
                "durable DeepSeek Harness operation requires persisted attempt metadata"
            )
        operator_config_sha256 = attempt_metadata.get("operator_config_sha256")
        root_config_sha256 = attempt_metadata.get("root_config_sha256")
        process_generation = attempt_metadata.get("process_generation")
        for name, value in (
            ("operator_config_sha256", operator_config_sha256),
            ("root_config_sha256", root_config_sha256),
        ):
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise RuntimeBindingMismatchError(
                    f"durable DeepSeek Harness operation has no {name}"
                )
        if not isinstance(process_generation, str) or not process_generation:
            raise RuntimeBindingMismatchError(
                "durable DeepSeek Harness operation has no process-generation pin"
            )
        return operator_config_sha256, root_config_sha256, process_generation

    @staticmethod
    def _is_process_generation_mismatch(error: BaseException) -> bool:
        """Recognize the optional SDK's typed pre-admission rejection."""
        return type(error).__name__ == "RuntimeGenerationMismatchError"

    @staticmethod
    def _require_process_generation_api(harness: _Harness) -> None:
        run = getattr(harness, "run", None)
        get_process_binding = getattr(
            harness,
            "get_runtime_process_binding",
            None,
        )
        if not callable(run) or not callable(get_process_binding):
            raise RuntimeBindingMismatchError(
                "DeepSeek Harness SDK lacks process-generation admission APIs"
            )
        try:
            parameters = inspect.signature(run).parameters.values()
        except (TypeError, ValueError) as error:
            raise RuntimeBindingMismatchError(
                "DeepSeek Harness SDK run signature cannot be verified"
            ) from error
        names = {parameter.name for parameter in parameters}
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        if not accepts_kwargs and "expected_process_generation" not in names:
            raise RuntimeBindingMismatchError(
                "DeepSeek Harness SDK run cannot pin process generation"
            )

    @staticmethod
    def _require_cancel_process_generation_api(harness: _Harness) -> None:
        cancel_attempt = getattr(harness, "cancel_attempt", None)
        if not callable(cancel_attempt):
            raise RuntimeBindingMismatchError(
                "DeepSeek Harness SDK lacks durable cancellation API"
            )
        try:
            parameters = inspect.signature(cancel_attempt).parameters.values()
        except (TypeError, ValueError) as error:
            raise RuntimeBindingMismatchError(
                "DeepSeek Harness SDK cancel signature cannot be verified"
            ) from error
        names = {parameter.name for parameter in parameters}
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        if not accepts_kwargs and "expected_process_generation" not in names:
            raise RuntimeBindingMismatchError(
                "DeepSeek Harness SDK cancel cannot pin process generation"
            )

    def _validate_result_binding(
        self,
        result: _RunResult,
        binding: RuntimeBindingEnvelope | None,
        *,
        operator_config_sha256: str | None = None,
        root_config_sha256: str | None = None,
    ) -> None:
        if binding is None:
            raise RuntimeBindingMismatchError(
                "durable result has no expected runtime binding"
            )
        observed = self._normalize_sdk_binding(
            getattr(result, "runtime_binding", None)
        )
        expected = (
            binding.protocol_version,
            binding.schema_version,
            binding.persistence_instance_id,
        )
        if observed[:3] != expected:
            raise RuntimeBindingMismatchError(
                "DeepSeek Harness result binding differs from the dispatch fence"
            )
        observed_root_config_sha256 = observed[3]
        if (
            root_config_sha256 is not None
            and observed_root_config_sha256 != root_config_sha256
        ):
            raise RuntimeBindingMismatchError(
                "DeepSeek Harness result root config differs from the dispatch fence"
            )
        self._validate_operator_root_config(observed_root_config_sha256)
        operator_config_sha256 = (
            self._runtime_configuration_sha256()
            if operator_config_sha256 is None
            else self._validate_sha256(
                operator_config_sha256,
                name="operator_config_sha256",
            )
        )
        if binding.runtime_config_sha256 != self._composite_runtime_config_sha256(
            operator_config_sha256=operator_config_sha256,
            root_config_sha256=observed_root_config_sha256,
        ):
            raise RuntimeBindingMismatchError(
                "DeepSeek Harness result config differs from the dispatch fence"
            )

    def _snapshot_for_attempt(
        self,
        execution_id: str,
        attempt_id: str,
    ) -> RuntimeSnapshot | None:
        with self._history_lock:
            for snapshot in reversed(self._history.get(execution_id, ())):
                if snapshot.metadata.get("attempt_id") == attempt_id:
                    return deepcopy(snapshot)
        return None

    def _replay_attempt(
        self,
        snapshot: RuntimeSnapshot,
        *,
        input_sha256: str,
    ) -> Mapping[str, Any]:
        receipt = snapshot.values.get("_dsh")
        if not isinstance(receipt, Mapping):
            raise RuntimeError("stored DeepSeek Harness attempt has no receipt")
        if receipt.get("input_sha256") != input_sha256:
            raise ValueError(
                "attempt_id is already bound to a different DeepSeek Harness input"
            )
        finish_reason = receipt.get("finish_reason")
        if not isinstance(finish_reason, str):
            raise RuntimeError("stored DeepSeek Harness attempt has no finish reason")
        session_id = receipt.get("session_id")
        self._reject_crash_repaired_terminal(
            finish_reason,
            session_id=session_id if isinstance(session_id, str) else None,
        )
        values = deepcopy(snapshot.values)
        if finish_reason == "completed":
            return values
        raise RuntimeTerminalError(
            "DeepSeek Harness attempt already has a trusted non-success terminal "
            f"outcome: finish_reason={finish_reason!r}",
            finish_reason=finish_reason,
            session_id=session_id if isinstance(session_id, str) else None,
            result_state=values,
            receipt=deepcopy(receipt),
        )

    def _durable_result_receipt(
        self,
        result: _RunResult,
        *,
        workflow: DeepSeekHarnessWorkflow,
        execution_id: str,
        attempt_id: str,
        input_sha256: str,
        session_id: str,
        input_state: Mapping[str, Any],
        binding: RuntimeBindingEnvelope,
        record_history: bool,
        terminal_run_result: bool,
        operator_config_sha256: str | None = None,
        root_config_sha256: str | None = None,
    ) -> RuntimeOutcomeReceipt:
        """Validate and materialize one public durable attempt record."""
        if result is None:
            raise RuntimeError("DeepSeek Harness returned no attempt record")
        if getattr(result, "session_id", None) != session_id:
            raise RuntimeError("DeepSeek Harness attempt belongs to another session")
        if getattr(result, "attempt_id", None) != attempt_id:
            raise RuntimeError("DeepSeek Harness attempt belongs to another attempt")
        if getattr(result, "input_sha256", None) != input_sha256:
            raise RuntimeError("DeepSeek Harness attempt belongs to another input")
        self._validate_result_binding(
            result,
            binding,
            operator_config_sha256=operator_config_sha256,
            root_config_sha256=root_config_sha256,
        )
        message_id = getattr(result, "message_id", None)
        if not isinstance(message_id, str) or not message_id:
            raise RuntimeError("DeepSeek Harness attempt has no durable message id")
        status = getattr(result, "status", None)
        if terminal_run_result:
            if status not in {None, "terminal"}:
                raise RuntimeError("DeepSeek Harness run result is not terminal")
        elif status != "terminal":
            raise RuntimeError("DeepSeek Harness attempt is not terminal")
        finish_reason = self._finish_reason_kind(
            getattr(result, "finish_reason", None)
        )
        self._reject_crash_repaired_terminal(
            finish_reason,
            session_id=session_id,
        )
        outcome = self._terminal_outcome(finish_reason)
        final_response = getattr(result, "final_response", None)
        if not isinstance(final_response, str):
            raise RuntimeError("DeepSeek Harness attempt has no final response")

        raw_events = getattr(result, "events", None)
        if raw_events is None:
            raw_events = []
        if not isinstance(raw_events, list) or not all(
            isinstance(event, dict) for event in raw_events
        ):
            raise RuntimeError("DeepSeek Harness attempt has invalid events")
        if raw_events:
            self._validate_terminal_evidence(raw_events, finish_reason)

        receipt: dict[str, Any] = {
            "protocol": "deepseek-harness-sdk-jsonrpc",
            "session_id": session_id,
            "session_namespace": self._session_namespace,
            "execution_id": execution_id,
            "attempt_id": attempt_id,
            "input_sha256": input_sha256,
            "message_id": message_id,
            "attempt_status": status or "terminal",
            "finish_reason": finish_reason,
            "event_count": len(raw_events),
            "last_event_seq": self._last_event_seq(raw_events),
            "binding": binding.to_dict(),
        }
        if operator_config_sha256 is not None:
            receipt["operator_config_sha256"] = operator_config_sha256
        if root_config_sha256 is not None:
            receipt["root_config_sha256"] = root_config_sha256
        session_root = getattr(result, "session_root", None)
        if session_root is not None:
            if not isinstance(session_root, str) or not session_root:
                raise RuntimeError("DeepSeek Harness attempt has invalid session root")
            receipt["session_root_configured"] = True
            if workflow.include_session_root:
                receipt["session_root"] = session_root
        else:
            receipt["session_root_configured"] = False
        if len(raw_events) <= workflow.max_event_count:
            receipt["event_types"] = [
                cast(str, event["type"]) for event in raw_events
            ]
            if workflow.include_events:
                event_bytes = self._json_size(raw_events)
                receipt["event_bytes"] = event_bytes
                if event_bytes <= workflow.max_event_bytes:
                    receipt["events"] = deepcopy(raw_events)
                else:
                    receipt["event_evidence_omitted_reason"] = (
                        "event_bytes_limit"
                    )
        else:
            receipt["event_evidence_omitted_reason"] = "event_count_limit"

        output = deepcopy(dict(input_state))
        output[workflow.result_key] = final_response
        output["_dsh"] = receipt
        snapshot = RuntimeSnapshot(
            execution_id=execution_id,
            checkpoint_id=None,
            values=deepcopy(output),
            metadata={
                "runtime": "deepseek-harness",
                "recoverable": False,
                "session_id": session_id,
                "attempt_id": attempt_id,
                "input_sha256": input_sha256,
                "message_id": message_id,
                "attempt_status": status or "terminal",
                "finish_reason": finish_reason,
                "last_event_seq": receipt["last_event_seq"],
            },
        )
        if record_history:
            with self._history_lock:
                self._history.setdefault(execution_id, []).append(snapshot)
        return RuntimeOutcomeReceipt(
            execution_id=execution_id,
            attempt_id=attempt_id,
            input_sha256=input_sha256,
            outcome=outcome,
            binding=binding,
            values=output,
            receipt=receipt,
            session_id=session_id,
            detail=f"DeepSeek Harness finish_reason={finish_reason}",
        )

    def _default_harness_factory(self) -> _Harness:
        try:
            sdk = (
                self._load_local_sdk(self._sdk_source_path)
                if self._sdk_source_path is not None
                else self._load_installed_sdk()
            )
        except ImportError as error:
            raise RuntimeError(
                "DeepSeek Harness Python SDK is unavailable, and no published "
                "SDK currently satisfies WTB's durable-attempt-v2 contract. "
                "For source-mode development, install this checkout's "
                "'deepseek-harness-source' extra, pass "
                "sdk_source_path='<deepseek-harness checkout>' and "
                "runtime_command=('node', '<built JSON-RPC runtime>.js'); the "
                "runtime command also needs an explicit Cordis config."
            ) from error
        deepseek_harness = getattr(sdk, "DeepSeekHarness", None)
        if not callable(deepseek_harness):
            raise RuntimeError(
                "DeepSeek Harness SDK source does not export DeepSeekHarness"
            )
        return cast(_Harness, deepseek_harness(**self._harness_options))

    @staticmethod
    def _load_installed_sdk() -> ModuleType:
        import deepseek_harness

        return deepseek_harness

    @staticmethod
    def _resolve_sdk_source_path(source: str | Path) -> Path:
        source_path = Path(source).expanduser().resolve()
        candidates = (source_path, source_path / "python" / "sdk" / "src")
        for candidate in candidates:
            if (candidate / "deepseek_harness" / "__init__.py").is_file():
                return candidate
        raise FileNotFoundError(
            "sdk_source_path must be the DeepSeek Harness checkout root or its "
            f"python/sdk/src directory; no deepseek_harness package found under {source_path}"
        )

    @staticmethod
    def _load_local_sdk(source_root: Path) -> ModuleType:
        """Load one checkout under a private package name without changing sys.path."""
        package_dir = source_root / "deepseek_harness"
        module_key = (
            "_wtb_deepseek_harness_"
            + hashlib.sha256(str(source_root).encode("utf-8")).hexdigest()[:16]
        )
        with _LOCAL_SDK_IMPORT_LOCK:
            existing = sys.modules.get(module_key)
            if isinstance(existing, ModuleType):
                return existing
            spec = importlib.util.spec_from_file_location(
                module_key,
                package_dir / "__init__.py",
                submodule_search_locations=[str(package_dir)],
            )
            if spec is None or spec.loader is None:
                raise ImportError(
                    f"could not create an import spec for DeepSeek Harness at {package_dir}"
                )
            module = importlib.util.module_from_spec(spec)
            before = set(sys.modules)
            sys.modules[module_key] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                for name in set(sys.modules).difference(before):
                    if name == module_key or name.startswith(f"{module_key}."):
                        sys.modules.pop(name, None)
                raise
            return module

    @staticmethod
    def _validate_runtime_command(command: Sequence[str]) -> tuple[str, ...]:
        if isinstance(command, (str, bytes)):
            raise TypeError("runtime_command must be an argv sequence, not a string")
        normalized = tuple(command)
        if not normalized or any(
            not isinstance(argument, str) or not argument for argument in normalized
        ):
            raise ValueError("runtime_command must contain non-empty string arguments")
        return normalized

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("DeepSeek Harness runtime is closed")

    @classmethod
    def _require_execution_id(cls, execution_id: str) -> str:
        return cls._validate_session_component(
            execution_id,
            name="execution_id",
            max_length=128,
        )

    @staticmethod
    def _validate_session_component(
        value: str,
        *,
        name: str,
        max_length: int,
    ) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a non-empty string")
        if len(value) > max_length:
            raise ValueError(f"{name} must contain at most {max_length} characters")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
            raise ValueError(
                f"{name} must contain only safe ASCII identifier characters"
            )
        return value

    @staticmethod
    def _validate_sha256(value: str, *, name: str = "input_sha256") -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        return value

    def _session_id(
        self,
        workflow: DeepSeekHarnessWorkflow,
        execution_id: str,
    ) -> str:
        if self._fixed_session_id is not None:
            return self._fixed_session_id
        return f"{workflow.session_prefix}{self._session_namespace}-{execution_id}"

    @classmethod
    def _validated_prompt(cls, value: Any, *, prompt_key: str) -> object:
        if isinstance(value, str):
            return value
        if not isinstance(value, list):
            raise TypeError(
                f"DeepSeek Harness input '{prompt_key}' must be a string or "
                "a list of JSON content-block objects"
            )
        for index, block in enumerate(value):
            if not isinstance(block, dict):
                raise TypeError(
                    f"DeepSeek Harness input '{prompt_key}[{index}]' must be "
                    "a JSON object"
                )
        cls._validate_json_value(value, path=prompt_key, active=set())
        return deepcopy(value)




    @classmethod
    def _validate_json_value(
        cls,
        value: Any,
        *,
        path: str,
        active: set[int],
    ) -> None:
        if value is None or isinstance(value, (str, bool, int)):
            return
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"{path} contains a non-finite JSON number")
            return
        if isinstance(value, dict):
            identity = id(value)
            if identity in active:
                raise ValueError(f"{path} contains a cyclic JSON object")
            active.add(identity)
            try:
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise TypeError(f"{path} contains a non-string JSON key")
                    cls._validate_json_value(
                        item,
                        path=f"{path}.{key}",
                        active=active,
                    )
            finally:
                active.remove(identity)
            return
        if isinstance(value, list):
            identity = id(value)
            if identity in active:
                raise ValueError(f"{path} contains a cyclic JSON array")
            active.add(identity)
            try:
                for index, item in enumerate(value):
                    cls._validate_json_value(
                        item,
                        path=f"{path}[{index}]",
                        active=active,
                    )
            finally:
                active.remove(identity)
            return
        raise TypeError(
            f"{path} contains a non-JSON value of type {type(value).__name__}"
        )

    @classmethod
    def _finish_reason_kind(cls, finish_reason: Any) -> str:
        """Normalize RunResult strings and typed receipt ``finishReason``."""
        if isinstance(finish_reason, Mapping):
            finish_reason = finish_reason.get("kind")
        if not isinstance(finish_reason, str) or not finish_reason:
            raise RuntimeError("DeepSeek Harness returned an invalid finish reason")
        if len(finish_reason) > 128:
            raise RuntimeError("DeepSeek Harness returned an oversized finish reason")
        return finish_reason

    @classmethod
    def _validate_terminal_evidence(
        cls,
        events: Sequence[Mapping[str, Any]],
        finish_reason: Any,
    ) -> str:
        finish_reason = cls._finish_reason_kind(finish_reason)

        previous_sequence = -1
        saw_sequence = False
        saw_missing_sequence = False
        terminal_events: list[Mapping[str, Any]] = []
        for index, event in enumerate(events):
            cls._validate_json_value(
                event,
                path=f"events[{index}]",
                active=set(),
            )
            event_type = event.get("type")
            if (
                not isinstance(event_type, str)
                or not event_type
                or len(event_type) > 128
            ):
                raise RuntimeError("DeepSeek Harness returned an invalid event type")
            sequence = event.get("seq")
            if sequence is None and "seq" not in event:
                saw_missing_sequence = True
            else:
                if (
                    not isinstance(sequence, int)
                    or isinstance(sequence, bool)
                    or sequence < 0
                    or sequence <= previous_sequence
                ):
                    raise RuntimeError(
                        "DeepSeek Harness event sequences must be non-negative and "
                        "strictly increasing"
                    )
                saw_sequence = True
                previous_sequence = sequence
            if event_type == "turn/end":
                terminal_events.append(event)

        if saw_sequence and saw_missing_sequence:
            raise RuntimeError(
                "DeepSeek Harness event sequences must be present on every event "
                "or omitted from every event"
            )

        if len(terminal_events) != 1:
            raise RuntimeError(
                "DeepSeek Harness response must contain exactly one turn/end event"
            )
        terminal_data = terminal_events[0].get("data")
        if not isinstance(terminal_data, dict):
            raise RuntimeError("DeepSeek Harness turn/end event has invalid data")
        reason = terminal_data.get("reason")
        if not isinstance(reason, dict):
            raise RuntimeError("DeepSeek Harness turn/end event has invalid reason")
        reason_kind = reason.get("kind")
        if not isinstance(reason_kind, str) or not reason_kind:
            raise RuntimeError(
                "DeepSeek Harness turn/end event has invalid reason kind"
            )
        if reason_kind != finish_reason:
            raise RuntimeError(
                "DeepSeek Harness finish_reason disagrees with turn/end reason"
            )
        return finish_reason

    @staticmethod
    def _json_size(value: Any) -> int:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return len(encoded)

    @staticmethod
    def _state_sha256(input_state: Mapping[str, Any]) -> str:
        return canonical_json_sha256(input_state)

    @staticmethod
    def _terminal_outcome(finish_reason: str) -> RuntimeTerminalOutcome:
        if finish_reason == "completed":
            return RuntimeTerminalOutcome.COMPLETED
        if finish_reason in {
            "aborted",
            "canceled",
            "cancelled",
            "stopped",
        }:
            return RuntimeTerminalOutcome.CANCELLED
        return RuntimeTerminalOutcome.FAILED

    @staticmethod
    def _reject_crash_repaired_terminal(
        finish_reason: str,
        *,
        session_id: str | None,
    ) -> None:
        """Keep crash-only ``interrupted`` behind the unknown-outcome fence."""
        if finish_reason != "interrupted":
            return
        raise RuntimeOutcomeUnknownError(
            "DeepSeek Harness crash repair closed the attempt as interrupted; "
            "the model or tool side effects may already have happened, so the "
            "outcome cannot be acknowledged or retried automatically",
            session_id=session_id,
        )

    @staticmethod
    def _last_event_seq(events: Sequence[Mapping[str, Any]]) -> int | None:
        if not events or any("seq" not in event for event in events):
            return None
        sequence = events[-1].get("seq")
        return (
            sequence
            if isinstance(sequence, int) and not isinstance(sequence, bool)
            else None
        )
