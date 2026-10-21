"""Fail-closed authority checks at an external tool mutation boundary.

The operator deliberately does not infer authority from model text.  It binds
one raw mutation intent to a snapshot derived from WTB's persisted execution
aggregate, exact runtime-attempt identity, publication receipts, and the
workspace digest observed during planning.  A caller must re-compute the
workspace digest immediately before I/O and may execute only a ``PERMIT``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from wtb.domain.interfaces.workflow_runtime import RuntimeBindingEnvelope
from wtb.domain.models.workflow import Execution, ExecutionStatus
from wtb.domain.runtime_identity import canonical_json_sha256

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class AuthorityResolution(str, Enum):
    """Whether one snapshot names exactly one authoritative material."""

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class AuthorityResolutionReason(str, Enum):
    """Why WTB selected a material or failed closed."""

    GIT_ATTEMPT_COMPLETED = "git_attempt_completed"
    GIT_PUBLICATION_MISMATCH = "git_publication_mismatch"
    EXTERNAL_ATTEMPT_COMPLETED = "external_attempt_completed"
    KNOWN_NON_SUCCESS_FALLBACK = "known_non_success_fallback"
    RUNTIME_RECOVERY_REQUIRED = "runtime_recovery_required"
    RUNTIME_EVIDENCE_INVALID = "runtime_evidence_invalid"
    EXTERNAL_PUBLICATION_MISSING = "external_publication_missing"
    EXTERNAL_PUBLICATION_MISMATCH = "external_publication_mismatch"
    EXTERNAL_PUBLICATION_NOT_ACCEPTED_FALLBACK = (
        "external_publication_not_accepted_fallback"
    )


class MutationPermitDisposition(str, Enum):
    """Decision returned immediately before a mutation implementation."""

    PERMIT = "permit"
    DENY = "deny"


class MutationPermitReason(str, Enum):
    """Stable reason codes for operator receipts and experiment metrics."""

    AUTHORITY_AND_WORKSPACE_BOUND = "authority_and_workspace_bound"
    AUTHORITY_UNRESOLVED = "authority_unresolved"
    AUTHORITY_SNAPSHOT_CHANGED = "authority_snapshot_changed"
    TARGET_NOT_AUTHORIZED = "target_not_authorized"
    MATERIAL_NOT_AUTHORIZED = "material_not_authorized"
    WORKSPACE_DIGEST_CHANGED = "workspace_digest_changed"


def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _require_positive_integer(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _require_safe_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded opaque safe identifier")
    return value


def _require_safe_relative_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty relative path")
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if (
        normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized) is not None
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError(f"{field} must be a safe relative path")
    return normalized


@dataclass(frozen=True, slots=True)
class PublicationEvidence:
    """One self-hashed Git or external publication fact.

    A Git receipt proves material visible at an exact commit.  An external
    receipt instead binds material to the exact WTB runtime attempt, input,
    and durable memory snapshot from which it was published.
    """

    channel: str
    generation: int
    artifact_sha256: str
    contract_sha256: str
    git_visible: bool
    receipt_sha256: str
    commit: str | None = None
    attempt_id: str | None = None
    input_sha256: str | None = None
    memory_snapshot_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.channel not in {"git", "wtb-external"}:
            raise ValueError("publication channel must be git or wtb-external")
        _require_positive_integer(self.generation, "generation")
        _require_sha256(self.artifact_sha256, "artifact_sha256")
        _require_sha256(self.contract_sha256, "contract_sha256")
        _require_sha256(self.receipt_sha256, "receipt_sha256")
        if not isinstance(self.git_visible, bool):
            raise TypeError("git_visible must be a bool")
        if self.channel == "git":
            if not self.git_visible:
                raise ValueError("Git publication must be marked Git-visible")
            if not isinstance(self.commit, str) or _GIT_COMMIT.fullmatch(self.commit) is None:
                raise ValueError("Git publication must bind a lowercase 40-hex commit")
            attempt_binding = (
                self.attempt_id,
                self.input_sha256,
                self.memory_snapshot_sha256,
            )
            if any(value is not None for value in attempt_binding) and not all(
                value is not None for value in attempt_binding
            ):
                raise ValueError(
                    "Git publication attempt binding must be all-or-none"
                )
            if all(value is not None for value in attempt_binding):
                _require_safe_id(self.attempt_id, "attempt_id")
                _require_sha256(self.input_sha256, "input_sha256")
                _require_sha256(
                    self.memory_snapshot_sha256,
                    "memory_snapshot_sha256",
                )
        else:
            if self.git_visible or self.commit is not None:
                raise ValueError("External publication cannot claim Git visibility")
            _require_safe_id(self.attempt_id, "attempt_id")
            _require_sha256(self.input_sha256, "input_sha256")
            _require_sha256(
                self.memory_snapshot_sha256,
                "memory_snapshot_sha256",
            )

    def body(self) -> dict[str, object]:
        """Return the canonical receipt body, excluding its own digest."""

        body: dict[str, object] = {
            "schemaVersion": 1,
            "channel": self.channel,
            "generation": self.generation,
            "artifactSha256": self.artifact_sha256,
            "contractSha256": self.contract_sha256,
            "gitVisible": self.git_visible,
        }
        if self.channel == "git":
            body["commit"] = self.commit
            if self.attempt_id is not None:
                body.update(
                    {
                        "attemptId": self.attempt_id,
                        "inputSha256": self.input_sha256,
                        "memorySnapshotSha256": self.memory_snapshot_sha256,
                    }
                )
        else:
            body.update(
                {
                    "attemptId": self.attempt_id,
                    "inputSha256": self.input_sha256,
                    "memorySnapshotSha256": self.memory_snapshot_sha256,
                }
            )
        return body

    def to_mapping(self) -> dict[str, object]:
        """Serialize the exact self-hashed publication receipt."""

        return {**self.body(), "receiptSha256": self.receipt_sha256}

    @classmethod
    def from_mapping(cls, value: object) -> "PublicationEvidence":
        """Parse one strict publication receipt and verify its digest."""

        if not isinstance(value, Mapping):
            raise TypeError("publication evidence must be a mapping")
        channel = value.get("channel")
        common = {
            "schemaVersion",
            "channel",
            "generation",
            "artifactSha256",
            "contractSha256",
            "gitVisible",
            "receiptSha256",
        }
        if channel == "git":
            unbound_git = common | {"commit"}
            bound_git = unbound_git | {
                "attemptId",
                "inputSha256",
                "memorySnapshotSha256",
            }
            supplied_keys = frozenset(value)
            if supplied_keys not in {
                frozenset(unbound_git),
                frozenset(bound_git),
            }:
                if set(value) & {
                    "attemptId",
                    "inputSha256",
                    "memorySnapshotSha256",
                }:
                    raise ValueError(
                        "Git publication attempt binding must be all-or-none"
                    )
                raise ValueError("publication evidence has an invalid schema")
        else:
            expected = common | {
                "attemptId",
                "inputSha256",
                "memorySnapshotSha256",
            }
            if set(value) != expected:
                raise ValueError("publication evidence has an invalid schema")
        if value.get("schemaVersion") != 1:
            raise ValueError("publication evidence has an invalid schema")
        publication = cls(
            channel=str(channel),
            generation=value["generation"],
            artifact_sha256=value["artifactSha256"],
            contract_sha256=value["contractSha256"],
            git_visible=value["gitVisible"],
            receipt_sha256=value["receiptSha256"],
            commit=value.get("commit"),
            attempt_id=value.get("attemptId"),
            input_sha256=value.get("inputSha256"),
            memory_snapshot_sha256=value.get("memorySnapshotSha256"),
        )
        if canonical_json_sha256(publication.body()) != publication.receipt_sha256:
            raise ValueError("publication receipt digest does not match its body")
        return publication


@dataclass(frozen=True, slots=True)
class NonPublicationEvidence:
    """Self-hashed proof that one exact external publication was not accepted."""

    channel: str
    generation: int
    outcome: str
    attempt_id: str
    input_sha256: str
    memory_snapshot_sha256: str
    request_sha256: str
    response_sha256: str
    receipt_sha256: str

    def __post_init__(self) -> None:
        if self.channel != "wtb-external":
            raise ValueError("non-publication evidence must use the WTB channel")
        _require_positive_integer(self.generation, "generation")
        if self.outcome != "not_published":
            raise ValueError("non-publication outcome must be not_published")
        _require_safe_id(self.attempt_id, "attempt_id")
        _require_sha256(self.input_sha256, "input_sha256")
        _require_sha256(
            self.memory_snapshot_sha256,
            "memory_snapshot_sha256",
        )
        _require_sha256(self.request_sha256, "request_sha256")
        _require_sha256(self.response_sha256, "response_sha256")
        _require_sha256(self.receipt_sha256, "receipt_sha256")

    def body(self) -> dict[str, object]:
        """Return the exact negative-publication receipt body."""

        return {
            "schemaVersion": 1,
            "channel": self.channel,
            "generation": self.generation,
            "outcome": self.outcome,
            "attemptId": self.attempt_id,
            "inputSha256": self.input_sha256,
            "memorySnapshotSha256": self.memory_snapshot_sha256,
            "requestSha256": self.request_sha256,
            "responseSha256": self.response_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        """Serialize the complete self-hashed negative receipt."""

        return {**self.body(), "receiptSha256": self.receipt_sha256}

    @classmethod
    def from_mapping(cls, value: object) -> "NonPublicationEvidence":
        """Parse one strict negative receipt and verify its digest."""

        if not isinstance(value, Mapping):
            raise TypeError("non-publication evidence must be a mapping")
        expected = {
            "schemaVersion",
            "channel",
            "generation",
            "outcome",
            "attemptId",
            "inputSha256",
            "memorySnapshotSha256",
            "requestSha256",
            "responseSha256",
            "receiptSha256",
        }
        if set(value) != expected or value.get("schemaVersion") != 1:
            raise ValueError("non-publication evidence has an invalid schema")
        evidence = cls(
            channel=value["channel"],
            generation=value["generation"],
            outcome=value["outcome"],
            attempt_id=value["attemptId"],
            input_sha256=value["inputSha256"],
            memory_snapshot_sha256=value["memorySnapshotSha256"],
            request_sha256=value["requestSha256"],
            response_sha256=value["responseSha256"],
            receipt_sha256=value["receiptSha256"],
        )
        if canonical_json_sha256(evidence.body()) != evidence.receipt_sha256:
            raise ValueError(
                "non-publication receipt digest does not match its body"
            )
        return evidence


@dataclass(frozen=True, slots=True)
class AuthorityEvidence:
    """Raw evidence WTB uses to derive one immutable authority snapshot."""

    evidence_generation: int
    observed_workspace_digest: str
    memory_snapshot_sha256: str
    target_path: str
    git_publication: PublicationEvidence
    external_publication: PublicationEvidence | None = None
    external_non_publication: NonPublicationEvidence | None = None

    def __post_init__(self) -> None:
        _require_positive_integer(self.evidence_generation, "evidence_generation")
        _require_sha256(
            self.observed_workspace_digest,
            "observed_workspace_digest",
        )
        _require_sha256(self.memory_snapshot_sha256, "memory_snapshot_sha256")
        _require_safe_relative_path(self.target_path, "target_path")
        if not isinstance(self.git_publication, PublicationEvidence):
            raise TypeError("git_publication must be PublicationEvidence")
        if self.git_publication.channel != "git":
            raise ValueError("git_publication must use the Git channel")
        if self.external_publication is not None:
            if not isinstance(self.external_publication, PublicationEvidence):
                raise TypeError("external_publication must be PublicationEvidence")
            if self.external_publication.channel != "wtb-external":
                raise ValueError("external_publication must use the WTB channel")
        if self.external_non_publication is not None:
            if not isinstance(
                self.external_non_publication,
                NonPublicationEvidence,
            ):
                raise TypeError(
                    "external_non_publication must be NonPublicationEvidence"
                )
        if (
            self.external_publication is not None
            and self.external_non_publication is not None
        ):
            raise ValueError(
                "positive and negative external publication evidence conflict"
            )
        greatest_generation = max(
            self.git_publication.generation,
            0
            if self.external_publication is None
            else self.external_publication.generation,
            0
            if self.external_non_publication is None
            else self.external_non_publication.generation,
        )
        if self.evidence_generation < greatest_generation:
            raise ValueError("evidence_generation cannot precede publication evidence")

    def body(self) -> dict[str, object]:
        """Return canonical evidence for snapshot and audit digests."""

        return {
            "schemaVersion": 1,
            "evidenceGeneration": self.evidence_generation,
            "observedWorkspaceDigest": self.observed_workspace_digest,
            "memorySnapshotSha256": self.memory_snapshot_sha256,
            "targetPath": self.target_path,
            "gitPublication": self.git_publication.to_mapping(),
            "externalPublication": (
                None
                if self.external_publication is None
                else self.external_publication.to_mapping()
            ),
            "externalNonPublication": (
                None
                if self.external_non_publication is None
                else self.external_non_publication.to_mapping()
            ),
        }

    @property
    def evidence_sha256(self) -> str:
        """Digest of all authority inputs before resolution."""

        return canonical_json_sha256(self.body())

    @classmethod
    def from_mapping(cls, value: object) -> "AuthorityEvidence":
        """Load a strict JSON authority-evidence document."""

        if not isinstance(value, Mapping):
            raise TypeError("authority evidence must be a mapping")
        expected = {
            "schemaVersion",
            "evidenceGeneration",
            "observedWorkspaceDigest",
            "memorySnapshotSha256",
            "targetPath",
            "gitPublication",
            "externalPublication",
            "externalNonPublication",
        }
        if set(value) != expected or value.get("schemaVersion") != 1:
            raise ValueError("authority evidence has an invalid schema")
        external = value["externalPublication"]
        non_publication = value["externalNonPublication"]
        return cls(
            evidence_generation=value["evidenceGeneration"],
            observed_workspace_digest=value["observedWorkspaceDigest"],
            memory_snapshot_sha256=value["memorySnapshotSha256"],
            target_path=value["targetPath"],
            git_publication=PublicationEvidence.from_mapping(value["gitPublication"]),
            external_publication=(
                None if external is None else PublicationEvidence.from_mapping(external)
            ),
            external_non_publication=(
                None
                if non_publication is None
                else NonPublicationEvidence.from_mapping(non_publication)
            ),
        )


@dataclass(frozen=True, slots=True)
class AuthoritySnapshot:
    """WTB-owned resolved authority state consumed at the mutation boundary."""

    execution_id: str
    resolution: AuthorityResolution
    resolution_reason: AuthorityResolutionReason
    evidence_generation: int
    evidence_sha256: str
    observed_workspace_digest: str
    memory_snapshot_sha256: str
    target_path: str
    attempt_id: str | None
    attempt_outcome: str | None
    authoritative_generation: int | None
    authoritative_material_sha256: str | None
    authoritative_contract_sha256: str | None
    external_recovery_required: bool

    def __post_init__(self) -> None:
        _require_safe_id(self.execution_id, "execution_id")
        if not isinstance(self.resolution, AuthorityResolution):
            raise TypeError("resolution must be AuthorityResolution")
        if not isinstance(self.resolution_reason, AuthorityResolutionReason):
            raise TypeError("resolution_reason must be AuthorityResolutionReason")
        _require_positive_integer(self.evidence_generation, "evidence_generation")
        _require_sha256(self.evidence_sha256, "evidence_sha256")
        _require_sha256(
            self.observed_workspace_digest,
            "observed_workspace_digest",
        )
        _require_sha256(self.memory_snapshot_sha256, "memory_snapshot_sha256")
        _require_safe_relative_path(self.target_path, "target_path")
        if self.attempt_id is not None:
            _require_safe_id(self.attempt_id, "attempt_id")
        if not isinstance(self.external_recovery_required, bool):
            raise TypeError("external_recovery_required must be a bool")
        authoritative = (
            self.authoritative_generation,
            self.authoritative_material_sha256,
            self.authoritative_contract_sha256,
        )
        if self.resolution is AuthorityResolution.RESOLVED:
            _require_positive_integer(
                self.authoritative_generation,
                "authoritative_generation",
            )
            _require_sha256(
                self.authoritative_material_sha256,
                "authoritative_material_sha256",
            )
            _require_sha256(
                self.authoritative_contract_sha256,
                "authoritative_contract_sha256",
            )
            if self.external_recovery_required:
                raise ValueError("resolved authority cannot require external recovery")
        elif any(value is not None for value in authoritative):
            raise ValueError("unresolved authority cannot name material")

    def body(self) -> dict[str, object]:
        """Return the canonical snapshot body excluding its derived digest."""

        return {
            "schemaVersion": 1,
            "executionId": self.execution_id,
            "resolution": self.resolution.value,
            "resolutionReason": self.resolution_reason.value,
            "evidenceGeneration": self.evidence_generation,
            "evidenceSha256": self.evidence_sha256,
            "observedWorkspaceDigest": self.observed_workspace_digest,
            "memorySnapshotSha256": self.memory_snapshot_sha256,
            "targetPath": self.target_path,
            "attemptId": self.attempt_id,
            "attemptOutcome": self.attempt_outcome,
            "authoritativeGeneration": self.authoritative_generation,
            "authoritativeMaterialSha256": self.authoritative_material_sha256,
            "authoritativeContractSha256": self.authoritative_contract_sha256,
            "externalRecoveryRequired": self.external_recovery_required,
        }

    @property
    def snapshot_sha256(self) -> str:
        """Immutable identity carried from planning to the pre-I/O check."""

        return canonical_json_sha256(self.body())

    def to_mapping(self) -> dict[str, object]:
        """Serialize one self-hashed snapshot for a process boundary."""

        return {**self.body(), "snapshotSha256": self.snapshot_sha256}

    @classmethod
    def from_mapping(cls, value: object) -> "AuthoritySnapshot":
        """Load a strict snapshot and reject any body/digest mismatch."""

        if not isinstance(value, Mapping):
            raise TypeError("authority snapshot must be a mapping")
        expected = {
            "schemaVersion",
            "executionId",
            "resolution",
            "resolutionReason",
            "evidenceGeneration",
            "evidenceSha256",
            "observedWorkspaceDigest",
            "memorySnapshotSha256",
            "targetPath",
            "attemptId",
            "attemptOutcome",
            "authoritativeGeneration",
            "authoritativeMaterialSha256",
            "authoritativeContractSha256",
            "externalRecoveryRequired",
            "snapshotSha256",
        }
        if set(value) != expected or value.get("schemaVersion") != 1:
            raise ValueError("authority snapshot has an invalid schema")
        snapshot = cls(
            execution_id=value["executionId"],
            resolution=AuthorityResolution(value["resolution"]),
            resolution_reason=AuthorityResolutionReason(value["resolutionReason"]),
            evidence_generation=value["evidenceGeneration"],
            evidence_sha256=value["evidenceSha256"],
            observed_workspace_digest=value["observedWorkspaceDigest"],
            memory_snapshot_sha256=value["memorySnapshotSha256"],
            target_path=value["targetPath"],
            attempt_id=value["attemptId"],
            attempt_outcome=value["attemptOutcome"],
            authoritative_generation=value["authoritativeGeneration"],
            authoritative_material_sha256=value["authoritativeMaterialSha256"],
            authoritative_contract_sha256=value["authoritativeContractSha256"],
            external_recovery_required=value["externalRecoveryRequired"],
        )
        supplied = _require_sha256(value["snapshotSha256"], "snapshotSha256")
        if snapshot.snapshot_sha256 != supplied:
            raise ValueError("authority snapshot digest does not match its body")
        return snapshot


@dataclass(frozen=True, slots=True)
class ToolMutationIntent:
    """A raw DSH tool call projected into WTB's immutable mutation contract."""

    tool_name: str
    arguments_sha256: str
    action: str
    target_path: str
    selected_material_sha256: str
    authority_snapshot_sha256: str

    def __post_init__(self) -> None:
        _require_safe_id(self.tool_name, "tool_name")
        _require_sha256(self.arguments_sha256, "arguments_sha256")
        _require_safe_id(self.action, "action")
        _require_safe_relative_path(self.target_path, "target_path")
        _require_sha256(
            self.selected_material_sha256,
            "selected_material_sha256",
        )
        _require_sha256(
            self.authority_snapshot_sha256,
            "authority_snapshot_sha256",
        )

    @classmethod
    def from_tool_call(
        cls,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        action: str,
        authority_snapshot_sha256: str,
    ) -> "ToolMutationIntent":
        """Bind an exact DSH ``write`` call without trusting model-supplied hashes."""

        if tool_name != "write":
            raise ValueError("native operator slice supports only the DSH write tool")
        if not isinstance(arguments, Mapping):
            raise TypeError("tool arguments must be a JSON object")
        if set(arguments) != {"file_path", "content"}:
            raise ValueError("write intent must contain exactly file_path and content")
        target_path = _require_safe_relative_path(arguments["file_path"], "file_path")
        content = arguments["content"]
        if not isinstance(content, str):
            raise TypeError("write content must be a string")
        return cls(
            tool_name=tool_name,
            arguments_sha256=canonical_json_sha256(arguments),
            action=action,
            target_path=target_path,
            selected_material_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            authority_snapshot_sha256=authority_snapshot_sha256,
        )

    def body(self) -> dict[str, object]:
        """Return the exact intent identity used by a permit receipt."""

        return {
            "schemaVersion": 1,
            "toolName": self.tool_name,
            "argumentsSha256": self.arguments_sha256,
            "action": self.action,
            "targetPath": self.target_path,
            "selectedMaterialSha256": self.selected_material_sha256,
            "authoritySnapshotSha256": self.authority_snapshot_sha256,
        }

    @property
    def intent_sha256(self) -> str:
        """Digest binding one raw call to one authority snapshot."""

        return canonical_json_sha256(self.body())


@dataclass(frozen=True, slots=True)
class MutationPermitReceipt:
    """Auditable WTB decision consumed by a thin runtime bridge."""

    disposition: MutationPermitDisposition
    reason: MutationPermitReason
    execution_id: str
    evidence_generation: int
    evidence_sha256: str
    authority_snapshot_sha256: str
    intent_sha256: str
    tool_name: str
    arguments_sha256: str
    action: str
    target_path: str
    selected_material_sha256: str
    authoritative_generation: int | None
    observed_workspace_digest: str
    current_workspace_digest: str
    external_recovery_required: bool
    replan_required: bool

    @property
    def recovery_required(self) -> bool:
        """Aggregate recovery flag retained for WTB lifecycle compatibility."""

        return self.external_recovery_required or self.replan_required

    def body(self) -> dict[str, object]:
        """Return the complete decision body excluding its own digest."""

        return {
            "schemaVersion": 1,
            "disposition": self.disposition.value,
            "reason": self.reason.value,
            "executionId": self.execution_id,
            "evidenceGeneration": self.evidence_generation,
            "evidenceSha256": self.evidence_sha256,
            "authoritySnapshotSha256": self.authority_snapshot_sha256,
            "intentSha256": self.intent_sha256,
            "toolName": self.tool_name,
            "argumentsSha256": self.arguments_sha256,
            "action": self.action,
            "targetPath": self.target_path,
            "selectedMaterialSha256": self.selected_material_sha256,
            "authoritativeGeneration": self.authoritative_generation,
            "observedWorkspaceDigest": self.observed_workspace_digest,
            "currentWorkspaceDigest": self.current_workspace_digest,
            "externalRecoveryRequired": self.external_recovery_required,
            "replanRequired": self.replan_required,
            "recoveryRequired": self.recovery_required,
        }

    @property
    def receipt_sha256(self) -> str:
        """Self-hash used by the DSH bridge and independent audit."""

        return canonical_json_sha256(self.body())

    def to_mapping(self) -> dict[str, object]:
        """Serialize one complete, self-hashed permit/deny receipt."""

        return {**self.body(), "receiptSha256": self.receipt_sha256}


class ActionAuthorityOperator:
    """WTB owner of authority resolution and immediate pre-I/O decisions."""

    @classmethod
    def snapshot_from_execution(
        cls,
        execution: Execution,
        evidence: AuthorityEvidence,
    ) -> AuthoritySnapshot:
        """Resolve authority from persisted WTB lifecycle and raw evidence.

        ``RUNNING + recovery_required`` and malformed attempt identities are
        unresolved.  A verified completed attempt selects its external
        publication; a verified failed/cancelled attempt selects the exact
        Git-visible fallback.
        """

        if not isinstance(execution, Execution):
            raise TypeError("execution must be a WTB Execution aggregate")
        if not isinstance(evidence, AuthorityEvidence):
            raise TypeError("evidence must be AuthorityEvidence")
        metadata = execution.metadata
        if not isinstance(metadata, Mapping):
            return cls._unresolved(
                execution,
                evidence,
                AuthorityResolutionReason.RUNTIME_EVIDENCE_INVALID,
            )
        if (
            metadata.get("recovery_required") is True
            or isinstance(metadata.get("runtime_attempt"), Mapping)
            or execution.status in {
                ExecutionStatus.PENDING,
                ExecutionStatus.RUNNING,
                ExecutionStatus.PAUSED,
            }
        ):
            return cls._unresolved(
                execution,
                evidence,
                AuthorityResolutionReason.RUNTIME_RECOVERY_REQUIRED,
                external_recovery_required=True,
            )

        terminal = metadata.get("last_runtime_attempt")
        identity = cls._terminal_identity(execution, terminal)
        if identity is None:
            return cls._unresolved(
                execution,
                evidence,
                AuthorityResolutionReason.RUNTIME_EVIDENCE_INVALID,
            )
        attempt_id, input_sha256, outcome = identity
        if outcome == "completed":
            publication = evidence.external_publication
            non_publication = evidence.external_non_publication
            if publication is None and non_publication is None:
                git_publication = evidence.git_publication
                git_binding = (
                    git_publication.attempt_id,
                    git_publication.input_sha256,
                    git_publication.memory_snapshot_sha256,
                )
                if all(value is not None for value in git_binding):
                    if (
                        git_publication.attempt_id != attempt_id
                        or git_publication.input_sha256 != input_sha256
                        or git_publication.memory_snapshot_sha256
                        != evidence.memory_snapshot_sha256
                    ):
                        return cls._unresolved(
                            execution,
                            evidence,
                            AuthorityResolutionReason.GIT_PUBLICATION_MISMATCH,
                            attempt_id=attempt_id,
                            attempt_outcome=outcome,
                        )
                    return cls._resolved(
                        execution,
                        evidence,
                        git_publication,
                        AuthorityResolutionReason.GIT_ATTEMPT_COMPLETED,
                        attempt_id=attempt_id,
                        attempt_outcome=outcome,
                    )
                return cls._unresolved(
                    execution,
                    evidence,
                    AuthorityResolutionReason.EXTERNAL_PUBLICATION_MISSING,
                    attempt_id=attempt_id,
                    attempt_outcome=outcome,
                )
            if publication is not None:
                if (
                    publication.attempt_id != attempt_id
                    or publication.input_sha256 != input_sha256
                    or publication.memory_snapshot_sha256
                    != evidence.memory_snapshot_sha256
                    or publication.generation <= evidence.git_publication.generation
                ):
                    return cls._unresolved(
                        execution,
                        evidence,
                        AuthorityResolutionReason.EXTERNAL_PUBLICATION_MISMATCH,
                        attempt_id=attempt_id,
                        attempt_outcome=outcome,
                    )
                return cls._resolved(
                    execution,
                    evidence,
                    publication,
                    AuthorityResolutionReason.EXTERNAL_ATTEMPT_COMPLETED,
                    attempt_id=attempt_id,
                    attempt_outcome=outcome,
                )
            assert non_publication is not None
            if (
                non_publication.attempt_id != attempt_id
                or non_publication.input_sha256 != input_sha256
                or non_publication.memory_snapshot_sha256
                != evidence.memory_snapshot_sha256
                or non_publication.generation <= evidence.git_publication.generation
            ):
                return cls._unresolved(
                    execution,
                    evidence,
                    AuthorityResolutionReason.EXTERNAL_PUBLICATION_MISMATCH,
                    attempt_id=attempt_id,
                    attempt_outcome=outcome,
                )
            return cls._resolved(
                execution,
                evidence,
                evidence.git_publication,
                AuthorityResolutionReason.EXTERNAL_PUBLICATION_NOT_ACCEPTED_FALLBACK,
                attempt_id=attempt_id,
                attempt_outcome=outcome,
            )

        if outcome in {"failed", "cancelled"}:
            if (
                evidence.external_publication is not None
                or evidence.external_non_publication is not None
            ):
                return cls._unresolved(
                    execution,
                    evidence,
                    AuthorityResolutionReason.EXTERNAL_PUBLICATION_MISMATCH,
                    attempt_id=attempt_id,
                    attempt_outcome=outcome,
                )
            return cls._resolved(
                execution,
                evidence,
                evidence.git_publication,
                AuthorityResolutionReason.KNOWN_NON_SUCCESS_FALLBACK,
                attempt_id=attempt_id,
                attempt_outcome=outcome,
            )
        return cls._unresolved(
            execution,
            evidence,
            AuthorityResolutionReason.RUNTIME_EVIDENCE_INVALID,
            attempt_id=attempt_id,
            attempt_outcome=outcome,
        )

    @staticmethod
    def authorize(
        snapshot: AuthoritySnapshot,
        intent: ToolMutationIntent,
        *,
        current_workspace_digest: str,
    ) -> MutationPermitReceipt:
        """Revalidate snapshot, target, material, and workspace before I/O."""

        if not isinstance(snapshot, AuthoritySnapshot):
            raise TypeError("snapshot must be AuthoritySnapshot")
        if not isinstance(intent, ToolMutationIntent):
            raise TypeError("intent must be ToolMutationIntent")
        current_workspace_digest = _require_sha256(
            current_workspace_digest,
            "current_workspace_digest",
        )

        def decision(
            disposition: MutationPermitDisposition,
            reason: MutationPermitReason,
            *,
            replan_required: bool,
        ) -> MutationPermitReceipt:
            return MutationPermitReceipt(
                disposition=disposition,
                reason=reason,
                execution_id=snapshot.execution_id,
                evidence_generation=snapshot.evidence_generation,
                evidence_sha256=snapshot.evidence_sha256,
                authority_snapshot_sha256=snapshot.snapshot_sha256,
                intent_sha256=intent.intent_sha256,
                tool_name=intent.tool_name,
                arguments_sha256=intent.arguments_sha256,
                action=intent.action,
                target_path=intent.target_path,
                selected_material_sha256=intent.selected_material_sha256,
                authoritative_generation=snapshot.authoritative_generation,
                observed_workspace_digest=snapshot.observed_workspace_digest,
                current_workspace_digest=current_workspace_digest,
                external_recovery_required=snapshot.external_recovery_required,
                replan_required=replan_required,
            )

        if snapshot.resolution is AuthorityResolution.UNRESOLVED:
            return decision(
                MutationPermitDisposition.DENY,
                MutationPermitReason.AUTHORITY_UNRESOLVED,
                replan_required=True,
            )
        if intent.authority_snapshot_sha256 != snapshot.snapshot_sha256:
            return decision(
                MutationPermitDisposition.DENY,
                MutationPermitReason.AUTHORITY_SNAPSHOT_CHANGED,
                replan_required=True,
            )
        if intent.target_path != snapshot.target_path:
            return decision(
                MutationPermitDisposition.DENY,
                MutationPermitReason.TARGET_NOT_AUTHORIZED,
                replan_required=True,
            )
        if intent.selected_material_sha256 != snapshot.authoritative_material_sha256:
            return decision(
                MutationPermitDisposition.DENY,
                MutationPermitReason.MATERIAL_NOT_AUTHORIZED,
                replan_required=True,
            )
        if current_workspace_digest != snapshot.observed_workspace_digest:
            return decision(
                MutationPermitDisposition.DENY,
                MutationPermitReason.WORKSPACE_DIGEST_CHANGED,
                replan_required=True,
            )
        return decision(
            MutationPermitDisposition.PERMIT,
            MutationPermitReason.AUTHORITY_AND_WORKSPACE_BOUND,
            replan_required=False,
        )

    @staticmethod
    def _terminal_identity(
        execution: Execution,
        terminal: object,
    ) -> tuple[str, str, str] | None:
        if not isinstance(terminal, Mapping):
            return None
        try:
            binding = RuntimeBindingEnvelope.from_mapping(terminal.get("binding"))
        except (TypeError, ValueError):
            return None
        attempt_id = terminal.get("attempt_id")
        input_sha256 = terminal.get("input_sha256")
        session_id = terminal.get("session_id")
        outcome = terminal.get("outcome")
        if (
            attempt_id != binding.attempt_id
            or input_sha256 != binding.input_sha256
            or session_id != binding.exact_session_id
            or outcome not in {"completed", "failed", "cancelled"}
        ):
            return None
        expected_status = {
            "completed": ExecutionStatus.COMPLETED,
            "failed": ExecutionStatus.FAILED,
            "cancelled": ExecutionStatus.CANCELLED,
        }[outcome]
        if execution.status is not expected_status:
            return None
        return binding.attempt_id, binding.input_sha256, outcome

    @staticmethod
    def _resolved(
        execution: Execution,
        evidence: AuthorityEvidence,
        publication: PublicationEvidence,
        reason: AuthorityResolutionReason,
        *,
        attempt_id: str,
        attempt_outcome: str,
    ) -> AuthoritySnapshot:
        return AuthoritySnapshot(
            execution_id=execution.id,
            resolution=AuthorityResolution.RESOLVED,
            resolution_reason=reason,
            evidence_generation=evidence.evidence_generation,
            evidence_sha256=evidence.evidence_sha256,
            observed_workspace_digest=evidence.observed_workspace_digest,
            memory_snapshot_sha256=evidence.memory_snapshot_sha256,
            target_path=evidence.target_path,
            attempt_id=attempt_id,
            attempt_outcome=attempt_outcome,
            authoritative_generation=publication.generation,
            authoritative_material_sha256=publication.artifact_sha256,
            authoritative_contract_sha256=publication.contract_sha256,
            external_recovery_required=False,
        )

    @staticmethod
    def _unresolved(
        execution: Execution,
        evidence: AuthorityEvidence,
        reason: AuthorityResolutionReason,
        *,
        attempt_id: str | None = None,
        attempt_outcome: str | None = None,
        external_recovery_required: bool = False,
    ) -> AuthoritySnapshot:
        return AuthoritySnapshot(
            execution_id=execution.id,
            resolution=AuthorityResolution.UNRESOLVED,
            resolution_reason=reason,
            evidence_generation=evidence.evidence_generation,
            evidence_sha256=evidence.evidence_sha256,
            observed_workspace_digest=evidence.observed_workspace_digest,
            memory_snapshot_sha256=evidence.memory_snapshot_sha256,
            target_path=evidence.target_path,
            attempt_id=attempt_id,
            attempt_outcome=attempt_outcome,
            authoritative_generation=None,
            authoritative_material_sha256=None,
            authoritative_contract_sha256=None,
            external_recovery_required=external_recovery_required,
        )


__all__ = [
    "ActionAuthorityOperator",
    "AuthorityEvidence",
    "AuthorityResolution",
    "AuthorityResolutionReason",
    "AuthoritySnapshot",
    "MutationPermitDisposition",
    "MutationPermitReason",
    "MutationPermitReceipt",
    "NonPublicationEvidence",
    "PublicationEvidence",
    "ToolMutationIntent",
]
