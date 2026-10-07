"""ai-service: the whitelisted restart command the dashboard uses."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from ai_ops_agent import service_control
from ai_ops_agent.services_list import MANAGED_SERVICES

REPO = Path(__file__).resolve().parent.parent


class FakeRun:
    def __init__(self, code=0, out=""):
        self.calls, self.code, self.out = [], code, out

    def __call__(self, args, timeout):
        self.calls.append(args)
        return self.code, self.out


def test_whitelist_covers_every_agent_and_the_dashboard():
    assert MANAGED_SERVICES == [
        "ai-coding-agent",
        "ai-pm-agent",
        "ai-ops-agent",
        "ai-dashboard",
    ]


def test_restart_is_queued_without_blocking():
    run = FakeRun()
    assert service_control.restart("ai-pm-agent", run) == {
        "ok": True,
        "service": "ai-pm-agent",
        "queued": True,
    }
    assert run.calls == [["systemctl", "--no-block", "restart", "ai-pm-agent"]]


def test_unknown_or_crafted_names_never_reach_systemctl():
    run = FakeRun()
    for name in ("sshd", "--force", "ai-pm-agent --now", "../ai-pm-agent", ""):
        assert service_control.restart(name, run)["ok"] is False
    assert run.calls == []


def test_systemctl_failure_is_reported():
    result = service_control.restart("ai-dashboard", FakeRun(1, "Unit not found"))
    assert result == {"ok": False, "error": "Unit not found"}


def test_cli(capsys, tmp_path):
    assert service_control.main(["list"]) == 0
    assert json.loads(capsys.readouterr().out)["services"] == MANAGED_SERVICES
    assert service_control.main(["stop", "ai-pm-agent"]) == 2
    assert "usage" in json.loads(capsys.readouterr().out)["error"]
    with patch.object(service_control.os, "geteuid", return_value=1000):
        assert service_control.main(["restart", "ai-pm-agent"]) == 1
    assert "root" in json.loads(capsys.readouterr().out)["error"]
    with (
        patch.object(service_control.os, "geteuid", return_value=0),
        patch.object(service_control, "LOG", str(tmp_path / "svc.log")),
    ):
        assert service_control.main(["restart", "ai-pm-agent"], FakeRun()) == 0
    assert "restart ai-pm-agent: queued" in (tmp_path / "svc.log").read_text()


def test_wrapper_runs_without_bot_credentials():
    # The dashboard runs it with no OPS_TELEGRAM_BOT_TOKEN in its environment.
    done = subprocess.run(
        ["sh", str(REPO / "deploy" / "ai-service"), "list"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "AI_OPS_AGENT_DIR": str(REPO)},
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(done.stdout)["services"] == MANAGED_SERVICES


def test_reboot_is_queued_without_blocking(tmp_path):
    run = FakeRun()
    assert service_control.reboot(run, {tmp_path / "absent": "x"}) == {
        "ok": True,
        "queued": True,
    }
    assert run.calls == [
        [
            "systemd-run",
            "--quiet",
            "--collect",
            "--unit=ai-server-reboot",
            "--on-active=5s",
            "/usr/bin/systemctl",
            "reboot",
        ]
    ]


def test_reboot_is_refused_while_protected_work_holds_its_lock(tmp_path):
    import fcntl

    lock = tmp_path / "ai-packages.lock"
    run = FakeRun()
    with open(lock, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        result = service_control.reboot(run, {lock: "a package upgrade"})
    assert result == {
        "ok": False,
        "busy": True,
        "error": "a package upgrade is running",
    }
    assert run.calls == []
    # A lock file left behind by a finished run does not block.
    assert service_control.reboot(run, {lock: "a package upgrade"})["ok"] is True


def test_reboot_watches_every_long_running_ops_command():
    from ai_ops_agent import cleanup, package_updates, tool_updates

    watched = set(service_control.BUSY_LOCKS)
    assert {package_updates.LOCK, tool_updates.LOCK, cleanup.Paths().lock} <= watched


def test_reboot_cli_takes_no_arguments_and_needs_root(capsys, tmp_path):
    run = FakeRun()
    for argv in (["reboot", "--force"], ["reboot", "now"], ["poweroff"]):
        assert service_control.main(argv, run) == 2
    with patch.object(service_control.os, "geteuid", return_value=1000):
        assert service_control.main(["reboot"], run) == 1
    assert run.calls == []
    capsys.readouterr()
    with (
        patch.object(service_control.os, "geteuid", return_value=0),
        patch.object(service_control, "LOG", str(tmp_path / "svc.log")),
    ):
        assert service_control.main(["reboot"], run, {}) == 0
    assert json.loads(capsys.readouterr().out)["queued"] is True
    assert "reboot: queued" in (tmp_path / "svc.log").read_text()


def test_reboot_refuses_detached_upgrade_without_a_caller_lock(tmp_path):
    run = FakeRun(0, "active")
    with patch.object(service_control, "BUSY_LOCKS", {tmp_path / "absent": "upgrade"}):
        result = service_control.reboot(run)
    assert result["busy"] is True
    assert "package upgrade" in result["error"]
    assert not any(call[0] == "systemd-run" for call in run.calls)
