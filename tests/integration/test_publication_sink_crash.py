"""Process termination at SQLite commit boundaries; no product crash hooks."""

import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.evidence.test_publication_sink import setup_sink

WORKER = """
import os, sys
from contextlib import contextmanager
from wtb.infrastructure.evidence.sqlite_store import SQLiteEvidenceStore
from wtb.infrastructure.evidence.publication_sink import SQLitePublicationSink
store = SQLiteEvidenceStore(sys.argv[1])
sink = SQLitePublicationSink(store)
original = store.transaction
@contextmanager
def crash_transaction():
    with original() as db:
        yield db
        if sys.argv[3] == 'before': os._exit(71)
    if sys.argv[3] == 'after': os._exit(72)
store.transaction = crash_transaction
result = sink.publish(sys.argv[2])
assert result.allowed
"""


@pytest.mark.parametrize(
    "boundary,code,initial_effects", [("before", 71, 0), ("after", 72, 1)]
)
def test_real_process_crash_and_reopen(tmp_path, boundary, code, initial_effects):
    sink = setup_sink(tmp_path / "state.sqlite")
    approval = sink.approve("artifact.txt")
    result = subprocess.run(
        [sys.executable, "-c", WORKER, str(sink.store.path), approval, boundary],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == code, result.stderr.decode()
    with sink.store.transaction() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM publication_effects").fetchone()[0]
            == initial_effects
        )
        assert (
            db.execute("SELECT consumed FROM publication_approvals").fetchone()[0]
            == initial_effects
        )
    assert sink.publish(approval).allowed
    with sink.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM publication_effects").fetchone()[0] == 1


def test_real_concurrent_process_consumption(tmp_path):
    sink = setup_sink(tmp_path / "state.sqlite")
    approval = sink.approve("artifact.txt")
    commands = [
        subprocess.Popen(
            [sys.executable, "-c", WORKER, str(sink.store.path), approval, "none"],
            cwd=Path(__file__).resolve().parents[2],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    for process in commands:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr.decode()
    with sink.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM publication_effects").fetchone()[0] == 1
