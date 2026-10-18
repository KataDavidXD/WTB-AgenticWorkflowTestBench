from dataclasses import replace

import pytest

from tests.unit.evidence.test_event_store import identity
from wtb.application.services.evidence_collector import EvidenceCollector
from wtb.domain.execution_evidence import EvidenceError
from wtb.domain.interfaces.workflow_runtime import (
    RuntimeOutcomeReceipt,
    RuntimeTerminalOutcome,
)
from wtb.domain.models.workflow import ExecutionStatus
from wtb.infrastructure.evidence.sqlite_store import SQLiteEvidenceStore


def receipt():
    i = identity()
    return RuntimeOutcomeReceipt(
        i.execution_id,
        i.binding.attempt_id,
        i.binding.input_sha256,
        RuntimeTerminalOutcome.COMPLETED,
        binding=i.binding,
        session_id=i.binding.exact_session_id,
        receipt={"message_id": "native-message-93"},
        values={"answer": "done"},
    )


@pytest.fixture
def collector(tmp_path):
    store = SQLiteEvidenceStore(tmp_path / "store.sqlite")
    store.register(identity(), expected_attempt=None)
    return EvidenceCollector(store)


@pytest.mark.parametrize(
    "changes",
    [
        {"execution_id": "wrong"},
        {"attempt_id": "wrong"},
        {"input_sha256": "e" * 64},
        {"session_id": "wrong"},
        {"binding": None},
        {"outcome": "completed"},
    ],
)
def test_receipt_ownership_is_not_inferred(collector, changes):
    with pytest.raises(EvidenceError, match="receipt_binding_mismatch"):
        collector.record(
            identity(),
            replace(receipt(), **changes),
            native_reference="native-message-93",
        )
    assert collector.store.get("e1", "a1").state == "created"


def test_projection_is_compatible_and_exact(collector):
    collector.record(identity(), receipt(), native_reference="native-message-93")
    execution = collector.execution("e1", "a1")
    assert execution.status is ExecutionStatus.COMPLETED
    terminal = execution.metadata["last_runtime_attempt"]
    assert terminal["binding"] == identity().binding.to_dict()
    assert terminal["receipt"]["nativeReference"] == "native-message-93"
    assert len(terminal["receipt"]["rawReceiptSha256"]) == 64
    collector.store.register(identity("a2"), expected_attempt="a1")
    with pytest.raises(EvidenceError, match="attempt_changed"):
        collector.execution("e1", "a1")


def test_unknown_lookup_does_not_dispatch_and_failures_stay_failed(collector):
    class LookupOnly:
        def reconcile(self, execution_id, attempt_id, **kwargs):
            assert (execution_id, attempt_id) == ("e1", "a1")
            return None

    assert (
        collector.collect(LookupOnly(), identity(), native_reference=lambda r: "native")
        is None
    )
    assert collector.store.get("e1", "a1").state == "unknown"
    failed = replace(receipt(), outcome=RuntimeTerminalOutcome.FAILED)
    collector.record(identity(), failed, native_reference="native-message-93")
    assert collector.execution("e1", "a1").status is ExecutionStatus.FAILED


def test_missing_native_identity_is_not_manufactured(collector):
    with pytest.raises(EvidenceError, match="native_reference_missing"):
        collector.record(identity(), receipt(), native_reference="")


def test_foreign_identity_rejected_before_runtime_reconciliation(collector):
    class MustNotCall:
        def reconcile(self, *args, **kwargs):
            pytest.fail("unadmitted identity reached runtime")

    with pytest.raises(EvidenceError, match="binding_conflict"):
        collector.collect(
            MustNotCall(),
            replace(identity(), runtime_id="foreign"),
            native_reference=lambda r: "native",
        )


def test_nested_mapping_receipts_are_normalized(collector):
    from types import MappingProxyType

    observed = replace(
        receipt(), receipt={"native": MappingProxyType({"id": "native"})}
    )
    collector.record(identity(), observed, native_reference="native")
    assert collector.execution("e1", "a1").status is ExecutionStatus.COMPLETED
