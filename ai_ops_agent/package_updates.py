"""Check for and install system package updates; the dashboard calls this.

Run as ``ai-packages check`` or ``ai-packages upgrade``; prints one JSON
document. The mode is the only argument, so a caller can never choose packages
or pass apt options. Same commands as the bot's /update and /upgrade.

Standard library only, run with the system python3.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

from ai_ops_agent.cleanup import Runner, exclusive, run_command
from ai_ops_agent.packages import group_updates

LOCK = Path("/run/ai-packages.lock")
LOG = Path("/var/log/ai-packages.log")
REBOOT_FLAG = Path("/var/run/reboot-required")
REFRESH_TIMEOUT = 300
LIST_TIMEOUT = 120
UPGRADE_TIMEOUT = 900
START_TIMEOUT = 30
UNIT = "ai-packages-upgrade"
# Keep the installed configuration files instead of stopping to ask.
APT_UPGRADE = [
    "apt-get",
    "upgrade",
    "-y",
    "-q",
    "-o",
    "Dpkg::Options::=--force-confdef",
    "-o",
    "Dpkg::Options::=--force-confold",
]
STATE = Path("/var/lib/ai-packages")


def state_directory(lock: Path) -> Path:
    """Keep custom locks isolated from production state."""
    return STATE if lock == Path("/run/ai-packages.lock") else lock.parent / "packages"


def save(path: Path, result: dict) -> None:
    """Atomically persist a record before exposing it to readers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(result, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_result(state: Path, run_id: str) -> dict:
    """Read only the requested run's evidence; never consult journal text."""
    result = json.loads((state / f"{run_id}.json").read_text())
    if result["run_id"] != run_id:
        raise ValueError("package outcome run ID mismatch")
    return result


def _tail(text: str, lines: int = 5) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])[-4096:]


def pending(run: Runner) -> dict:
    """List upgradable packages by kind, without refreshing the indexes."""
    try:
        code, out = run(["apt", "list", "--upgradable"], LIST_TIMEOUT)
    except OSError as exc:
        code, out = 127, str(exc)
    if code != 0 or not out.strip():
        return {
            "ok": False,
            "total": None,
            "security": None,
            "stable": None,
            "untested": None,
            "error": _tail(out)
            or (
                f"apt list exited with {code}"
                if code
                else "apt list returned no output"
            ),
        }
    groups = group_updates(out)
    return {
        "ok": True,
        "total": groups.total,
        "security": [item.name for item in groups.critical],
        "stable": [item.name for item in groups.stable],
        "untested": [item.name for item in groups.not_recommended],
    }


def check(run: Runner, reboot_flag: Path = REBOOT_FLAG) -> dict:
    """Refresh the package indexes and report what can be upgraded."""
    code, out = run(["apt-get", "update", "-q"], REFRESH_TIMEOUT)
    if code != 0:
        return {
            "ok": False,
            "error": _tail(out) or f"apt-get update exited with {code}",
        }
    result = pending(run)
    result["checked_at"] = time.time()
    result["reboot_required"] = reboot_flag.exists()
    return result


def start_command(lock: Path) -> list[str]:
    """Launch the standard-library worker in a service independent of callers."""
    return [
        "systemd-run",
        "--quiet",
        "--collect",
        f"--unit={UNIT}",
        "--setenv=DEBIAN_FRONTEND=noninteractive",
        "--setenv=LC_ALL=C",
        "/usr/bin/python3",
        "-c",
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from ai_ops_agent.package_upgrade_worker import main; "
        "sys.exit(main(sys.argv[2:]))",
        str(Path(__file__).resolve().parent.parent),
        str(lock),
        str(state_directory(lock)),
        json.loads((state_directory(lock) / "active.json").read_text())["run_id"],
    ]


def start_upgrade(run: Runner | None = None, lock: Path | None = None) -> dict:
    """Fail closed with structured evidence if durable state is unavailable."""
    try:
        return _start_upgrade(run, lock)
    except (OSError, ValueError, KeyError) as exc:
        return {
            "ok": False,
            "execution": "unknown",
            "completion_confirmed": False,
            "upgraded": None,
            "remaining": None,
            "apt_exit_code": None,
            "error": "package state unavailable; do not retry: " + _tail(str(exc)),
        }


def _start_upgrade(run: Runner | None = None, lock: Path | None = None) -> dict:
    """Reserve a durable run before launch; unresolved runs cannot be retried."""
    run = run or run_command
    lock = lock or LOCK
    state = state_directory(lock)
    with exclusive(lock) as acquired:
        if not acquired:
            return {
                "ok": False,
                "busy": True,
                "error": "a package run is already active",
            }
        active = state / "active.json"
        if active.exists():
            previous = read_result(state, json.loads(active.read_text())["run_id"])
            if not previous["completion_confirmed"]:
                return {
                    **previous,
                    "ok": False,
                    "busy": True,
                    "error": "previous package run is unresolved; do not retry",
                }
        run_id = uuid.uuid4().hex
        result = {
            "ok": False,
            "run_id": run_id,
            "unit": f"{UNIT}.service",
            "execution": "queued",
            "verification": "not_run",
            "completion_confirmed": False,
            "apt_exit_code": None,
            "upgraded": None,
            "remaining": None,
            "remaining_packages": None,
            "reboot_required": None,
            "started_at": time.time(),
            "finished_at": None,
            "checked_at": None,
            "diagnostics": "",
            "launcher_exit_code": None,
            "launcher_error": None,
            "error": "upgrade completion unknown",
        }
        save(state / f"{run_id}.json", result)
        save(active, {"run_id": run_id})
        try:
            code, out = run(start_command(lock), START_TIMEOUT)
        except OSError as exc:
            code, out = 127, str(exc)
        result["launcher_exit_code"] = code
        if code:
            result["execution"] = "unknown" if code == 124 else "launch_failed"
            result["launcher_error"] = _tail(out) or f"systemd-run exited with {code}"
            result["error"] = result["launcher_error"]
        save(state / f"{run_id}.json", result)
    return {**result, "ok": code == 0}


def upgrade(
    run: Runner,
    reboot_flag: Path = REBOOT_FLAG,
    lock: Path = LOCK,
    wait_timeout: float = UPGRADE_TIMEOUT,
) -> dict:
    """Wait for durable worker evidence without owning or stopping apt."""
    result = start_upgrade(run, lock)
    if not result.get("ok"):
        return result
    deadline = time.monotonic() + wait_timeout
    while True:
        result = read_result(state_directory(lock), result["run_id"])
        if result["completion_confirmed"]:
            return result
        if time.monotonic() >= deadline:
            return {
                **result,
                "ok": False,
                "execution": "unknown",
                "error": "caller wait timed out; detached completion unknown",
                "caller_timed_out": True,
            }
        time.sleep(0.1)


def _log(log: Path, mode: str, result: dict) -> None:
    if not result["ok"]:
        outcome = "FAILED: " + result["error"].splitlines()[-1]
    elif mode == "upgrade":
        outcome = f"{result['upgraded']} upgraded, {result['remaining']} remaining"
    else:
        outcome = f"{result['total']} available"
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {mode}: {outcome}\n"
    with contextlib.suppress(OSError), open(log, "a", encoding="utf-8") as handle:
        handle.write(line)


def main(
    argv: list[str],
    run: Runner = run_command,
    lock: Path = LOCK,
    log: Path = LOG,
    reboot_flag: Path = REBOOT_FLAG,
) -> int:
    """CLI entry: ``check`` or ``upgrade``; prints one JSON document."""
    if argv not in (["check"], ["upgrade"]):
        print(json.dumps({"ok": False, "error": "usage: ai-packages check|upgrade"}))
        return 2
    if os.geteuid() != 0:
        print(json.dumps({"ok": False, "error": "ai-packages must run as root"}))
        return 1
    mode = argv[0]
    if mode == "upgrade":
        result = upgrade(run, reboot_flag, lock)
    else:
        with exclusive(lock) as acquired:
            if not acquired:
                print(
                    json.dumps(
                        {"ok": False, "error": "a package run is already active"}
                    )
                )
                return 1
            result = check(run, reboot_flag)
    _log(log, mode, result)
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
