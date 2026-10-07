"""Restart a whitelisted service; the dashboard's Restart buttons call this.

Run as ``ai-service list`` or ``ai-service restart <service>``; prints one JSON
document. Only names in services_list.MANAGED_SERVICES are accepted, so a
caller can never reach another unit or pass systemctl options.

The restart is queued (``--no-block``) and the command returns at once: the
caller may be the very service being restarted (the dashboard restarting
itself), and must be able to answer before it goes down.

Standard library only, run with the system python3.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable

from ai_ops_agent.services_list import MANAGED_SERVICES

LOG = "/var/log/ai-service.log"
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


def _log(line: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
    except OSError:
        pass


def main(argv: list[str], run: Runner = run_command) -> int:
    """CLI entry: ``list`` or ``restart <service>``; prints one JSON document."""
    if argv == ["list"]:
        print(json.dumps({"ok": True, "services": MANAGED_SERVICES}))
        return 0
    if len(argv) != 2 or argv[0] != "restart":
        print(
            json.dumps(
                {"ok": False, "error": "usage: ai-service list|restart <service>"}
            )
        )
        return 2
    if os.geteuid() != 0:
        print(json.dumps({"ok": False, "error": "ai-service must run as root"}))
        return 1
    result = restart(argv[1], run)
    _log(f"restart {argv[1]}: {'queued' if result['ok'] else result['error']}")
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
