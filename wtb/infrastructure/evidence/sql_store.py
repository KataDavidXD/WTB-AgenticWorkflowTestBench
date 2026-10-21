"""Shared SQL append-only evidence with explicit attempt admission.

No receipt is authenticated by this store. Only trusted collectors may append
facts or reconcile conflicts. Concrete backends own connection lifetimes.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from functools import lru_cache

from wtb.domain.execution_evidence import (
    AttemptEvidence,
    AttemptIdentity,
    EvidenceError,
    EvidenceEvent,
    encode,
)
from wtb.domain.runtime_identity import canonical_json_sha256

_TERMINAL = {"completed", "failed", "cancelled"}


class SQLEvidenceStore:
    blob_type: str

    def transaction(self):
        raise NotImplementedError

    def _initialize_schema(self, connection=None) -> None:
        with (
            self.transaction() if connection is None else nullcontext(connection) as db
        ):
            db.execute(
                "CREATE TABLE IF NOT EXISTS evidence_schema (version INTEGER NOT NULL)"
            )
            version = db.execute("SELECT version FROM evidence_schema").fetchone()
            if version is None:
                db.execute("INSERT INTO evidence_schema VALUES (1)")
            elif version["version"] != 1:
                raise EvidenceError("unsupported_evidence_schema", "upgrade_store")
            db.execute(
                "CREATE TABLE IF NOT EXISTS evidence_attempts ("
                "execution TEXT, attempt TEXT, identity TEXT NOT NULL, "
                "PRIMARY KEY(execution,attempt))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS evidence_heads ("
                "execution TEXT PRIMARY KEY, attempt TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS evidence_events ("
                "execution TEXT, attempt TEXT, digest TEXT, event_id TEXT, "
                "kind TEXT NOT NULL, body TEXT NOT NULL, "
                "PRIMARY KEY(execution,attempt,digest))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS evidence_resolutions ("
                "execution TEXT, attempt TEXT, revision TEXT, event_id TEXT, "
                "kind TEXT NOT NULL, body TEXT NOT NULL, "
                "PRIMARY KEY(execution,attempt,revision))"
            )

            # Freeze the representative used for receipt binding. Backfill the
            # former digest-order winner so existing approvals remain valid.
            db.execute(
                "CREATE TABLE IF NOT EXISTS evidence_terminal_bindings (execution TEXT, attempt TEXT, content_digest TEXT, event_id TEXT NOT NULL, PRIMARY KEY(execution,attempt,content_digest))"
            )
            rows = db.execute(
                "SELECT * FROM evidence_events WHERE kind IN ('completed','failed','cancelled') ORDER BY digest DESC"
            ).fetchall()
            for row in rows:
                db.execute(
                    "INSERT INTO evidence_terminal_bindings VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                    (
                        row["execution"],
                        row["attempt"],
                        canonical_json_sha256(
                            {"kind": row["kind"], "body": row["body"]}
                        ),
                        row["event_id"],
                    ),
                )

    def register(
        self, identity: AttemptIdentity, *, expected_attempt: str | None
    ) -> None:
        encoded = encode(identity.to_mapping())
        execution, attempt = identity.execution_id, identity.binding.attempt_id
        with self.transaction() as db:
            old = db.execute(
                "SELECT identity FROM evidence_attempts WHERE execution=? AND attempt=?",
                (execution, attempt),
            ).fetchone()
            if old:
                if old["identity"] != encoded:
                    raise EvidenceError("binding_conflict")
                # Repeated admission never resurrects an old head.
                return
            if self._current(db, execution) != expected_attempt:
                raise EvidenceError("attempt_changed", "bind_current_attempt")
            db.execute(
                "INSERT INTO evidence_attempts VALUES (?,?,?)",
                (execution, attempt, encoded),
            )
            db.execute(
                "INSERT INTO evidence_heads VALUES (?,?) ON CONFLICT(execution) "
                "DO UPDATE SET attempt=excluded.attempt",
                (execution, attempt),
            )

    @staticmethod
    def _current(db, execution_id):
        row = db.execute(
            "SELECT attempt FROM evidence_heads WHERE execution=?", (execution_id,)
        ).fetchone()
        return row["attempt"] if row else None

    def current_attempt(self, execution_id: str) -> str | None:
        with self.transaction() as db:
            return self._current(db, execution_id)

    def append(self, execution_id: str, attempt_id: str, event: EvidenceEvent) -> None:
        with self.transaction() as db:
            self._identity(db, execution_id, attempt_id)
            db.execute(
                "INSERT INTO evidence_events VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (
                    execution_id,
                    attempt_id,
                    event.digest,
                    event.event_id,
                    event.kind,
                    event.body_json,
                ),
            )

            if event.kind in _TERMINAL:
                db.execute(
                    "INSERT INTO evidence_terminal_bindings VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                    (
                        execution_id,
                        attempt_id,
                        canonical_json_sha256(
                            {"kind": event.kind, "body": event.body_json}
                        ),
                        event.event_id,
                    ),
                )

    @staticmethod
    def _identity(db, execution_id, attempt_id):
        row = db.execute(
            "SELECT identity FROM evidence_attempts WHERE execution=? AND attempt=?",
            (execution_id, attempt_id),
        ).fetchone()
        if row is None:
            raise EvidenceError("attempt_missing")
        return _decode_identity(row["identity"])

    def get(self, execution_id: str, attempt_id: str) -> AttemptEvidence:
        with self.transaction() as db:
            return self.read(db, execution_id, attempt_id)

    def get_current(self, execution_id: str, attempt_id: str) -> AttemptEvidence:
        with self.transaction() as db:
            if self._current(db, execution_id) != attempt_id:
                raise EvidenceError("attempt_changed", "bind_current_attempt")
            return self.read(db, execution_id, attempt_id)

    def read(self, db, execution_id: str, attempt_id: str) -> AttemptEvidence:
        identity = self._identity(db, execution_id, attempt_id)
        rows = db.execute(
            "SELECT * FROM evidence_events WHERE execution=? AND attempt=? ORDER BY digest",
            (execution_id, attempt_id),
        ).fetchall()
        revision = canonical_json_sha256({"events": [r["digest"] for r in rows]})
        resolution = db.execute(
            "SELECT * FROM evidence_resolutions WHERE execution=? AND attempt=? AND revision=?",
            (execution_id, attempt_id, revision),
        ).fetchone()
        if resolution:
            terminal = _decode_event(
                resolution["event_id"], resolution["kind"], resolution["body"]
            )
            return AttemptEvidence(
                identity, "reconciled", terminal.kind, terminal, revision
            )
        terminal_rows = {
            (r["kind"], r["body"]): r for r in rows if r["kind"] in _TERMINAL
        }
        collision = len({r["event_id"] for r in rows}) != len(rows)
        terminal = None
        outcome = None
        kinds = {r["kind"] for r in rows}
        if collision or len(terminal_rows) > 1:
            state = "unknown"
        elif terminal_rows:
            row = next(iter(terminal_rows.values()))
            representative = db.execute(
                "SELECT event_id FROM evidence_terminal_bindings WHERE execution=? AND attempt=? AND content_digest=?",
                (
                    execution_id,
                    attempt_id,
                    canonical_json_sha256({"kind": row["kind"], "body": row["body"]}),
                ),
            ).fetchone()
            terminal = _decode_event(
                representative["event_id"], row["kind"], row["body"]
            )
            outcome = terminal.kind
            state = "reconciled" if "unknown" in kinds else outcome
        else:
            state = (
                "unknown"
                if "unknown" in kinds
                else ("running" if "running" in kinds else "created")
            )
        return AttemptEvidence(identity, state, outcome, terminal, revision)

    def resolve(
        self,
        execution_id: str,
        attempt_id: str,
        terminal: EvidenceEvent,
        *,
        expected_revision: str,
    ) -> None:
        """Append trusted reconciliation; any new fact invalidates this resolution."""
        if terminal.kind not in _TERMINAL:
            raise EvidenceError("terminal_required")
        with self.transaction() as db:
            current = self.read(db, execution_id, attempt_id)
            if current.revision != expected_revision:
                raise EvidenceError("evidence_changed")
            old = db.execute(
                "SELECT kind,body FROM evidence_resolutions WHERE execution=? AND attempt=? AND revision=?",
                (execution_id, attempt_id, expected_revision),
            ).fetchone()
            if old and (old["kind"], old["body"]) != (
                terminal.kind,
                terminal.body_json,
            ):
                raise EvidenceError("reconciliation_conflict")
            db.execute(
                "INSERT INTO evidence_resolutions VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (
                    execution_id,
                    attempt_id,
                    expected_revision,
                    terminal.event_id,
                    terminal.kind,
                    terminal.body_json,
                ),
            )


@lru_cache(maxsize=128)
def _decode_identity(encoded):
    return AttemptIdentity.from_mapping(json.loads(encoded))


@lru_cache(maxsize=128)
def _decode_event(event_id, kind, body):
    return EvidenceEvent(event_id, kind, json.loads(body))
