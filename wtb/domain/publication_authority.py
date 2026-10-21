"""WTB authority fence for publishing a current shared workspace.

This module is intentionally independent of any benchmark oracle.  It validates
actor/session/attempt/scope mutation evidence against terminal WTB executions,
binds the resulting authority snapshot to one publication intent, and rechecks
the current workspace digest immediately before a caller may publish.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from wtb.domain.interfaces.workflow_runtime import RuntimeBindingEnvelope
from wtb.domain.models.workflow import Execution, ExecutionStatus
from wtb.domain.runtime_identity import canonical_json_sha256

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
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


class PublicationAction(str, Enum):
    """Raw publication-coordinator actions accepted by the native fence."""

    PUBLISH_CURRENT = "PUBLISH_CURRENT"
    BLOCK_AND_RECONCILE = "BLOCK_AND_RECONCILE"


class PublicationAuthorityResolution(str, Enum):
    """Whether the persisted mutation chain authorizes publication."""

    AUTHORIZED = "authorized"
    BLOCKED = "blocked"
    UNRESOLVED = "unresolved"


class PublicationDisposition(str, Enum):
    """Only a permit authorizes an external publication effect."""

    PERMIT_PUBLICATION = "permit_publication"
    DENY_PUBLICATION = "deny_publication"


class PublicationReason(str, Enum):
    """Stable audit reason codes for snapshot and decision receipts."""

    SCOPE_BINDINGS_AUTHORIZED = "scope_bindings_authorized"
    AUTHORITY_AND_WORKSPACE_BOUND = "authority_and_workspace_bound"
    AUTHORITY_SCOPE_MISMATCH = "authority_scope_mismatch"
    ATTEMPT_UNRESOLVED = "attempt_unresolved"
    AUTHORITY_SNAPSHOT_CHANGED = "authority_snapshot_changed"
    MATERIAL_NOT_AUTHORIZED = "material_not_authorized"
    WORKSPACE_DIGEST_CHANGED = "workspace_digest_changed"
    RECONCILIATION_REQUESTED = "reconciliation_requested"
    AUTHORIZED_MATERIAL_BLOCKED = "authorized_material_blocked"
    INTENT_MISSING = "intent_missing"


@dataclass(frozen=True, slots=True)
class ScopePolicyBinding:
    """One scope-to-session authorization in the publication policy."""

    scope_id: str
    authorized_session_id: str

    def __post_init__(self) -> None:
        _require_safe_id(self.scope_id, "scope_id")
        _require_safe_id(self.authorized_session_id, "authorized_session_id")

    def to_mapping(self) -> dict[str, object]:
        return {
            "scopeId": self.scope_id,
            "authorizedSessionId": self.authorized_session_id,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "ScopePolicyBinding":
        if not isinstance(value, Mapping) or set(value) != {
            "scopeId",
            "authorizedSessionId",
        }:
            raise ValueError("scope policy binding has an invalid schema")
        return cls(
            scope_id=value["scopeId"],
            authorized_session_id=value["authorizedSessionId"],
        )


@dataclass(frozen=True, slots=True)
class PathMaterial:
    """Current material identity of one target path in a multi-path scope."""

    path: str
    material_sha256: str

    def __post_init__(self) -> None:
        _require_safe_relative_path(self.path, "path")
        _require_sha256(self.material_sha256, "material_sha256")

    def to_mapping(self) -> dict[str, object]:
        return {"path": self.path, "materialSha256": self.material_sha256}

    @classmethod
    def from_mapping(cls, value: object) -> "PathMaterial":
        if not isinstance(value, Mapping) or set(value) != {"path", "materialSha256"}:
            raise ValueError("path material has an invalid schema")
        return cls(path=value["path"], material_sha256=value["materialSha256"])


def derive_scope_material_sha256(materials: Sequence[PathMaterial]) -> str:
    """Scope-level material identity: canonical JSON ``{path: materialSha256}``.

    Every target path contributes exactly once; the digest therefore changes
    whenever any path's material changes.
    """

    if not materials or not all(isinstance(row, PathMaterial) for row in materials):
        raise TypeError("scope material requires PathMaterial rows")
    paths = [row.path for row in materials]
    if len(set(paths)) != len(paths):
        raise ValueError("scope material paths must be unique")
    return canonical_json_sha256({row.path: row.material_sha256 for row in materials})


def _path_materials_from(value: object, field: str) -> tuple[PathMaterial, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TypeError(f"{field} must be a list")
    return tuple(PathMaterial.from_mapping(row) for row in value)


def _target_paths_from(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TypeError(f"{field} must be a list")
    return tuple(_require_safe_relative_path(row, field) for row in value)


@dataclass(frozen=True, slots=True)
class ScopeMutationEvidence:
    """One self-hashed mutation bound to a terminal WTB runtime attempt."""

    scope_id: str
    actor_session_id: str
    execution_id: str
    attempt_id: str
    input_sha256: str
    source_receipt_sha256: str
    logical_mutation_id: str
    mutation_sequence: int
    target_path: str
    patch_sha256: str
    before_material_sha256: str
    after_material_sha256: str
    pre_workspace_digest: str
    post_workspace_digest: str

    def __post_init__(self) -> None:
        _require_safe_id(self.scope_id, "scope_id")
        _require_safe_id(self.actor_session_id, "actor_session_id")
        _require_safe_id(self.execution_id, "execution_id")
        _require_safe_id(self.attempt_id, "attempt_id")
        _require_sha256(self.input_sha256, "input_sha256")
        _require_sha256(self.source_receipt_sha256, "source_receipt_sha256")
        _require_safe_id(self.logical_mutation_id, "logical_mutation_id")
        if (
            not isinstance(self.mutation_sequence, int)
            or isinstance(self.mutation_sequence, bool)
            or self.mutation_sequence <= 0
        ):
            raise ValueError("mutation_sequence must be a positive integer")
        _require_safe_relative_path(self.target_path, "target_path")
        for field in (
            "patch_sha256",
            "before_material_sha256",
            "after_material_sha256",
            "pre_workspace_digest",
            "post_workspace_digest",
        ):
            _require_sha256(getattr(self, field), field)

    def body(self) -> dict[str, object]:
        return {
            "schemaVersion": 2,
            "scopeId": self.scope_id,
            "actorSessionId": self.actor_session_id,
            "executionId": self.execution_id,
            "attemptId": self.attempt_id,
            "inputSha256": self.input_sha256,
            "sourceReceiptSha256": self.source_receipt_sha256,
            "logicalMutationId": self.logical_mutation_id,
            "mutationSequence": self.mutation_sequence,
            "targetPath": self.target_path,
            "patchSha256": self.patch_sha256,
            "beforeMaterialSha256": self.before_material_sha256,
            "afterMaterialSha256": self.after_material_sha256,
            "preWorkspaceDigest": self.pre_workspace_digest,
            "postWorkspaceDigest": self.post_workspace_digest,
        }

    @property
    def receipt_sha256(self) -> str:
        return canonical_json_sha256(self.body())

    def to_mapping(self) -> dict[str, object]:
        return {**self.body(), "receiptSha256": self.receipt_sha256}

    @classmethod
    def from_mapping(cls, value: object) -> "ScopeMutationEvidence":
        if not isinstance(value, Mapping):
            raise TypeError("scope mutation evidence must be a mapping")
        expected = {
            "schemaVersion",
            "scopeId",
            "actorSessionId",
            "executionId",
            "attemptId",
            "inputSha256",
            "sourceReceiptSha256",
            "logicalMutationId",
            "mutationSequence",
            "targetPath",
            "patchSha256",
            "beforeMaterialSha256",
            "afterMaterialSha256",
            "preWorkspaceDigest",
            "postWorkspaceDigest",
            "receiptSha256",
        }
        if set(value) != expected or value.get("schemaVersion") != 2:
            raise ValueError("scope mutation evidence has an invalid schema")
        mutation = cls(
            scope_id=value["scopeId"],
            actor_session_id=value["actorSessionId"],
            execution_id=value["executionId"],
            attempt_id=value["attemptId"],
            input_sha256=value["inputSha256"],
            source_receipt_sha256=value["sourceReceiptSha256"],
            logical_mutation_id=value["logicalMutationId"],
            mutation_sequence=value["mutationSequence"],
            target_path=value["targetPath"],
            patch_sha256=value["patchSha256"],
            before_material_sha256=value["beforeMaterialSha256"],
            after_material_sha256=value["afterMaterialSha256"],
            pre_workspace_digest=value["preWorkspaceDigest"],
            post_workspace_digest=value["postWorkspaceDigest"],
        )
        supplied = _require_sha256(value["receiptSha256"], "receiptSha256")
        if mutation.receipt_sha256 != supplied:
            raise ValueError("mutation evidence digest does not match its body")
        return mutation


@dataclass(frozen=True, slots=True)
class PublicationAuthorityEvidence:
    """Policy and complete mutation chain consumed by the WTB operator."""

    evidence_generation: int
    observed_workspace_digest: str
    current_material_sha256: str
    target_path: str
    policy_bindings: tuple[ScopePolicyBinding, ...]
    mutations: tuple[ScopeMutationEvidence, ...]
    # Multi-path scope (optional): ordered target paths with one current
    # material per path.  Empty means the single-path form, whose
    # serialization is unchanged.  In the multi-path form ``target_path`` is
    # the scope label carried by snapshots and receipts, each mutation targets
    # one of ``target_paths`` with that path's before/after material, and
    # ``current_material_sha256`` must equal ``derive_scope_material_sha256``.
    target_paths: tuple[str, ...] = ()
    path_materials: tuple[PathMaterial, ...] = ()

    @property
    def multi_path(self) -> bool:
        return bool(self.target_paths)

    @property
    def effective_target_paths(self) -> tuple[str, ...]:
        return self.target_paths if self.target_paths else (self.target_path,)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.evidence_generation, int)
            or isinstance(self.evidence_generation, bool)
            or self.evidence_generation <= 0
        ):
            raise ValueError("evidence_generation must be a positive integer")
        _require_sha256(self.observed_workspace_digest, "observed_workspace_digest")
        _require_sha256(self.current_material_sha256, "current_material_sha256")
        _require_safe_relative_path(self.target_path, "target_path")
        if not self.policy_bindings or not all(
            isinstance(row, ScopePolicyBinding) for row in self.policy_bindings
        ):
            raise TypeError("policy_bindings must contain native policy bindings")
        if not self.mutations or not all(
            isinstance(row, ScopeMutationEvidence) for row in self.mutations
        ):
            raise TypeError("mutations must contain native mutation evidence")
        policy_scopes = [row.scope_id for row in self.policy_bindings]
        mutation_scopes = [row.scope_id for row in self.mutations]
        if len(set(policy_scopes)) != len(policy_scopes):
            raise ValueError("publication policy contains duplicate scopes")
        if set(policy_scopes) != set(mutation_scopes) or len(mutation_scopes) != len(
            policy_scopes
        ):
            raise ValueError("mutation chain must cover each publication scope exactly once")
        expected_sequences = list(range(1, len(self.mutations) + 1))
        if [row.mutation_sequence for row in self.mutations] != expected_sequences:
            raise ValueError("mutation sequence must be ordered and contiguous")
        for previous, current in zip(self.mutations, self.mutations[1:], strict=False):
            if previous.post_workspace_digest != current.pre_workspace_digest:
                raise ValueError("mutation workspace chain is discontinuous")
        if self.mutations[-1].post_workspace_digest != self.observed_workspace_digest:
            raise ValueError("mutation chain does not end at the observed workspace")
        if not self.target_paths:
            if self.path_materials:
                raise ValueError("path materials require target paths")
            if any(row.target_path != self.target_path for row in self.mutations):
                raise ValueError("mutation target differs from publication target")
            for previous, current in zip(self.mutations, self.mutations[1:], strict=False):
                if previous.after_material_sha256 != current.before_material_sha256:
                    raise ValueError("mutation material chain is discontinuous")
            if self.mutations[-1].after_material_sha256 != self.current_material_sha256:
                raise ValueError("mutation chain does not end at the current material")
            return
        # Multi-path form.
        paths = tuple(_require_safe_relative_path(p, "target_paths") for p in self.target_paths)
        if len(set(paths)) != len(paths):
            raise ValueError("target paths must be unique")
        if not all(isinstance(row, PathMaterial) for row in self.path_materials):
            raise TypeError("path_materials must contain PathMaterial rows")
        if tuple(row.path for row in self.path_materials) != paths:
            raise ValueError("path material rows must cover the target paths in order")
        for mutation in self.mutations:
            if mutation.target_path not in paths:
                raise ValueError("mutation target is outside the publication target paths")
        materials = {row.path: row.material_sha256 for row in self.path_materials}
        for path in paths:
            chain = [row for row in self.mutations if row.target_path == path]
            for previous, current in zip(chain, chain[1:], strict=False):
                if previous.after_material_sha256 != current.before_material_sha256:
                    raise ValueError(f"mutation material chain is discontinuous for {path}")
            if chain and chain[-1].after_material_sha256 != materials[path]:
                raise ValueError(f"mutation chain does not end at the path material for {path}")
        if self.current_material_sha256 != derive_scope_material_sha256(self.path_materials):
            raise ValueError("current material differs from the derived scope material")

    @property
    def policy_sha256(self) -> str:
        return canonical_json_sha256(
            {
                "schemaVersion": 1,
                "bindings": [row.to_mapping() for row in self.policy_bindings],
            }
        )

    def body(self) -> dict[str, object]:
        body: dict[str, object] = {
            "schemaVersion": 2,
            "evidenceGeneration": self.evidence_generation,
            "observedWorkspaceDigest": self.observed_workspace_digest,
            "currentMaterialSha256": self.current_material_sha256,
            "targetPath": self.target_path,
            "policySha256": self.policy_sha256,
            "policyBindings": [row.to_mapping() for row in self.policy_bindings],
            "mutations": [row.to_mapping() for row in self.mutations],
        }
        if self.target_paths:
            body["targetPaths"] = list(self.target_paths)
            body["pathMaterials"] = [row.to_mapping() for row in self.path_materials]
        return body

    @property
    def evidence_sha256(self) -> str:
        return canonical_json_sha256(self.body())

    def to_mapping(self) -> dict[str, object]:
        return {**self.body(), "evidenceSha256": self.evidence_sha256}

    @classmethod
    def from_mapping(cls, value: object) -> "PublicationAuthorityEvidence":
        if not isinstance(value, Mapping):
            raise TypeError("publication authority evidence must be a mapping")
        expected = {
            "schemaVersion",
            "evidenceGeneration",
            "observedWorkspaceDigest",
            "currentMaterialSha256",
            "targetPath",
            "policySha256",
            "policyBindings",
            "mutations",
            "evidenceSha256",
        }
        multi = expected | {"targetPaths", "pathMaterials"}
        if set(value) not in (expected, multi) or value.get("schemaVersion") != 2:
            raise ValueError("publication authority evidence has an invalid schema")
        policy = value["policyBindings"]
        mutations = value["mutations"]
        if not isinstance(policy, list) or not isinstance(mutations, list):
            raise TypeError("publication authority evidence collections are invalid")
        evidence = cls(
            evidence_generation=value["evidenceGeneration"],
            observed_workspace_digest=value["observedWorkspaceDigest"],
            current_material_sha256=value["currentMaterialSha256"],
            target_path=value["targetPath"],
            policy_bindings=tuple(
                ScopePolicyBinding.from_mapping(row) for row in policy
            ),
            mutations=tuple(ScopeMutationEvidence.from_mapping(row) for row in mutations),
            target_paths=_target_paths_from(value.get("targetPaths"), "targetPaths"),
            path_materials=_path_materials_from(value.get("pathMaterials"), "pathMaterials"),
        )
        supplied_policy = _require_sha256(value["policySha256"], "policySha256")
        supplied_evidence = _require_sha256(
            value["evidenceSha256"],
            "evidenceSha256",
        )
        if evidence.policy_sha256 != supplied_policy:
            raise ValueError("publication policy digest does not match its bindings")
        if evidence.evidence_sha256 != supplied_evidence:
            raise ValueError("publication evidence digest does not match its body")
        return evidence


@dataclass(frozen=True, slots=True)
class PublicationAuthoritySnapshot:
    """WTB-owned immutable relation between attempts, scopes, and material."""

    resolution: PublicationAuthorityResolution
    resolution_reason: PublicationReason
    evidence_generation: int
    evidence_sha256: str
    binding_set_sha256: str
    observed_workspace_digest: str
    current_material_sha256: str
    target_path: str
    external_recovery_required: bool
    target_paths: tuple[str, ...] = ()
    path_materials: tuple[PathMaterial, ...] = ()

    def __post_init__(self) -> None:
        if bool(self.target_paths) != bool(self.path_materials):
            raise ValueError("multi-path snapshot needs both target paths and path materials")
        if self.target_paths:
            if tuple(row.path for row in self.path_materials) != tuple(self.target_paths):
                raise ValueError("snapshot path materials must cover the target paths in order")
            if derive_scope_material_sha256(self.path_materials) != self.current_material_sha256:
                raise ValueError("snapshot current material differs from the derived scope material")

    @property
    def effective_target_paths(self) -> tuple[str, ...]:
        return self.target_paths if self.target_paths else (self.target_path,)

    def body(self) -> dict[str, object]:
        body: dict[str, object] = {
            "schemaVersion": 1,
            "resolution": self.resolution.value,
            "resolutionReason": self.resolution_reason.value,
            "evidenceGeneration": self.evidence_generation,
            "evidenceSha256": self.evidence_sha256,
            "bindingSetSha256": self.binding_set_sha256,
            "observedWorkspaceDigest": self.observed_workspace_digest,
            "currentMaterialSha256": self.current_material_sha256,
            "targetPath": self.target_path,
            "externalRecoveryRequired": self.external_recovery_required,
        }
        if self.target_paths:
            body["targetPaths"] = list(self.target_paths)
            body["pathMaterials"] = [row.to_mapping() for row in self.path_materials]
        return body

    @property
    def snapshot_sha256(self) -> str:
        return canonical_json_sha256(self.body())

    def to_mapping(self) -> dict[str, object]:
        return {**self.body(), "snapshotSha256": self.snapshot_sha256}

    @classmethod
    def from_mapping(cls, value: object) -> "PublicationAuthoritySnapshot":
        if not isinstance(value, Mapping):
            raise TypeError("publication authority snapshot must be a mapping")
        expected = {
            "schemaVersion",
            "resolution",
            "resolutionReason",
            "evidenceGeneration",
            "evidenceSha256",
            "bindingSetSha256",
            "observedWorkspaceDigest",
            "currentMaterialSha256",
            "targetPath",
            "externalRecoveryRequired",
            "snapshotSha256",
        }
        multi = expected | {"targetPaths", "pathMaterials"}
        if set(value) not in (expected, multi) or value.get("schemaVersion") != 1:
            raise ValueError("publication authority snapshot has an invalid schema")
        snapshot = cls(
            resolution=PublicationAuthorityResolution(value["resolution"]),
            resolution_reason=PublicationReason(value["resolutionReason"]),
            evidence_generation=value["evidenceGeneration"],
            evidence_sha256=_require_sha256(
                value["evidenceSha256"],
                "evidenceSha256",
            ),
            binding_set_sha256=_require_sha256(
                value["bindingSetSha256"],
                "bindingSetSha256",
            ),
            observed_workspace_digest=_require_sha256(
                value["observedWorkspaceDigest"],
                "observedWorkspaceDigest",
            ),
            current_material_sha256=_require_sha256(
                value["currentMaterialSha256"],
                "currentMaterialSha256",
            ),
            target_path=_require_safe_relative_path(value["targetPath"], "targetPath"),
            external_recovery_required=value["externalRecoveryRequired"],
            target_paths=_target_paths_from(value.get("targetPaths"), "targetPaths"),
            path_materials=_path_materials_from(value.get("pathMaterials"), "pathMaterials"),
        )
        if not isinstance(snapshot.external_recovery_required, bool):
            raise TypeError("externalRecoveryRequired must be a bool")
        supplied = _require_sha256(value["snapshotSha256"], "snapshotSha256")
        if snapshot.snapshot_sha256 != supplied:
            raise ValueError("publication snapshot digest does not match its body")
        return snapshot


@dataclass(frozen=True, slots=True)
class PublicationIntent:
    """One immutable publish/block decision bound to one authority snapshot."""

    action: PublicationAction
    selected_material_sha256: str | None
    authority_snapshot_sha256: str
    # Optional per-path selection for multi-path scopes; when present the
    # scope-level selected material must be the digest derived from it.
    selected_path_materials: tuple[PathMaterial, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.action, PublicationAction):
            raise TypeError("action must be PublicationAction")
        _require_sha256(
            self.authority_snapshot_sha256,
            "authority_snapshot_sha256",
        )
        if self.action is PublicationAction.PUBLISH_CURRENT:
            _require_sha256(
                self.selected_material_sha256,
                "selected_material_sha256",
            )
        elif self.selected_material_sha256 is not None:
            raise ValueError("block intent cannot select publication material")
        if self.selected_path_materials:
            if self.action is not PublicationAction.PUBLISH_CURRENT:
                raise ValueError("selected path materials require a publish intent")
            if not all(isinstance(row, PathMaterial) for row in self.selected_path_materials):
                raise TypeError("selected_path_materials must contain PathMaterial rows")
            if derive_scope_material_sha256(self.selected_path_materials) != (
                self.selected_material_sha256
            ):
                raise ValueError("selected path materials do not derive the selected material")

    def body(self) -> dict[str, object]:
        body: dict[str, object] = {
            "schemaVersion": 1,
            "action": self.action.value,
            "selectedMaterialSha256": self.selected_material_sha256,
            "authoritySnapshotSha256": self.authority_snapshot_sha256,
        }
        if self.selected_path_materials:
            body["selectedPathMaterials"] = [row.to_mapping() for row in self.selected_path_materials]
        return body

    @property
    def intent_sha256(self) -> str:
        return canonical_json_sha256(self.body())

    def to_mapping(self) -> dict[str, object]:
        return {**self.body(), "intentSha256": self.intent_sha256}

    @classmethod
    def from_mapping(cls, value: object) -> "PublicationIntent":
        if not isinstance(value, Mapping):
            raise TypeError("publication intent must be a mapping")
        expected = {
            "schemaVersion",
            "action",
            "selectedMaterialSha256",
            "authoritySnapshotSha256",
            "intentSha256",
        }
        if set(value) not in (expected, expected | {"selectedPathMaterials"}) or (
            value.get("schemaVersion") != 1
        ):
            raise ValueError("publication intent has an invalid schema")
        intent = cls(
            action=PublicationAction(value["action"]),
            selected_material_sha256=value["selectedMaterialSha256"],
            authority_snapshot_sha256=value["authoritySnapshotSha256"],
            selected_path_materials=_path_materials_from(
                value.get("selectedPathMaterials"), "selectedPathMaterials"
            ),
        )
        supplied = _require_sha256(value["intentSha256"], "intentSha256")
        if intent.intent_sha256 != supplied:
            raise ValueError("publication intent digest does not match its body")
        return intent


@dataclass(frozen=True, slots=True)
class PublicationDecisionReceipt:
    """Self-hashed pre-effect publication decision for durable persistence."""

    disposition: PublicationDisposition
    reason: PublicationReason
    authority_resolution: PublicationAuthorityResolution
    authority_snapshot_sha256: str
    evidence_sha256: str
    intent_sha256: str | None
    action: PublicationAction | None
    selected_material_sha256: str | None
    observed_workspace_digest: str
    current_workspace_digest: str
    current_material_sha256: str
    intent_accepted: bool
    effect: str
    pre_effect_digest_rechecked: bool
    external_recovery_required: bool
    replan_required: bool
    reconciliation_required: bool
    # Number of target paths validated by the pre-effect material check; 1 in
    # the single-path form (not serialized) and len(targetPaths) otherwise.
    validated_path_count: int = 1

    @property
    def recovery_required(self) -> bool:
        return self.external_recovery_required or self.replan_required

    def body(self) -> dict[str, object]:
        body: dict[str, object] = {
            "schemaVersion": 1,
            "disposition": self.disposition.value,
            "reason": self.reason.value,
            "authorityResolution": self.authority_resolution.value,
            "authoritySnapshotSha256": self.authority_snapshot_sha256,
            "evidenceSha256": self.evidence_sha256,
            "intentSha256": self.intent_sha256,
            "action": None if self.action is None else self.action.value,
            "selectedMaterialSha256": self.selected_material_sha256,
            "observedWorkspaceDigest": self.observed_workspace_digest,
            "currentWorkspaceDigest": self.current_workspace_digest,
            "currentMaterialSha256": self.current_material_sha256,
            "intentAccepted": self.intent_accepted,
            "effect": self.effect,
            "preEffectDigestRechecked": self.pre_effect_digest_rechecked,
            "externalRecoveryRequired": self.external_recovery_required,
            "replanRequired": self.replan_required,
            "reconciliationRequired": self.reconciliation_required,
            "recoveryRequired": self.recovery_required,
        }
        if self.validated_path_count != 1:
            body["validatedPathCount"] = self.validated_path_count
        return body

    @property
    def receipt_sha256(self) -> str:
        return canonical_json_sha256(self.body())

    def to_mapping(self) -> dict[str, object]:
        return {**self.body(), "receiptSha256": self.receipt_sha256}

    @classmethod
    def from_mapping(cls, value: object) -> "PublicationDecisionReceipt":
        if not isinstance(value, Mapping):
            raise TypeError("publication decision receipt must be a mapping")
        expected = {
            "schemaVersion",
            "disposition",
            "reason",
            "authorityResolution",
            "authoritySnapshotSha256",
            "evidenceSha256",
            "intentSha256",
            "action",
            "selectedMaterialSha256",
            "observedWorkspaceDigest",
            "currentWorkspaceDigest",
            "currentMaterialSha256",
            "intentAccepted",
            "effect",
            "preEffectDigestRechecked",
            "externalRecoveryRequired",
            "replanRequired",
            "reconciliationRequired",
            "recoveryRequired",
            "receiptSha256",
        }
        if set(value) not in (expected, expected | {"validatedPathCount"}) or (
            value.get("schemaVersion") != 1
        ):
            raise ValueError("publication decision receipt has an invalid schema")
        validated = value.get("validatedPathCount", 1)
        if not isinstance(validated, int) or isinstance(validated, bool) or validated < 1:
            raise ValueError("validatedPathCount must be a positive integer")
        action = value["action"]
        intent_sha256 = value["intentSha256"]
        receipt = cls(
            disposition=PublicationDisposition(value["disposition"]),
            reason=PublicationReason(value["reason"]),
            authority_resolution=PublicationAuthorityResolution(
                value["authorityResolution"]
            ),
            authority_snapshot_sha256=_require_sha256(
                value["authoritySnapshotSha256"],
                "authoritySnapshotSha256",
            ),
            evidence_sha256=_require_sha256(
                value["evidenceSha256"],
                "evidenceSha256",
            ),
            intent_sha256=(
                None
                if intent_sha256 is None
                else _require_sha256(intent_sha256, "intentSha256")
            ),
            action=None if action is None else PublicationAction(action),
            selected_material_sha256=value["selectedMaterialSha256"],
            observed_workspace_digest=_require_sha256(
                value["observedWorkspaceDigest"],
                "observedWorkspaceDigest",
            ),
            current_workspace_digest=_require_sha256(
                value["currentWorkspaceDigest"],
                "currentWorkspaceDigest",
            ),
            current_material_sha256=_require_sha256(
                value["currentMaterialSha256"],
                "currentMaterialSha256",
            ),
            intent_accepted=value["intentAccepted"],
            effect=value["effect"],
            pre_effect_digest_rechecked=value["preEffectDigestRechecked"],
            external_recovery_required=value["externalRecoveryRequired"],
            replan_required=value["replanRequired"],
            reconciliation_required=value["reconciliationRequired"],
            validated_path_count=validated,
        )
        bool_fields = (
            receipt.intent_accepted,
            receipt.pre_effect_digest_rechecked,
            receipt.external_recovery_required,
            receipt.replan_required,
            receipt.reconciliation_required,
        )
        if not all(isinstance(item, bool) for item in bool_fields):
            raise TypeError("publication decision boolean fields are invalid")
        if receipt.effect not in {"publish_current", "reconciliation_hold", "none"}:
            raise ValueError("publication decision effect is invalid")
        if value["recoveryRequired"] is not receipt.recovery_required:
            raise ValueError("publication decision recovery aggregate is invalid")
        supplied = _require_sha256(value["receiptSha256"], "receiptSha256")
        if receipt.receipt_sha256 != supplied:
            raise ValueError("publication decision receipt digest does not match its body")
        return receipt


class PublicationAuthorityOperator:
    """Validate shared-workspace authority and fence publication effects."""

    @classmethod
    def snapshot_from_executions(
        cls,
        executions: Sequence[Execution],
        evidence: PublicationAuthorityEvidence,
    ) -> PublicationAuthoritySnapshot:
        if not isinstance(evidence, PublicationAuthorityEvidence):
            raise TypeError("evidence must be PublicationAuthorityEvidence")
        execution_by_id = {
            execution.id: execution
            for execution in executions
            if isinstance(execution, Execution)
        }
        binding_rows: list[dict[str, object]] = []
        unresolved = len(execution_by_id) != len(executions)
        for mutation in evidence.mutations:
            execution = execution_by_id.get(mutation.execution_id)
            terminal: object = None
            row_unresolved = False
            if execution is not None and isinstance(execution.metadata, Mapping):
                terminal = execution.metadata.get("last_runtime_attempt")
                row_unresolved = (
                    execution.status is not ExecutionStatus.COMPLETED
                    or execution.metadata.get("recovery_required") is True
                    or isinstance(execution.metadata.get("runtime_attempt"), Mapping)
                )
            else:
                row_unresolved = True
            binding = cls._terminal_binding(terminal)
            source_receipt_sha256 = cls._terminal_source_receipt_sha256(terminal)
            if binding is None:
                row_unresolved = True
            else:
                attempt_id, input_sha256, session_id, outcome = binding
                row_unresolved = row_unresolved or (
                    attempt_id != mutation.attempt_id
                    or input_sha256 != mutation.input_sha256
                    or session_id != mutation.actor_session_id
                    or outcome != "completed"
                )
            row_unresolved = row_unresolved or (
                source_receipt_sha256 != mutation.source_receipt_sha256
            )
            unresolved = unresolved or row_unresolved
            binding_rows.append(
                {
                    "mutationReceiptSha256": mutation.receipt_sha256,
                    "executionId": mutation.execution_id,
                    "terminalBindingValid": not row_unresolved,
                }
            )

        policy = {row.scope_id: row.authorized_session_id for row in evidence.policy_bindings}
        policy_matches = all(
            policy[mutation.scope_id] == mutation.actor_session_id
            for mutation in evidence.mutations
        )
        if unresolved:
            resolution = PublicationAuthorityResolution.UNRESOLVED
            reason = PublicationReason.ATTEMPT_UNRESOLVED
        elif policy_matches:
            resolution = PublicationAuthorityResolution.AUTHORIZED
            reason = PublicationReason.SCOPE_BINDINGS_AUTHORIZED
        else:
            resolution = PublicationAuthorityResolution.BLOCKED
            reason = PublicationReason.AUTHORITY_SCOPE_MISMATCH
        return PublicationAuthoritySnapshot(
            resolution=resolution,
            resolution_reason=reason,
            evidence_generation=evidence.evidence_generation,
            evidence_sha256=evidence.evidence_sha256,
            binding_set_sha256=canonical_json_sha256(
                {
                    "schemaVersion": 1,
                    "policySha256": evidence.policy_sha256,
                    "bindings": binding_rows,
                }
            ),
            observed_workspace_digest=evidence.observed_workspace_digest,
            current_material_sha256=evidence.current_material_sha256,
            target_path=evidence.target_path,
            external_recovery_required=unresolved,
            target_paths=evidence.target_paths,
            path_materials=evidence.path_materials,
        )

    @staticmethod
    def authorize(
        snapshot: PublicationAuthoritySnapshot,
        intent: PublicationIntent | None,
        *,
        current_workspace_digest: str,
    ) -> PublicationDecisionReceipt:
        if not isinstance(snapshot, PublicationAuthoritySnapshot):
            raise TypeError("snapshot must be PublicationAuthoritySnapshot")
        current_workspace_digest = _require_sha256(
            current_workspace_digest,
            "current_workspace_digest",
        )

        def decision(
            reason: PublicationReason,
            *,
            permit: bool = False,
            accepted: bool = False,
            effect: str = "none",
            replan: bool = False,
            reconcile: bool = False,
        ) -> PublicationDecisionReceipt:
            return PublicationDecisionReceipt(
                disposition=(
                    PublicationDisposition.PERMIT_PUBLICATION
                    if permit
                    else PublicationDisposition.DENY_PUBLICATION
                ),
                reason=reason,
                authority_resolution=snapshot.resolution,
                authority_snapshot_sha256=snapshot.snapshot_sha256,
                evidence_sha256=snapshot.evidence_sha256,
                intent_sha256=None if intent is None else intent.intent_sha256,
                action=None if intent is None else intent.action,
                selected_material_sha256=(
                    None if intent is None else intent.selected_material_sha256
                ),
                observed_workspace_digest=snapshot.observed_workspace_digest,
                current_workspace_digest=current_workspace_digest,
                current_material_sha256=snapshot.current_material_sha256,
                intent_accepted=accepted,
                effect=effect,
                pre_effect_digest_rechecked=True,
                external_recovery_required=snapshot.external_recovery_required,
                replan_required=replan,
                reconciliation_required=reconcile,
                validated_path_count=len(snapshot.effective_target_paths),
            )

        if snapshot.resolution is PublicationAuthorityResolution.UNRESOLVED:
            return decision(PublicationReason.ATTEMPT_UNRESOLVED, replan=True)
        if intent is not None and (
            intent.authority_snapshot_sha256 != snapshot.snapshot_sha256
        ):
            return decision(PublicationReason.AUTHORITY_SNAPSHOT_CHANGED, replan=True)
        if current_workspace_digest != snapshot.observed_workspace_digest:
            return decision(PublicationReason.WORKSPACE_DIGEST_CHANGED, replan=True)
        if intent is None:
            return decision(PublicationReason.INTENT_MISSING, replan=True)
        if intent.action is PublicationAction.BLOCK_AND_RECONCILE:
            reason = (
                PublicationReason.AUTHORIZED_MATERIAL_BLOCKED
                if snapshot.resolution is PublicationAuthorityResolution.AUTHORIZED
                else PublicationReason.RECONCILIATION_REQUESTED
            )
            return decision(
                reason,
                accepted=True,
                effect="reconciliation_hold",
                reconcile=True,
            )
        if snapshot.resolution is PublicationAuthorityResolution.BLOCKED:
            return decision(
                PublicationReason.AUTHORITY_SCOPE_MISMATCH,
                effect="reconciliation_hold",
                reconcile=True,
            )
        if intent.selected_material_sha256 != snapshot.current_material_sha256:
            return decision(PublicationReason.MATERIAL_NOT_AUTHORIZED, replan=True)
        if snapshot.path_materials:
            # Every target path is rechecked: the snapshot's per-path materials
            # must derive the scope material, and an explicit per-path
            # selection must match each path exactly.
            if derive_scope_material_sha256(snapshot.path_materials) != (
                snapshot.current_material_sha256
            ):
                return decision(PublicationReason.MATERIAL_NOT_AUTHORIZED, replan=True)
            if intent.selected_path_materials and (
                tuple(intent.selected_path_materials) != tuple(snapshot.path_materials)
            ):
                return decision(PublicationReason.MATERIAL_NOT_AUTHORIZED, replan=True)
        return decision(
            PublicationReason.AUTHORITY_AND_WORKSPACE_BOUND,
            permit=True,
            accepted=True,
            effect="publish_current",
        )

    @staticmethod
    def _terminal_binding(terminal: object) -> tuple[str, str, str, str] | None:
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
        return binding.attempt_id, binding.input_sha256, binding.exact_session_id, outcome

    @staticmethod
    def _terminal_source_receipt_sha256(terminal: object) -> str | None:
        if not isinstance(terminal, Mapping):
            return None
        receipt = terminal.get("receipt")
        if not isinstance(receipt, Mapping):
            return None
        value = receipt.get("rawReceiptSha256")
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            return None
        return value


__all__ = [
    "PathMaterial",
    "PublicationAction",
    "PublicationAuthorityEvidence",
    "PublicationAuthorityOperator",
    "PublicationAuthorityResolution",
    "PublicationAuthoritySnapshot",
    "PublicationDecisionReceipt",
    "PublicationDisposition",
    "PublicationIntent",
    "PublicationReason",
    "ScopeMutationEvidence",
    "ScopePolicyBinding",
    "derive_scope_material_sha256",
]
