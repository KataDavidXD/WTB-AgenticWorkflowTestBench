"""Transactional SQL publication: checked bytes are the actual stored effect.

No filesystem/API/Git side effect is performed inside this contract. All policy,
attempt, receipt, material and consumption writers use the same backend transaction boundary.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from functools import lru_cache

from wtb.application.services.evidence_collector import EvidenceCollector
from wtb.domain.execution_evidence import EvidenceError, encode
from wtb.domain.publication_authority import (
    PublicationAction,
    PublicationAuthorityEvidence,
    PublicationAuthorityOperator,
    PublicationDisposition,
    PublicationIntent,
    ScopeMutationEvidence,
    ScopePolicyBinding,
    _require_safe_id,
    _require_safe_relative_path,
)
from wtb.infrastructure.evidence.sql_store import SQLEvidenceStore
from wtb.infrastructure.evidence.sqlite_store import SQLiteEvidenceStore


@dataclass(frozen=True)
class PublicationResult:
    allowed: bool
    new_effect: bool
    reason: str
    recovery: str | None = None


class SQLPublicationSink:
    def __init__(self, store: SQLEvidenceStore) -> None:
        self.store = store
        # Per-instance bounded cache of frozen inputs/outputs. Every call still
        # reads current policy, attempt head, event set and material bytes.
        self._build_snapshot = lru_cache(maxsize=128)(self._uncached_snapshot)
        with store.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS publication_policies ("
                "scope TEXT PRIMARY KEY, actor TEXT NOT NULL, generation BIGINT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS publication_material ("
                f"target TEXT PRIMARY KEY, payload {store.blob_type} NOT NULL, mutations TEXT NOT NULL, version BIGINT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS publication_material_history ("
                f"target TEXT, version BIGINT, payload {store.blob_type} NOT NULL, mutations TEXT NOT NULL, PRIMARY KEY(target,version))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS publication_approvals ("
                "id TEXT PRIMARY KEY, target TEXT NOT NULL, intent TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS publication_approval_material_versions ("
                "approval TEXT PRIMARY KEY, target TEXT NOT NULL, version BIGINT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS publication_effects ("
                f"approval TEXT PRIMARY KEY, payload {store.blob_type} NOT NULL, decision TEXT NOT NULL)"
            )

    def update_policy(self, scope: str, actor: str, *, expected_generation: int) -> int:
        """Authority-owner port. Atomic CAS also invalidates revoke/regrant ABA."""
        ScopePolicyBinding(scope, actor)
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT generation FROM publication_policies WHERE scope=?", (scope,)
            ).fetchone()
            generation = row["generation"] if row else 0
            if generation != expected_generation:
                raise EvidenceError("authority_changed", "request_authorization")
            db.execute(
                "INSERT INTO publication_policies VALUES (?,?,?) ON CONFLICT(scope) "
                "DO UPDATE SET actor=excluded.actor,generation=excluded.generation",
                (scope, actor, generation + 1),
            )
            return generation + 1

    def record_material(
        self,
        target: str,
        payload: bytes,
        mutations: tuple[ScopeMutationEvidence, ...],
        *,
        expected_version: int,
    ) -> int:
        """Trusted workspace-owner port; binds immutable bytes to exact facts.

        This call records material, not permission. It cannot authorize a failed
        attempt or an actor absent from the authority-owner policy.

        Every successful call creates a new material generation, including when
        payload and mutations equal the current record. expected_version is a
        compare-and-swap precondition, not a content-based deduplication key.
        Material owners must call this port for each new generation; suppressing
        that call because bytes match prevents the library from observing it.
        """
        _require_safe_relative_path(target, "target")
        if (
            not isinstance(payload, bytes)
            or not mutations
            or any(
                not isinstance(m, ScopeMutationEvidence) or m.target_path != target
                for m in mutations
            )
        ):
            raise EvidenceError("material_mismatch", "bind_current_material")
        if hashlib.sha256(payload).hexdigest() != mutations[-1].after_material_sha256:
            raise EvidenceError("material_mismatch", "bind_current_material")
        try:
            # Structural validation only; these actor bindings grant no authority.
            PublicationAuthorityEvidence(
                1,
                mutations[-1].post_workspace_digest,
                hashlib.sha256(payload).hexdigest(),
                target,
                tuple(
                    ScopePolicyBinding(m.scope_id, m.actor_session_id)
                    for m in mutations
                ),
                mutations,
            )
        except (ValueError, TypeError) as error:
            raise EvidenceError(
                "invalid_material_chain", "bind_current_material"
            ) from error
        encoded = encode({"mutations": [m.to_mapping() for m in mutations]})
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT version FROM publication_material WHERE target=?", (target,)
            ).fetchone()
            version = row["version"] if row else 0
            if version != expected_version:
                raise EvidenceError("material_changed", "bind_current_material")
            db.execute(
                "INSERT INTO publication_material_history VALUES (?,?,?,?)",
                (target, version + 1, payload, encoded),
            )
            db.execute(
                "INSERT INTO publication_material VALUES (?,?,?,?) ON CONFLICT(target) "
                "DO UPDATE SET payload=excluded.payload,mutations=excluded.mutations,version=excluded.version",
                (target, payload, encoded, version + 1),
            )
            return version + 1

    @staticmethod
    def material(db, target):
        row = db.execute(
            "SELECT * FROM publication_material WHERE target=?", (target,)
        ).fetchone()
        if row is None:
            raise EvidenceError("material_missing", "bind_current_material")
        mutations = _decode_mutations(row["mutations"])
        if (
            hashlib.sha256(row["payload"]).hexdigest()
            != mutations[-1].after_material_sha256
        ):
            raise EvidenceError("material_mismatch", "bind_current_material")
        return row["payload"], mutations, row["version"]

    def _snapshot_facts(self, db, target):
        payload, mutations, _ = self.material(db, target)
        policies, executions, generation = [], [], 0
        for mutation in mutations:
            row = db.execute(
                "SELECT * FROM publication_policies WHERE scope=?", (mutation.scope_id,)
            ).fetchone()
            if row is None:
                raise EvidenceError("authority_missing", "request_authorization")
            policies.append(ScopePolicyBinding(mutation.scope_id, row["actor"]))
            generation += row["generation"]
            if self.store._current(db, mutation.execution_id) != mutation.attempt_id:
                raise EvidenceError("attempt_changed", "bind_current_attempt")
            observed = self.store.read(db, mutation.execution_id, mutation.attempt_id)
            if observed.identity.scope_id != mutation.scope_id:
                raise EvidenceError("scope_binding_mismatch")
            executions.append(observed)
        evidence = PublicationAuthorityEvidence(
            generation,
            mutations[-1].post_workspace_digest,
            mutations[-1].after_material_sha256,
            target,
            tuple(policies),
            mutations,
        )
        return executions, evidence, payload

    def snapshot_inputs(self, db, target):
        observed, evidence, payload = self._snapshot_facts(db, target)
        return [EvidenceCollector.project(item) for item in observed], evidence, payload

    @staticmethod
    def _uncached_snapshot(evidence, observed):
        return PublicationAuthorityOperator.snapshot_from_executions(
            [EvidenceCollector.project(item) for item in observed], evidence
        )

    def snapshot(self, db, target):
        observed, evidence, payload = self._snapshot_facts(db, target)
        return self._build_snapshot(evidence, tuple(observed)), payload

    def approve(self, target: str) -> str:
        with self.store.transaction() as db:
            snapshot, _ = self.snapshot(db, target)
            # Material generations distinguish repeated records with identical
            # bytes and evidence. Capture the trusted store's version here;
            # callers cannot omit or substitute the approval's version binding.
            _, _, material_version = self.material(db, target)
            intent = PublicationIntent(
                PublicationAction.PUBLISH_CURRENT,
                snapshot.current_material_sha256,
                snapshot.snapshot_sha256,
            )
            decision = PublicationAuthorityOperator.authorize(
                snapshot,
                intent,
                current_workspace_digest=snapshot.observed_workspace_digest,
            )
            if decision.disposition is not PublicationDisposition.PERMIT_PUBLICATION:
                raise EvidenceError(
                    decision.reason.value,
                    "query_runtime"
                    if decision.external_recovery_required
                    else "request_authorization",
                )
            approval = uuid.uuid4().hex
            db.execute(
                "INSERT INTO publication_approvals(id,target,intent) VALUES (?,?,?)",
                (approval, target, encode(intent.to_mapping())),
            )
            db.execute(
                "INSERT INTO publication_approval_material_versions VALUES (?,?,?)",
                (approval, target, material_version),
            )
            return approval

    def verify(self, db, approval):
        row = db.execute(
            "SELECT * FROM publication_approvals WHERE id=?", (approval,)
        ).fetchone()
        if row is None:
            raise EvidenceError("approval_missing", "request_authorization")
        bound = db.execute(
            "SELECT target,version FROM publication_approval_material_versions "
            "WHERE approval=?", (approval,),
        ).fetchone()
        if bound is None or bound["target"] != row["target"]:
            # An old pending approval has no reconstructible generation. Do
            # not infer its version from today's material during an upgrade.
            raise EvidenceError("approval_version_missing", "request_authorization")
        _, _, current_version = self.material(db, row["target"])
        if current_version != bound["version"]:
            raise EvidenceError("material_changed", "bind_current_material")
        snapshot, payload = self.snapshot(db, row["target"])
        intent = PublicationIntent.from_mapping(json.loads(row["intent"]))
        decision = PublicationAuthorityOperator.authorize(
            snapshot,
            intent,
            current_workspace_digest=snapshot.observed_workspace_digest,
        )
        return decision, payload

    def publish(self, approval: str) -> PublicationResult:
        _require_safe_id(approval, "approval")
        with self.store.transaction() as db:
            done = db.execute(
                "SELECT 1 FROM publication_effects WHERE approval=?", (approval,)
            ).fetchone()
            if done:
                return PublicationResult(True, False, "persisted_replay")
            consumed = db.execute(
                "SELECT consumed FROM publication_approvals WHERE id=?", (approval,)
            ).fetchone()
            if consumed and consumed["consumed"]:
                return PublicationResult(
                    False, False, "external_delivery_reserved", "query_sink"
                )
            try:
                decision, payload = self.verify(db, approval)
            except EvidenceError as error:
                return PublicationResult(False, False, error.code, error.recovery)
            if decision.disposition is not PublicationDisposition.PERMIT_PUBLICATION:
                return PublicationResult(
                    False,
                    False,
                    decision.reason.value,
                    "query_runtime"
                    if decision.external_recovery_required
                    else "request_authorization",
                )
            # This INSERT is the effect itself. A killed process before commit
            # rolls it back along with consumption; after commit both survive.
            db.execute(
                "INSERT INTO publication_effects VALUES (?,?,?)",
                (approval, payload, encode(decision.to_mapping())),
            )
            db.execute(
                "UPDATE publication_approvals SET consumed=1 WHERE id=?", (approval,)
            )
            return PublicationResult(True, True, decision.reason.value)


@lru_cache(maxsize=128)
def _decode_mutations(encoded):
    """Cache immutable content only; current material is still read and hashed."""
    return tuple(
        ScopeMutationEvidence.from_mapping(m) for m in json.loads(encoded)["mutations"]
    )


class SQLitePublicationSink(SQLPublicationSink):
    """Backward-compatible SQLite publication entry point."""

    def __init__(self, store: SQLiteEvidenceStore) -> None:
        super().__init__(store)
