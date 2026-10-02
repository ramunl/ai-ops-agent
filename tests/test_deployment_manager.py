"""Exercise real Git checkouts and isolated runtime/artifact rollback transactions."""

import json
import subprocess
from pathlib import Path

import pytest

from ai_ops_agent.deployment.cli import parser, recover, schedule
from ai_ops_agent.deployment.engine import Manager
from ai_ops_agent.deployment.model import DeploymentError, Store, Target, targets
from ai_ops_agent.deployment.system import System


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


class FakeSystem(System):
    def __init__(self):
        self.fail_install = False
        self.fail_restarts = 0
        self.restarts = []
        self.commands = []
        self.worker_active = False
        self.interrupt_install = False

    def run(self, args, timeout=180, *, environment=None):
        self.commands.append(args)
        if args[0] == "systemctl":
            if args[1] == "is-active" and not self.worker_active:
                raise DeploymentError("Inactive worker")
            return ""
        if args[0] == "systemd-run":
            return "Started job"
        if args[0].endswith("/bin/pip"):
            venv = Path(args[0]).parent.parent
            requirements = Path(args[-1])
            (venv / "dependency").write_text(
                requirements.parent.joinpath("VERSION").read_text()
            )
            if self.interrupt_install:
                raise KeyboardInterrupt()
            if self.fail_install:
                raise DeploymentError("Dependency install failed")
            return ""
        return super().run(args, timeout, environment=environment)

    def restart(self, target):
        self.restarts.append(git(target.repo, "rev-parse", "HEAD"))
        if self.fail_restarts:
            self.fail_restarts -= 1
            raise DeploymentError("Failed health verification")


@pytest.fixture
def manager(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(origin)], check=True, capture_output=True
    )
    git(origin, "config", "user.email", "test@example.test")
    git(origin, "config", "user.name", "Test")
    (origin / "VERSION").write_text("1.0.0")
    (origin / "requirements.txt").write_text("")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "baseline")
    repo = tmp_path / "checkout"
    subprocess.run(
        ["git", "clone", str(origin), str(repo)], check=True, capture_output=True
    )
    (origin / "VERSION").write_text("2.0.0")
    (origin / "deploy").mkdir()
    (origin / "deploy/bridge").write_text("NEW bridge")
    (origin / "deploy/unit").write_text("NEW unit")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "release")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "dependency").write_text("OLD exact dependency")
    unit = tmp_path / "installed/unit"
    unit.parent.mkdir()
    unit.write_text("OLD unit")
    target = Target(
        "test-agent",
        repo,
        venv,
        (
            ("deploy/bridge", tmp_path / "installed/bridge", 0o755),
            ("deploy/unit", unit, 0o644),
        ),
    )
    return Manager(Store(tmp_path / "state"), {target.name: target}, FakeSystem())


def test_deploy_preserves_branch_and_restores_exact_dependencies_and_artifacts(manager):
    target = manager.fleet["test-agent"]
    before = git(target.repo, "rev-parse", "main")
    job = manager.reserve("deploy", target.name, "main")
    assert manager.execute(job["operation"])
    status = manager.status()["targets"][0]
    assert status["status"] == "healthy"
    assert status["current"]["version"] == "2.0.0"
    assert status["previous"]["commit"] == before
    assert status["previous"]["verified_at"]
    assert git(target.repo, "rev-parse", "main") == before
    assert git(target.repo, "branch", "--show-current") == ""
    assert (target.artifacts[0][1]).read_text() == "NEW bridge"
    job = manager.reserve("rollback", target.name, None, before)
    assert manager.execute(job["operation"])
    assert (target.venv / "dependency").read_text() == "OLD exact dependency"
    assert not target.artifacts[0][1].exists()
    assert target.artifacts[1][1].read_text() == "OLD unit"
    assert manager.status()["targets"][0]["current"]["version"] == "1.0.0"
    assert len(list(manager.store.root.glob("test-agent-*"))) == 1


def test_failed_install_automatically_restores_original_runtime(manager):
    manager.system.fail_install = True
    target = manager.fleet["test-agent"]
    before = git(target.repo, "rev-parse", "HEAD")
    job = manager.reserve("deploy", target.name, "main")
    assert not manager.execute(job["operation"])
    assert git(target.repo, "rev-parse", "HEAD") == before
    assert (target.venv / "dependency").read_text() == "OLD exact dependency"
    record = manager.status()["targets"][0]
    assert record["status"] == "failed"
    assert "restored and verified" in record["error"]
    assert manager.store.read()["active"] is None


def test_failed_baseline_is_not_adopted_as_known_good(manager):
    manager.system.fail_restarts = 1
    job = manager.reserve("deploy", "test-agent", "main")
    assert not manager.execute(job["operation"])
    record = manager.status()["targets"][0]
    assert record["current"] is None and record["previous"] is None
    assert not list(manager.store.root.glob("test-agent-*"))


def test_failed_recovery_retains_backup_and_can_retry(manager):
    original_install = manager.system.install

    def fail_after_install(target):
        original_install(target)
        manager.system.fail_restarts = 2

    manager.system.install = fail_after_install
    job = manager.reserve("deploy", "test-agent", "main")
    assert not manager.execute(job["operation"])
    state = manager.store.read()
    assert state["targets"]["test-agent"]["status"] == "rollback_failed"
    assert Path(state["active"]["recovery"]["backup"]).is_dir()
    assert manager.execute(job["operation"]) is False
    assert manager.store.read()["active"] is None
    assert "restored and verified" in manager.status()["targets"][0]["error"]


def test_interrupted_worker_can_restore_without_repeating_deploy(manager):
    manager.system.interrupt_install = True
    job = manager.reserve("deploy", "test-agent", "main")
    with pytest.raises(KeyboardInterrupt):
        manager.execute(job["operation"])
    assert manager.store.read()["active"]["recovery"]
    manager.system.interrupt_install = False
    assert manager.execute(job["operation"]) is False
    assert manager.status()["targets"][0]["current"]["version"] == "1.0.0"


def test_unknown_refs_targets_dirty_checkout_and_duplicate_jobs_refused(manager):
    with pytest.raises(DeploymentError, match="Unknown"):
        manager.reserve("deploy", "arbitrary", "main")
    for ref in ("no-such-ref", "--upload-pack=evil", "main;echo malicious"):
        with pytest.raises(DeploymentError):
            manager.reserve("deploy", "test-agent", ref)
    target = manager.fleet["test-agent"]
    (target.repo / "untracked").write_text("work")
    with pytest.raises(DeploymentError, match="local changes"):
        manager.reserve("deploy", target.name, "main")
    (target.repo / "untracked").unlink()
    manager.reserve("deploy", target.name, "main")
    with pytest.raises(DeploymentError, match="queued or running"):
        manager.reserve("deploy", target.name, "main")


def test_stale_rollback_confirmation_does_not_queue(manager):
    job = manager.reserve("deploy", "test-agent", "main")
    assert manager.execute(job["operation"])
    with pytest.raises(DeploymentError, match="changed"):
        manager.reserve("rollback", "test-agent", None, "a" * 40)
    assert manager.store.read()["active"] is None


def test_initial_status_reports_unverified_observed_checkout(manager):
    record = manager.status()["targets"][0]
    assert record["status"] == "untracked"
    assert record["observed_commit"]
    assert record["current"] is None


def test_state_is_private_and_corrupt_state_is_not_discarded(manager):
    manager.store.write(manager.store.read())
    path = manager.store.root / "state.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert manager.store.root.stat().st_mode & 0o777 == 0o700
    path.write_text("bad JSON")
    with pytest.raises(DeploymentError, match="corrupt"):
        manager.status()


def test_scheduler_runs_worker_independently_and_supports_wait(manager):
    job = manager.reserve("deploy", "test-agent", "main")
    result = schedule(manager, job, wait=True)
    command = manager.system.commands[-1]
    assert command[0] == "systemd-run"
    assert "--wait" in command
    assert command[-3:] == ["/usr/local/sbin/ai-deploy", "execute", job["operation"]]
    assert result["status"] == "queued"


def test_recover_refuses_live_worker_and_clears_only_interrupted_queue(manager):
    manager.reserve("deploy", "test-agent", "main")
    manager.system.worker_active = True
    with pytest.raises(DeploymentError, match="still active"):
        recover(manager)
    manager.system.worker_active = False
    assert recover(manager)["status"] == "failed"
    assert manager.store.read()["active"] is None


def test_batch_stops_after_failure_and_reports_remaining_not_run(manager, tmp_path):
    first = manager.fleet["test-agent"]
    second = Target("other-agent", first.repo, first.venv, first.artifacts)
    manager.fleet[second.name] = second
    manager.system.fail_install = True
    job = manager.reserve("deploy", "all", "main")
    assert not manager.execute(job["operation"])
    assert manager.store.read()["targets"]["other-agent"]["status"] == "not_run"


def test_fixed_targets_include_service_units_and_required_bridges():
    fleet = targets()
    assert set(fleet) == {
        "ai-coding-agent",
        "ai-pm-agent",
        "ai-ops-agent",
        "ai-dashboard",
    }
    assert fleet["ai-pm-agent"].artifacts[0][0] == "ai-pm-agent.service"
    assert fleet["ai-ops-agent"].artifacts[0][0] == "ai-ops-agent.service"
    assert fleet["ai-pm-agent"].artifacts[1][1] == Path("/usr/local/sbin/ai-pm-todos")
    assert fleet["ai-dashboard"].health_url.endswith("/healthz")


def test_cli_rejects_no_restart_semantically_and_parses_atomic_confirmation():
    args = parser().parse_args(
        ["submit", "rollback", "ai-pm-agent", "--expected-commit", "a" * 40]
    )
    assert args.expected_commit == "a" * 40
    assert (
        parser()
        .parse_args(["deploy", "ai-coding-agent", "main", "--no-restart"])
        .no_restart
    )


def test_health_waits_for_startup_then_requires_ten_seconds_stability(
    monkeypatch, tmp_path
):
    system = System()
    target = Target("test", tmp_path, tmp_path)
    readings = iter([DeploymentError("starting"), 9, *([9] * 10)])
    sleeps = []

    def process(_):
        item = next(readings)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(system, "process", process)
    monkeypatch.setattr("ai_ops_agent.deployment.system.time.sleep", sleeps.append)
    system.healthy(target, old_pid=8)
    assert sleeps == [1] * 11


def test_health_rejects_process_crashing_after_initial_three_seconds(
    monkeypatch, tmp_path
):
    system = System()
    target = Target("test", tmp_path, tmp_path)
    readings = iter([9, 9, 9, 9, 10])
    monkeypatch.setattr(system, "process", lambda _: next(readings))
    monkeypatch.setattr("ai_ops_agent.deployment.system.time.sleep", lambda _: None)
    with pytest.raises(DeploymentError, match="restarting repeatedly"):
        system.healthy(target, old_pid=8)


def test_health_rejects_dashboard_application_failure(monkeypatch, tmp_path):
    system = System()
    target = Target("test", tmp_path, tmp_path, health_url="http://127.0.0.1/healthz")
    monkeypatch.setattr(system, "process", lambda _: 9)
    monkeypatch.setattr("ai_ops_agent.deployment.system.time.sleep", lambda _: None)

    def fail_health(*_, **__):
        raise OSError("offline")

    monkeypatch.setattr(
        "ai_ops_agent.deployment.system.urllib.request.urlopen", fail_health
    )
    with pytest.raises(DeploymentError, match="application health"):
        system.healthy(target, old_pid=8)


def test_timeout_kills_descendant_that_would_modify_restored_runtime(tmp_path):
    import sys
    import time

    sentinel = tmp_path / "sentinel"
    child = (
        "import time,pathlib; time.sleep(0.8); pathlib.Path(%r).write_text('late')"
        % str(sentinel)
    )
    parent = (
        "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',%r]); time.sleep(10)"
        % child
    )
    with pytest.raises(DeploymentError, match="could not complete"):
        System().run([sys.executable, "-c", parent], timeout=0.1)
    time.sleep(1)
    assert not sentinel.exists()


def test_manager_lock_rejects_second_writer(manager):
    with manager.store.lock():
        with pytest.raises(DeploymentError, match="transaction is running"):
            with manager.store.lock():
                pass


def test_runtime_snapshot_capacity_failure_does_not_modify_checkout(
    manager, monkeypatch
):
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        "ai_ops_agent.deployment.backup.shutil.disk_usage", lambda _: usage(1, 1, 0)
    )
    before = git(manager.fleet["test-agent"].repo, "rev-parse", "HEAD")
    job = manager.reserve("deploy", "test-agent", "main")
    assert not manager.execute(job["operation"])
    assert git(manager.fleet["test-agent"].repo, "rev-parse", "HEAD") == before
    assert "Insufficient space" in manager.status()["targets"][0]["error"]


def test_aliases_resolve_to_fixed_targets(manager):
    target = manager.fleet.pop("test-agent")
    manager.fleet["ai-pm-agent"] = Target(
        "ai-pm-agent", target.repo, target.venv, target.artifacts
    )
    job = manager.reserve("deploy", "pm", "main")
    assert job["target"] == "ai-pm-agent"


def test_cli_no_restart_does_not_queue_and_prints_json(monkeypatch, manager, capsys):
    from ai_ops_agent.deployment import cli

    monkeypatch.setattr(cli, "Store", lambda: manager.store)
    monkeypatch.setattr(cli, "targets", lambda: manager.fleet)
    monkeypatch.setattr(cli, "System", lambda: manager.system)
    assert cli.main(["deploy", "test-agent", "main", "--no-restart"]) == 1
    assert "cannot verify" in json.loads(capsys.readouterr().out)["error"]
    assert manager.store.read()["active"] is None


def test_recovery_journal_is_committed_before_backup_deleted(manager, monkeypatch):
    import shutil

    manager.system.fail_install = True
    original = shutil.rmtree
    checked = []

    def inspect_before_delete(path, **kwargs):
        if Path(path).name.startswith("test-agent-"):
            state = manager.store.read()
            assert "recovery" not in state["active"]
            assert state["targets"]["test-agent"]["current"]["verified_at"]
            checked.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(
        "ai_ops_agent.deployment.engine.shutil.rmtree", inspect_before_delete
    )
    job = manager.reserve("deploy", "test-agent", "main")
    assert manager.execute(job["operation"]) is False
    assert checked


def test_target_update_log_contains_operation_and_verified_commit(manager, tmp_path):
    target = manager.fleet["test-agent"]
    log = tmp_path / "logs/update.log"
    manager.fleet[target.name] = Target(
        target.name, target.repo, target.venv, target.artifacts, log_file=log
    )
    job = manager.reserve("deploy", target.name, "main")
    assert manager.execute(job["operation"])
    text = log.read_text()
    assert job["operation"] in text
    assert manager.status()["targets"][0]["current"]["commit"] in text
