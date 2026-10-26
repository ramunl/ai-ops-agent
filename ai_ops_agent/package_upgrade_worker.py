"""Detached, standard-library package execution and durable verification."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from ai_ops_agent import package_updates as packages
from ai_ops_agent.cleanup import Runner, run_command

COUNTS = re.compile(r"^(\d+) upgraded, \d+ newly installed", re.MULTILINE)


def execute_apt(result: dict, path: Path) -> tuple[int, str]:
    """Keep apt and the worker lock alive after the execution deadline."""
    with tempfile.TemporaryFile() as output:
        with subprocess.Popen(
            packages.APT_UPGRADE,
            stdout=output,
            stderr=subprocess.STDOUT,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C"},
        ) as process:
            try:
                process.wait(timeout=packages.UPGRADE_TIMEOUT)
            except subprocess.TimeoutExpired:
                result.update(
                    execution="timeout", error="apt deadline exceeded; still waiting"
                )
                packages.save(path, result)
                # Killing apt can leave dpkg descendants running. Retain the lock
                # and wait for confirmed exit instead, even if all callers vanish.
                process.wait()
            output.seek(0)
            count = None
            tail = b""
            for line in output:
                match = COUNTS.search(line.decode("utf-8", errors="replace"))
                if match:
                    count = int(match.group(1))
                tail = (tail + line)[-4096:]
            result["reported_upgraded"] = count
            return process.returncode, tail.decode("utf-8", errors="replace")


def work(
    lock: Path,
    state: Path,
    run_id: str,
    run: Runner | None = None,
    reboot_flag: Path = packages.REBOOT_FLAG,
) -> dict:
    """Own the package lock through apt completion and remaining-package query."""
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        result = packages.read_result(state, run_id)
        if result["completion_confirmed"] or result["execution"] not in (
            "queued",
            "unknown",
            "launch_failed",
        ):
            return result
        if json.loads((state / "active.json").read_text())["run_id"] != run_id:
            return result
        path = state / f"{run_id}.json"
        result.update(execution="running", worker_started_at=time.time())
        packages.save(path, result)
        try:
            code, out = (
                run(packages.APT_UPGRADE, packages.UPGRADE_TIMEOUT)
                if run
                else execute_apt(result, path)
            )
        except OSError as exc:
            result.update(execution="launch_failed", error=str(exc)[-4096:])
            packages.save(path, result)
            return result
        if run and code == 124:
            # The generic runner cannot establish whether descendants stopped.
            result.update(
                execution="timeout",
                diagnostics=packages._tail(out),
                error="apt timeout; completion unknown",
            )
            packages.save(path, result)
            return result
        timed_out = result["execution"] == "timeout"
        counts = COUNTS.search(out)
        reported = result.pop("reported_upgraded", None)
        result.update(
            execution="timeout"
            if timed_out
            else ("succeeded" if code == 0 else "apt_failed"),
            apt_exit_code=code,
            diagnostics=packages._tail(out),
            # Apt prints a plan before doing work; on failure it is not a count
            # of completed upgrades.
            upgraded=(
                reported
                if reported is not None
                else int(counts.group(1))
                if counts
                else None
            )
            if code == 0
            else None,
            apt_finished_at=time.time(),
        )
        packages.save(path, result)
        left = packages.pending(run or run_command)
        checked_at = time.time()
        reboot = reboot_flag.exists()
        left.update(checked_at=checked_at, reboot_required=reboot)
        result.update(
            completion_confirmed=True,
            finished_at=time.time(),
            verification="succeeded" if left["ok"] else "failed",
            remaining=left.get("total"),
            remaining_packages=left,
            checked_at=checked_at,
            reboot_required=reboot,
            ok=code == 0 and not timed_out and left["ok"] and left["total"] == 0,
            verification_error=left.get("error"),
        )
        if code != 0:
            result["error"] = (
                packages._tail(out) or f"apt-get upgrade exited with {code}"
            )
        elif timed_out:
            result["error"] = "apt exceeded its deadline; completion now confirmed"
        elif not left["ok"]:
            result["error"] = "Upgrade finished; recheck failed: " + left["error"]
        elif left["total"]:
            # apt exited 0 but kept packages back; success would hide them.
            names = [*left["security"], *left["stable"], *left["untested"]]
            result["error"] = (
                f"apt finished but {left['total']} package(s) remain upgradable: "
                + ", ".join(names)
            )[-4096:]
        else:
            result.pop("error", None)
        packages.save(path, result)
        return result


def main(argv: list[str]) -> int:
    """Internal worker entry; paths and run ID are supplied only by the launcher."""
    if len(argv) != 3:
        return 2
    result = work(Path(argv[0]), Path(argv[1]), argv[2])
    return 0 if result["ok"] else 1
