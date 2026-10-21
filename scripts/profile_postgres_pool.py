"""Alternate pinned short-connection and pooled backends on one local database."""

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time
import types
import uuid
from contextlib import ExitStack
from pathlib import Path

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.unit.evidence.test_publication_sink import setup_sink
from wtb.infrastructure.evidence.postgres_store import PostgresEvidenceStore
from wtb.infrastructure.evidence.publication_sink import SQLPublicationSink

BASE = "cb5242b7a8b70a2860ed4160691a4ad0cb4a90dd"


def summarize(values):
    return {
        "n": len(values),
        "median_ms": statistics.median(values),
        "p95_ms": sorted(values)[min(len(values) - 1, int(len(values) * 0.95))],
        "samples_ms": values,
    }


def run(n):
    source = subprocess.check_output(
        ["git", "show", BASE + ":wtb/infrastructure/evidence/postgres_store.py"]
    )
    module = types.ModuleType("wtb_pinned_postgres_backend")
    exec(compile(source, f"{BASE}:postgres_store.py", "exec"), module.__dict__)
    classes = {"before": module.PostgresEvidenceStore, "after": PostgresEvidenceStore}
    dsn = os.environ["WTB_EVIDENCE_POSTGRES_DSN"]
    schemas = []
    resources = ExitStack()

    def create(cls):
        schema = "wtb_pool_bench_" + uuid.uuid4().hex
        schemas.append(schema)
        store = cls(dsn, schema=schema)
        if hasattr(store, "close"):
            resources.callback(store.close)
        return store

    def empty(sink):
        with sink.store.transaction():
            pass

    def snapshot(sink):
        with sink.store.transaction() as db:
            return sink.snapshot(db, "artifact.txt")[0].snapshot_sha256

    def publish(sink):
        assert sink.publish(sink.approve("artifact.txt")).new_effect

    try:
        with psycopg.connect(dsn) as db:
            version = db.execute("SELECT version()").fetchone()[0]
        initialization = {arm: [] for arm in classes}
        for i in range(10):
            for arm in list(classes) if i % 2 == 0 else list(reversed(classes)):
                start = time.perf_counter_ns()
                store = create(classes[arm])
                initialization[arm].append((time.perf_counter_ns() - start) / 1e6)
                if hasattr(store, "close"):
                    store.close()
        sinks = {
            arm: setup_sink(store=create(cls), sink_type=SQLPublicationSink)
            for arm, cls in classes.items()
        }
        assert snapshot(sinks["before"]) == snapshot(sinks["after"])
        pids = {}
        for arm, sink in sinks.items():
            observed = set()
            for _ in range(10):
                with sink.store.transaction() as db:
                    observed.add(db.execute("SELECT pg_backend_pid()").fetchone()[0])
            pids[arm] = len(observed)
        stages = {}
        for name, operation in [
            ("empty_transaction", empty),
            ("snapshot_with_read", snapshot),
            ("approve_and_effect", publish),
        ]:
            samples = {arm: [] for arm in classes}
            for i in range(n):
                for arm in list(classes) if i % 2 == 0 else list(reversed(classes)):
                    start = time.perf_counter_ns()
                    operation(sinks[arm])
                    samples[arm].append((time.perf_counter_ns() - start) / 1e6)
            stages[name] = {arm: summarize(values) for arm, values in samples.items()}
        return {
            "baseline": BASE,
            "baseline_backend_sha256": hashlib.sha256(source).hexdigest(),
            "postgres": version,
            "psycopg": psycopg.__version__,
            "scope": "single client, same schema contents and SQL lifecycle; alternating paired order; initialization excludes Python import",
            "initialization": {
                arm: summarize(values) for arm, values in initialization.items()
            },
            "distinct_backend_pids_in_10_operations": pids,
            "stages": stages,
        }
    finally:
        resources.close()
        with psycopg.connect(dsn) as db:
            for schema in schemas:
                db.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(schema)
                    )
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=100)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.n < 1:
        parser.error("--n must be positive")
    args.output.write_text(json.dumps(run(args.n), indent=2) + "\n", encoding="utf-8")
