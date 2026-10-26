"""Why apt leaves an upgradable package alone, in words the owner can act on.

``apt list --upgradable`` lists every newer version, but ``apt-get upgrade``
skips some of them on purpose:

- phased: Ubuntu rolls the update out to a growing share of servers; apt
  installs it by itself once this server's turn comes. Nothing to do.
- held: someone ran ``apt-mark hold``. Unhold it to upgrade.
- kept back: the new version needs a package removed or replaced, which a
  plain upgrade never does. Check what ``apt full-upgrade`` would remove first.

A simulated upgrade (nothing changes) names the skipped packages; the held list
and ``apt-cache policy`` say which reason applies. Standard library only.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

Runner = Callable[[list[str], int], tuple[int, str]]

TIMEOUT = 120
PHASED = "phased"
HELD = "held"
KEPT_BACK = "kept_back"
REASONS = {
    PHASED: "deferred by Ubuntu's phased rollout; apt installs it on its own "
    "once this server is in the rollout",
    HELD: "held (apt-mark hold); run apt-mark unhold to allow it",
    KEPT_BACK: "kept back: needs a package removed or replaced; check what "
    "apt full-upgrade would remove, then run it by hand",
}
_NAME = re.compile(r"^[a-z0-9][a-z0-9+.:-]{0,127}$")
# Headers apt prints before the packages a plain upgrade skips.
_SKIPPED_HEADERS = (
    "The following packages have been kept back:",
    "The following upgrades have been deferred due to phasing:",
)
_PHASING_HEADER = _SKIPPED_HEADERS[1]


def simulate_command() -> list[str]:
    """The same upgrade the button runs, simulated: nothing is changed."""
    return [
        "apt-get",
        "-s",
        "-o",
        "Debug::NoLocking=1",
        "upgrade",
        "--with-new-pkgs",
    ]


def skipped_sections(output: str) -> dict[str, set[str]]:
    """Package names under each 'kept back' / 'deferred' header of apt output."""
    sections: dict[str, set[str]] = {}
    current: set[str] | None = None
    for line in output.splitlines():
        if line.strip() in _SKIPPED_HEADERS:
            current = sections.setdefault(line.strip(), set())
        elif current is not None and line.startswith(" "):
            current.update(name for name in line.split() if _NAME.match(name))
        else:
            current = None
    return sections


def phased_in_policy(output: str) -> set[str]:
    """Packages whose ``apt-cache policy`` block shows a phased source."""
    phased: set[str] = set()
    package = None
    for line in output.splitlines():
        if line and not line[0].isspace() and line.endswith(":"):
            package = line[:-1]
        elif package and "(phased " in line:
            phased.add(package)
    return phased


def explain(run: Runner, names: Iterable[str]) -> dict[str, str]:
    """Map each package a plain upgrade would skip to PHASED, HELD or KEPT_BACK.

    Packages missing from the result are ones the upgrade would install. When
    the simulation cannot run, nothing is explained (and nothing is assumed).
    """
    wanted = {name for name in names if _NAME.match(name)}
    if not wanted:
        return {}
    try:
        code, out = run(simulate_command(), TIMEOUT)
    except OSError:
        return {}
    if code != 0:
        return {}
    sections = skipped_sections(out)
    skipped = set().union(*sections.values()) & wanted if sections else set()
    if not skipped:
        return {}
    reasons = {name: PHASED for name in sections.get(_PHASING_HEADER, set())}
    held = _lines(run, ["apt-mark", "showhold"])
    phased = _policy_phased(run, sorted(skipped - held - set(reasons)))
    for name in skipped:
        if name in held:
            reasons[name] = HELD
        elif name in phased:
            reasons[name] = PHASED
        reasons.setdefault(name, KEPT_BACK)
    return {name: reason for name, reason in reasons.items() if name in skipped}


def _lines(run: Runner, argv: list[str]) -> set[str]:
    try:
        code, out = run(argv, TIMEOUT)
    except OSError:
        return set()
    return {line.strip() for line in out.splitlines()} if code == 0 else set()


def _policy_phased(run: Runner, names: list[str]) -> set[str]:
    if not names:
        return set()
    try:
        code, out = run(["apt-cache", "policy", *names], TIMEOUT)
    except OSError:
        return set()
    return phased_in_policy(out) if code == 0 else set()
