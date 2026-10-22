"""Tell the owner about SSH logins within a minute, warning on new ones.

Every CHECK_SECONDS the ops bot reads sshd's journal since the last check and
finds successful logins. A login from an address or with a key never seen
before is sent at once with a warning; logins from known ones are sent too
only when OPS_LOGIN_ALERTS=all (the default, "new", keeps routine logins of
your own tools quiet). The first run learns the last 30 days without alerting.
Known addresses and keys are kept in STATE_FILE.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path

from ai_ops_agent.security_stats import _ACCEPTED, _STAMP

STATE_FILE = Path(
    os.environ.get("OPS_LOGIN_STATE", "/var/lib/ai-ops-agent/login-watch.json")
)
MODE = os.environ.get("OPS_LOGIN_ALERTS", "new").strip().lower()  # new|all|off
CHECK_SECONDS = 60
LEARN_DAYS = 30
MAX_KNOWN = 500
MAX_ALERTS = 10  # per check; a flood is summarized

Runner = Callable[[list[str]], "str | None"]


def journal_since(run: Runner, since: float) -> str | None:
    """sshd's journal since an epoch time, one line per entry with its time."""
    return run(
        [
            "journalctl",
            "-u",
            "ssh",
            "-u",
            "sshd",
            "--since",
            f"@{int(since)}",
            "-o",
            "short-unix",
            "--no-pager",
            "-q",
        ]
    )


def logins(text: str, after: float) -> list[dict]:
    """Successful logins later than after: user, from, method, key, at."""
    found = []
    for line in text.splitlines():
        stamp = _STAMP.match(line)
        match = _ACCEPTED.search(line)
        if not stamp or not match:
            continue
        at = float(stamp.group(1))
        if at <= after:
            continue
        key = f"{match['keytype']} …{match['key'][-8:]}" if match["key"] else ""
        found.append(
            {
                "user": match["user"][:32],
                "from": match["ip"],
                "method": match["method"][:32],
                "key": key,
                "at": at,
            }
        )
    return found


def load(path: Path) -> dict:
    """The saved state, or a fresh one that still has to learn."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"last": 0.0, "known": [], "learned": False}
    if not isinstance(state, dict):
        return {"last": 0.0, "known": [], "learned": False}
    state.setdefault("known", [])
    state.setdefault("last", 0.0)
    state.setdefault("learned", False)
    return state


def save(path: Path, state: dict) -> None:
    """Write the state atomically; a failure only loses what was learned."""
    state["known"] = state["known"][-MAX_KNOWN:]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _marks(login: dict) -> list[str]:
    marks = [f"ip:{login['from']}"]
    if login["key"]:
        marks.append(f"key:{login['key']}")
    return marks


def message(login: dict, news: list[str]) -> str:
    """One chat line for a login, with what is new about it."""
    clock = time.strftime("%H:%M", time.localtime(login["at"]))
    key = f", key {login['key']}" if login["key"] else ""
    head = "⚠️ New SSH login" if news else "🔐 SSH login"
    text = (
        f"{head}: {login['user']} from {login['from']} at {clock} "
        f"({login['method']}{key})"
    )
    if news:
        text += "\nFirst time for this " + " and ".join(news) + "."
        text += " If it wasn't you: change keys and check /security."
    return text


def check(
    run: Runner, path: Path = STATE_FILE, now: float | None = None, mode: str = MODE
) -> list[str]:
    """Read new logins since the last check; return the messages to send."""
    now = time.time() if now is None else now
    if mode == "off":
        return []
    state = load(path)
    learning = not state["learned"]
    since = now - LEARN_DAYS * 86400 if learning else max(state["last"], now - 86400)
    text = journal_since(run, since)
    if text is None:
        return []  # journal unreadable: try again next time, keep the cursor
    known = set(state["known"])
    alerts = []
    for login in logins(text, 0 if learning else state["last"]):
        marks = _marks(login)
        news = [
            "address" if mark.startswith("ip:") else "key"
            for mark in marks
            if mark not in known
        ]
        if not learning and (news or mode == "all"):
            alerts.append(message(login, news))
        for mark in marks:
            if mark not in known:
                known.add(mark)
                state["known"].append(mark)
        state["last"] = max(state["last"], login["at"])
    state["last"] = max(state["last"], now - 5) if not learning else now
    state["learned"] = True
    save(path, state)
    if len(alerts) > MAX_ALERTS:
        extra = len(alerts) - MAX_ALERTS
        alerts = [
            *alerts[:MAX_ALERTS],
            f"… and {extra} more SSH logins; see /security.",
        ]
    return alerts
