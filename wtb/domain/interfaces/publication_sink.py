"""External effect contract. Providers must enforce this, not just accept JSON."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ExternalPublication:
    idempotency_key: str
    payload: bytes
    authority_snapshot_sha256: str


@dataclass(frozen=True)
class ExternalOutcome:
    # applied, rejected, absent (authoritative lookup only), or unknown
    status: str
    receipt: str | None = None
    reason: str | None = None
    recovery_hint: str | None = None

    def __post_init__(self):
        if self.status not in {"applied", "rejected", "absent", "unknown"}:
            raise ValueError("invalid external outcome")
        if self.status == "applied" and (
            not isinstance(self.receipt, str) or not self.receipt
        ):
            raise ValueError("applied outcome requires a durable native receipt")

    @property
    def recovery(self) -> str | None:
        return self.recovery_hint or {
            "unknown": "query_sink",
            "absent": "explicit_retry",
            "rejected": "request_authorization",
        }.get(self.status)


class ExternalPublicationSink(Protocol):
    """Provider-owned atomic idempotency + authority guard + effect.

    apply must atomically compare the supplied authority token against the
    provider's current authority AND produce the effect under that guard. A
    plain Git push or HTTP call without this capability does not implement this
    protocol. WTB cannot manufacture cross-system atomicity with a callback.
    lookup must bind its answer to the entire request, not merely its key.
    """

    sink_id: str

    def apply(self, request: ExternalPublication) -> ExternalOutcome: ...
    def lookup(self, request: ExternalPublication) -> ExternalOutcome: ...
