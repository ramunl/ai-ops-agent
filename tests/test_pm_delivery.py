"""Durable retry state and safe replay independently of deployment recovery."""

import json
from datetime import UTC, datetime, timedelta
from threading import Event, Thread

import pytest

from ai_ops_agent.deployment.model import Store
from ai_ops_agent.deployment.pm_bridge import PMBridge
from ai_ops_agent.deployment.pm_delivery import (
    BASE_DELAY,
    LEASE_SECONDS,
    MAX_ATTEMPTS,
    MAX_DELAY,
    DeliveryError,
    PMDelivery,
)
from tests.test_pm_bridge import Contract

pytest_plugins = ["tests.test_pm_bridge"]

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_offline_restart_immutable_replay(tmp_path, event, caplog):
    store = Store(tmp_path / "store")
    remote = Contract()
    remote.offline = True
    delivery = PMDelivery(store, PMBridge(remote))
    first = delivery.enqueue(event, now=NOW)
    identity = event["identity"]
    assert delivery.due(now=NOW) == [identity]
    failed = delivery.attempt_due(identity, now=NOW)
    assert failed["attempts"] == 1 and failed["confirmed_ticket_id"] is None
    assert delivery.due(now=NOW) == []
    remote.offline = False
    restarted = PMDelivery(Store(store.root), PMBridge(remote))
    replay = restarted.enqueue({**event, "rollback_outcome": "succeeded"}, now=NOW)
    assert replay["ticket"] == first["ticket"]
    saved = restarted.attempt_due(identity, now=NOW + timedelta(seconds=BASE_DELAY))
    assert saved["status"] == "delivered"
    assert saved["confirmed_ticket_id"] == first["ticket"]["id"]
    assert restarted.due(now=NOW + timedelta(days=10)) == []
    path = delivery.directory / f"{identity}.json"
    assert "SECRET" not in path.read_text() + caplog.text + json.dumps(remote.calls)
    assert path.stat().st_mode & 0o777 == 0o600
    assert delivery.directory.stat().st_mode & 0o777 == 0o700
    assert not (store.root / "state.json").exists()


def test_lost_response_reuses_text_and_id(tmp_path, event):
    remote = Contract()
    remote.lose_response = True
    remote.published = False
    delivery = PMDelivery(Store(tmp_path / "store"), PMBridge(remote))
    first = delivery.enqueue(event, now=NOW)
    delivery.attempt_due(event["identity"], now=NOW)
    delivery.enqueue({**event, "rollback_outcome": "failed"}, now=NOW)
    result = delivery.attempt_due(event["identity"], now=NOW + timedelta(seconds=60))
    assert result["confirmed_ticket_id"] == first["ticket"]["id"]
    assert len(remote.items) == 1


def test_bounded_exponential_retries(tmp_path, event):
    remote = Contract()
    remote.offline = True
    delivery = PMDelivery(Store(tmp_path / "store"), PMBridge(remote))
    delivery.enqueue(event, now=NOW)
    when = NOW
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = delivery.attempt_due(event["identity"], now=when)
        assert result["attempts"] == attempt
        if attempt < MAX_ATTEMPTS:
            next_time = datetime.fromisoformat(result["next_attempt_utc"])
            assert (next_time - when).total_seconds() == min(
                MAX_DELAY, 60 * 2 ** (attempt - 1)
            )
            when = next_time
    assert result["status"] == "exhausted" and result["next_attempt_utc"] is None
    assert delivery.attempt_due(event["identity"], now=when) == result
    assert len(remote.calls) == MAX_ATTEMPTS


def test_crash_claim_replay(tmp_path, event):
    class Crash:
        def add(self, ticket):
            raise RuntimeError("simulated process death")

    store = Store(tmp_path / "store")
    delivery = PMDelivery(store, Crash())
    delivery.enqueue(event, now=NOW)
    with pytest.raises(RuntimeError):
        delivery.attempt_due(event["identity"], now=NOW)
    restarted = PMDelivery(Store(store.root), PMBridge(Contract()))
    assert restarted.due(now=NOW) == []
    result = restarted.attempt_due(
        event["identity"], now=NOW + timedelta(seconds=LEASE_SECONDS)
    )
    assert result["status"] == "delivered" and result["attempts"] == 2


def test_concurrent_duplicate_and_unlocked_bridge(tmp_path, event):
    store = Store(tmp_path / "store")
    entered, release = Event(), Event()
    remote = Contract()

    def run(*args):
        with store.lock():  # Bridge I/O never holds the store lock.
            pass
        entered.set()
        assert release.wait(5)
        return remote(*args)

    delivery = PMDelivery(store, PMBridge(run))
    delivery.enqueue(event, now=NOW)
    results = []
    thread = Thread(
        target=lambda: results.append(delivery.attempt_due(event["identity"], now=NOW))
    )
    thread.start()
    try:
        assert entered.wait(5)
        duplicate = PMDelivery(Store(store.root), PMBridge(remote))
        assert duplicate.enqueue(event, now=NOW)["attempts"] == 1
        assert duplicate.attempt_due(event["identity"], now=NOW)["attempts"] == 1
    finally:
        release.set()
        thread.join(5)
    assert results[0]["status"] == "delivered" and len(remote.items) == 1


def test_corrupt_state_is_preserved(tmp_path, event):
    delivery = PMDelivery(Store(tmp_path / "store"))
    delivery.enqueue(event, now=NOW)
    path = delivery.directory / f"{event['identity']}.json"
    path.write_text('{"error":"SECRET"}')
    with pytest.raises(DeliveryError, match="Corrupt"):
        delivery.enqueue(event, now=NOW)
    assert path.read_text() == '{"error":"SECRET"}'
    with pytest.raises(ValueError):
        delivery.due(limit=100)


def test_capacity_and_oversized_state(tmp_path, event, monkeypatch):
    import ai_ops_agent.deployment.pm_delivery as module

    delivery = PMDelivery(Store(tmp_path / "store"))
    monkeypatch.setattr(module, "MAX_RECORDS", 0)
    with pytest.raises(DeliveryError, match="capacity"):
        delivery.enqueue(event, now=NOW)
    monkeypatch.setattr(module, "MAX_RECORDS", 1)
    delivery.enqueue(event, now=NOW)
    # Existing delivery remains readable and replayable at capacity.
    assert delivery.enqueue(event, now=NOW)["attempts"] == 0
    path = delivery.directory / f"{event['identity']}.json"
    path.write_text("x" * 65537)
    with pytest.raises(DeliveryError, match="Corrupt"):
        delivery.load(event["identity"])


def test_stale_completion_cannot_erase_newer_confirmation(tmp_path, event):
    from ai_ops_agent.deployment.pm_bridge import BridgeError

    store = Store(tmp_path / "store")
    newer = PMDelivery(Store(store.root), PMBridge(Contract()))

    class Expired:
        def add(self, ticket):
            newer.attempt_due(
                event["identity"], now=NOW + timedelta(seconds=LEASE_SECONDS)
            )
            raise BridgeError("PM bridge unavailable")

    older = PMDelivery(store, Expired())
    older.enqueue(event, now=NOW)
    result = older.attempt_due(event["identity"], now=NOW)
    assert result["status"] == "delivered" and result["attempts"] == 2
