"""Read the server's security facts: SSH attempts, bans, sessions, ports.

Every reading is a fixed command with no caller input, parsed here into plain
numbers and short lists. A missing tool (no fail2ban, no ufw) is reported as
not installed, never as an error. Standard library only: runs as ai-audit.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from collections.abc import Callable
from pathlib import Path

# argv -> stdout, or None when the command is missing or fails.
Runner = Callable[[list[str]], "str | None"]

TOP_ATTACKERS = 5
MAX_SESSIONS = 10
MAX_PORTS = 20
_JAIL = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_IP = r"(?P<ip>[0-9a-fA-F:.]+)"
_FAILED = re.compile(
    r"(?:Failed (?:password|publickey|keyboard-interactive/pam) for "
    r"(?:invalid user )?(?P<user>\S+)|Invalid user (?P<invalid>\S+)) from " + _IP
)
_ACCEPTED = re.compile(
    r"Accepted (?P<method>\S+) for (?P<user>\S+) from " + _IP + r" port \d+"
    r"(?: \S+: (?P<keytype>\S+) (?P<key>SHA256:\S+))?"
)
_STAMP = re.compile(r"^(\d{9,11}(?:\.\d+)?) ")
_LOCAL = ("127.", "::1", "[::1]", "localhost")


def run(argv: list[str]) -> str | None:
    """Run a fixed read-only command; its output, or None if it can't run."""
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def ssh_activity(run: Runner) -> dict:
    """Failed and accepted SSH logins in the last 24 hours."""
    text = run(
        [
            "journalctl",
            "-u",
            "ssh",
            "-u",
            "sshd",
            "--since",
            "24 hours ago",
            "-o",
            "short-unix",
            "--no-pager",
            "-q",
        ]
    )
    if text is None:
        return {"available": False}
    attackers: Counter[str] = Counter()
    users: Counter[str] = Counter()
    accepted = []
    groups: dict[tuple, dict] = {}
    for line in text.splitlines():
        if match := _FAILED.search(line):
            attackers[match["ip"]] += 1
            users[match["user"] or match["invalid"]] += 1
        elif match := _ACCEPTED.search(line):
            stamp = _STAMP.match(line)
            at = float(stamp.group(1)) if stamp else None
            key = f"{match['keytype']} …{match['key'][-8:]}" if match["key"] else ""
            accepted.append(
                {"user": match["user"], "from": match["ip"], "method": match["method"]}
            )
            group = groups.setdefault(
                (match["user"], match["ip"], match["method"], key),
                {
                    "user": match["user"][:32],
                    "from": match["ip"],
                    "method": match["method"][:32],
                    "key": key,
                    "count": 0,
                    "last": None,
                },
            )
            group["count"] += 1
            if at is not None:
                group["last"] = at
    return {
        "available": True,
        "failed": sum(attackers.values()),
        "failed_ips": len(attackers),
        "top_attackers": [
            {"ip": ip, "count": count}
            for ip, count in attackers.most_common(TOP_ATTACKERS)
        ],
        "top_users": [
            {"user": user[:32], "count": count}
            for user, count in users.most_common(TOP_ATTACKERS)
        ],
        "accepted": len(accepted),
        "password_logins": sum(1 for item in accepted if item["method"] == "password"),
        "logins": accepted[-MAX_SESSIONS:],
        # Who logged in: per user, address, method and key, most frequent first.
        "login_groups": sorted(
            groups.values(), key=lambda item: item["count"], reverse=True
        )[:MAX_SESSIONS],
    }


def _field(text: str, name: str) -> int:
    match = re.search(rf"{name}:\s*(\d+)", text)
    return int(match.group(1)) if match else 0


def fail2ban(run: Runner) -> dict:
    """Jails with their current and total bans; installed False without it."""
    status = run(["fail2ban-client", "status"])
    if status is None:
        return {"installed": False, "jails": []}
    match = re.search(r"Jail list:\s*(.*)", status)
    names = [name.strip() for name in (match.group(1) if match else "").split(",")]
    jails = []
    for name in names:
        if not _JAIL.match(name):
            continue
        text = run(["fail2ban-client", "status", name]) or ""
        jails.append(
            {
                "name": name,
                "banned_now": _field(text, "Currently banned"),
                "banned_total": _field(text, "Total banned"),
                "failed_total": _field(text, "Total failed"),
            }
        )
    return {"installed": True, "jails": jails}


def sessions(run: Runner) -> list[dict]:
    """Who is logged in now (user, terminal, from where, since when)."""
    text = run(["who"]) or ""
    found = []
    for line in text.splitlines()[:MAX_SESSIONS]:
        parts = line.split()
        if len(parts) < 3:
            continue
        origin = re.search(r"\(([^)]*)\)", line)
        found.append(
            {
                "user": parts[0][:32],
                "tty": parts[1][:16],
                "since": " ".join(parts[2:4])[:20],
                "from": origin.group(1)[:64] if origin else "local",
            }
        )
    return found


def public_ports(run: Runner) -> list[dict]:
    """Listening sockets reachable from outside (not bound to localhost)."""
    text = run(["ss", "-H", "-tulpn"]) or ""
    ports: dict[tuple[str, int], dict] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        proto, local = parts[0], parts[4]
        address, _, port = local.rpartition(":")
        if not port.isdigit() or address.startswith(_LOCAL):
            continue
        process = re.search(r'\(\("([^"]+)"', line)
        key = (proto, int(port))
        ports.setdefault(
            key,
            {
                "proto": proto,
                "port": int(port),
                "process": process.group(1)[:32] if process else "?",
            },
        )
    return sorted(ports.values(), key=lambda item: item["port"])[:MAX_PORTS]


def sshd_settings(run: Runner) -> dict[str, str]:
    """The effective sshd settings that matter for security."""
    text = run(["sshd", "-T"]) or ""
    wanted = {
        "permitrootlogin",
        "passwordauthentication",
        "kbdinteractiveauthentication",
        "pubkeyauthentication",
        "maxauthtries",
        "port",
    }
    settings = {}
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key in wanted and key not in settings:
            settings[key] = value.strip()
    return settings


def firewall(run: Runner) -> str:
    """'active', 'inactive' or 'missing' (ufw)."""
    text = run(["ufw", "status"])
    if text is None:
        return "missing"
    return "active" if "Status: active" in text else "inactive"


def security_updates(run: Runner) -> int | None:
    """Packages with a pending security update; None if apt can't tell."""
    text = run(["apt-get", "-s", "-o", "Debug::NoLocking=1", "upgrade"])
    if text is None:
        return None
    return sum(
        1
        for line in text.splitlines()
        if line.startswith("Inst ") and "-security" in line
    )


def auto_updates(path: Path = Path("/etc/apt/apt.conf.d/20auto-upgrades")) -> bool:
    """True when unattended security upgrades are switched on."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(re.search(r'Unattended-Upgrade\s+"1"', text))


def loose_secrets(paths: tuple[str, ...]) -> list[str]:
    """Secret files that group or others can read."""
    loose = []
    for name in paths:
        try:
            mode = Path(name).stat().st_mode
        except OSError:
            continue
        if mode & 0o077:
            loose.append(f"{name} ({oct(mode & 0o777)})")
    return loose
