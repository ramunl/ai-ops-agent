"""Resolve reviewed tool commands when services have a minimal PATH."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# Fixed locations only; Coding's untrusted manifest never supplies executables.
_USER_BINS = {
    "codex": (".local/bin", ".npm-global/bin"),
    "claude": (".local/bin", ".npm-global/bin"),
    "opencode": (".opencode/bin", ".local/bin", ".npm-global/bin"),
}


def version_command(command: tuple[str, ...] | list[str]) -> list[str]:
    """Keep PATH precedence, then probe known user installation locations."""
    executable, *args = command
    if shutil.which(executable):
        return list(command)
    for directory in _USER_BINS.get(executable, ()):
        candidate = Path.home() / directory / executable
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return [str(candidate), *args]
    return list(command)


def update_command(command: tuple[str, ...] | list[str], package: str) -> list[str]:
    """Use Claude's native updater for native installs; npm for npm tools."""
    resolved = version_command(command)
    if Path(command[0]).name == "claude":
        executable = shutil.which(resolved[0])
        if executable:
            native_versions = Path.home() / ".local/share/claude/versions"
            if Path(executable).resolve().parent == native_versions.resolve():
                # Official native installer: code.claude.com/docs/en/setup.
                return [resolved[0], "update"]
    return ["npm", "install", "-g", f"{package}@latest"]
