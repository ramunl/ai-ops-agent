"""Pure, allowlisted diagnostic extraction; never retain raw service output."""

from __future__ import annotations

import re

MAX_INPUT_CHARS = 65536
MAX_DIAGNOSTIC_CHARS = 1024
_EXCEPTION_TYPES = (
    "RuntimeError",
    "ValueError",
    "TypeError",
    "ImportError",
    "ModuleNotFoundError",
    "FileNotFoundError",
    "PermissionError",
    "OSError",
    "KeyError",
    "AttributeError",
    "SyntaxError",
    "NameError",
    "TimeoutError",
    "ConnectionError",
)
SAFE_DIAGNOSTICS = frozenset(
    {
        "systemd: 217/USER",
        "systemd: 203/EXEC",
        "systemd: failed",
        "missing executable: ai-coding-runner",
        "Python traceback (source omitted)",
        "diagnostic input truncated",
        *(f"Python exception: {name}" for name in _EXCEPTION_TYPES),
    }
)


def parse_diagnostics(status: str = "", journal: str = "") -> tuple[str, ...]:
    """Extract bounded facts, discarding paths, arguments and exception messages.

    Only fixed output vocabulary is emitted. Unknown payloads and traceback
    frames/source are omitted, not heuristically scrubbed. Inspect at most 64 KiB
    of each input; no filesystem, subprocess, Telegram or provider dependency.
    """
    facts: set[str] = set()
    for source in (status, journal):
        if len(source) > MAX_INPUT_CHARS:
            facts.add("diagnostic input truncated")
        for line in source[:MAX_INPUT_CHARS].splitlines():
            for code in ("217/USER", "203/EXEC"):
                if re.search(r"\bstatus=" + code + r"\b", line):
                    facts.add(f"systemd: {code}")
            if "Failed at step USER" in line:
                facts.add("systemd: 217/USER")
            if re.search(r"(?:Active: failed\b|^ActiveState=failed$)", line):
                facts.add("systemd: failed")
            if re.search(r"(?<![\w-])ai-coding-runner(?![\w-])", line) and any(
                marker in line
                for marker in (
                    "No such file or directory",
                    "not found",
                    "cannot execute",
                )
            ):
                facts.add("missing executable: ai-coding-runner")
            if line.strip().endswith("Traceback (most recent call last):"):
                facts.add("Python traceback (source omitted)")
            # Journal prefixes end in ': '; never keep their arbitrary content.
            for name in _EXCEPTION_TYPES:
                if re.search(r"(?:^|: )" + name + r":", line):
                    facts.add(f"Python exception: {name}")
    return tuple(sorted(facts))
