"""Parse and classify available operating-system package updates."""

from __future__ import annotations

import re
from dataclasses import dataclass

_APT_UPGRADABLE_RE = re.compile(
    r"^(?P<name>[^/\s]+)/(?P<suites>\S+)\s+"
    r"(?P<version>\S+)\s+(?P<arch>\S+)"
    r"(?:\s+\[upgradable from:\s+(?P<old>[^\]]+)\])?"
)


_UNTESTED_APT_CHANNELS = (
    "proposed",
    "backports",
    "testing",
    "unstable",
    "experimental",
    "devel",
)


@dataclass(frozen=True)
class AptUpdate:
    """Describe one available operating-system package update."""

    name: str
    suites: tuple[str, ...]
    version: str
    arch: str
    old_version: str | None

    @property
    def channel(self) -> str:
        """Return the comma-separated package release channels."""
        return ",".join(self.suites)


@dataclass(frozen=True)
class AptUpdateReport:
    """Group available package updates by release channel."""

    critical: list[AptUpdate]
    stable: list[AptUpdate]
    not_recommended: list[AptUpdate]

    @property
    def total(self) -> int:
        """Return the number of updates across all categories."""
        return len(self.critical) + len(self.stable) + len(self.not_recommended)


def parse_upgradable(raw: str) -> list[AptUpdate]:
    """Parse package rows from apt list output and ignore notices."""
    updates = []
    for line in raw.splitlines():
        line = line.strip()
        if (
            not line
            or line == "Listing..."
            or line.startswith("WARNING:")
            or line.startswith("N:")
        ):
            continue
        match = _APT_UPGRADABLE_RE.match(line)
        if not match:
            continue
        suites = tuple(
            suite.strip() for suite in match.group("suites").split(",") if suite.strip()
        )
        updates.append(
            AptUpdate(
                name=match.group("name"),
                suites=suites,
                version=match.group("version"),
                arch=match.group("arch"),
                old_version=match.group("old"),
            )
        )
    return updates


def group_updates(raw: str) -> AptUpdateReport:
    """Group parsed packages into security, stable, and review categories."""
    grouped = {"critical": [], "stable": [], "not_recommended": []}
    for item in parse_upgradable(raw):
        grouped[classify_update(item)].append(item)
    return AptUpdateReport(
        critical=grouped["critical"],
        stable=grouped["stable"],
        not_recommended=grouped["not_recommended"],
    )


def classify_update(item: AptUpdate) -> str:
    """Classify a package using its configured release channels."""
    channel = item.channel.lower()
    if "security" in channel:
        return "critical"
    if any(marker in channel for marker in _UNTESTED_APT_CHANNELS):
        return "not_recommended"
    return "stable"
