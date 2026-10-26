"""ai-packages: the fixed check/upgrade command the dashboard uses."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_ops_agent import package_updates

REPO = Path(__file__).resolve().parent.parent
LISTING = (
    "Listing...\n"
    "openssl/noble-security 3.0.13-1 amd64 [upgradable from: 3.0.13-0]\n"
    "curl/noble-updates 8.5.0-2 amd64 [upgradable from: 8.5.0-1]\n"
    "vim/noble-proposed 9.1-2 amd64 [upgradable from: 9.1-1]\n"
)


class FakeRun:
    """Answer by the first two words of the command; record every call."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = {"apt list": (0, LISTING), **(answers or {})}

    def __call__(self, args, timeout):
        self.calls.append(args)
        return self.answers.get(" ".join(args[:2]), (0, ""))


@pytest.fixture
def cli(tmp_path):
    def call(argv, run):
        with patch.object(package_updates.os, "geteuid", return_value=0):
            return package_updates.main(
                argv,
                run,
                lock=tmp_path / "lock",
                log=tmp_path / "log",
                reboot_flag=tmp_path / "reboot-required",
            )

    return call


def test_check_refreshes_then_groups_updates(tmp_path):
    run = FakeRun()
    result = package_updates.check(run, tmp_path / "absent")
    assert [call[:2] for call in run.calls] == [
        ["apt-get", "update"],
        ["apt", "list"],
        ["apt-get", "-s"],
    ]
    assert result["ok"] is True
    assert result["total"] == 3
    assert result["security"] == ["openssl"]
    assert result["stable"] == ["curl"]
    assert result["untested"] == ["vim"]
    assert result["reboot_required"] is False


def test_check_stops_when_refresh_fails(tmp_path):
    run = FakeRun({"apt-get update": (100, "E: Could not get lock")})
    result = package_updates.check(run, tmp_path / "absent")
    assert result == {"ok": False, "error": "E: Could not get lock"}
    assert len(run.calls) == 1


@pytest.mark.parametrize(
    "argv", [[], ["run"], ["upgrade", "curl"], ["check", "-o", "x"], ["--help"]]
)
def test_only_the_two_fixed_modes_are_accepted(cli, capsys, argv):
    run = FakeRun()
    assert cli(argv, run) == 2
    assert "usage" in json.loads(capsys.readouterr().out)["error"]
    assert run.calls == []


def test_cli_requires_root(capsys):
    run = FakeRun()
    with patch.object(package_updates.os, "geteuid", return_value=1000):
        assert package_updates.main(["upgrade"], run) == 1
    assert "root" in json.loads(capsys.readouterr().out)["error"]
    assert run.calls == []


def test_cli_runs_and_logs(cli, capsys, tmp_path):
    assert cli(["check"], FakeRun()) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 3
    assert "check: 3 available" in (tmp_path / "log").read_text()


def test_cli_refuses_a_second_run(cli, capsys, tmp_path):
    run = FakeRun()
    with package_updates.exclusive(tmp_path / "lock") as held:
        assert held
        assert cli(["upgrade"], run) == 1
    assert "already active" in json.loads(capsys.readouterr().out)["error"]
    assert run.calls == []


def test_wrapper_runs_without_the_bot_environment(tmp_path):
    done = subprocess.run(
        [str(REPO / "deploy" / "ai-packages"), "bogus"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "AI_OPS_AGENT_DIR": str(REPO)},
        check=False,
    )
    assert done.returncode == 2
    assert "usage" in json.loads(done.stdout)["error"]


@pytest.mark.parametrize(
    "journal", [(0, "9 upgraded, 0 newly installed"), (1, "journal unavailable")]
)
def test_launch_errors_are_not_replaced_by_journal(tmp_path, journal):
    run = FakeRun(
        {
            "systemd-run --quiet": (1, "launcher denied"),
            "journalctl -u": journal,
        }
    )
    result = package_updates.start_upgrade(run, tmp_path / "lock")
    assert result["execution"] == "launch_failed"
    assert result["launcher_error"] == "launcher denied"
    assert result["diagnostics"] == ""
    assert result["upgraded"] is None
    assert len(run.calls) == 1


def test_caller_timeout_does_not_allow_retry(tmp_path):
    run = FakeRun()
    lock = tmp_path / "lock"
    result = package_updates.upgrade(run, lock=lock, wait_timeout=0)
    assert result["execution"] == "unknown"
    assert result["remaining"] is None
    assert package_updates.start_upgrade(run, lock)["busy"]
    assert len(run.calls) == 1
    assert "--wait" not in run.calls[0]
    assert "--pipe" not in run.calls[0]


def test_launcher_timeout_is_unresolved(tmp_path):
    run = FakeRun({"systemd-run --quiet": (124, "timed out")})
    result = package_updates.start_upgrade(run, tmp_path / "lock")
    assert result["execution"] == "unknown"
    assert not result["completion_confirmed"]
    assert result["apt_exit_code"] is None


def test_upgrade_never_forces_or_removes_packages():
    assert package_updates.APT_UPGRADE[:2] == ["apt-get", "upgrade"]
    assert "-y" in package_updates.APT_UPGRADE
    assert not {"dist-upgrade", "full-upgrade", "autoremove"} & set(
        package_updates.APT_UPGRADE
    )


def test_corrupt_reservation_fails_closed(tmp_path):
    state = package_updates.state_directory(tmp_path / "lock")
    state.mkdir()
    (state / "active.json").write_text("broken")
    run = FakeRun()
    result = package_updates.start_upgrade(run, tmp_path / "lock")
    assert result["execution"] == "unknown"
    assert "do not retry" in result["error"]
    assert run.calls == []
