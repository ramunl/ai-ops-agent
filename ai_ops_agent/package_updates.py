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
import re
import sys
import time
from pathlib import Path

from ai_ops_agent.cleanup import Runner, exclusive, run_command
from ai_ops_agent.packages import group_updates

LOCK = Path("/run/ai-packages.lock")
LOG = Path("/var/log/ai-packages.log")
REBOOT_FLAG = Path("/var/run/reboot-required")
REFRESH_TIMEOUT = 300
LIST_TIMEOUT = 120
UPGRADE_TIMEOUT = 900
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
# Its own systemd unit: if the caller is stopped mid-run (the dashboard being
# restarted or redeployed), apt still finishes instead of leaving dpkg broken.
DETACHED = [
    "systemd-run",
    "--quiet",
    "--wait",
    "--collect",
    "--unit=ai-packages-upgrade",
    "--setenv=DEBIAN_FRONTEND=noninteractive",
    "--setenv=LC_ALL=C",
]
UPGRADE = DETACHED + APT_UPGRADE
_UPGRADED_RE = re.compile(r"^(\d+) upgraded, (\d+) newly installed", re.MULTILINE)


def _tail(text: str, lines: int = 5) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def pending(run: Runner) -> dict:
    """List upgradable packages by kind, without refreshing the indexes."""
    code, out = run(["apt", "list", "--upgradable"], LIST_TIMEOUT)
    if code != 0:
        return {"ok": False, "error": _tail(out) or f"apt list exited with {code}"}
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


def upgrade(run: Runner, reboot_flag: Path = REBOOT_FLAG) -> dict:
    """Upgrade installed packages, then report what is still pending."""
    started = time.time()
    code, out = run(UPGRADE, UPGRADE_TIMEOUT)
    journal_code, journal = run(
        [
            "journalctl",
            "-u",
            "ai-packages-upgrade",
            "--since",
            f"@{started:.6f}",
            "--no-pager",
            "-o",
            "cat",
        ],
        LIST_TIMEOUT,
    )
    if journal_code == 0 and journal:
        out = journal
    if code != 0:
        return {
            "ok": False,
            "error": _tail(out) or f"apt-get upgrade exited with {code}",
        }
    counts = _UPGRADED_RE.search(out)
    left = pending(run)
    if not left.get("ok"):
        return {
            "ok": False,
            "error": "Upgrade finished; recheck failed: " + left["error"],
        }
    return {
        "ok": True,
        "upgraded": int(counts.group(1)) if counts else 0,
        "started_at": started,
        "finished_at": time.time(),
        "remaining": left.get("total", 0),
        "reboot_required": reboot_flag.exists(),
    }


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
    with exclusive(lock) as acquired:
        if not acquired:
            print(json.dumps({"ok": False, "error": "a package run is already active"}))
            return 1
        result = (check if mode == "check" else upgrade)(run, reboot_flag)
    _log(log, mode, result)
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
