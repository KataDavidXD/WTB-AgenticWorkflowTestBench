"""Shared receipt validation and legacy Execution projection.

Adapters only select native references. They do not decide authorization or
infer runtime identity from paths, insertion order, or a session display name.
"""

from __future__ import annotations

from typing import Callable

from wtb.domain.execution_evidence import (
    AttemptIdentity,
    EvidenceError,
    EvidenceEvent,
    EvidenceStore,
)
from wtb.domain.interfaces.workflow_runtime import (
    RuntimeOutcomeReceipt,
    RuntimeTerminalOutcome,
    WorkflowRuntime,
)
from wtb.domain.models.workflow import Execution, ExecutionStatus
from wtb.domain.runtime_identity import canonical_json_sha256


class EvidenceCollector:
    def __init__(self, store: EvidenceStore) -> None:
        self.store = store

    @staticmethod
    def receipt_event(
        identity: AttemptIdentity,
        receipt: RuntimeOutcomeReceipt,
        *,
        native_reference: str,
    ) -> EvidenceEvent:
        binding = identity.binding
        if not isinstance(receipt, RuntimeOutcomeReceipt) or (
            receipt.execution_id != identity.execution_id
            or receipt.attempt_id != binding.attempt_id
            or receipt.input_sha256 != binding.input_sha256
            or receipt.session_id != binding.exact_session_id
            or receipt.binding != binding
            or not isinstance(receipt.outcome, RuntimeTerminalOutcome)
        ):
            raise EvidenceError("receipt_binding_mismatch")
        if (
            not isinstance(native_reference, str)
            or not native_reference
            or len(native_reference) > 1024
        ):
            raise EvidenceError("native_reference_missing")
        body = dict(
            binding=binding.to_dict(),
            execution_id=identity.execution_id,
            native_reference=native_reference,
            receipt=dict(receipt.receipt),
            values=dict(receipt.values),
        )
        digest = canonical_json_sha256({"outcome": receipt.outcome.value, **body})
        return EvidenceEvent(digest, receipt.outcome.value, body)

    def record(
        self,
        identity: AttemptIdentity,
        receipt: RuntimeOutcomeReceipt,
        *,
        native_reference: str,
    ) -> None:
        current = self.store.get(identity.execution_id, identity.binding.attempt_id)
        if current.identity != identity:
            raise EvidenceError("binding_conflict")
        event = self.receipt_event(identity, receipt, native_reference=native_reference)
        self.store.append(identity.execution_id, identity.binding.attempt_id, event)

    def collect(
        self,
        runtime: WorkflowRuntime,
        identity: AttemptIdentity,
        *,
        native_reference: Callable[[RuntimeOutcomeReceipt], str],
        input_state=None,
    ):
        """Use the runtime's exact reconciliation contract, never invoke implicitly.

        A runtime may perform its own proven-safe reconciliation when supplied
        fenced input; its existing capability/recovery policy still applies.
        """
        admitted = self.store.get(identity.execution_id, identity.binding.attempt_id)
        if admitted.identity != identity:
            raise EvidenceError("binding_conflict")
        receipt = runtime.reconcile(
            identity.execution_id,
            identity.binding.attempt_id,
            binding=identity.binding,
            input_state=input_state,
        )
        if receipt is None:
            self.store.append(
                identity.execution_id,
                identity.binding.attempt_id,
                EvidenceEvent("lookup-unknown", "unknown", {}),
            )
            return None
        self.record(identity, receipt, native_reference=native_reference(receipt))
        return receipt

    @staticmethod
    def project(evidence) -> Execution:
        identity, binding = evidence.identity, evidence.identity.binding
        terminal = evidence.terminal
        status = {
            "completed": ExecutionStatus.COMPLETED,
            "failed": ExecutionStatus.FAILED,
            "cancelled": ExecutionStatus.CANCELLED,
        }.get(evidence.outcome, ExecutionStatus.RUNNING)
        metadata = {"recovery_required": terminal is None}
        if terminal is not None:
            body = terminal.body
            if (
                body.get("binding") != binding.to_dict()
                or body.get("execution_id") != identity.execution_id
                or not isinstance(body.get("native_reference"), str)
                or not body["native_reference"]
            ):
                raise EvidenceError("receipt_binding_mismatch")
            # Publication's existing source-receipt field binds the entire
            # collector observation, including native reference and result.
            metadata["last_runtime_attempt"] = dict(
                attempt_id=binding.attempt_id,
                input_sha256=binding.input_sha256,
                session_id=binding.exact_session_id,
                binding=binding.to_dict(),
                outcome=evidence.outcome,
                receipt=dict(
                    rawReceiptSha256=terminal.digest,
                    nativeReference=body["native_reference"],
                ),
            )
        return Execution(
            id=identity.execution_id,
            workflow_id=identity.runtime_id,
            status=status,
            session_id=binding.exact_session_id,
            metadata=metadata,
        )

    def execution(self, execution_id: str, attempt_id: str) -> Execution:
        # Transactional sinks use read/project within their single transaction.
        return self.project(self.store.get_current(execution_id, attempt_id))


def deepseek_native_reference(receipt: RuntimeOutcomeReceipt) -> str:
    """Native durable message identity returned by the DSH SDK, never synthesized."""
    return receipt.receipt.get("message_id", "")


def langgraph_native_reference(receipt: RuntimeOutcomeReceipt) -> str:
    """Native checkpoint identity, scoped by the separately persisted envelope."""
    return receipt.receipt.get("checkpoint_id", "")
