import pytest

from tests.unit.evidence.test_publication_sink import setup_sink
from wtb.domain.interfaces.publication_sink import ExternalOutcome
from wtb.infrastructure.evidence.external_delivery import ExternalDelivery


@pytest.fixture
def sink(tmp_path):
    return setup_sink(tmp_path / "events.sqlite")


class Provider:
    sink_id = "provider-store-1"

    def __init__(self):
        self.effects = {}
        self.calls = 0
        self.lose_ack = False

    def apply(self, request):
        self.calls += 1
        self.effects[request.idempotency_key] = request
        if self.lose_ack:
            raise TimeoutError("effect committed, acknowledgement lost")
        return ExternalOutcome("applied", "native-receipt")

    def lookup(self, request):
        if self.effects.get(request.idempotency_key) == request:
            return ExternalOutcome("applied", "native-receipt")
        return ExternalOutcome("absent")


def test_lost_ack_reconciles_without_another_effect(sink):
    provider = Provider()
    provider.lose_ack = True
    delivery = ExternalDelivery(sink)
    approval = sink.approve("artifact.txt")
    assert delivery.dispatch(approval, provider).status == "unknown"
    assert not sink.publish(approval).allowed  # Cannot consume at a second sink.
    assert ExternalDelivery(sink).dispatch(approval, provider).status == "applied"
    assert provider.calls == 1
    assert len(provider.effects) == 1


def test_absent_unknown_does_not_blindly_redrive(sink):
    provider = Provider()
    delivery = ExternalDelivery(sink)
    approval = sink.approve("artifact.txt")
    delivery.reserve(approval, provider.sink_id)
    # Represents process loss after durable claim, before external dispatch.
    assert delivery.dispatch(approval, provider).status == "absent"
    assert provider.calls == 0
    sink.update_policy("scope", "other", expected_generation=1)
    assert delivery.retry_absent(approval, provider).status == "rejected"
    assert provider.calls == 0


def test_explicit_retry_revalidates_and_keeps_key(sink):
    provider = Provider()
    delivery = ExternalDelivery(sink)
    approval = sink.approve("artifact.txt")
    delivery.reserve(approval, provider.sink_id)
    assert delivery.retry_absent(approval, provider).status == "applied"
    assert list(provider.effects) == [approval]
    assert delivery.dispatch(approval, provider).status == "applied"
    assert provider.calls == 1


def test_provider_rejection_is_persisted(sink):
    provider = Provider()
    provider.apply = lambda request: ExternalOutcome("rejected")
    delivery = ExternalDelivery(sink)
    approval = sink.approve("artifact.txt")
    assert delivery.dispatch(approval, provider).status == "rejected"
    assert delivery.dispatch(approval, provider).status == "rejected"


@pytest.mark.parametrize("operation", ["dispatch", "retry_absent"])
@pytest.mark.parametrize(
    "change,reason,recovery",
    [
        ("attempt", "attempt_changed", "bind_current_attempt"),
        ("sink", "sink_binding_mismatch", "query_sink"),
    ],
)
def test_local_rejection_preserves_recovery(sink, operation, change, reason, recovery):
    from tests.unit.evidence.test_event_store import identity

    provider = Provider()
    delivery = ExternalDelivery(sink)
    approval = sink.approve("artifact.txt")
    if operation == "retry_absent" or change == "sink":
        delivery.reserve(approval, provider.sink_id)
    if change == "attempt":
        sink.store.register(identity("a2"), expected_attempt="a1")
    else:
        provider.sink_id = "other-provider"
    result = getattr(delivery, operation)(approval, provider)
    assert result.status == "rejected"
    assert result.reason == reason
    assert result.recovery == recovery
    assert provider.calls == 0
