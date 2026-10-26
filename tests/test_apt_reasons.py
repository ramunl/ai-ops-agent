"""Why apt skips an upgradable package, and what the upgrade then reports."""

import json
from unittest.mock import patch

from ai_ops_agent import apt_reasons, package_updates
from ai_ops_agent import package_upgrade_worker as worker
from ai_ops_agent.bot import services

LISTING = (
    "Listing...\n"
    "fwupd/noble-updates 1.9.30-0ubuntu1 amd64 [upgradable from: 1.9.29-0ubuntu1]\n"
    "curl/noble-updates 8.5.0-2 amd64 [upgradable from: 8.5.0-1]\n"
)
# apt 2.7+ (Ubuntu 24.04) names phasing itself.
NOBLE = (
    "Calculating upgrade...\n"
    "The following upgrades have been deferred due to phasing:\n"
    "  fwupd\n"
    "The following packages will be upgraded:\n"
    "  curl\n"
    "1 upgraded, 0 newly installed, 0 to remove and 1 not upgraded.\n"
)
# Older apt only says "kept back"; apt-cache policy shows the phasing.
JAMMY = (
    "The following packages have been kept back:\n"
    "  fwupd vim\n"
    "0 upgraded, 0 newly installed, 0 to remove and 2 not upgraded.\n"
)
POLICY = (
    "fwupd:\n"
    "  Installed: 1.9.29-0ubuntu1\n"
    "  Candidate: 1.9.30-0ubuntu1\n"
    "  Version table:\n"
    "     1.9.30-0ubuntu1 500 (phased 30%)\n"
    "        500 http://archive.ubuntu.com/ubuntu jammy-updates/main amd64 Packages\n"
    "vim:\n"
    "  Installed: 2:8.2\n"
)


def runner(simulation, held="", policy=POLICY, listing=LISTING, upgrade=""):
    calls = []

    def run(args, timeout):
        calls.append(args)
        if args[:2] == ["apt-get", "-s"]:
            return 0, simulation
        if args[:2] == ["apt-get", "upgrade"]:
            return 0, upgrade
        if args[0] == "apt-mark":
            return 0, held
        if args[0] == "apt-cache":
            return 0, policy
        return 0, listing

    run.calls = calls
    return run


def test_noble_phasing_header_is_read_without_more_commands():
    run = runner(NOBLE)
    assert apt_reasons.explain(run, ["fwupd", "curl"]) == {"fwupd": "phased"}


def test_kept_back_is_split_into_phased_held_and_needs_removal():
    run = runner(
        "The following packages have been kept back:\n  fwupd vim git\n0 upgraded\n",
        held="git\n",
    )
    reasons = apt_reasons.explain(run, ["fwupd", "vim", "git"])
    assert reasons == {"fwupd": "phased", "vim": "kept_back", "git": "held"}
    assert ["apt-cache", "policy", "fwupd", "vim"] in run.calls


def test_nothing_is_explained_when_the_simulation_fails():
    def run(args, timeout):
        return (100, "E: lock") if args[1] == "-s" else (0, "")

    assert apt_reasons.explain(run, ["fwupd"]) == {}
    assert apt_reasons.explain(run, ["bad name;rm"]) == {}


def test_pending_counts_only_what_the_upgrade_can_install():
    result = package_updates.pending(runner(JAMMY))
    assert result["total"] == 1
    assert result["waiting"] == 1
    assert result["stable"] == ["curl"]
    fwupd = result["packages"][0]
    assert fwupd["name"] == "fwupd"
    assert fwupd["installed_version"] == "1.9.29-0ubuntu1"
    assert fwupd["candidate_version"] == "1.9.30-0ubuntu1"
    assert fwupd["skipped"] == "phased"
    assert "phased rollout" in fwupd["reason"]
    assert "reason" not in result["packages"][1]


def test_upgrade_with_only_a_phased_package_left_is_ok_and_says_why(tmp_path):
    lock = tmp_path / "lock"
    run_id = package_updates.start_upgrade(lambda a, t: (0, ""), lock)["run_id"]
    state = package_updates.state_directory(lock)
    only_fwupd = "Listing...\n" + LISTING.splitlines()[1] + "\n"
    output = (
        "Unpacking curl (8.5.0-2) over (8.5.0-1) ...\n"
        "1 upgraded, 0 newly installed, 0 to remove and 1 not upgraded.\n"
    )
    result = worker.work(
        lock,
        state,
        run_id,
        runner(NOBLE, listing=only_fwupd, upgrade=output),
        tmp_path / "absent",
    )
    assert result["ok"] is True
    assert (result["remaining"], result["waiting"]) == (0, 1)
    assert result["upgraded"] == 1
    assert result["package_results"] == [
        {
            "name": "curl",
            "old_version": "8.5.0-1",
            "new_version": "8.5.0-2",
            "outcome": "upgraded",
        }
    ]
    assert result["remaining_packages"][0]["skipped"] == "phased"
    assert result["unchanged_reason"] is None
    log = package_updates.log_path(state).read_text()
    assert f"upgrade {run_id}: running apt-get upgrade" in log
    assert "apt: 1 upgraded, 0 newly installed" in log
    assert "upgraded curl 8.5.0-1 -> 8.5.0-2" in log
    assert "fwupd 1.9.29-0ubuntu1 -> 1.9.30-0ubuntu1 [deferred by Ubuntu" in log
    assert log.rstrip().endswith(f"upgrade {run_id}: ok")


def test_nothing_upgraded_says_why(tmp_path):
    lock = tmp_path / "lock"
    run_id = package_updates.start_upgrade(lambda a, t: (0, ""), lock)["run_id"]
    state = package_updates.state_directory(lock)
    only_fwupd = "Listing...\n" + LISTING.splitlines()[1] + "\n"
    result = worker.work(
        lock,
        state,
        run_id,
        runner(JAMMY, listing=only_fwupd, upgrade=JAMMY),
        tmp_path / "absent",
    )
    assert result["ok"] is True
    assert result["upgraded"] == 0
    assert result["unchanged_reason"].startswith("fwupd: deferred by Ubuntu")


def test_status_mode_lists_without_refreshing(tmp_path, capsys):
    run = runner(NOBLE)
    with patch.object(package_updates.os, "geteuid", return_value=0):
        code = package_updates.main(
            ["status"], run, tmp_path / "lock", tmp_path / "log", tmp_path / "x"
        )
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert (result["total"], result["waiting"]) == (1, 1)
    assert not any(call[:2] == ["apt-get", "update"] for call in run.calls)
    assert "status: 1 available, 1 waiting" in (tmp_path / "log").read_text()


def test_bot_shows_the_package_log(tmp_path):
    log = tmp_path / "log"
    assert services.package_log(log) == "No package runs logged yet."
    log.write_text("".join(f"line {number}\n" for number in range(50)))
    text = services.package_log(log)
    assert text.splitlines() == [f"line {number}" for number in range(10, 50)]
