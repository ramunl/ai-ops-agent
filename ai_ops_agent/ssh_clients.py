"""Authenticated SSH transports, excluding listeners and pre-authentication bots."""

from __future__ import annotations

import re
from collections.abc import Callable

Runner = Callable[[list[str]], str | None]
_AUTHENTICATED = re.compile(r"^(?:sshd|sshd-session): ([^\s@]+)@(\S+)")


def collect(probe: Runner) -> dict:
    """Count authenticated sshd children, including sessions without terminals."""
    processes = probe(["ps", "-C", "sshd,sshd-session", "-o", "pid=,ppid=,comm=,args="])
    settings = probe(["sshd", "-T"])
    ports = [
        int(match[1]) for match in re.finditer(r"^port (\d+)$", settings or "", re.M)
    ]
    if processes is None:
        return {
            "available": False,
            "error": "Authenticated SSH sessions unavailable",
            "ports": ports,
        }
    sessions = {}
    for line in processes.splitlines():
        fields = line.split(None, 3)
        if len(fields) != 4 or fields[2] not in ("sshd", "sshd-session"):
            continue
        match = _AUTHENTICATED.match(fields[3])
        if match and fields[0].isdigit():
            sessions[fields[0]] = {
                "id": fields[0],
                "name": f"{match[1][:64]} · {match[2][:64]}",
                "connections": 1,
            }
    return {
        "available": True,
        "active": len(sessions),
        "unit": "authenticated connections",
        "ports": ports,
        "clients": list(sessions.values())[:50],
        "details_truncated": len(sessions) > 50,
        "note": "Authenticated SSH connections, including commands and tunnels without "
        "a terminal. Login attempts are excluded. Multiplexed channels share "
        "one connection. TCP counts also include unauthenticated sockets. "
        "Server checks can briefly create an SSH connection.",
    }
