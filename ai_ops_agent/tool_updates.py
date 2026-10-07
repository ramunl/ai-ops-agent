"""Report and update the AI command-line tools; the dashboard calls this.

Run as ``ai-tools check`` or ``ai-tools update <tool>``; prints one JSON
document. Only names in tools_list.AI_TOOLS are accepted, so a caller can
never choose a package or pass npm options. Same commands as the bot's
/ai_tools.

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
from ai_ops_agent.tools_list import AI_TOOLS, AiTool

LOCK = Path("/run/ai-tools.lock")
LOG = Path("/var/log/ai-tools.log")
VERSION_TIMEOUT = 30
LATEST_TIMEOUT = 60
INSTALL_TIMEOUT = 300
USAGE = "usage: ai-tools check|update <tool>"


def installed_version(run: Runner, tool: AiTool) -> str | None:
    """First line of the tool's version output, or None if it is not installed."""
    code, out = run(list(tool.version_cmd), VERSION_TIMEOUT)
    if code != 0 or not out.strip():
        return None
    return out.splitlines()[0].strip()


def latest_version(run: Runner, tool: AiTool) -> str | None:
    """Published version of the tool's package, or None if npm cannot say."""
    code, out = run(["npm", "view", tool.package, "version"], LATEST_TIMEOUT)
    if code != 0 or not out.strip():
        return None
    return out.splitlines()[-1].strip()


def newer_version(installed: str | None, latest: str | None) -> bool:
    """Compare numeric release versions without matching version substrings."""
    current = re.search(r"(?<![0-9])([0-9]+)\.([0-9]+)\.([0-9]+)", installed or "")
    published = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", latest or "")
    if current is None or published is None:
        return False
    return tuple(map(int, published.groups())) > tuple(map(int, current.groups()))


def inspect(run: Runner, name: str) -> dict:
    """One tool's installed and published versions."""
    tool = AI_TOOLS[name]
    version = installed_version(run, tool)
    latest = latest_version(run, tool)
    return {
        "name": name,
        "package": tool.package,
        "version": version,
        "latest": latest,
        "update_available": newer_version(version, latest),
    }


def check(run: Runner) -> dict:
    """Every known tool with its installed and published versions."""
    return {
        "ok": True,
        "tools": [inspect(run, name) for name in AI_TOOLS],
        "checked_at": time.time(),
    }


def update(run: Runner, name: str) -> dict:
    """Install the latest published version of one known tool."""
    if name not in AI_TOOLS:
        return {"ok": False, "error": f"unknown tool '{name}'"}
    tool = AI_TOOLS[name]
    before = installed_version(run, tool)
    code, out = run(["npm", "install", "-g", f"{tool.package}@latest"], INSTALL_TIMEOUT)
    if code != 0:
        tail = "\n".join(out.strip().splitlines()[-5:])
        return {"ok": False, "name": name, "error": tail or f"npm exited with {code}"}
    return {
        "ok": True,
        "name": name,
        "before": before,
        "after": installed_version(run, tool),
        "finished_at": time.time(),
    }


def _log(log: Path, result: dict) -> None:
    if result["ok"]:
        outcome = f"{result['before']} -> {result['after']}"
    else:
        outcome = "FAILED: " + result["error"].splitlines()[-1]
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} update {result['name']}: {outcome}\n"
    with contextlib.suppress(OSError), open(log, "a", encoding="utf-8") as handle:
        handle.write(line)


def main(
    argv: list[str], run: Runner = run_command, lock: Path = LOCK, log: Path = LOG
) -> int:
    """CLI entry: ``check`` or ``update <tool>``; prints one JSON document."""
    if argv == ["check"]:
        print(json.dumps(check(run)))
        return 0
    if len(argv) != 2 or argv[0] != "update":
        print(json.dumps({"ok": False, "error": USAGE}))
        return 2
    if argv[1] not in AI_TOOLS:
        print(json.dumps({"ok": False, "error": f"unknown tool '{argv[1]}'"}))
        return 2
    if os.geteuid() != 0:
        print(json.dumps({"ok": False, "error": "ai-tools update must run as root"}))
        return 1
    with exclusive(lock) as acquired:
        if not acquired:
            print(json.dumps({"ok": False, "error": "a tool update is already active"}))
            return 1
        result = update(run, argv[1])
    _log(log, result)
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
