"""Durable external delivery. Unknown results are looked up, never blind retried."""

from wtb.domain.execution_evidence import EvidenceError
from wtb.domain.interfaces.publication_sink import (
    ExternalOutcome,
    ExternalPublication,
    ExternalPublicationSink,
)
from wtb.domain.publication_authority import PublicationDisposition, _require_safe_id
from wtb.infrastructure.evidence.publication_sink import SQLPublicationSink


class ExternalDelivery:
    def __init__(self, publication: SQLPublicationSink):
        self.publication = publication
        self.store = publication.store
        with self.store.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS publication_deliveries ("
                f"approval TEXT PRIMARY KEY, sink_id TEXT NOT NULL, payload {self.store.blob_type} NOT NULL, "
                "authority TEXT NOT NULL, status TEXT NOT NULL, receipt TEXT)"
            )

    def reserve(self, approval: str, sink_id: str) -> bool:
        _require_safe_id(sink_id, "sink_id")
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM publication_deliveries WHERE approval=?", (approval,)
            ).fetchone()
            if row:
                if row["sink_id"] != sink_id:
                    raise EvidenceError("sink_binding_mismatch", "query_sink")
                return False
            consumed = db.execute(
                "SELECT consumed FROM publication_approvals WHERE id=?", (approval,)
            ).fetchone()
            if consumed and consumed["consumed"]:
                raise EvidenceError("approval_consumed", "query_sink")
            decision, payload = self.publication.verify(db, approval)
            if decision.disposition is not PublicationDisposition.PERMIT_PUBLICATION:
                raise EvidenceError(
                    decision.reason.value,
                    "query_runtime"
                    if decision.external_recovery_required
                    else "request_authorization",
                )
            # Commit unknown BEFORE calling the provider. A crash at any point
            # from here requires lookup, including a crash before actual send.
            db.execute(
                "INSERT INTO publication_deliveries VALUES (?,?,?,?,?,NULL)",
                (
                    approval,
                    sink_id,
                    payload,
                    decision.authority_snapshot_sha256,
                    "unknown",
                ),
            )
            db.execute(
                "UPDATE publication_approvals SET consumed=1 WHERE id=?", (approval,)
            )
            return True

    def _request(self, approval, sink_id):
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM publication_deliveries WHERE approval=?", (approval,)
            ).fetchone()
            if row is None or row["sink_id"] != sink_id:
                raise EvidenceError("sink_binding_mismatch", "query_sink")
            return (
                ExternalPublication(approval, row["payload"], row["authority"]),
                ExternalOutcome(row["status"], row["receipt"]),
            )

    def _finish(self, approval, outcome):
        if not isinstance(outcome, ExternalOutcome):
            outcome = ExternalOutcome("unknown")
        with self.store.transaction() as db:
            old = db.execute(
                "SELECT status,receipt FROM publication_deliveries WHERE approval=?",
                (approval,),
            ).fetchone()
            if old["status"] in {"applied", "rejected"}:
                return ExternalOutcome(old["status"], old["receipt"])
            # 'absent' is only a lookup observation, not terminal consumption.
            if outcome.status in {"applied", "rejected"}:
                db.execute(
                    "UPDATE publication_deliveries SET status=?,receipt=? WHERE approval=?",
                    (outcome.status, outcome.receipt, approval),
                )
            return outcome

    def dispatch(self, approval: str, sink: ExternalPublicationSink) -> ExternalOutcome:
        try:
            fresh = self.reserve(approval, sink.sink_id)
        except EvidenceError as error:
            return ExternalOutcome(
                "rejected", reason=error.code, recovery_hint=error.recovery
            )
        try:
            request, outcome = self._request(approval, sink.sink_id)
        except EvidenceError as error:
            return ExternalOutcome(
                "rejected", reason=error.code, recovery_hint=error.recovery
            )
        if outcome.status in {"applied", "rejected"}:
            return outcome
        try:
            result = sink.apply(request) if fresh else sink.lookup(request)
        except Exception:
            result = ExternalOutcome("unknown")
        return self._finish(approval, result)

    def retry_absent(
        self, approval: str, sink: ExternalPublicationSink
    ) -> ExternalOutcome:
        """Explicit recovery only, with authoritative absence and fresh validation.

        Provider idempotency and atomic authority checks also protect against a
        delayed first sender. This method does not invent those guarantees.
        """
        try:
            request, outcome = self._request(approval, sink.sink_id)
        except EvidenceError as error:
            return ExternalOutcome(
                "rejected", reason=error.code, recovery_hint=error.recovery
            )
        if outcome.status in {"applied", "rejected"}:
            return outcome
        try:
            observed = sink.lookup(request)
        except Exception:
            return ExternalOutcome("unknown")
        if observed.status != "absent":
            return self._finish(approval, observed)
        try:
            with self.store.transaction() as db:
                decision, payload = self.publication.verify(db, approval)
                if (
                    decision.disposition
                    is not PublicationDisposition.PERMIT_PUBLICATION
                    or decision.authority_snapshot_sha256
                    != request.authority_snapshot_sha256
                    or payload != request.payload
                ):
                    return ExternalOutcome(
                        "rejected",
                        reason=decision.reason.value
                        if decision.disposition
                        is not PublicationDisposition.PERMIT_PUBLICATION
                        else "authority_snapshot_changed",
                        recovery_hint="query_runtime"
                        if decision.external_recovery_required
                        else "request_authorization",
                    )
            return self._finish(approval, sink.apply(request))
        except EvidenceError as error:
            return ExternalOutcome(
                "rejected", reason=error.code, recovery_hint=error.recovery
            )
        except Exception:
            return ExternalOutcome("unknown")
