"""Immutable execution facts; authority remains a separate, mutable concern."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from wtb.domain.interfaces.workflow_runtime import RuntimeBindingEnvelope
from wtb.domain.publication_authority import _require_safe_id
from wtb.domain.runtime_identity import _normalize_json, canonical_json_sha256


class EvidenceError(RuntimeError):
    """Stable failure code with a bounded recovery action, never a permission."""

    def __init__(self, code: str, recovery: str = "query_runtime") -> None:
        self.code = code
        self.recovery = recovery
        super().__init__(code)


def encode(value: Mapping[str, Any]) -> str:
    # Reuse the core strict JSON contract (reject non-string keys/NaN/cycles).
    if not isinstance(value, Mapping):
        raise TypeError("evidence must be a JSON object")
    normalized = _normalize_json(value, path="$", active=set())
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


@dataclass(frozen=True)
class AttemptIdentity:
    execution_id: str
    runtime_id: str
    scope_id: str
    binding: RuntimeBindingEnvelope

    def __post_init__(self) -> None:
        for name in ("execution_id", "runtime_id", "scope_id"):
            _require_safe_id(getattr(self, name), name)
        if not isinstance(self.binding, RuntimeBindingEnvelope):
            raise TypeError("binding must be RuntimeBindingEnvelope")

    def to_mapping(self) -> dict:
        return dict(
            execution_id=self.execution_id,
            runtime_id=self.runtime_id,
            scope_id=self.scope_id,
            binding=self.binding.to_dict(),
        )

    @classmethod
    def from_mapping(cls, value: Mapping) -> AttemptIdentity:
        return cls(
            value["execution_id"],
            value["runtime_id"],
            value["scope_id"],
            RuntimeBindingEnvelope.from_mapping(value["binding"]),
        )


@dataclass(frozen=True, init=False)
class EvidenceEvent:
    event_id: str
    kind: str
    body_json: str

    def __init__(self, event_id: str, kind: str, body: Mapping[str, Any]) -> None:
        _require_safe_id(event_id, "event_id")
        if kind not in {
            "created",
            "running",
            "completed",
            "failed",
            "cancelled",
            "unknown",
        }:
            raise ValueError("invalid evidence event kind")
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "body_json", encode(body))

    @property
    def body(self) -> dict:
        return json.loads(self.body_json)

    @property
    def digest(self) -> str:
        return canonical_json_sha256(
            dict(id=self.event_id, kind=self.kind, body=self.body)
        )


@dataclass(frozen=True)
class AttemptEvidence:
    identity: AttemptIdentity
    state: str
    outcome: str | None
    terminal: EvidenceEvent | None
    revision: str


class EvidenceStore(Protocol):
    def register(
        self, identity: AttemptIdentity, *, expected_attempt: str | None
    ) -> None: ...
    def append(
        self, execution_id: str, attempt_id: str, event: EvidenceEvent
    ) -> None: ...
    def get(self, execution_id: str, attempt_id: str) -> AttemptEvidence: ...
    def get_current(self, execution_id: str, attempt_id: str) -> AttemptEvidence: ...
    def current_attempt(self, execution_id: str) -> str | None: ...
