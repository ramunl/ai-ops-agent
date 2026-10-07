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
    assert [call[:2] for call in run.calls] == [["apt-get", "update"], ["apt", "list"]]
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


def test_upgrade_counts_and_reports_reboot(tmp_path):
    flag = tmp_path / "reboot-required"
    flag.write_text("")
    out = "Reading...\n2 upgraded, 0 newly installed, 0 to remove and 1 not upgraded."
    run = FakeRun({"systemd-run --quiet": (0, out)})
    result = package_updates.upgrade(run, flag)
    assert run.calls[0] == package_updates.UPGRADE
    assert result["upgraded"] == 2
    assert result["remaining"] == 3
    assert result["reboot_required"] is True


def test_upgrade_failure_keeps_the_last_lines(tmp_path):
    run = FakeRun({"systemd-run --quiet": (100, "a\nb\nE: dpkg was interrupted")})
    result = package_updates.upgrade(run, tmp_path / "absent")
    assert result["ok"] is False
    assert result["error"].endswith("E: dpkg was interrupted")


def test_upgrade_never_removes_packages_or_asks():
    assert package_updates.APT_UPGRADE[:2] == ["apt-get", "upgrade"]
    assert "-y" in package_updates.APT_UPGRADE
    assert not {"dist-upgrade", "full-upgrade", "autoremove"} & set(
        package_updates.UPGRADE
    )


def test_upgrade_runs_in_its_own_unit_so_a_caller_restart_cannot_break_dpkg():
    assert package_updates.UPGRADE[0] == "systemd-run"
    assert "--wait" in package_updates.UPGRADE
    assert package_updates.UPGRADE[-len(package_updates.APT_UPGRADE) :] == (
        package_updates.APT_UPGRADE
    )


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


def test_upgrade_output_is_independent_of_the_dashboard(tmp_path):
    run = FakeRun({"journalctl -u": (0, "4 upgraded, 0 newly installed")})
    result = package_updates.upgrade(run, tmp_path / "absent")
    assert "--pipe" not in package_updates.UPGRADE
    assert "--unit=ai-packages-upgrade" in package_updates.UPGRADE
    assert result["upgraded"] == 4


def test_upgrade_does_not_report_zero_remaining_when_recheck_fails(tmp_path):
    run = FakeRun({"apt list": (100, "E: unavailable")})
    result = package_updates.upgrade(run, tmp_path / "absent")
    assert result["ok"] is False
    assert "recheck failed" in result["error"]
