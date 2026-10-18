"""Single-host SQLite evidence transaction boundary."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from wtb.infrastructure.evidence.sql_store import SQLEvidenceStore


class SQLiteEvidenceStore(SQLEvidenceStore):
    blob_type = "BLOB"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._initialize_schema()

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
