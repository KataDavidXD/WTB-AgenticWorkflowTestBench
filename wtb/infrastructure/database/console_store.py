"""Additive SQLite read model for console jobs, events and paginated history."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class ConsoleStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS console_records (
                    kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS console_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, execution_id TEXT,
                    payload TEXT NOT NULL);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def put(self, kind, item):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO console_records VALUES (?,?,?)",
                (kind, item["id"], json.dumps(item, ensure_ascii=False, default=str)),
            )

    def get(self, kind, id):
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM console_records WHERE kind=? AND id=?", (kind, id)
            ).fetchone()
        if not row:
            raise KeyError(id)
        return json.loads(row[0])

    def list(self, kind):
        with self.connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT payload FROM console_records WHERE kind=? ORDER BY rowid DESC",
                    (kind,),
                )
            ]

    def event(self, item):
        with self.connect() as db:
            return db.execute(
                "INSERT INTO console_events(execution_id,payload) VALUES (?,?)",
                (
                    item.get("executionId"),
                    json.dumps(item, ensure_ascii=False, default=str),
                ),
            ).lastrowid

    def events(self, execution_id=None, limit=50, offset=0):
        where, args = (
            (" WHERE execution_id=?", [execution_id]) if execution_id else ("", [])
        )
        with self.connect() as db:
            total = db.execute(
                "SELECT count(*) FROM console_events" + where, args
            ).fetchone()[0]
            rows = db.execute(
                "SELECT seq,payload FROM console_events"
                + where
                + " ORDER BY seq DESC LIMIT ? OFFSET ?",
                args + [limit, offset],
            ).fetchall()
        return {
            "items": [dict(json.loads(row[1]), seq=row[0]) for row in rows],
            "total": total,
        }
