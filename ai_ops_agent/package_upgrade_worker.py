"""Detached, standard-library package execution and durable verification.

Every step (start, apt's summary, each package, what is left and why, the
outcome) is written to the run log (package_updates.note) and to stderr,
which is the journal of the ai-packages-upgrade unit.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ai_ops_agent import package_updates as packages
from ai_ops_agent.cleanup import Runner, run_command

COUNTS = re.compile(r"^(\d+) upgraded, \d+ newly installed", re.MULTILINE)
SUMMARY = re.compile(r"^\d+ upgraded, \d+ newly installed.*$", re.MULTILINE)
UNPACKED = re.compile(r"^Unpacking (\S+) \((\S+)\) over \((\S+)\)")
MAX_RESULTS = 200


def unpacked(line: str) -> dict | None:
    """A package result from apt's 'Unpacking name (new) over (old)' line."""
    match = UNPACKED.match(line.strip())
    if not match:
        return None
    return {
        "name": match.group(1),
        "old_version": match.group(3),
        "new_version": match.group(2),
        "outcome": "upgraded",
    }


def _say(state: Path, text: str) -> None:
    packages.note(packages.log_path(state), text)
    print(text, file=sys.stderr, flush=True)


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
            results = []
            notes = []
            for line in output:
                text = line.decode("utf-8", errors="replace")
                match = COUNTS.search(text)
                if match:
                    count = int(match.group(1))
                    notes.append(text.strip())
                if (found := unpacked(text)) and len(results) < MAX_RESULTS:
                    results.append(found)
                tail = (tail + line)[-4096:]
            result["reported_upgraded"] = count
            result["package_results"] = results
            result["apt_summary"] = notes[-1] if notes else None
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
        _say(state, f"upgrade {run_id}: running {' '.join(packages.APT_UPGRADE)}")
        try:
            code, out = (
                run(packages.APT_UPGRADE, packages.UPGRADE_TIMEOUT)
                if run
                else execute_apt(result, path)
            )
        except OSError as exc:
            result.update(execution="launch_failed", error=str(exc)[-4096:])
            packages.save(path, result)
            _say(state, f"upgrade {run_id}: apt did not start: {exc}")
            return result
        if run and code == 124:
            # The generic runner cannot establish whether descendants stopped.
            result.update(
                execution="timeout",
                diagnostics=packages._tail(out),
                error="apt timeout; completion unknown",
            )
            packages.save(path, result)
            _say(state, f"upgrade {run_id}: apt timed out; completion unknown")
            return result
        timed_out = result["execution"] == "timeout"
        counts = COUNTS.search(out)
        reported = result.pop("reported_upgraded", None)
        if run:  # the real runner collected these while reading all of apt's output
            summaries = SUMMARY.findall(out)
            result["apt_summary"] = summaries[-1] if summaries else None
            result["package_results"] = [
                found for line in out.splitlines() if (found := unpacked(line))
            ][:MAX_RESULTS]
        _say(
            state,
            "\n".join(
                [
                    f"upgrade {run_id}: apt exited {code}"
                    + (" after its deadline" if timed_out else ""),
                    f"  apt: {result.get('apt_summary') or 'no summary line'}",
                    *(
                        f"  upgraded {item['name']} {item['old_version']} -> "
                        f"{item['new_version']}"
                        for item in result["package_results"]
                    ),
                    *(
                        [f"  apt output: {line}" for line in out.splitlines()[-5:]]
                        if code
                        else []
                    ),
                ]
            ),
        )
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
        _say(
            state,
            "\n".join(
                ["  left: " + packages.describe(left)[0]] + packages.describe(left)[1:]
            ),
        )
        checked_at = time.time()
        reboot = reboot_flag.exists()
        waiting = [row for row in left.get("packages", []) if row.get("reason")]
        result.update(
            completion_confirmed=True,
            finished_at=time.time(),
            verification="succeeded" if left["ok"] else "failed",
            remaining=left.get("total"),
            waiting=left.get("waiting"),
            remaining_packages=left.get("packages"),
            security=left.get("security"),
            stable=left.get("stable"),
            untested=left.get("untested"),
            checked_at=checked_at,
            reboot_required=reboot,
            ok=code == 0 and not timed_out and left["ok"] and left["total"] == 0,
            verification_error=left.get("error"),
            unchanged_reason=(
                "; ".join(f"{row['name']}: {row['reason']}" for row in waiting)
                or "apt found nothing it may upgrade"
            )
            if code == 0 and result["upgraded"] == 0
            else None,
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
            prefix = f"apt finished but {left['total']} package(s) remain upgradable: "
            result["error"] = (
                prefix + ", ".join(names) + " (details: /logs packages)"
            )[-4096:]
        else:
            result.pop("error", None)
        packages.save(path, result)
        _say(
            state,
            f"upgrade {run_id}: "
            + ("ok" if result["ok"] else "FAILED: " + result.get("error", "")[-300:])
            + (
                f" ({result['unchanged_reason']})" if result["unchanged_reason"] else ""
            ),
        )
        return result


def main(argv: list[str]) -> int:
    """Internal worker entry; paths and run ID are supplied only by the launcher."""
    if len(argv) != 3:
        return 2
    result = work(Path(argv[0]), Path(argv[1]), argv[2])
    return 0 if result["ok"] else 1
