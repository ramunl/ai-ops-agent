"""Durable failure identities and private persistence."""

import json
from dataclasses import replace

import pytest

from ai_ops_agent.deployment.failure_events import (
    FailureEvents,
    FailureRecordError,
    failure_identity,
    new_failure,
)
from ai_ops_agent.deployment.model import DeploymentError, Store, Target


@pytest.fixture
def target(tmp_path):
    return Target("agent", tmp_path / "repo", tmp_path / "venv")


def record(target, **changes):
    value = new_failure(
        operation="job-1",
        target=target,
        stage="verify",
        requested_ref="main",
        requested_commit="a" * 40,
        previous_revision="b" * 40,
        exit_code=217,
        status="status=217/USER secret=SECRET",
        journal="SECRET",
    )
    value.update(changes)
    return value


def test_roundtrip_restart_and_rollback_replay(tmp_path, target):
    store = Store(tmp_path / "store")
    events = FailureEvents(store)
    original = record(target)
    assert events.save(original) == original
    replay = record(
        target,
        failed_at="2020-01-01T00:00:00+00:00",
        exit_code=1,
        diagnostics=[],
        rollback_outcome="succeeded",
    )
    saved = FailureEvents(Store(store.root)).save(replay)
    assert saved == {**original, "rollback_outcome": "succeeded"}
    assert events.load(original["identity"]) == saved
    assert len(list((store.root / "failure-events").glob("*.json"))) == 1
    assert not (store.root / "state.json").exists()
    assert (
        "SECRET"
        not in (
            store.root / "failure-events" / (original["identity"] + ".json")
        ).read_text()
    )
    assert events.save(original) == saved  # pending replay cannot erase recovery


def test_identity_dimensions_and_canonical_encoding(target):
    identity = failure_identity("job-1", target, "verify")
    assert len(identity) == 64
    assert identity == record(target)["identity"]
    assert (
        len(
            {
                identity,
                failure_identity("job-2", target, "verify"),
                failure_identity("job-1", replace(target, name="other"), "verify"),
                failure_identity(
                    "job-1", replace(target, repo=target.repo / "other"), "verify"
                ),
                failure_identity("job-1", target, "restart"),
            }
        )
        == 5
    )


def test_private_permissions_and_lock_contract(tmp_path, target):
    store = Store(tmp_path / "store")
    events = FailureEvents(store)
    value = events.save(record(target))
    directory = store.root / "failure-events"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / (value["identity"] + ".json")).stat().st_mode & 0o777 == 0o600
    with store.lock(), pytest.raises(DeploymentError):
        events.save(value)


@pytest.mark.parametrize("payload", ["{", "[]", '{"version":99}', '{"version":true}'])
def test_corruption_never_overwritten(tmp_path, target, payload):
    events = FailureEvents(Store(tmp_path / "store"))
    value = events.save(record(target))
    path = events.store.root / "failure-events" / (value["identity"] + ".json")
    path.write_text(payload)
    with pytest.raises(FailureRecordError):
        events.load(value["identity"])
    with pytest.raises(FailureRecordError):
        events.save(value)
    assert path.read_text() == payload


@pytest.mark.parametrize(
    "field,value",
    [
        ("identity", "0" * 64),
        ("diagnostics", ["SECRET"]),
        ("exit_code", True),
        ("failed_at", "yesterday"),
        ("rollback_outcome", "SECRET"),
        ("requested_commit", 42),
        ("extra", "SECRET"),
    ],
)
def test_invalid_records(tmp_path, target, field, value):
    events = FailureEvents(Store(tmp_path / "store"))
    original = events.save(record(target))
    path = events.store.root / "failure-events" / (original["identity"] + ".json")
    path.write_text(json.dumps({**original, field: value}))
    with pytest.raises(FailureRecordError):
        events.load(original["identity"])


def test_interrupted_replace_preserves_original(tmp_path, target, monkeypatch):
    events = FailureEvents(Store(tmp_path / "store"))
    value = events.save(record(target))

    def interrupted(*args):
        raise OSError("interrupted")

    monkeypatch.setattr("ai_ops_agent.deployment.model.os.replace", interrupted)
    with pytest.raises(OSError):
        events.save({**value, "rollback_outcome": "failed"})
    assert events.load(value["identity"]) == value
    assert len(list((events.store.root / "failure-events").iterdir())) == 1


def test_absent_and_invalid_identity(tmp_path):
    events = FailureEvents(Store(tmp_path / "store"))
    assert events.load("a" * 64) is None
    with pytest.raises(FailureRecordError):
        events.load("../state")


def test_failed_rollback_can_later_succeed_without_changing_failure(tmp_path, target):
    events = FailureEvents(Store(tmp_path / "store"))
    original = events.save(record(target, rollback_outcome="failed"))
    replay = record(target, rollback_outcome="succeeded", requested_ref="other")
    assert events.save(replay) == {**original, "rollback_outcome": "succeeded"}


def test_first_write_interruption_and_recovery_state_are_independent(
    tmp_path, target, monkeypatch
):
    store = Store(tmp_path / "store")
    state = {"targets": {}, "active": {"operation": "job-1"}}
    with store.lock():
        store.write(state)
    events = FailureEvents(store)
    value = record(target)
    with monkeypatch.context() as patch:

        def interrupted(*args):
            raise OSError("interrupted")

        patch.setattr("ai_ops_agent.deployment.model.os.replace", interrupted)
        with pytest.raises(OSError):
            events.save(value)
    assert events.load(value["identity"]) is None
    assert events.save(value) == value
    assert store.read() == state


def test_unsupported_version_is_reported_separately(tmp_path, target):
    events = FailureEvents(Store(tmp_path / "store"))
    value = events.save(record(target))
    path = events.store.root / "failure-events" / (value["identity"] + ".json")
    path.write_text(json.dumps({**value, "version": 2}))
    with pytest.raises(FailureRecordError, match="Unsupported"):
        events.save(value)
