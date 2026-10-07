"""Restart a whitelisted service; the dashboard's Restart buttons call this.

Run as ``ai-service list``, ``ai-service restart <service>`` or
``ai-service reboot``; prints one JSON document. Only names in
services_list.MANAGED_SERVICES are accepted, so a caller can never reach
another unit or pass systemctl options.

A reboot is refused while an upgrade, cleanup, tool update or deployment
holds its lock: interrupting those can leave the server half-changed.

The restart is queued (``--no-block``) and the command returns at once: the
caller may be the very service being restarted (the dashboard restarting
itself), and must be able to answer before it goes down.

Standard library only, run with the system python3.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from ai_ops_agent.services_list import MANAGED_SERVICES

LOG = "/var/log/ai-service.log"
USAGE = "usage: ai-service list|restart <service>|reboot"
# Work a reboot must not interrupt, by the lock each one holds while running.
BUSY_LOCKS = {
    Path("/run/ai-packages.lock"): "a package upgrade",
    Path("/run/lock/ai-cleanup.lock"): "a disk cleanup",
    Path("/run/ai-tools.lock"): "an AI tool update",
    Path("/var/lib/ai-deploy/lock"): "a deployment",
}
Runner = Callable[[list[str], int], tuple[int, str]]


def run_command(args: list[str], timeout: int) -> tuple[int, str]:
    """Run a command; return (exit code, combined output)."""
    try:
        done = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return 127, f"{args[0]}: not installed"
    except subprocess.TimeoutExpired:
        return 124, f"{args[0]}: timed out after {timeout} s"
    return done.returncode, (done.stdout + done.stderr).strip()


def restart(service: str, run: Runner = run_command) -> dict:
    """Queue a restart of a whitelisted service."""
    if service not in MANAGED_SERVICES:
        return {"ok": False, "error": f"unknown service '{service}'"}
    code, out = run(["systemctl", "--no-block", "restart", service], 30)
    if code != 0:
        return {"ok": False, "error": out or f"systemctl exited with {code}"}
    return {"ok": True, "service": service, "queued": True}


def is_locked(path: Path) -> bool:
    """Whether another process holds the lock file (never creates it)."""
    try:
        handle = open(path, encoding="utf-8")  # noqa: SIM115 (closed below)
    except OSError:
        return False
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        handle.close()
    return False


def reboot(run: Runner = run_command, locks: dict[Path, str] | None = None) -> dict:
    """Queue a reboot of the server unless protected work is running."""
    locks = BUSY_LOCKS if locks is None else locks
    busy = [what for path, what in locks.items() if is_locked(path)]
    if busy:
        return {"ok": False, "busy": True, "error": f"{busy[0]} is running"}
    if locks == BUSY_LOCKS:
        try:
            state_path = Path("/var/lib/ai-deploy/state.json")
            if state_path.exists() and json.loads(state_path.read_text()).get("active"):
                return {"ok": False, "busy": True, "error": "a deployment is running"}
        except (OSError, ValueError, AttributeError):
            return {"ok": False, "error": "Cannot verify deployment state"}
        unit_code, unit_state = run(
            ["systemctl", "is-active", "ai-packages-upgrade.service"], 10
        )
        if unit_code == 0 and unit_state.strip() in {"active", "activating"}:
            return {"ok": False, "busy": True, "error": "a package upgrade is running"}
    code, out = run(
        [
            "systemd-run",
            "--quiet",
            "--collect",
            "--unit=ai-server-reboot",
            "--on-active=5s",
            "/usr/bin/systemctl",
            "reboot",
        ],
        30,
    )
    if code != 0:
        return {"ok": False, "error": out or f"systemctl exited with {code}"}
    return {"ok": True, "queued": True}


def _log(line: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
    except OSError:
        pass


def main(
    argv: list[str], run: Runner = run_command, locks: dict[Path, str] | None = None
) -> int:
    """CLI entry: ``list``, ``restart <service>`` or ``reboot``; prints JSON."""
    if argv == ["list"]:
        print(json.dumps({"ok": True, "services": MANAGED_SERVICES}))
        return 0
    is_reboot = argv == ["reboot"]
    if not is_reboot and (len(argv) != 2 or argv[0] != "restart"):
        print(json.dumps({"ok": False, "error": USAGE}))
        return 2
    if os.geteuid() != 0:
        print(json.dumps({"ok": False, "error": "ai-service must run as root"}))
        return 1
    result = reboot(run, locks) if is_reboot else restart(argv[1], run)
    _log(f"{' '.join(argv)}: {'queued' if result['ok'] else result['error']}")
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
