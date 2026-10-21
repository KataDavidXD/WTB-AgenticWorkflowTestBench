"""Repeat the new-WTB runtime, backend and receipt-protocol acceptance gates.

The run is deterministic and makes no model/API calls.  It writes progress after
every replicate so a detached job can be audited before completion.  Engineering
cost comes from the exact introducing commits; generated evidence/docs are
excluded from production and test line totals.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compare_evidence_lifecycle import run as compare
from validate_postgres_evidence import run as compare_postgres


STAGES = {
    "second_runtime": "6c0d314",
    "postgres_backend": "cb5242b",
    "pool_and_eager_initialization": "2ba13db",
    "receipt_protocol_change": "7014902",
}


def _write(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _numstat(commit: str) -> dict[str, object]:
    output = subprocess.check_output(
        ["git", "show", "--format=", "--numstat", commit], text=True
    )
    rows = []
    for line in output.splitlines():
        added, deleted, path = line.split("\t", 2)
        if not added.isdigit() or not deleted.isdigit():
            continue
        kind = (
            "production"
            if path.startswith("wtb/")
            else "test"
            if path.startswith("tests/")
            else "other"
        )
        rows.append(
            {"path": path, "added": int(added), "deleted": int(deleted), "kind": kind}
        )
    totals = {
        kind: {
            "files": sum(row["kind"] == kind for row in rows),
            "added": sum(row["added"] for row in rows if row["kind"] == kind),
            "deleted": sum(row["deleted"] for row in rows if row["kind"] == kind),
        }
        for kind in ("production", "test", "other")
    }
    return {"commit": commit, "totals": totals, "files": rows}


def _summarize(rows: list[dict[str, object]]) -> dict[str, object]:
    by_arm = {}
    for arm in sorted({row["arm"] for row in rows}):
        selected = [row for row in rows if row["arm"] == arm]
        samples = [row["latency_ms"] for row in selected]
        by_arm[arm] = {
            "correct": sum(row["allowed"] == row["expected"] for row in selected),
            "cells": len(selected),
            "median_ms": statistics.median(samples),
            "p95_ms": sorted(samples)[min(len(samples) - 1, int(len(samples) * 0.95))],
        }
    return by_arm


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--pg16-dsn", required=True)
    parser.add_argument("--pg17-dsn", required=True)
    parser.add_argument("--strong-summary", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    progress = {
        "status": "running",
        "wtb_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "repeats_requested": args.repeats,
        "completed": 0,
        "source_cost": {name: _numstat(commit) for name, commit in STAGES.items()},
        "runs": [],
    }
    if args.strong_summary:
        progress["strong_baseline_summary"] = json.loads(
            args.strong_summary.read_text(encoding="utf-8")
        )
    _write(args.output / "progress.json", progress)
    try:
        for index in range(args.repeats):
            started = time.perf_counter()
            runtime_started = time.perf_counter()
            runtime_env = os.environ.copy()
            runtime_env["WTB_REQUIRE_DSH_REAL_E2E"] = "1"
            runtime = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "tests/integration/test_evidence_collectors_real.py",
                ],
                text=True,
                capture_output=True,
                timeout=120,
                env=runtime_env,
            )
            if runtime.returncode:
                raise RuntimeError(
                    "second-runtime acceptance failed: "
                    + (runtime.stdout + runtime.stderr)[-2000:]
                )
            runtime_seconds = time.perf_counter() - runtime_started
            sqlite = compare()
            backends = {}
            for label, dsn in (("postgres16", args.pg16_dsn), ("postgres17", args.pg17_dsn)):
                os.environ["WTB_EVIDENCE_POSTGRES_DSN"] = dsn
                result = compare_postgres(0)
                backends[label] = {
                    "version": result["postgres_version"],
                    "arms": _summarize(result["paired"]["rows"]),
                }
            run = {
                "replicate": index,
                "second_runtime": {
                    "passed": True,
                    "wall_seconds": runtime_seconds,
                    "summary": runtime.stdout.strip().splitlines()[-1],
                },
                "sqlite": _summarize(sqlite["rows"]),
                "backends": backends,
                "wall_seconds": time.perf_counter() - started,
            }
            progress["runs"].append(run)
            progress["completed"] = index + 1
            _write(args.output / "progress.json", progress)
        progress["status"] = "complete"
    except BaseException as error:
        progress["status"] = "failed"
        progress["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        _write(args.output / "progress.json", progress)
        if progress["status"] == "complete":
            _write(args.output / "results.json", progress)


if __name__ == "__main__":
    main()
