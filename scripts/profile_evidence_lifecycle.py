"""Reproducible local cold/steady/end-to-end profile, no model/API calls."""

import argparse
import cProfile
import io
import json
import pstats
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compare_evidence_lifecycle import Before, setup_sink
from wtb.domain.publication_authority import (
    PublicationAction,
    PublicationAuthorityOperator,
    PublicationIntent,
)


def measure(call, n):
    samples = []
    for _ in range(n):
        start = time.perf_counter_ns()
        call()
        samples.append((time.perf_counter_ns() - start) / 1e6)
    return {
        "n": n,
        "median_ms": statistics.median(samples),
        "p95_ms": sorted(samples)[min(len(samples) - 1, int(len(samples) * 0.95))],
        "samples_ms": samples,
    }


def run(n, sink_factory=None):
    with tempfile.TemporaryDirectory() as root:
        sink = (
            sink_factory()
            if sink_factory is not None
            else setup_sink(Path(root) / "state.sqlite")
        )
        with sink.store.transaction() as db:
            executions, evidence, _ = sink.snapshot_inputs(db, "artifact.txt")
            snapshot, _ = sink.snapshot(db, "artifact.txt")
        intent = PublicationIntent(
            PublicationAction.PUBLISH_CURRENT,
            snapshot.current_material_sha256,
            snapshot.snapshot_sha256,
        )

        def read():
            with sink.store.transaction() as db:
                return sink.snapshot_inputs(db, "artifact.txt")

        def empty_transaction():
            with sink.store.transaction():
                pass

        def snapshot_e2e():
            with sink.store.transaction() as db:
                return sink.snapshot(db, "artifact.txt")

        def publish():
            approval = sink.approve("artifact.txt")
            assert sink.publish(approval).new_effect

        before = Before(sink.store)
        paired = {"before": [], "after": []}
        # Fixed alternating order, same immutable facts and read transaction.
        for i in range(n):
            for label, subject in (
                [("before", before), ("after", sink)]
                if i % 2 == 0
                else [("after", sink), ("before", before)]
            ):
                start = time.perf_counter_ns()
                with subject.store.transaction() as db:
                    subject.snapshot(db, "artifact.txt")
                paired[label].append((time.perf_counter_ns() - start) / 1e6)

        profile = cProfile.Profile()
        profile.enable()
        for _ in range(n):
            snapshot_e2e()
        profile.disable()
        report = io.StringIO()
        pstats.Stats(profile, stream=report).sort_stats("cumulative").print_stats(15)
        cold = []
        code = "import time; t=time.perf_counter_ns(); import wtb.domain.publication_authority; print((time.perf_counter_ns()-t)/1e6)"
        for _ in range(5):
            output = subprocess.check_output([sys.executable, "-c", code], text=True)
            cold.append(float(output.strip()))
        return {
            "cold_import": {
                "n": 5,
                "median_ms": statistics.median(cold),
                "samples_ms": cold,
            },
            "paired_snapshot_with_read": {
                key: {
                    "median_ms": statistics.median(values),
                    "samples_ms": values,
                    "n": n,
                }
                for key, values in paired.items()
            },
            "evidence_read_and_decode": measure(read, n),
            "transaction_open_close": measure(empty_transaction, n),
            "pure_snapshot_build": measure(
                lambda: PublicationAuthorityOperator.snapshot_from_executions(
                    executions, evidence
                ),
                n,
            ),
            "pure_authorize": measure(
                lambda: PublicationAuthorityOperator.authorize(
                    snapshot,
                    intent,
                    current_workspace_digest=snapshot.observed_workspace_digest,
                ),
                n,
            ),
            "snapshot_with_read": measure(snapshot_e2e, n),
            "approve_and_transaction_effect": measure(publish, min(n, 50)),
            "profile": report.getvalue(),
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--n", type=int, default=300)
    args = parser.parse_args()
    args.output.write_text(json.dumps(run(args.n), indent=2) + "\n", encoding="utf-8")
