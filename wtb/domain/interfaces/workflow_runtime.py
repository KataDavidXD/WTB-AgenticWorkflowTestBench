"""Framework-neutral workflow execution port.

The runtime owns framework-specific invocation semantics. State persistence,
file/CAS recovery, and WTB execution lifecycle remain separate ports.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Protocol, runtime_checkable


class RuntimeOutcomeUnknownError(RuntimeError):
    """A runtime call may have committed, but its result was not observed."""

    def __init__(self, message: str, *, session_id: str | None = None) -> None:
        super().__init__(message)
        self.session_id = session_id


class RuntimePreDispatchError(RuntimeError):
    """The runtime proved that no external workflow turn was dispatched."""


class RuntimeBindingMismatchError(RuntimePreDispatchError):
    """Current runtime/store identity differs from the persisted envelope."""


class RuntimeTerminalOutcome(str, Enum):
    """A verified terminal outcome reported by a workflow runtime."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RuntimeTerminalError(RuntimeError):
    """A runtime returned a trusted, non-success terminal receipt.

    This is deliberately distinct from :class:`RuntimeOutcomeUnknownError`.
    A known ``turn/end`` failure can be persisted as terminal without wedging
    the execution behind an outcome-unknown recovery gate.
    """

    def __init__(
        self,
        message: str,
        *,
        finish_reason: str,
        session_id: str | None = None,
        result_state: Mapping[str, Any] | None = None,
        receipt: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if not isinstance(finish_reason, str) or not finish_reason:
            raise ValueError("finish_reason must be a non-empty string")
        self.finish_reason = finish_reason
        self.session_id = session_id
        self.result_state = result_state
        self.receipt = receipt


@dataclass(frozen=True, slots=True)
class RuntimeBindingEnvelope:
    """Complete durable identity of one external runtime dispatch.

    The envelope is committed by WTB before dispatch and must be supplied back
    to the runtime for lookup, redrive, and cancellation.  In particular,
    ``persistence_instance_id`` is an opaque provider identity; it must never
    be inferred from a filesystem path or other mutable operator input.
    """

    exact_session_id: str
    persistence_instance_id: str
    runtime_config_sha256: str
    protocol_version: int
    schema_version: int
    input_sha256: str
    attempt_id: str

    _FIELDS = (
        "exact_session_id",
        "persistence_instance_id",
        "runtime_config_sha256",
        "protocol_version",
        "schema_version",
        "input_sha256",
        "attempt_id",
    )

    def __post_init__(self) -> None:
        for name, max_length in (
            ("exact_session_id", 256),
            ("persistence_instance_id", 128),
            ("attempt_id", 128),
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) is None
                or len(value) > max_length
            ):
                raise ValueError(
                    f"{name} must be a bounded opaque safe identifier"
                )
        for name in ("runtime_config_sha256", "input_sha256"):
            value = getattr(self, name)
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        for name in ("protocol_version", "schema_version"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    def to_dict(self) -> dict[str, str | int]:
        """Serialize the versioned envelope for execution metadata/outbox."""
        return {name: getattr(self, name) for name in self._FIELDS}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RuntimeBindingEnvelope":
        """Load a persisted envelope without accepting partial identities."""
        if not isinstance(value, Mapping):
            raise TypeError("runtime binding envelope must be a mapping")
        missing = set(cls._FIELDS).difference(value)
        unexpected = set(value).difference(cls._FIELDS)
        if missing or unexpected:
            detail = []
            if missing:
                detail.append(f"missing {', '.join(sorted(missing))}")
            if unexpected:
                detail.append(f"unexpected {', '.join(sorted(unexpected))}")
            raise ValueError(f"invalid runtime binding envelope: {'; '.join(detail)}")
        return cls(**{name: value[name] for name in cls._FIELDS})


@dataclass(frozen=True, slots=True)
class RuntimeOutcomeReceipt:
    """Runtime-owned proof used to reconcile or acknowledge cancellation.

    ``attempt_id`` is the durable WTB dispatch identity. Runtimes must only
    return a receipt after verifying that the external session/turn belongs to
    that exact attempt and execution. ``input_sha256`` is the lowercase
    SHA-256 identity committed in WTB's pre-dispatch fence; it is a first-class
    ownership field rather than untyped evidence hidden inside ``receipt``.
    """

    execution_id: str
    attempt_id: str
    input_sha256: str
    outcome: RuntimeTerminalOutcome
    binding: RuntimeBindingEnvelope | None = None
    values: Mapping[str, Any] = field(default_factory=dict)
    receipt: Mapping[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeAttemptPlan:
    """Runtime identity acquired without dispatching model/tool work."""

    execution_id: str
    attempt_id: str
    session_id: str
    binding: RuntimeBindingEnvelope
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.binding, RuntimeBindingEnvelope):
            raise TypeError("binding must be a RuntimeBindingEnvelope")
        if self.attempt_id != self.binding.attempt_id:
            raise ValueError("attempt plan and runtime binding attempt disagree")
        if self.session_id != self.binding.exact_session_id:
            raise ValueError("attempt plan and runtime binding session disagree")


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """One runtime observation, optionally tied to a restorable checkpoint.

    ``checkpoint_id=None`` deliberately means evidence only. A runtime must not
    expose an event sequence or receipt identifier as a rewindable checkpoint.
    """

    execution_id: str
    checkpoint_id: str | None
    values: Mapping[str, Any]
    metadata: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class WorkflowRuntime(Protocol):
    """Minimal execution contract implemented by harness/framework adapters."""

    def supports_checkpoint_recovery(self) -> bool:
        """Return whether checkpoint IDs can be resumed by this runtime."""

    def supports_forking(self) -> bool:
        """Return whether checkpoints can seed another execution safely."""

    def shares_checkpoint_store(self, state_adapter: Any) -> bool:
        """Return whether runtime receipts are owned by this exact adapter."""

    def is_bound(self) -> bool:
        """Return whether an executable workflow definition is bound."""

    def bind(self, workflow: Any, *, force_rebind: bool = True) -> None:
        """Bind a framework-specific workflow definition."""

    def prepare_attempt(
        self,
        execution_id: str,
        attempt_id: str,
        *,
        input_sha256: str,
        checkpoint_id: str | None = None,
    ) -> RuntimeAttemptPlan:
        """Plan deterministic session identity without dispatching work."""

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
        """Invoke or resume one execution-scoped, durably identified attempt.

        ``attempt_metadata`` is the runtime-owned plan metadata that WTB
        committed in its pre-dispatch fence. Ephemeral admission pins belong
        here rather than in :class:`RuntimeBindingEnvelope`.
        """

    def snapshot(
        self,
        execution_id: str,
        checkpoint_id: str | None = None,
    ) -> RuntimeSnapshot | None:
        """Return an observation or restorable snapshot when supported."""

    def history(self, execution_id: str) -> Sequence[RuntimeSnapshot]:
        """Return runtime observations for one WTB execution."""

    def patch_checkpoint(
        self,
        execution_id: str,
        checkpoint_id: str,
        values: Mapping[str, Any],
        *,
        as_node: str | None = None,
    ) -> str:
        """Apply updates and return the checkpoint ID that must be resumed."""

    def reconcile(
        self,
        execution_id: str,
        attempt_id: str,
        *,
        binding: RuntimeBindingEnvelope,
        input_state: Mapping[str, Any] | None = None,
    ) -> RuntimeOutcomeReceipt | None:
        """Return or safely recover one durably identified dispatch attempt."""

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
        """Cancel one attempt using its fenced input, plan, and verified receipt."""
