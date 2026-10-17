"""ai-clients: fixed read-only probes with private, atomic local sample history."""

from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from ai_ops_agent import client_sources
from ai_ops_agent.client_history import update

STATE = Path("/var/lib/ai-clients")


def check(directory: Path = STATE) -> dict:
    """Sample under one lock and persist the last report for rates and daily usage."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            previous = json.loads((directory / "report.json").read_text())
            if not isinstance(previous, dict):
                previous = {}
        except (OSError, ValueError):
            previous = {}
        now = time.time()
        report = update(
            {
                "ok": True,
                "checked_at": now,
                "services": client_sources.collect(
                    client_sources.run, client_sources.fetch, now
                ),
            },
            previous,
            now,
        )
        fd, name = tempfile.mkstemp(dir=directory, prefix=".report-")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(report, handle)
            os.replace(name, directory / "report.json")
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return report


def main(argv: list[str]) -> int:
    """Accept only 'check'; no caller-controlled commands or paths."""
    if argv != ["check"]:
        print(json.dumps({"ok": False, "error": "usage: ai-clients check"}))
        return 2
    try:
        print(json.dumps(check()))
    except (OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": f"Client sample failed: {error}"}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
