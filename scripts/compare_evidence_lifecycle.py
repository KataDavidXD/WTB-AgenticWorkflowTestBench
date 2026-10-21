"""Paired strong-control comparison on identical trusted facts and SQL effects.

WTB-uncached rebuilds the current single-path snapshot on every call. WTB-runtime
uses the reusable collector/store/sink path and its version-checked immutable
cache. Complete-binding checks the same facts independently. Shared collector,
state and transaction services are paid by all arms; this is a decision-boundary
control, not a rerun of upstream CONTINUITY or MasDrift ecosystems.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wtb.application.services.evidence_collector import EvidenceCollector
from wtb.domain.execution_evidence import (
    AttemptIdentity,
    EvidenceError,
    EvidenceEvent,
    encode,
)
from wtb.domain.interfaces.workflow_runtime import (
    RuntimeBindingEnvelope,
    RuntimeOutcomeReceipt,
    RuntimeTerminalOutcome,
)
from wtb.domain.publication_authority import (
    PublicationAuthorityOperator,
    PublicationDisposition,
    PublicationReason,
    ScopeMutationEvidence,
)
from wtb.infrastructure.evidence.publication_sink import SQLPublicationSink
from wtb.infrastructure.evidence.publication_sink import SQLitePublicationSink
from wtb.infrastructure.evidence.sqlite_store import SQLiteEvidenceStore


def identity(attempt="a1"):
    return AttemptIdentity(
        "e1",
        "runtime",
        "scope",
        RuntimeBindingEnvelope("actor", "store", "c" * 64, 2, 1, "d" * 64, attempt),
    )


def event(kind, event_id=None, body=None):
    return EvidenceEvent(event_id or kind, kind, body or {})


def receipt(attempt="a1"):
    admitted = identity(attempt)
    return RuntimeOutcomeReceipt(
        admitted.execution_id,
        admitted.binding.attempt_id,
        admitted.binding.input_sha256,
        RuntimeTerminalOutcome.COMPLETED,
        binding=admitted.binding,
        session_id=admitted.binding.exact_session_id,
        receipt={"message_id": "native-message-93"},
        values={"answer": "done"},
    )


def setup_sink(path):
    store = SQLiteEvidenceStore(path)
    admitted = identity()
    store.register(admitted, expected_attempt=None)
    EvidenceCollector(store).record(
        admitted, receipt(), native_reference="native-message-93"
    )
    source = store.get("e1", "a1").terminal.digest
    mutation = ScopeMutationEvidence(
        "scope",
        "actor",
        "e1",
        "a1",
        "d" * 64,
        source,
        "mutation",
        1,
        "artifact.txt",
        "d" * 64,
        hashlib.sha256(b"").hexdigest(),
        hashlib.sha256(b"payload").hexdigest(),
        hashlib.sha256(b"").hexdigest(),
        hashlib.sha256(b"payload").hexdigest(),
    )
    sink = SQLitePublicationSink(store)
    sink.update_policy("scope", "actor", expected_generation=0)
    sink.record_material("artifact.txt", b"payload", (mutation,), expected_version=0)
    return sink


class Before(SQLPublicationSink):
    def snapshot(self, db, target):
        executions, evidence, payload = self.snapshot_inputs(db, target)
        return PublicationAuthorityOperator.snapshot_from_executions(
            executions, evidence
        ), payload


def facts(sink, db, target):
    executions, evidence, payload = sink.snapshot_inputs(db, target)
    current = {
        "evidence": evidence.to_mapping(),
        "executions": [
            {"id": ex.id, "status": ex.status.value, "metadata": ex.metadata}
            for ex in executions
        ],
    }
    policy = {p.scope_id: p.authorized_session_id for p in evidence.policy_bindings}
    valid = True
    for ex, mutation in zip(executions, evidence.mutations, strict=True):
        terminal = ex.metadata.get("last_runtime_attempt", {})
        binding = terminal.get("binding", {})
        valid = valid and (
            ex.status.value == "completed"
            and not ex.metadata.get("recovery_required")
            and terminal.get("outcome") == "completed"
            and terminal.get("attempt_id")
            == binding.get("attempt_id")
            == mutation.attempt_id
            and terminal.get("input_sha256")
            == binding.get("input_sha256")
            == mutation.input_sha256
            and terminal.get("session_id")
            == binding.get("exact_session_id")
            == mutation.actor_session_id
            and terminal.get("receipt", {}).get("rawReceiptSha256")
            == mutation.source_receipt_sha256
            and policy[mutation.scope_id] == mutation.actor_session_id
        )
    return current, valid, payload


class CompleteEnhanced(SQLPublicationSink):
    def approve(self, target):
        with self.store.transaction() as db:
            current, valid, _ = facts(self, db, target)
            if not valid:
                raise EvidenceError("invalid_evidence")
            approval = uuid.uuid4().hex
            db.execute(
                "INSERT INTO publication_approvals(id,target,intent) VALUES (?,?,?)",
                (approval, target, encode(current)),
            )
            return approval

    def verify(self, db, approval):
        row = db.execute(
            "SELECT * FROM publication_approvals WHERE id=?", (approval,)
        ).fetchone()
        current, valid, payload = facts(self, db, row["target"])
        allowed = valid and current == json.loads(row["intent"])

        class Decision:
            disposition = (
                PublicationDisposition.PERMIT_PUBLICATION
                if allowed
                else PublicationDisposition.DENY_PUBLICATION
            )
            reason = (
                PublicationReason.AUTHORITY_AND_WORKSPACE_BOUND
                if allowed
                else PublicationReason.AUTHORITY_SNAPSHOT_CHANGED
            )
            external_recovery_required = False

            def to_mapping(self):
                return {"completeEnhancedFieldCheck": allowed, "facts": current}

        return Decision(), payload


SCENARIOS = [
    "clean",
    "duplicate",
    "late-running",
    "unrelated-policy",
    "policy",
    "aba",
    "retry",
    "failed",
    "unknown",
    "cancelled",
    "conflicting-completion",
    "missing-receipt",
    "wrong-receipt",
    "material",
    "scope",
    "replay",
]


def change(sink, scenario):
    if scenario in {"duplicate", "late-running"}:
        sink.store.append(
            "e1",
            "a1",
            sink.store.get("e1", "a1").terminal
            if scenario == "duplicate"
            else event("running"),
        )
    elif scenario == "unrelated-policy":
        sink.update_policy("other", "actor", expected_generation=0)
    elif scenario == "policy":
        sink.update_policy("scope", "actor", expected_generation=1)
    elif scenario == "aba":
        sink.update_policy("scope", "other", expected_generation=1)
        sink.update_policy("scope", "actor", expected_generation=2)
    elif scenario == "retry":
        sink.store.register(identity("a2"), expected_attempt="a1")
    elif scenario in {"failed", "cancelled", "conflicting-completion"}:
        sink.store.append(
            "e1",
            "a1",
            event(
                "completed" if scenario == "conflicting-completion" else scenario,
                "conflict",
            ),
        )
    elif scenario in {"unknown", "missing-receipt"}:
        with sink.store.transaction() as db:
            db.execute(
                "DELETE FROM evidence_events"
            )  # Deliberate storage-loss injection.
        if scenario == "unknown":
            sink.store.append("e1", "a1", event("unknown"))
    elif scenario in {"wrong-receipt", "scope"}:
        with sink.store.transaction() as db:
            payload, mutations, version = sink.material(db, "artifact.txt")
        replacement = replace(
            mutations[0],
            **(
                {"source_receipt_sha256": "f" * 64}
                if scenario == "wrong-receipt"
                else {"scope_id": "other"}
            ),
        )
        if scenario == "scope":
            sink.update_policy("other", "actor", expected_generation=0)
        sink.record_material(
            "artifact.txt", payload, (replacement,), expected_version=version
        )
    elif scenario == "material":
        with sink.store.transaction() as db:
            db.execute("UPDATE publication_material SET payload=?", (b"bad",))


def run(sink_factory=None):
    current = (
        Path("wtb/domain/publication_authority.py").read_bytes().replace(b"\r\n", b"\n")
    )
    rows = []
    with tempfile.TemporaryDirectory() as temp:
        for scenario in SCENARIOS:
            expected = scenario in {
                "clean",
                "duplicate",
                "late-running",
                "unrelated-policy",
                "replay",
            }
            for arm, cls in [
                ("wtb-uncached", Before),
                ("wtb-runtime", SQLPublicationSink),
                ("complete-binding", CompleteEnhanced),
            ]:
                path = Path(temp) / f"{scenario}-{arm}.sqlite"
                initial = (
                    sink_factory() if sink_factory is not None else setup_sink(path)
                )
                sink = cls(initial.store)
                with sink.store.transaction() as db:
                    _, original_mutations, _ = sink.material(db, "artifact.txt")
                approval = sink.approve("artifact.txt")
                change(sink, scenario)
                start = time.perf_counter_ns()
                result = sink.publish(approval)
                elapsed = (time.perf_counter_ns() - start) / 1e6
                assert result.allowed == expected, (arm, scenario, result)
                if scenario == "replay":
                    assert not sink.publish(approval).new_effect
                with sink.store.transaction() as db:
                    effects = db.execute(
                        "SELECT payload FROM publication_effects"
                    ).fetchall()
                assert len(effects) == int(expected)
                assert all(row["payload"] == b"payload" for row in effects)
                recovered = None
                if not expected:
                    # Controlled repair: NEW verified attempt and approval,
                    # never turn a rejected fact into permission in place.
                    previous = sink.store.current_attempt("e1")
                    repaired = identity("repaired-attempt")
                    sink.store.register(repaired, expected_attempt=previous)
                    EvidenceCollector(sink.store).record(
                        repaired,
                        replace(
                            receipt(),
                            attempt_id=repaired.binding.attempt_id,
                            binding=repaired.binding,
                        ),
                        native_reference="repair-message",
                    )
                    observed = sink.store.get("e1", repaired.binding.attempt_id)
                    mutation = replace(
                        original_mutations[0],
                        attempt_id=repaired.binding.attempt_id,
                        source_receipt_sha256=observed.terminal.digest,
                    )
                    with sink.store.transaction() as db:
                        version = db.execute(
                            "SELECT version FROM publication_material"
                        ).fetchone()[0]
                    sink.record_material(
                        "artifact.txt",
                        b"payload",
                        (mutation,),
                        expected_version=version,
                    )
                    renewed = sink.approve("artifact.txt")
                    recovered = sink.publish(renewed).new_effect
                    assert recovered
                rows.append(
                    dict(
                        arm=arm,
                        scenario=scenario,
                        allowed=result.allowed,
                        expected=expected,
                        actual_effects=len(effects),
                        latency_ms=elapsed,
                        recovered_with_new_attempt_and_approval=recovered,
                    )
                )
    return {
        "wtb_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "operator_sha256": hashlib.sha256(current).hexdigest(),
        "scope": "deterministic paired effect contracts; shared trusted services for all arms",
        "rows": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(run(), indent=2) + "\n", encoding="utf-8")
