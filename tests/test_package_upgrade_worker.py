"""Detached execution uses fake apt runners and isolated durable state."""

import json
import subprocess
import threading
from unittest.mock import patch

import pytest

from ai_ops_agent import package_updates as packages
from ai_ops_agent import package_upgrade_worker as worker
from ai_ops_agent.service_control import is_locked

LISTING = "curl/noble-updates 2 amd64 [upgradable from: 1]\n"


def reserve(tmp_path):
    lock = tmp_path / "lock"
    result = packages.start_upgrade(lambda args, timeout: (0, ""), lock)
    return lock, packages.state_directory(lock), result["run_id"]


@pytest.mark.parametrize(
    "code,output,execution,count",
    [
        (0, "", "succeeded", None),
        (0, "2 upgraded, 0 newly installed", "succeeded", 2),
        (100, "", "apt_failed", None),
        (100, "2 upgraded, 0 newly installed\nE: dpkg failed", "apt_failed", None),
    ],
)
def test_completion_always_verifies_remaining(tmp_path, code, output, execution, count):
    lock, state, run_id = reserve(tmp_path)
    calls = []

    def run(args, timeout):
        assert is_locked(lock)
        calls.append(args)
        return (code, output) if args[0] == "apt-get" else (0, LISTING)

    result = worker.work(lock, state, run_id, run, tmp_path / "absent")
    assert result["execution"] == execution
    assert result["apt_exit_code"] == code
    assert result["upgraded"] == count
    assert result["remaining"] == 1
    assert result["remaining_packages"]["stable"] == ["curl"]
    assert result["checked_at"] >= result["apt_finished_at"]
    assert result["completion_confirmed"]
    assert len(calls) == 2
    assert packages.read_result(state, run_id) == result
    assert not is_locked(lock)
    assert worker.work(lock, state, run_id, run) == result
    assert len(calls) == 2


def test_clean_exit_with_nothing_remaining_is_ok(tmp_path):
    lock, state, run_id = reserve(tmp_path)
    result = worker.work(
        lock,
        state,
        run_id,
        lambda args, timeout: (
            (0, "1 upgraded, 0 newly installed")
            if args[0] == "apt-get"
            else (0, "Listing...\n")
        ),
        tmp_path / "absent",
    )
    assert result["execution"] == "succeeded"
    assert result["remaining"] == 0
    assert result["ok"] is True
    assert "error" not in result


def test_exit_zero_with_kept_back_package_is_not_success(tmp_path):
    lock, state, run_id = reserve(tmp_path)
    kept_back = "0 upgraded, 0 newly installed, 0 to remove and 1 not upgraded."
    result = worker.work(
        lock,
        state,
        run_id,
        lambda args, timeout: (
            (0, kept_back) if args[0] == "apt-get" else (0, LISTING)
        ),
        tmp_path / "absent",
    )
    assert result["execution"] == "succeeded"
    assert result["upgraded"] == 0
    assert result["remaining"] == 1
    assert result["remaining_packages"]["stable"] == ["curl"]
    assert result["ok"] is False
    assert result["error"] == "apt finished but 1 package(s) remain upgradable: curl"


def test_failed_verification_is_unknown_not_zero(tmp_path):
    lock, state, run_id = reserve(tmp_path)
    result = worker.work(
        lock,
        state,
        run_id,
        lambda args, timeout: (
            (0, "") if args[0] == "apt-get" else (100, "listing unavailable")
        ),
        tmp_path / "absent",
    )
    assert result["execution"] == "succeeded"
    assert result["verification"] == "failed"
    assert result["remaining"] is None
    assert result["remaining_packages"]["total"] is None
    assert not result["ok"]


def test_unconfirmed_timeout_blocks_retry(tmp_path):
    lock, state, run_id = reserve(tmp_path)
    calls = []

    def run(args, timeout):
        calls.append(args)
        return 124, "apt timed out"

    result = worker.work(lock, state, run_id, run)
    assert result["execution"] == "timeout"
    assert result["apt_exit_code"] is None
    assert result["verification"] == "not_run"
    assert packages.start_upgrade(run, lock)["busy"]
    assert len(calls) == 1


def test_caller_loss_and_concurrent_launch_during_verification(tmp_path):
    lock, state, run_id = reserve(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def run(args, timeout):
        if args[0] == "apt":
            entered.set()
            assert release.wait(5)
        return 0, LISTING

    thread = threading.Thread(
        target=worker.work, args=(lock, state, run_id, run, tmp_path / "absent")
    )
    thread.start()
    try:
        assert entered.wait(5)
        assert packages.start_upgrade(run, lock)["busy"]
        assert not packages.read_result(state, run_id)["completion_confirmed"]
    finally:
        release.set()
        thread.join(5)
    assert packages.read_result(state, run_id)["completion_confirmed"]
    assert packages.start_upgrade(lambda args, timeout: (0, ""), lock)["ok"]
    # Old outcomes remain readable and cannot run apt again.
    assert worker.work(lock, state, run_id, run)["completion_confirmed"]


def test_real_runner_deadline_keeps_lock_until_exit(tmp_path):
    lock, state, run_id = reserve(tmp_path)

    class Process:
        returncode = 100

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def wait(self, timeout=None):
            assert is_locked(lock)
            if timeout is not None:
                raise subprocess.TimeoutExpired("apt-get", timeout)
            assert packages.read_result(state, run_id)["execution"] == "timeout"

    with (
        patch.object(worker.subprocess, "Popen", return_value=Process()),
        patch.object(worker, "run_command", return_value=(0, LISTING)),
    ):
        result = worker.work(lock, state, run_id, reboot_flag=tmp_path / "absent")
    assert result["execution"] == "timeout"
    assert result["apt_exit_code"] == 100
    assert result["remaining"] == 1
    assert result["completion_confirmed"]
    assert not result["ok"]


def test_bounded_diagnostics_and_correlated_records(tmp_path):
    lock, state, run_id = reserve(tmp_path)
    result = worker.work(lock, state, run_id, lambda args, timeout: (100, "x" * 9000))
    assert len(result["diagnostics"]) == 4096
    assert len(result["verification_error"]) <= 4096
    assert not list(state.glob("*.tmp"))
    record = state / f"{run_id}.json"
    result["run_id"] = "unrelated"
    record.write_text(json.dumps(result))
    with pytest.raises(ValueError, match="mismatch"):
        packages.read_result(state, run_id)


@pytest.mark.parametrize("listing", [(0, ""), (124, "list timeout")])
def test_missing_or_timed_out_listing_is_unknown(tmp_path, listing):
    lock, state, run_id = reserve(tmp_path)
    flag = tmp_path / "reboot"
    flag.touch()
    result = worker.work(
        lock,
        state,
        run_id,
        lambda args, timeout: (0, "") if args[0] == "apt-get" else listing,
        flag,
    )
    assert result["remaining"] is None
    assert result["verification"] == "failed"
    assert result["reboot_required"] is True
    assert result["remaining_packages"]["checked_at"] == result["checked_at"]


def test_cli_waits_for_worker_failure_and_returns_structured_json(tmp_path, capsys):
    lock = tmp_path / "lock"
    threads = []

    def launch(args, timeout):
        run_id = args[-1]
        thread = threading.Thread(
            target=worker.work,
            args=(
                lock,
                packages.state_directory(lock),
                run_id,
                lambda command, limit: (
                    (100, "apt failed") if command[0] == "apt-get" else (0, LISTING)
                ),
                tmp_path / "absent",
            ),
        )
        thread.start()
        threads.append(thread)
        return 0, ""

    with patch.object(packages.os, "geteuid", return_value=0):
        assert packages.main(["upgrade"], launch, lock, tmp_path / "log") == 1
    for thread in threads:
        thread.join(5)
    result = json.loads(capsys.readouterr().out)
    assert result["apt_exit_code"] == 100
    assert result["remaining_packages"]["stable"] == ["curl"]
    assert result["error"] == "apt failed"
