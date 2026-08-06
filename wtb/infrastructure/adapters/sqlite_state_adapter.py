"""Durable SQLite state adapter for graphless WTB node execution.

The ordinary ``InMemoryStateAdapter`` remains the lightweight default.
This adapter preserves the same domain contract while persisting sessions,
checkpoints, and node boundaries in execution-local SQLite tables. Its table
names are disjoint from LangGraph's saver tables, so both backends may share an
actor checkpoint database without interpreting each other's rows.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, Union

from wtb.domain.interfaces.state_adapter import CheckpointTrigger
from wtb.domain.models import ExecutionState

from .inmemory_state_adapter import (
    InMemoryCheckpoint,
    InMemoryNodeBoundary,
    InMemoryStateAdapter,
)


class SqliteStateAdapter(InMemoryStateAdapter):
    """Reopenable node-mode adapter backed by an execution-local SQLite DB."""

    state_adapter_backend = "node_sqlite"

    def __init__(self, storage_path: Union[str, Path]):
        super().__init__()
        self.storage_path = Path(storage_path).expanduser().resolve()
        self._config = SimpleNamespace(connection_string=str(self.storage_path))
        self._storage_lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._open_storage()

    @staticmethod
    def _serialize_state(state: ExecutionState) -> str:
        """Use the same JSON-compatible state contract as ExecutionRepository."""
        return json.dumps(state.to_dict(), ensure_ascii=False)

    @staticmethod
    def _deserialize_state(payload: str) -> ExecutionState:
        return ExecutionState.from_dict(json.loads(payload))

    def _open_storage(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(self.storage_path),
            timeout=5.0,
            check_same_thread=False,
        )
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS wtb_node_state_sessions (
                    session_id TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL UNIQUE,
                    initial_state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS wtb_node_state_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    trigger_type TEXT NOT NULL,
                    name TEXT,
                    metadata_json TEXT NOT NULL,
                    step INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id)
                        REFERENCES wtb_node_state_sessions(session_id)
                        ON DELETE CASCADE,
                    UNIQUE(session_id, step)
                );

                CREATE INDEX IF NOT EXISTS idx_wtb_node_state_checkpoints_session
                    ON wtb_node_state_checkpoints(session_id, step);

                CREATE TABLE IF NOT EXISTS wtb_node_state_boundaries (
                    boundary_id TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    entry_checkpoint_id TEXT NOT NULL,
                    exit_checkpoint_id TEXT,
                    node_status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    error_message TEXT,
                    FOREIGN KEY(session_id)
                        REFERENCES wtb_node_state_sessions(session_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_wtb_node_state_boundaries_session
                    ON wtb_node_state_boundaries(session_id, started_at);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_wtb_node_state_one_open_node
                    ON wtb_node_state_boundaries(session_id, node_id)
                    WHERE node_status = 'started';

                CREATE UNIQUE INDEX IF NOT EXISTS idx_wtb_node_state_one_open_session
                    ON wtb_node_state_boundaries(session_id)
                    WHERE node_status = 'started';

                CREATE TABLE IF NOT EXISTS wtb_node_state_resume_claims (
                    session_id TEXT PRIMARY KEY,
                    resume_token TEXT NOT NULL,
                    claim_id TEXT UNIQUE,
                    boundary_id TEXT UNIQUE,
                    node_id TEXT,
                    claimed_at TEXT,
                    FOREIGN KEY(session_id)
                        REFERENCES wtb_node_state_sessions(session_id)
                        ON DELETE CASCADE
                );
                """
            )
            connection.commit()
        except Exception:
            connection.close()
            raise
        self._connection = connection
        self._reload_from_storage()

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("SQLite node checkpoint adapter is closed")
        return self._connection

    def _reload_from_storage(self) -> None:
        """Refresh committed rows before execution/session-scoped operations."""
        with self._storage_lock:
            connection = self._require_connection()
            sessions: dict[str, dict[str, Any]] = {}
            checkpoints: dict[str, InMemoryCheckpoint] = {}
            boundaries: dict[str, InMemoryNodeBoundary] = {}

            for row in connection.execute(
                """
                SELECT session_id, execution_id, initial_state_json, created_at
                FROM wtb_node_state_sessions
                ORDER BY created_at, session_id
                """
            ):
                sessions[row[0]] = {
                    "id": row[0],
                    "execution_id": row[1],
                    "initial_state": self._deserialize_state(row[2]),
                    "created_at": row[3],
                }

            for row in connection.execute(
                """
                SELECT checkpoint_id, session_id, state_json, node_id,
                       trigger_type, name, metadata_json, step, created_at
                FROM wtb_node_state_checkpoints
                ORDER BY session_id, step
                """
            ):
                checkpoint = InMemoryCheckpoint(
                    id=row[0],
                    session_id=row[1],
                    state=self._deserialize_state(row[2]),
                    node_id=row[3],
                    trigger=CheckpointTrigger(row[4]),
                    name=row[5],
                    metadata=json.loads(row[6]),
                    step=int(row[7]),
                )
                checkpoint.created_at = row[8]
                checkpoints[checkpoint.id] = checkpoint

            for row in connection.execute(
                """
                SELECT boundary_id, execution_id, session_id, node_id,
                       entry_checkpoint_id, exit_checkpoint_id, node_status,
                       started_at, completed_at, error_message
                FROM wtb_node_state_boundaries
                ORDER BY session_id, started_at, boundary_id
                """
            ):
                boundary = InMemoryNodeBoundary(
                    id=row[0],
                    execution_id=row[1],
                    session_id=row[2],
                    node_id=row[3],
                    entry_checkpoint_id=row[4],
                )
                boundary.exit_checkpoint_id = row[5]
                boundary.node_status = row[6]
                boundary.started_at = row[7]
                boundary.completed_at = row[8]
                boundary.error_message = row[9]
                boundaries[boundary.id] = boundary

            self._sessions = sessions
            self._checkpoints = checkpoints
            self._boundaries = boundaries

    def _max_step_for_session(self, session_id: str) -> int:
        return max(
            (
                checkpoint.step
                for checkpoint in self._checkpoints.values()
                if checkpoint.session_id == session_id
            ),
            default=0,
        )

    def _refresh_current_session(
        self,
        session_id: str | None,
        execution_id: str | None,
    ) -> None:
        self._reload_from_storage()
        self._current_session_id = session_id
        self._current_execution_id = execution_id
        self._step_counter = (
            self._max_step_for_session(session_id) if session_id else 0
        )

    def initialize_session(
        self,
        execution_id: str,
        initial_state: ExecutionState,
    ) -> str | None:
        session_id = f"wtb-{execution_id}"
        payload = self._serialize_state(initial_state)
        created_at = datetime.now().isoformat()
        with self._storage_lock:
            connection = self._require_connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO wtb_node_state_sessions (
                        session_id, execution_id, initial_state_json, created_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        execution_id = excluded.execution_id,
                        initial_state_json = excluded.initial_state_json
                    """,
                    (session_id, execution_id, payload, created_at),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._refresh_current_session(session_id, execution_id)
        return session_id

    def set_current_session(
        self,
        session_id: str,
        execution_id: str | None = None,
    ) -> bool:
        self._reload_from_storage()
        session = self._sessions.get(session_id)
        if session is None and execution_id:
            reconstructed_id = f"wtb-{execution_id}"
            session = self._sessions.get(reconstructed_id)
            if session is not None:
                session_id = reconstructed_id
        if session is None:
            return False
        stored_execution_id = session["execution_id"]
        if execution_id is not None and stored_execution_id != execution_id:
            return False
        self._current_session_id = session_id
        self._current_execution_id = stored_execution_id
        self._step_counter = self._max_step_for_session(session_id)
        return True

    def get_recovery_head(self) -> dict[str, Any] | None:
        """Return the latest durable node boundary for the active execution.

        SQLite insertion order is the recovery order. A completed head is only
        usable when its exit checkpoint still exists and belongs to the active
        session; recovery never guesses a replacement checkpoint.
        """
        session_id = self._current_session_id
        execution_id = self._current_execution_id
        if session_id is None or execution_id is None:
            raise RuntimeError("No active session")

        with self._storage_lock:
            connection = self._require_connection()
            row = connection.execute(
                """
                SELECT rowid, boundary_id, execution_id, node_id,
                       exit_checkpoint_id, node_status, started_at,
                       completed_at, error_message
                FROM wtb_node_state_boundaries
                WHERE session_id = ?
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            if row[2] != execution_id:
                raise RuntimeError(
                    "Latest node boundary does not belong to the active execution"
                )

            recovered_state: ExecutionState | None = None
            exit_checkpoint_id = row[4]
            if row[5] == "completed":
                checkpoint_row = None
                if exit_checkpoint_id:
                    checkpoint_row = connection.execute(
                        """
                        SELECT session_id, state_json
                        FROM wtb_node_state_checkpoints
                        WHERE checkpoint_id = ?
                        """,
                        (exit_checkpoint_id,),
                    ).fetchone()
                if checkpoint_row is None or checkpoint_row[0] != session_id:
                    raise RuntimeError(
                        "Completed recovery boundary has no valid owned exit checkpoint"
                    )
                try:
                    recovered_state = self._deserialize_state(checkpoint_row[1])
                except Exception as error:
                    raise RuntimeError(
                        "Completed recovery boundary has no valid owned exit checkpoint"
                    ) from error

        return {
            "sequence": int(row[0]),
            "boundary_id": row[1],
            "execution_id": row[2],
            "node_id": row[3],
            "exit_checkpoint_id": exit_checkpoint_id,
            "status": row[5],
            "started_at": row[6],
            "completed_at": row[7],
            "error_message": row[8],
            "state": recovered_state,
        }

    def save_checkpoint(
        self,
        state: ExecutionState,
        node_id: str,
        trigger: CheckpointTrigger,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        session_id = self._current_session_id
        execution_id = self._current_execution_id
        if session_id is None:
            raise RuntimeError("No active session")

        checkpoint_id = str(uuid.uuid4())
        state_payload = self._serialize_state(state)
        metadata_payload = json.dumps(metadata or {}, ensure_ascii=False)
        created_at = datetime.now().isoformat()
        with self._storage_lock:
            connection = self._require_connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT COALESCE(MAX(step), 0)
                    FROM wtb_node_state_checkpoints
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                step = int(row[0]) + 1
                connection.execute(
                    """
                    INSERT INTO wtb_node_state_checkpoints (
                        checkpoint_id, session_id, state_json, node_id,
                        trigger_type, name, metadata_json, step, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        checkpoint_id,
                        session_id,
                        state_payload,
                        node_id,
                        trigger.value,
                        name,
                        metadata_payload,
                        step,
                        created_at,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._refresh_current_session(session_id, execution_id)
        return checkpoint_id

    def rollback(self, to_checkpoint_id: str) -> ExecutionState:
        checkpoint = self._require_owned_checkpoint(to_checkpoint_id)
        # A rollback changes active state, not durable ordering. A later resume
        # allocates after the DB maximum rather than reusing old step numbers.
        self._step_counter = self._max_step_for_session(checkpoint.session_id)
        return deepcopy(checkpoint.state)

    def prepare_resume(self, resume_token: str) -> bool:
        """Publish the only resume token currently valid for this session."""
        session_id = self._current_session_id
        if session_id is None:
            raise RuntimeError("No active session")
        if not isinstance(resume_token, str) or not resume_token:
            return False

        prepared = False
        with self._storage_lock:
            connection = self._require_connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT boundary_id
                    FROM wtb_node_state_boundaries
                    WHERE session_id = ? AND node_status = 'started'
                    LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO wtb_node_state_resume_claims (
                            session_id, resume_token, claim_id, boundary_id,
                            node_id, claimed_at
                        ) VALUES (?, ?, NULL, NULL, NULL, NULL)
                        ON CONFLICT(session_id) DO UPDATE SET
                            resume_token = excluded.resume_token,
                            claim_id = NULL,
                            boundary_id = NULL,
                            node_id = NULL,
                            claimed_at = NULL
                        """,
                        (session_id, resume_token),
                    )
                    prepared = True
                    connection.commit()
                else:
                    connection.rollback()
            except Exception:
                connection.rollback()
                raise
        return prepared

    def claim_resume(
        self,
        resume_token: str,
    ) -> Union[str, Literal[False]]:
        """Atomically consume one explicit-resume token for this session."""
        session_id = self._current_session_id
        if session_id is None:
            raise RuntimeError("No active session")
        if not isinstance(resume_token, str) or not resume_token:
            return False

        claim_id = str(uuid.uuid4())
        claimed_at = datetime.now().isoformat()
        claimed = False
        with self._storage_lock:
            connection = self._require_connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT boundary_id
                    FROM wtb_node_state_boundaries
                    WHERE session_id = ? AND node_status = 'started'
                    LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                if existing is None:
                    cursor = connection.execute(
                        """
                        UPDATE wtb_node_state_resume_claims
                        SET claim_id = ?, claimed_at = ?
                        WHERE session_id = ? AND resume_token = ?
                              AND claim_id IS NULL
                        """,
                        (claim_id, claimed_at, session_id, resume_token),
                    )
                    claimed = cursor.rowcount == 1
                if claimed:
                    connection.commit()
                else:
                    connection.rollback()
            except Exception:
                connection.rollback()
                raise
        return claim_id if claimed else False

    def mark_node_started(
        self,
        node_id: str,
        entry_checkpoint_id: str,
        *,
        expected_predecessor_checkpoint_id: str | None = None,
        enforce_predecessor: bool = False,
        resume_claim_id: str | None = None,
    ) -> Union[str, Literal[False]]:
        session_id = self._current_session_id
        execution_id = self._current_execution_id
        if session_id is None:
            raise RuntimeError("No active session")
        self._require_owned_checkpoint(entry_checkpoint_id)

        boundary_id = str(uuid.uuid4())
        started_at = datetime.now().isoformat()
        claimed = False
        with self._storage_lock:
            connection = self._require_connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                latest = connection.execute(
                    """
                    SELECT node_status, exit_checkpoint_id
                    FROM wtb_node_state_boundaries
                    WHERE session_id = ?
                    ORDER BY rowid DESC
                    LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                existing = connection.execute(
                    """
                    SELECT boundary_id
                    FROM wtb_node_state_boundaries
                    WHERE session_id = ? AND node_status = 'started'
                    LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                predecessor_matches = True
                resume_claim_available = True
                if enforce_predecessor and resume_claim_id is None:
                    durable_predecessor = (
                        latest[1]
                        if latest is not None and latest[0] == "completed"
                        else None
                    )
                    predecessor_matches = (
                        latest is None or latest[0] == "completed"
                    ) and durable_predecessor == expected_predecessor_checkpoint_id
                elif enforce_predecessor:
                    resume_claim = connection.execute(
                        """
                        SELECT claim_id
                        FROM wtb_node_state_resume_claims
                        WHERE session_id = ? AND claim_id = ?
                              AND boundary_id IS NULL
                        """,
                        (session_id, resume_claim_id),
                    ).fetchone()
                    resume_claim_available = resume_claim is not None
                if (
                    existing is None
                    and predecessor_matches
                    and resume_claim_available
                ):
                    connection.execute(
                        """
                        INSERT INTO wtb_node_state_boundaries (
                            boundary_id, execution_id, session_id, node_id,
                            entry_checkpoint_id, node_status, started_at
                        ) VALUES (?, ?, ?, ?, ?, 'started', ?)
                        """,
                        (
                            boundary_id,
                            execution_id or "",
                            session_id,
                            node_id,
                            entry_checkpoint_id,
                            started_at,
                        ),
                    )
                    if resume_claim_id is not None:
                        cursor = connection.execute(
                            """
                            UPDATE wtb_node_state_resume_claims
                            SET boundary_id = ?, node_id = ?
                            WHERE session_id = ? AND claim_id = ?
                                  AND boundary_id IS NULL
                            """,
                            (
                                boundary_id,
                                node_id,
                                session_id,
                                resume_claim_id,
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise RuntimeError(
                                "Resume claim changed during node boundary claim"
                            )
                    claimed = True
                    connection.commit()
                else:
                    connection.rollback()
            except Exception:
                connection.rollback()
                raise
        self._refresh_current_session(session_id, execution_id)
        return boundary_id if claimed else False

    def _transition_open_boundary(
        self,
        node_id: str,
        *,
        node_status: str,
        exit_checkpoint_id: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        """Atomically claim one started boundary's terminal transition."""
        session_id = self._current_session_id
        execution_id = self._current_execution_id
        if session_id is None:
            return False

        completed_at = datetime.now().isoformat()
        updated = False
        with self._storage_lock:
            connection = self._require_connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT boundary_id
                    FROM wtb_node_state_boundaries
                    WHERE session_id = ? AND node_id = ?
                          AND node_status = 'started'
                    ORDER BY started_at DESC, boundary_id DESC
                    LIMIT 1
                    """,
                    (session_id, node_id),
                ).fetchone()
                if row is not None:
                    cursor = connection.execute(
                        """
                        UPDATE wtb_node_state_boundaries
                        SET exit_checkpoint_id = ?, node_status = ?,
                            completed_at = ?, error_message = ?
                        WHERE boundary_id = ? AND session_id = ?
                              AND node_status = 'started'
                        """,
                        (
                            exit_checkpoint_id,
                            node_status,
                            completed_at,
                            error_message,
                            row[0],
                            session_id,
                        ),
                    )
                    updated = cursor.rowcount == 1
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        # Both winner and loser must discard the cached started row so later
        # reads observe the committed terminal owner.
        self._refresh_current_session(session_id, execution_id)
        return updated

    def mark_node_completed(self, node_id: str, exit_checkpoint_id: str) -> bool:
        self._require_owned_checkpoint(exit_checkpoint_id)
        return self._transition_open_boundary(
            node_id,
            node_status="completed",
            exit_checkpoint_id=exit_checkpoint_id,
        )

    def mark_node_failed(self, node_id: str, error_message: str) -> bool:
        return self._transition_open_boundary(
            node_id,
            node_status="failed",
            error_message=error_message,
        )

    def cleanup(self, session_id: str, keep_latest: int = 5) -> int:
        self._reload_from_storage()
        session_checkpoints = [
            checkpoint
            for checkpoint in self._checkpoints.values()
            if checkpoint.session_id == session_id and checkpoint.is_auto
        ]
        session_checkpoints.sort(key=lambda checkpoint: checkpoint.created_at, reverse=True)
        checkpoint_ids = [checkpoint.id for checkpoint in session_checkpoints[keep_latest:]]
        if not checkpoint_ids:
            return 0

        placeholders = ", ".join("?" for _ in checkpoint_ids)
        with self._storage_lock:
            connection = self._require_connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    f"DELETE FROM wtb_node_state_checkpoints "
                    f"WHERE checkpoint_id IN ({placeholders})",
                    checkpoint_ids,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._refresh_current_session(
            self._current_session_id,
            self._current_execution_id,
        )
        return len(checkpoint_ids)

    def reset(self) -> None:
        with self._storage_lock:
            connection = self._require_connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM wtb_node_state_resume_claims")
                connection.execute("DELETE FROM wtb_node_state_boundaries")
                connection.execute("DELETE FROM wtb_node_state_checkpoints")
                connection.execute("DELETE FROM wtb_node_state_sessions")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        super().reset()

    def close(self) -> None:
        """Close the SQLite handle without deleting durable checkpoint rows."""
        with self._storage_lock:
            connection = self._connection
            if connection is None:
                return
            self._connection = None
            connection.close()
