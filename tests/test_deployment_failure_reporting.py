"""Failure handoff integration with isolated deployment transactions."""

import pytest
from test_deployment_manager import manager as manager_fixture

from ai_ops_agent.deployment.failure_events import FailureEvents
from ai_ops_agent.deployment.pm_delivery import PMDelivery

manager = manager_fixture


def test_install_failure_creates_ticket_after_recovery(manager):
    manager.system.fail_install = True
    job = manager.reserve("deploy", "test-agent", "main")
    assert not manager.execute(job["operation"])
    record = manager.status()["targets"][0]
    event = FailureEvents(manager.store).load(record["failure_identity"])
    assert event["stage"] == "install"
    assert event["rollback_outcome"] == "succeeded"
    delivery = PMDelivery(manager.store).load(event["identity"])
    assert "rollback=succeeded" in delivery["ticket"]["text"]
    assert record["ticket_status"] == "pending"
    assert record["confirmed_ticket_id"] is None
    assert record["history"][-1]["failure_identity"] == event["identity"]


def test_preflight_failure_is_journaled(manager):
    manager.system.resolve = lambda *args: (_ for _ in ()).throw(OSError("unavailable"))
    with pytest.raises(OSError):
        manager.reserve("deploy", "test-agent", "main")
    record = manager.status()["targets"][0]
    event = FailureEvents(manager.store).load(record["failure_identity"])
    assert event["stage"] == "preflight"
    assert event["rollback_outcome"] == "not_required"
    assert manager.store.read()["active"] is None


def test_pre_start_evidence_survives_restart(manager):
    evidence = ("ActiveState=failed", "Failed at step USER: secret")
    manager.system.evidence = lambda target: evidence
    restart = manager.system.restart

    def clear_evidence(target):
        nonlocal evidence
        evidence = ("", "")
        restart(target)

    manager.system.restart = clear_evidence
    manager.system.fail_restarts = 1
    job = manager.reserve("deploy", "test-agent", "main")
    assert not manager.execute(job["operation"])
    record = manager.status()["targets"][0]
    event = FailureEvents(manager.store).load(record["failure_identity"])
    assert "systemd: 217/USER" in event["diagnostics"]
    assert "secret" not in str(event)


def test_rollback_failure_keeps_original_and_separate_recovery_error(manager):
    original = manager.system.install

    def fail_install(target):
        original(target)
        manager.system.fail_restarts = 1
        from ai_ops_agent.deployment.system import CommandError

        raise CommandError("Install failed", 23)

    manager.system.install = fail_install
    job = manager.reserve("deploy", "test-agent", "main")
    assert not manager.execute(job["operation"])
    record = manager.status()["targets"][0]
    assert record["original_error"] == "Install failed"
    assert record["recovery_error"] == "Failed health verification"
    assert record["failure_exit_code"] == 23
    assert record["rollback_outcome"] == "failed"
    event = FailureEvents(manager.store).load(record["failure_identity"])
    assert event["exit_code"] == 23
    assert event["rollback_outcome"] == "failed"
    assert len(list((manager.store.root / "failure-events").glob("*.json"))) == 2


def test_interrupted_enqueue_replays_without_duplicate(manager, monkeypatch):
    enqueue = manager.reporting.delivery.enqueue

    def crash(event):
        enqueue(event)
        raise KeyboardInterrupt

    monkeypatch.setattr(manager.reporting.delivery, "enqueue", crash)
    manager.system.fail_install = True
    job = manager.reserve("deploy", "test-agent", "main")
    with pytest.raises(KeyboardInterrupt):
        manager.execute(job["operation"])
    assert manager.store.read()["failure_intents"]
    assert manager.store.read()["active"] is None
    monkeypatch.setattr(manager.reporting.delivery, "enqueue", enqueue)
    assert manager.reporting.flush()
    assert not manager.store.read()["failure_intents"]
    assert len(list((manager.store.root / "pm-delivery").glob("*.json"))) == 1


def test_reporting_storage_failure_does_not_prevent_rollback(manager, monkeypatch):
    def unavailable(event):
        raise OSError("sensitive path")

    monkeypatch.setattr(manager.reporting.events, "save", unavailable)
    manager.system.fail_install = True
    job = manager.reserve("deploy", "test-agent", "main")
    assert not manager.execute(job["operation"])
    record = manager.status()["targets"][0]
    assert record["rollback_outcome"] == "succeeded"
    assert record["reporting_error"] == "Failure reporting unavailable; retry pending"
    assert "sensitive" not in str(record)
    assert manager.store.read()["failure_intents"]


def test_offline_delivery_retries_without_recovery_actions(manager):
    from datetime import UTC, datetime, timedelta

    from ai_ops_agent.deployment.pm_bridge import BridgeError

    class Bridge:
        offline = True

        def add(self, ticket):
            with manager.store.lock():
                pass  # Bridge I/O must occur outside the transaction lock.
            if self.offline:
                raise BridgeError("unavailable")
            return ticket["id"]

    bridge = Bridge()
    manager.reporting.delivery.bridge = bridge
    manager.system.fail_install = True
    job = manager.reserve("deploy", "test-agent", "main")
    assert not manager.execute(job["operation"])
    actions = list(manager.system.commands)
    assert manager.reporting.retry()["attempted"] == 1
    identity = manager.status()["targets"][0]["failure_identity"]
    delivery = manager.reporting.delivery.load(identity)
    assert delivery["status"] == "pending"
    assert delivery["attempts"] == 1
    assert manager.reporting.retry()["attempted"] == 0
    bridge.offline = False
    manager.reporting.delivery.attempt_due(
        identity, now=datetime.now(UTC) + timedelta(minutes=2)
    )
    record = manager.status()["targets"][0]
    assert record["ticket_status"] == "delivered"
    assert record["confirmed_ticket_id"]
    assert record["history"][-1]["confirmed_ticket_id"] == record["confirmed_ticket_id"]
    # status uses git but retry must never restart, checkout or install.
    assert [c for c in manager.system.commands[len(actions) :] if c[0] != "git"] == []


def test_interrupted_install_reports_after_offline_recovery(manager):
    manager.system.interrupt_install = True
    job = manager.reserve("deploy", "test-agent", "main")
    with pytest.raises(KeyboardInterrupt):
        manager.execute(job["operation"])
    manager.system.interrupt_install = False
    assert not manager.execute(job["operation"])
    record = manager.status()["targets"][0]
    assert record["failure_stage"] == "interrupted"
    assert record["rollback_outcome"] == "succeeded"
    assert record["ticket_status"] == "pending"


def test_waited_cli_failure_keeps_results(manager, monkeypatch, capsys):
    import json

    from ai_ops_agent.deployment import cli
    from ai_ops_agent.deployment.system import CommandError

    original = manager.system.run

    def run(args, *positional, **kwargs):
        if args[0] == "systemd-run":
            assert not manager.execute(args[-1])
            raise CommandError("Worker failed", 1)
        return original(args, *positional, **kwargs)

    manager.system.fail_install = True
    manager.system.run = run
    monkeypatch.setattr(cli, "Manager", lambda *args: manager)
    monkeypatch.setattr(cli, "Store", lambda: manager.store)
    assert cli.main(["deploy", "test-agent", "main"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "Worker failed"
    assert result["targets"][0]["ticket_status"] == "pending"
    assert result["active"] is None


def test_scheduler_failure_creates_event(manager):
    from ai_ops_agent.deployment.cli import ScheduledFailureError, schedule
    from ai_ops_agent.deployment.model import DeploymentError

    job = manager.reserve("deploy", "test-agent", "main")

    def fail(*args, **kwargs):
        raise DeploymentError("Scheduler failed")

    manager.system.run = fail
    with pytest.raises(ScheduledFailureError):
        schedule(manager, job, wait=True)
    assert manager.store.read()["active"] is None
    record = manager.status()["targets"][0]
    assert record["failure_stage"] == "scheduling"
    assert record["ticket_status"] == "pending"


def test_crash_after_failure_intent_precedes_recovery(manager, monkeypatch):
    original = manager._recover_interrupted

    def crash(state, job):
        intent = next(iter(manager.store.read()["failure_intents"].values()))
        assert intent["rollback_outcome"] == "pending"
        assert intent["stage"] == "install"
        assert not list((manager.store.root / "pm-delivery").glob("*.json"))
        raise KeyboardInterrupt

    monkeypatch.setattr(manager, "_recover_interrupted", crash)
    manager.system.fail_install = True
    job = manager.reserve("deploy", "test-agent", "main")
    with pytest.raises(KeyboardInterrupt):
        manager.execute(job["operation"])
    assert not list((manager.store.root / "pm-delivery").glob("*.json"))
    monkeypatch.setattr(manager, "_recover_interrupted", original)
    assert not manager.execute(job["operation"])
    record = manager.status()["targets"][0]
    event = FailureEvents(manager.store).load(record["failure_identity"])
    assert event["stage"] == "install"
    assert event["rollback_outcome"] == "succeeded"
    assert record["original_error"] == "Dependency install failed"


def test_intent_write_failure_is_secondary_to_recovery(manager, monkeypatch):
    write = manager.store.write
    failed = False

    def fail_once(state):
        nonlocal failed
        if state.get("failure_intents") and not failed:
            failed = True
            raise OSError("private storage diagnostic")
        write(state)

    monkeypatch.setattr(manager.store, "write", fail_once)
    manager.system.fail_install = True
    job = manager.reserve("deploy", "test-agent", "main")
    assert not manager.execute(job["operation"])
    record = manager.status()["targets"][0]
    assert failed
    assert record["rollback_outcome"] == "succeeded"
    assert record["original_error"] == "Dependency install failed"
    assert record["ticket_status"] == "pending"
    assert "private storage diagnostic" not in str(manager.store.read())


def test_retry_contention_does_not_lose_intents(manager):
    manager.system.fail_install = True
    job = manager.reserve("deploy", "test-agent", "main")
    assert not manager.execute(job["operation"])
    before = manager.store.read()
    with manager.store.lock():
        result = manager.reporting.retry()
    assert result["ok"] is False
    assert manager.store.read() == before
