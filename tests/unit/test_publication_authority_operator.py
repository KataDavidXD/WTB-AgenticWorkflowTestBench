"""Contract tests for WTB's shared-workspace publication authority fence."""

from __future__ import annotations

import copy

import pytest

from wtb.domain.interfaces.workflow_runtime import RuntimeBindingEnvelope
from wtb.domain.models.workflow import Execution, ExecutionStatus
from wtb.domain.publication_authority import (
    PublicationAction,
    PublicationAuthorityEvidence,
    PublicationAuthorityOperator,
    PublicationAuthorityResolution,
    PublicationAuthoritySnapshot,
    PublicationDecisionReceipt,
    PublicationDisposition,
    PublicationIntent,
    PublicationReason,
    ScopeMutationEvidence,
    ScopePolicyBinding,
    canonical_json_sha256,
)


def _execution(
    session_id: str,
    *,
    status: ExecutionStatus = ExecutionStatus.COMPLETED,
    attempt_suffix: str | None = None,
    recovery_required: bool = False,
    raw_receipt_sha256: str | None = None,
) -> Execution:
    suffix = session_id if attempt_suffix is None else attempt_suffix
    attempt_id = f"attempt-{suffix}"
    input_sha256 = ("a" if session_id == "source-a" else "b") * 64
    binding = RuntimeBindingEnvelope(
        exact_session_id=session_id,
        persistence_instance_id=f"store-{session_id}",
        runtime_config_sha256="c" * 64,
        protocol_version=2,
        schema_version=1,
        input_sha256=input_sha256,
        attempt_id=attempt_id,
    )
    terminal = {
        "attempt_id": attempt_id,
        "input_sha256": input_sha256,
        "outcome": "completed",
        "session_id": session_id,
        "binding": binding.to_dict(),
        "receipt": {
            "finish_reason": "completed",
            "rawReceiptSha256": raw_receipt_sha256
            or (("d" if session_id == "source-a" else "e") * 64),
        },
    }
    metadata: dict[str, object] = {"last_runtime_attempt": terminal}
    if recovery_required:
        metadata["recovery_required"] = True
        metadata["runtime_attempt"] = copy.deepcopy(terminal)
    return Execution(
        id=f"execution-{session_id}",
        workflow_id="shared-publication-workflow",
        status=status,
        metadata=metadata,
    )


def _mutation(
    *,
    session_id: str,
    scope_id: str,
    sequence: int,
    before_material: str,
    after_material: str,
    pre_workspace: str,
    post_workspace: str,
) -> ScopeMutationEvidence:
    input_sha256 = ("a" if session_id == "source-a" else "b") * 64
    return ScopeMutationEvidence(
        scope_id=scope_id,
        actor_session_id=session_id,
        execution_id=f"execution-{session_id}",
        attempt_id=f"attempt-{session_id}",
        input_sha256=input_sha256,
        source_receipt_sha256=("d" if session_id == "source-a" else "e") * 64,
        logical_mutation_id=f"mutation-{sequence}",
        mutation_sequence=sequence,
        target_path="policy.py",
        patch_sha256=str(sequence) * 64,
        before_material_sha256=before_material,
        after_material_sha256=after_material,
        pre_workspace_digest=pre_workspace,
        post_workspace_digest=post_workspace,
    )


@pytest.fixture
def authority_case() -> dict[str, object]:
    base_material = "1" * 64
    middle_material = "2" * 64
    final_material = "3" * 64
    workspace_0 = "4" * 64
    workspace_1 = "5" * 64
    workspace_2 = "6" * 64
    policy = (
        ScopePolicyBinding(scope_id="retry-limit", authorized_session_id="source-a"),
        ScopePolicyBinding(scope_id="audit-mode", authorized_session_id="source-b"),
    )
    aligned_mutations = (
        _mutation(
            session_id="source-a",
            scope_id="retry-limit",
            sequence=1,
            before_material=base_material,
            after_material=middle_material,
            pre_workspace=workspace_0,
            post_workspace=workspace_1,
        ),
        _mutation(
            session_id="source-b",
            scope_id="audit-mode",
            sequence=2,
            before_material=middle_material,
            after_material=final_material,
            pre_workspace=workspace_1,
            post_workspace=workspace_2,
        ),
    )
    crossed_mutations = (
        _mutation(
            session_id="source-a",
            scope_id="audit-mode",
            sequence=1,
            before_material=base_material,
            after_material=middle_material,
            pre_workspace=workspace_0,
            post_workspace=workspace_1,
        ),
        _mutation(
            session_id="source-b",
            scope_id="retry-limit",
            sequence=2,
            before_material=middle_material,
            after_material=final_material,
            pre_workspace=workspace_1,
            post_workspace=workspace_2,
        ),
    )
    common = {
        "evidence_generation": 1,
        "observed_workspace_digest": workspace_2,
        "current_material_sha256": final_material,
        "target_path": "policy.py",
        "policy_bindings": policy,
    }
    return {
        "executions": (_execution("source-a"), _execution("source-b")),
        "aligned": PublicationAuthorityEvidence(
            **common,
            mutations=aligned_mutations,
        ),
        "crossed": PublicationAuthorityEvidence(
            **common,
            mutations=crossed_mutations,
        ),
        "workspace": workspace_2,
        "material": final_material,
    }


def _intent(snapshot_sha256: str, action: PublicationAction, material: str | None):
    return PublicationIntent(
        action=action,
        selected_material_sha256=material,
        authority_snapshot_sha256=snapshot_sha256,
    )


def test_aligned_terminal_attempts_permit_exact_current_publication(
    authority_case: dict[str, object],
) -> None:
    snapshot = PublicationAuthorityOperator.snapshot_from_executions(
        authority_case["executions"],
        authority_case["aligned"],
    )
    receipt = PublicationAuthorityOperator.authorize(
        snapshot,
        _intent(
            snapshot.snapshot_sha256,
            PublicationAction.PUBLISH_CURRENT,
            str(authority_case["material"]),
        ),
        current_workspace_digest=str(authority_case["workspace"]),
    )

    assert snapshot.resolution is PublicationAuthorityResolution.AUTHORIZED
    assert receipt.disposition is PublicationDisposition.PERMIT_PUBLICATION
    assert receipt.reason is PublicationReason.AUTHORITY_AND_WORKSPACE_BOUND
    assert receipt.intent_accepted is True
    assert receipt.effect == "publish_current"
    assert receipt.pre_effect_digest_rechecked is True
    assert receipt.recovery_required is False
    assert receipt.reconciliation_required is False


def test_crossed_authority_denies_an_unsafe_publish_and_places_a_hold(
    authority_case: dict[str, object],
) -> None:
    snapshot = PublicationAuthorityOperator.snapshot_from_executions(
        authority_case["executions"],
        authority_case["crossed"],
    )
    receipt = PublicationAuthorityOperator.authorize(
        snapshot,
        _intent(
            snapshot.snapshot_sha256,
            PublicationAction.PUBLISH_CURRENT,
            str(authority_case["material"]),
        ),
        current_workspace_digest=str(authority_case["workspace"]),
    )

    assert snapshot.resolution is PublicationAuthorityResolution.BLOCKED
    assert receipt.disposition is PublicationDisposition.DENY_PUBLICATION
    assert receipt.reason is PublicationReason.AUTHORITY_SCOPE_MISMATCH
    assert receipt.intent_accepted is False
    assert receipt.effect == "reconciliation_hold"
    assert receipt.reconciliation_required is True
    assert receipt.recovery_required is False


def test_crossed_block_intent_is_accepted_without_a_publication_effect(
    authority_case: dict[str, object],
) -> None:
    snapshot = PublicationAuthorityOperator.snapshot_from_executions(
        authority_case["executions"],
        authority_case["crossed"],
    )
    receipt = PublicationAuthorityOperator.authorize(
        snapshot,
        _intent(
            snapshot.snapshot_sha256,
            PublicationAction.BLOCK_AND_RECONCILE,
            None,
        ),
        current_workspace_digest=str(authority_case["workspace"]),
    )

    assert receipt.disposition is PublicationDisposition.DENY_PUBLICATION
    assert receipt.reason is PublicationReason.RECONCILIATION_REQUESTED
    assert receipt.intent_accepted is True
    assert receipt.effect == "reconciliation_hold"


def test_aligned_block_intent_is_reported_as_an_unnecessary_hold(
    authority_case: dict[str, object],
) -> None:
    snapshot = PublicationAuthorityOperator.snapshot_from_executions(
        authority_case["executions"],
        authority_case["aligned"],
    )
    receipt = PublicationAuthorityOperator.authorize(
        snapshot,
        _intent(
            snapshot.snapshot_sha256,
            PublicationAction.BLOCK_AND_RECONCILE,
            None,
        ),
        current_workspace_digest=str(authority_case["workspace"]),
    )

    assert receipt.reason is PublicationReason.AUTHORIZED_MATERIAL_BLOCKED
    assert receipt.intent_accepted is True
    assert receipt.effect == "reconciliation_hold"


def test_workspace_drift_fails_closed_before_publication(
    authority_case: dict[str, object],
) -> None:
    snapshot = PublicationAuthorityOperator.snapshot_from_executions(
        authority_case["executions"],
        authority_case["aligned"],
    )
    receipt = PublicationAuthorityOperator.authorize(
        snapshot,
        _intent(
            snapshot.snapshot_sha256,
            PublicationAction.PUBLISH_CURRENT,
            str(authority_case["material"]),
        ),
        current_workspace_digest="f" * 64,
    )

    assert receipt.disposition is PublicationDisposition.DENY_PUBLICATION
    assert receipt.reason is PublicationReason.WORKSPACE_DIGEST_CHANGED
    assert receipt.effect == "none"
    assert receipt.recovery_required is True


def test_running_or_mismatched_attempt_is_unresolved_and_cannot_publish(
    authority_case: dict[str, object],
) -> None:
    executions = (
        _execution(
            "source-a",
            status=ExecutionStatus.RUNNING,
            recovery_required=True,
        ),
        _execution("source-b"),
    )
    snapshot = PublicationAuthorityOperator.snapshot_from_executions(
        executions,
        authority_case["aligned"],
    )
    receipt = PublicationAuthorityOperator.authorize(
        snapshot,
        _intent(
            snapshot.snapshot_sha256,
            PublicationAction.PUBLISH_CURRENT,
            str(authority_case["material"]),
        ),
        current_workspace_digest=str(authority_case["workspace"]),
    )

    assert snapshot.resolution is PublicationAuthorityResolution.UNRESOLVED
    assert receipt.reason is PublicationReason.ATTEMPT_UNRESOLVED
    assert receipt.recovery_required is True
    assert receipt.effect == "none"


def test_terminal_receipt_must_bind_the_exact_source_mutation_receipt(
    authority_case: dict[str, object],
) -> None:
    executions = (
        _execution("source-a", raw_receipt_sha256="f" * 64),
        _execution("source-b"),
    )

    snapshot = PublicationAuthorityOperator.snapshot_from_executions(
        executions,
        authority_case["aligned"],
    )

    assert snapshot.resolution is PublicationAuthorityResolution.UNRESOLVED
    assert snapshot.external_recovery_required is True


def test_terminal_binding_validity_is_per_row_not_cumulative(
    authority_case: dict[str, object],
) -> None:
    evidence = authority_case["aligned"]
    assert isinstance(evidence, PublicationAuthorityEvidence)
    executions = (
        _execution("source-a", raw_receipt_sha256="f" * 64),
        _execution("source-b"),
    )

    snapshot = PublicationAuthorityOperator.snapshot_from_executions(
        executions,
        evidence,
    )
    expected_binding_set = canonical_json_sha256(
        {
            "schemaVersion": 1,
            "policySha256": evidence.policy_sha256,
            "bindings": [
                {
                    "mutationReceiptSha256": evidence.mutations[0].receipt_sha256,
                    "executionId": evidence.mutations[0].execution_id,
                    "terminalBindingValid": False,
                },
                {
                    "mutationReceiptSha256": evidence.mutations[1].receipt_sha256,
                    "executionId": evidence.mutations[1].execution_id,
                    "terminalBindingValid": True,
                },
            ],
        }
    )

    assert snapshot.binding_set_sha256 == expected_binding_set


def test_missing_model_intent_fails_closed_with_a_durable_receipt(
    authority_case: dict[str, object],
) -> None:
    snapshot = PublicationAuthorityOperator.snapshot_from_executions(
        authority_case["executions"],
        authority_case["aligned"],
    )
    receipt = PublicationAuthorityOperator.authorize(
        snapshot,
        None,
        current_workspace_digest=str(authority_case["workspace"]),
    )

    assert receipt.reason is PublicationReason.INTENT_MISSING
    assert receipt.disposition is PublicationDisposition.DENY_PUBLICATION
    assert receipt.effect == "none"
    assert receipt.recovery_required is True
    assert receipt.receipt_sha256 == receipt.to_mapping()["receiptSha256"]


def test_tampered_mutation_receipt_and_old_snapshot_intent_are_rejected(
    authority_case: dict[str, object],
) -> None:
    evidence = authority_case["aligned"]
    assert isinstance(evidence, PublicationAuthorityEvidence)
    mutation = evidence.mutations[0].to_mapping()
    mutation["scopeId"] = "audit-mode"
    with pytest.raises(ValueError, match="mutation evidence digest"):
        ScopeMutationEvidence.from_mapping(mutation)

    snapshot = PublicationAuthorityOperator.snapshot_from_executions(
        authority_case["executions"],
        evidence,
    )
    receipt = PublicationAuthorityOperator.authorize(
        snapshot,
        _intent(
            "0" * 64,
            PublicationAction.PUBLISH_CURRENT,
            str(authority_case["material"]),
        ),
        current_workspace_digest=str(authority_case["workspace"]),
    )
    assert receipt.reason is PublicationReason.AUTHORITY_SNAPSHOT_CHANGED
    assert receipt.effect == "none"
    assert receipt.recovery_required is True


def test_persisted_evidence_snapshot_intent_and_decision_round_trip_strictly(
    authority_case: dict[str, object],
) -> None:
    evidence = authority_case["aligned"]
    assert isinstance(evidence, PublicationAuthorityEvidence)
    snapshot = PublicationAuthorityOperator.snapshot_from_executions(
        authority_case["executions"],
        evidence,
    )
    intent = _intent(
        snapshot.snapshot_sha256,
        PublicationAction.PUBLISH_CURRENT,
        str(authority_case["material"]),
    )
    receipt = PublicationAuthorityOperator.authorize(
        snapshot,
        intent,
        current_workspace_digest=str(authority_case["workspace"]),
    )

    assert PublicationAuthorityEvidence.from_mapping(evidence.to_mapping()) == evidence
    assert PublicationAuthoritySnapshot.from_mapping(snapshot.to_mapping()) == snapshot
    assert PublicationIntent.from_mapping(intent.to_mapping()) == intent
    assert PublicationDecisionReceipt.from_mapping(receipt.to_mapping()) == receipt

    tampered = receipt.to_mapping()
    tampered["effect"] = "none"
    with pytest.raises(ValueError, match="decision receipt digest"):
        PublicationDecisionReceipt.from_mapping(tampered)


def test_receipt_bound_evidence_uses_v2_and_rejects_the_legacy_shape(
    authority_case: dict[str, object],
) -> None:
    evidence = authority_case["aligned"]
    assert isinstance(evidence, PublicationAuthorityEvidence)
    mapping = evidence.to_mapping()

    assert mapping["schemaVersion"] == 2
    assert all(row["schemaVersion"] == 2 for row in mapping["mutations"])
    legacy = copy.deepcopy(mapping)
    legacy["schemaVersion"] = 1
    for row in legacy["mutations"]:
        row["schemaVersion"] = 1
        row.pop("sourceReceiptSha256")

    with pytest.raises(ValueError, match="publication authority evidence.*schema"):
        PublicationAuthorityEvidence.from_mapping(legacy)


# --- multi-path publication evidence (ordered target paths, per-path chains) ---


def _multi_path_case(*, crossed: bool = False) -> dict[str, object]:
    from wtb.domain.publication_authority import PathMaterial, derive_scope_material_sha256

    base_a, after_a = "a1" * 32, "a2" * 32
    base_b, after_b = "b1" * 32, "b2" * 32
    workspace_0, workspace_1, workspace_2 = "4" * 64, "5" * 64, "6" * 64
    policy = (
        ScopePolicyBinding(scope_id="expressions-range", authorized_session_id="source-a"),
        ScopePolicyBinding(scope_id="parser-range", authorized_session_id="source-b"),
    )
    first = ("source-a", "expressions-range", "sqlglot/expressions.py", base_a, after_a)
    second = ("source-b", "parser-range", "sqlglot/parser.py", base_b, after_b)
    if crossed:
        first = ("source-a", "parser-range", "sqlglot/parser.py", base_b, after_b)
        second = ("source-b", "expressions-range", "sqlglot/expressions.py", base_a, after_a)
    mutations = []
    for sequence, (session, scope, path, before, after) in enumerate((first, second), start=1):
        mutation = _mutation(
            session_id=session, scope_id=scope, sequence=sequence,
            before_material=before, after_material=after,
            pre_workspace=workspace_0 if sequence == 1 else workspace_1,
            post_workspace=workspace_1 if sequence == 1 else workspace_2,
        )
        mutations.append(
            ScopeMutationEvidence(**{**{f: getattr(mutation, f) for f in mutation.__dataclass_fields__}, "target_path": path})
        )
    materials = (
        PathMaterial(path="sqlglot/expressions.py", material_sha256=after_a),
        PathMaterial(path="sqlglot/parser.py", material_sha256=after_b),
    )
    evidence = PublicationAuthorityEvidence(
        evidence_generation=1,
        observed_workspace_digest=workspace_2,
        current_material_sha256=derive_scope_material_sha256(materials),
        target_path="publication-material.json",
        policy_bindings=policy,
        mutations=tuple(mutations),
        target_paths=("sqlglot/expressions.py", "sqlglot/parser.py"),
        path_materials=materials,
    )
    return {
        "executions": (_execution("source-a"), _execution("source-b")),
        "evidence": evidence,
        "materials": materials,
        "workspace": workspace_2,
    }


def test_single_path_serialization_is_unchanged_by_the_multi_path_extension(
    authority_case: dict[str, object],
) -> None:
    evidence = authority_case["aligned"]
    assert isinstance(evidence, PublicationAuthorityEvidence)
    assert evidence.target_paths == () and evidence.path_materials == ()
    mapping = evidence.to_mapping()
    assert set(mapping) == {
        "schemaVersion", "evidenceGeneration", "observedWorkspaceDigest", "currentMaterialSha256",
        "targetPath", "policySha256", "policyBindings", "mutations", "evidenceSha256",
    }
    snapshot = PublicationAuthorityOperator.snapshot_from_executions(authority_case["executions"], evidence)
    assert set(snapshot.to_mapping()) == {
        "schemaVersion", "resolution", "resolutionReason", "evidenceGeneration", "evidenceSha256",
        "bindingSetSha256", "observedWorkspaceDigest", "currentMaterialSha256", "targetPath",
        "externalRecoveryRequired", "snapshotSha256",
    }
    intent = _intent(snapshot.snapshot_sha256, PublicationAction.PUBLISH_CURRENT, str(authority_case["material"]))
    assert set(intent.to_mapping()) == {
        "schemaVersion", "action", "selectedMaterialSha256", "authoritySnapshotSha256", "intentSha256",
    }
    assert evidence.effective_target_paths == ("policy.py",)


def test_multi_path_evidence_derives_the_scope_material_from_per_path_digests() -> None:
    from wtb.domain.publication_authority import PathMaterial, derive_scope_material_sha256

    case = _multi_path_case()
    evidence = case["evidence"]
    assert isinstance(evidence, PublicationAuthorityEvidence)
    assert evidence.effective_target_paths == ("sqlglot/expressions.py", "sqlglot/parser.py")
    assert evidence.current_material_sha256 == canonical_json_sha256(
        {"sqlglot/expressions.py": "a2" * 32, "sqlglot/parser.py": "b2" * 32}
    )
    mapping = evidence.to_mapping()
    assert mapping["targetPaths"] == ["sqlglot/expressions.py", "sqlglot/parser.py"]
    assert mapping["pathMaterials"] == [m.to_mapping() for m in case["materials"]]
    assert PublicationAuthorityEvidence.from_mapping(mapping) == evidence
    # Scope digest must be the derived one.
    with pytest.raises(ValueError, match="scope material"):
        PublicationAuthorityEvidence(
            **{f: getattr(evidence, f) for f in evidence.__dataclass_fields__ if f != "current_material_sha256"},
            current_material_sha256="9" * 64,
        )
    # Every path needs a material and the chain must end at it.
    with pytest.raises(ValueError, match="path material"):
        PublicationAuthorityEvidence(
            **{f: getattr(evidence, f) for f in evidence.__dataclass_fields__ if f not in ("path_materials", "current_material_sha256")},
            path_materials=(PathMaterial(path="sqlglot/expressions.py", material_sha256="a2" * 32),),
            current_material_sha256=derive_scope_material_sha256((PathMaterial(path="sqlglot/expressions.py", material_sha256="a2" * 32),)),
        )
    wrong_end = (
        PathMaterial(path="sqlglot/expressions.py", material_sha256="a2" * 32),
        PathMaterial(path="sqlglot/parser.py", material_sha256="c3" * 32),
    )
    with pytest.raises(ValueError, match="does not end at"):
        PublicationAuthorityEvidence(
            **{f: getattr(evidence, f) for f in evidence.__dataclass_fields__ if f not in ("path_materials", "current_material_sha256")},
            path_materials=wrong_end,
            current_material_sha256=derive_scope_material_sha256(wrong_end),
        )
    # A mutation outside the declared paths is rejected.
    stray = list(evidence.mutations)
    stray[1] = ScopeMutationEvidence(**{**{f: getattr(stray[1], f) for f in stray[1].__dataclass_fields__}, "target_path": "sqlglot/tokens.py"})
    with pytest.raises(ValueError, match="outside the publication target paths"):
        PublicationAuthorityEvidence(
            **{f: getattr(evidence, f) for f in evidence.__dataclass_fields__ if f != "mutations"},
            mutations=tuple(stray),
        )


def test_multi_path_operator_permits_aligned_and_denies_crossed_and_per_path_mismatch() -> None:
    from wtb.domain.publication_authority import PathMaterial

    aligned = _multi_path_case()
    evidence = aligned["evidence"]
    snapshot = PublicationAuthorityOperator.snapshot_from_executions(aligned["executions"], evidence)
    assert snapshot.resolution is PublicationAuthorityResolution.AUTHORIZED
    assert snapshot.target_paths == ("sqlglot/expressions.py", "sqlglot/parser.py")
    assert snapshot.path_materials == aligned["materials"]
    assert PublicationAuthoritySnapshot.from_mapping(snapshot.to_mapping()) == snapshot
    mapping = snapshot.to_mapping()
    assert "targetPaths" in mapping and "pathMaterials" in mapping

    intent = _intent(snapshot.snapshot_sha256, PublicationAction.PUBLISH_CURRENT, evidence.current_material_sha256)
    receipt = PublicationAuthorityOperator.authorize(snapshot, intent, current_workspace_digest=str(aligned["workspace"]))
    assert receipt.disposition is PublicationDisposition.PERMIT_PUBLICATION
    assert receipt.effect == "publish_current"
    assert receipt.validated_path_count == 2
    assert PublicationDecisionReceipt.from_mapping(receipt.to_mapping()) == receipt

    # Per-path selection: each selected path material must match the snapshot.
    explicit = PublicationIntent(
        action=PublicationAction.PUBLISH_CURRENT,
        selected_material_sha256=evidence.current_material_sha256,
        authority_snapshot_sha256=snapshot.snapshot_sha256,
        selected_path_materials=aligned["materials"],
    )
    assert PublicationIntent.from_mapping(explicit.to_mapping()) == explicit
    assert explicit.to_mapping()["selectedPathMaterials"] == [m.to_mapping() for m in aligned["materials"]]
    assert PublicationAuthorityOperator.authorize(snapshot, explicit, current_workspace_digest=str(aligned["workspace"])).effect == "publish_current"
    with pytest.raises(ValueError, match="selected path materials"):
        PublicationIntent(
            action=PublicationAction.PUBLISH_CURRENT,
            selected_material_sha256="9" * 64,
            authority_snapshot_sha256=snapshot.snapshot_sha256,
            selected_path_materials=aligned["materials"],
        )
    stale = PublicationIntent(
        action=PublicationAction.PUBLISH_CURRENT,
        selected_material_sha256=canonical_json_sha256({"sqlglot/expressions.py": "a2" * 32, "sqlglot/parser.py": "b1" * 32}),
        authority_snapshot_sha256=snapshot.snapshot_sha256,
        selected_path_materials=(
            PathMaterial(path="sqlglot/expressions.py", material_sha256="a2" * 32),
            PathMaterial(path="sqlglot/parser.py", material_sha256="b1" * 32),
        ),
    )
    denied = PublicationAuthorityOperator.authorize(snapshot, stale, current_workspace_digest=str(aligned["workspace"]))
    assert denied.disposition is PublicationDisposition.DENY_PUBLICATION
    assert denied.reason is PublicationReason.MATERIAL_NOT_AUTHORIZED
    assert denied.replan_required is True

    crossed = _multi_path_case(crossed=True)
    crossed_snapshot = PublicationAuthorityOperator.snapshot_from_executions(crossed["executions"], crossed["evidence"])
    assert crossed_snapshot.resolution is PublicationAuthorityResolution.BLOCKED
    crossed_intent = _intent(crossed_snapshot.snapshot_sha256, PublicationAction.PUBLISH_CURRENT, crossed["evidence"].current_material_sha256)
    held = PublicationAuthorityOperator.authorize(crossed_snapshot, crossed_intent, current_workspace_digest=str(crossed["workspace"]))
    assert held.reason is PublicationReason.AUTHORITY_SCOPE_MISMATCH and held.effect == "reconciliation_hold"
