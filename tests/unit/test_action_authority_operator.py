"""Contract tests for WTB's mutation-boundary authority operator."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from wtb.domain.action_authority import (
    ActionAuthorityOperator,
    AuthorityEvidence,
    AuthorityResolution,
    AuthorityResolutionReason,
    MutationPermitDisposition,
    MutationPermitReason,
    NonPublicationEvidence,
    PublicationEvidence,
    ToolMutationIntent,
)
from wtb.domain.interfaces.workflow_runtime import RuntimeBindingEnvelope
from wtb.domain.models.workflow import Execution, ExecutionStatus


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _publication(
    *,
    channel: str,
    generation: int,
    artifact: str,
    contract: str,
    attempt_id: str | None = None,
    input_sha256: str | None = None,
    memory_snapshot_sha256: str | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "schemaVersion": 1,
        "channel": channel,
        "generation": generation,
        "artifactSha256": artifact,
        "contractSha256": contract,
        "gitVisible": channel == "git",
    }
    if channel == "git":
        body["commit"] = "1" * 40
    else:
        body.update(
            {
                "attemptId": attempt_id,
                "inputSha256": input_sha256,
                "memorySnapshotSha256": memory_snapshot_sha256,
            }
        )
    return {**body, "receiptSha256": _digest(body)}


def _non_publication(
    *,
    attempt_id: str = "attempt-2",
    input_sha256: str = "2" * 64,
    memory_snapshot_sha256: str,
) -> dict[str, object]:
    body = {
        "schemaVersion": 1,
        "channel": "wtb-external",
        "generation": 2,
        "outcome": "not_published",
        "attemptId": attempt_id,
        "inputSha256": input_sha256,
        "memorySnapshotSha256": memory_snapshot_sha256,
        "requestSha256": "8" * 64,
        "responseSha256": "9" * 64,
    }
    return {**body, "receiptSha256": _digest(body)}


def _execution(
    *,
    outcome: str = "completed",
    recovery_required: bool = False,
) -> Execution:
    attempt_id = "attempt-2"
    input_sha256 = "2" * 64
    binding = RuntimeBindingEnvelope(
        exact_session_id="session-2",
        persistence_instance_id="store-2",
        runtime_config_sha256="3" * 64,
        protocol_version=2,
        schema_version=1,
        input_sha256=input_sha256,
        attempt_id=attempt_id,
    )
    status = {
        "completed": ExecutionStatus.COMPLETED,
        "failed": ExecutionStatus.FAILED,
        "cancelled": ExecutionStatus.CANCELLED,
        "running": ExecutionStatus.RUNNING,
    }[outcome]
    terminal = {
        "attempt_id": attempt_id,
        "input_sha256": input_sha256,
        "outcome": outcome,
        "session_id": "session-2",
        "binding": binding.to_dict(),
        "receipt": {"finish_reason": outcome},
    }
    metadata: dict[str, object] = {"last_runtime_attempt": terminal}
    if recovery_required:
        metadata.update(
            {
                "recovery_required": True,
                "recovery_reason": "runtime_outcome_unknown",
                "runtime_attempt": {
                    "attempt_id": attempt_id,
                    "input_sha256": input_sha256,
                    "session_id": "session-2",
                    "binding": binding.to_dict(),
                },
            }
        )
    return Execution(
        id="execution-2",
        workflow_id="workflow-1",
        status=status,
        metadata=metadata,
    )


@pytest.fixture
def authority_inputs() -> dict[str, object]:
    a1_artifact = hashlib.sha256(b"stale\n").hexdigest()
    a1_contract = _digest({"contract": "A1"})
    a2_artifact = hashlib.sha256(b"approved\n").hexdigest()
    a2_contract = _digest({"contract": "A2"})
    memory = _digest({"session": "session-2", "terminalEventSeq": 7})
    workspace = _digest({"task": "repair", "target": "stale"})
    return {
        "a1_artifact": a1_artifact,
        "a1_contract": a1_contract,
        "a2_artifact": a2_artifact,
        "a2_contract": a2_contract,
        "memory": memory,
        "workspace": workspace,
        "git": _publication(
            channel="git",
            generation=1,
            artifact=a1_artifact,
            contract=a1_contract,
        ),
        "external": _publication(
            channel="wtb-external",
            generation=2,
            artifact=a2_artifact,
            contract=a2_contract,
            attempt_id="attempt-2",
            input_sha256="2" * 64,
            memory_snapshot_sha256=memory,
        ),
        "non_publication": _non_publication(
            memory_snapshot_sha256=memory,
        ),
    }


def _evidence(
    values: dict[str, object],
    *,
    external: bool = True,
    non_publication: bool = False,
) -> AuthorityEvidence:
    return AuthorityEvidence(
        evidence_generation=2,
        observed_workspace_digest=str(values["workspace"]),
        memory_snapshot_sha256=str(values["memory"]),
        target_path="policy.py",
        git_publication=PublicationEvidence.from_mapping(values["git"]),
        external_publication=(
            PublicationEvidence.from_mapping(values["external"])
            if external
            else None
        ),
        external_non_publication=(
            NonPublicationEvidence.from_mapping(values["non_publication"])
            if non_publication
            else None
        ),
    )


def _intent(snapshot_sha256: str, content: str) -> ToolMutationIntent:
    return ToolMutationIntent.from_tool_call(
        tool_name="write",
        arguments={"file_path": "policy.py", "content": content},
        action="repair_target",
        authority_snapshot_sha256=snapshot_sha256,
    )


def test_completed_external_attempt_authorizes_only_exact_a2_material(
    authority_inputs: dict[str, object],
) -> None:
    snapshot = ActionAuthorityOperator.snapshot_from_execution(
        _execution(),
        _evidence(authority_inputs),
    )

    assert snapshot.resolution is AuthorityResolution.RESOLVED
    assert snapshot.authoritative_generation == 2
    assert snapshot.authoritative_material_sha256 == authority_inputs["a2_artifact"]

    permit = ActionAuthorityOperator.authorize(
        snapshot,
        _intent(snapshot.snapshot_sha256, "approved\n"),
        current_workspace_digest=str(authority_inputs["workspace"]),
    )
    stale = ActionAuthorityOperator.authorize(
        snapshot,
        _intent(snapshot.snapshot_sha256, "stale\n"),
        current_workspace_digest=str(authority_inputs["workspace"]),
    )

    assert permit.disposition is MutationPermitDisposition.PERMIT
    assert permit.reason is MutationPermitReason.AUTHORITY_AND_WORKSPACE_BOUND
    assert stale.disposition is MutationPermitDisposition.DENY
    assert stale.reason is MutationPermitReason.MATERIAL_NOT_AUTHORIZED


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
def test_known_non_success_attempt_falls_back_to_git_authority(
    authority_inputs: dict[str, object],
    outcome: str,
) -> None:
    snapshot = ActionAuthorityOperator.snapshot_from_execution(
        _execution(outcome=outcome),
        _evidence(authority_inputs, external=False),
    )

    assert snapshot.resolution is AuthorityResolution.RESOLVED
    assert snapshot.authoritative_generation == 1
    assert snapshot.authoritative_material_sha256 == authority_inputs["a1_artifact"]


def test_completed_attempt_with_exact_non_publication_falls_back_to_git(
    authority_inputs: dict[str, object],
) -> None:
    snapshot = ActionAuthorityOperator.snapshot_from_execution(
        _execution(),
        _evidence(authority_inputs, external=False, non_publication=True),
    )
    receipt = ActionAuthorityOperator.authorize(
        snapshot,
        _intent(snapshot.snapshot_sha256, "stale\n"),
        current_workspace_digest=str(authority_inputs["workspace"]),
    )

    assert snapshot.resolution is AuthorityResolution.RESOLVED
    assert (
        snapshot.resolution_reason
        is AuthorityResolutionReason.EXTERNAL_PUBLICATION_NOT_ACCEPTED_FALLBACK
    )
    assert snapshot.authoritative_generation == 1
    assert snapshot.authoritative_material_sha256 == authority_inputs["a1_artifact"]
    assert receipt.disposition is MutationPermitDisposition.PERMIT


def test_completed_attempt_without_positive_or_negative_receipt_is_unresolved(
    authority_inputs: dict[str, object],
) -> None:
    snapshot = ActionAuthorityOperator.snapshot_from_execution(
        _execution(),
        _evidence(authority_inputs, external=False),
    )

    assert snapshot.resolution is AuthorityResolution.UNRESOLVED
    assert (
        snapshot.resolution_reason
        is AuthorityResolutionReason.EXTERNAL_PUBLICATION_MISSING
    )


def test_completed_attempt_accepts_an_exact_attempt_bound_git_publication(
    authority_inputs: dict[str, object],
) -> None:
    values = copy.deepcopy(authority_inputs)
    body = {
        "schemaVersion": 1,
        "channel": "git",
        "generation": 2,
        "artifactSha256": values["a2_artifact"],
        "contractSha256": values["a2_contract"],
        "gitVisible": True,
        "commit": "2" * 40,
        "attemptId": "attempt-2",
        "inputSha256": "2" * 64,
        "memorySnapshotSha256": values["memory"],
    }
    values["git"] = {**body, "receiptSha256": _digest(body)}

    snapshot = ActionAuthorityOperator.snapshot_from_execution(
        _execution(),
        _evidence(values, external=False),
    )
    receipt = ActionAuthorityOperator.authorize(
        snapshot,
        _intent(snapshot.snapshot_sha256, "approved\n"),
        current_workspace_digest=str(values["workspace"]),
    )

    assert snapshot.resolution is AuthorityResolution.RESOLVED
    assert (
        snapshot.resolution_reason
        is AuthorityResolutionReason.GIT_ATTEMPT_COMPLETED
    )
    assert snapshot.authoritative_generation == 2
    assert snapshot.authoritative_material_sha256 == values["a2_artifact"]
    assert receipt.disposition is MutationPermitDisposition.PERMIT


def test_completed_attempt_rejects_a_mismatched_attempt_bound_git_publication(
    authority_inputs: dict[str, object],
) -> None:
    values = copy.deepcopy(authority_inputs)
    body = {
        "schemaVersion": 1,
        "channel": "git",
        "generation": 2,
        "artifactSha256": values["a2_artifact"],
        "contractSha256": values["a2_contract"],
        "gitVisible": True,
        "commit": "2" * 40,
        "attemptId": "another-attempt",
        "inputSha256": "2" * 64,
        "memorySnapshotSha256": values["memory"],
    }
    values["git"] = {**body, "receiptSha256": _digest(body)}

    snapshot = ActionAuthorityOperator.snapshot_from_execution(
        _execution(),
        _evidence(values, external=False),
    )

    assert snapshot.resolution is AuthorityResolution.UNRESOLVED
    assert (
        snapshot.resolution_reason
        is AuthorityResolutionReason.GIT_PUBLICATION_MISMATCH
    )


def test_attempt_bound_git_publication_requires_an_all_or_none_binding(
    authority_inputs: dict[str, object],
) -> None:
    body = {
        **authority_inputs["git"],
        "attemptId": "attempt-2",
    }
    body.pop("receiptSha256")
    body["receiptSha256"] = _digest(body)

    with pytest.raises(ValueError, match="Git publication attempt binding"):
        PublicationEvidence.from_mapping(body)


def test_non_publication_receipt_must_bind_the_exact_attempt(
    authority_inputs: dict[str, object],
) -> None:
    mismatched = copy.deepcopy(authority_inputs)
    mismatched["non_publication"] = _non_publication(
        attempt_id="other-attempt",
        memory_snapshot_sha256=str(authority_inputs["memory"]),
    )

    snapshot = ActionAuthorityOperator.snapshot_from_execution(
        _execution(),
        _evidence(mismatched, external=False, non_publication=True),
    )

    assert snapshot.resolution is AuthorityResolution.UNRESOLVED
    assert (
        snapshot.resolution_reason
        is AuthorityResolutionReason.EXTERNAL_PUBLICATION_MISMATCH
    )


def test_running_recovery_required_attempt_fails_closed(
    authority_inputs: dict[str, object],
) -> None:
    snapshot = ActionAuthorityOperator.snapshot_from_execution(
        _execution(outcome="running", recovery_required=True),
        _evidence(authority_inputs),
    )
    receipt = ActionAuthorityOperator.authorize(
        snapshot,
        _intent(snapshot.snapshot_sha256, "approved\n"),
        current_workspace_digest=str(authority_inputs["workspace"]),
    )

    assert snapshot.resolution is AuthorityResolution.UNRESOLVED
    assert receipt.disposition is MutationPermitDisposition.DENY
    assert receipt.reason is MutationPermitReason.AUTHORITY_UNRESOLVED
    assert receipt.external_recovery_required is True
    assert receipt.replan_required is True


def test_tampered_external_publication_is_not_accepted(
    authority_inputs: dict[str, object],
) -> None:
    tampered = copy.deepcopy(authority_inputs)
    assert isinstance(tampered["external"], dict)
    tampered["external"]["artifactSha256"] = str(authority_inputs["a1_artifact"])

    with pytest.raises(ValueError, match="publication receipt digest"):
        PublicationEvidence.from_mapping(tampered["external"])


def test_workspace_drift_denies_an_otherwise_authorized_intent(
    authority_inputs: dict[str, object],
) -> None:
    snapshot = ActionAuthorityOperator.snapshot_from_execution(
        _execution(),
        _evidence(authority_inputs),
    )
    receipt = ActionAuthorityOperator.authorize(
        snapshot,
        _intent(snapshot.snapshot_sha256, "approved\n"),
        current_workspace_digest="f" * 64,
    )

    assert receipt.disposition is MutationPermitDisposition.DENY
    assert receipt.reason is MutationPermitReason.WORKSPACE_DIGEST_CHANGED
    assert receipt.replan_required is True


def test_intent_cannot_reuse_a_permit_from_an_old_snapshot(
    authority_inputs: dict[str, object],
) -> None:
    snapshot = ActionAuthorityOperator.snapshot_from_execution(
        _execution(),
        _evidence(authority_inputs),
    )
    receipt = ActionAuthorityOperator.authorize(
        snapshot,
        _intent("0" * 64, "approved\n"),
        current_workspace_digest=str(authority_inputs["workspace"]),
    )

    assert receipt.disposition is MutationPermitDisposition.DENY
    assert receipt.reason is MutationPermitReason.AUTHORITY_SNAPSHOT_CHANGED
    assert receipt.replan_required is True
    assert receipt.receipt_sha256 == _digest(receipt.body())
