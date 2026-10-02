"""Measure and reclaim disk space on the agents' server.

Run as ``ai-cleanup report`` (measure, change nothing) or ``ai-cleanup run``
(clean). Both print one JSON document, which is the contract with the
dashboard. Only a fixed set of standard cleanups exists: callers choose
nothing but the mode, so a web page can never pass a command or a path.

Standard library only, so the system ``python3`` can run it without the bot's
dependencies or credentials.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

JOURNAL_KEEP = "7d"
JOURNAL_MAX = "200M"
JOURNAL_MAX_BYTES = 200 * 1024**2
LARGEST_COUNT = 10
DU_TIMEOUT = 180
STEP_TIMEOUT = 600

Runner = Callable[[list[str], int], tuple[int, str]]


def run_command(args: list[str], timeout: int) -> tuple[int, str]:
    """Run a command non-interactively; return (exit code, combined output)."""
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C"}
    try:
        done = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except FileNotFoundError:
        return 127, f"{args[0]}: not installed"
    except subprocess.TimeoutExpired:
        return 124, f"{args[0]}: timed out after {timeout} s"
    return done.returncode, (done.stdout + done.stderr).strip()


@dataclass(frozen=True)
class Paths:
    """Filesystem locations the cleanups look at; overridable for tests."""

    root: Path = Path("/")
    apt_archives: Path = Path("/var/cache/apt/archives")
    journal: Path = Path("/var/log/journal")
    snaps: Path = Path("/var/lib/snapd/snaps")
    caches: tuple[Path, ...] = (Path("/root/.cache/pip"), Path("/root/.npm/_cacache"))
    lock: Path = Path("/run/lock/ai-cleanup.lock")
    log: Path = Path("/var/log/ai-cleanup.log")


@dataclass
class Category:
    """One kind of reclaimable space, as shown on the dashboard."""

    id: str
    label: str
    bytes: int
    detail: str
    items: list[str] = field(default_factory=list)


def dir_size(path: Path) -> int:
    """Disk space used by a file, or everything under a directory (0 if absent)."""
    if path.is_file():
        return path.lstat().st_blocks * 512
    total = 0
    for directory, _dirs, files in os.walk(path, onerror=lambda _error: None):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(directory, name)).st_blocks * 512
    return total


def human(size: float) -> str:
    """Bytes as a short human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# ---------------------------------------------------------------- measuring


def unused_packages(run: Runner) -> list[str]:
    """Packages `apt-get autoremove` would remove (old kernels included)."""
    code, out = run(["apt-get", "-s", "autoremove", "--purge"], 120)
    if code != 0:
        return []
    return [
        line.split()[1]
        for line in out.splitlines()
        if line.startswith(("Purg ", "Remv "))
    ]


def installed_bytes(run: Runner, packages: list[str]) -> int:
    """Installed size of packages, from dpkg's own bookkeeping."""
    if not packages:
        return 0
    code, out = run(["dpkg-query", "-W", "-f=${Installed-Size}\\n", *packages], 60)
    if code != 0:
        return 0
    return sum(int(value) * 1024 for value in out.split() if value.isdigit())


def disabled_snaps(run: Runner) -> list[tuple[str, str]]:
    """(name, revision) of snap revisions kept only for rollback."""
    code, out = run(["snap", "list", "--all"], 60)
    if code != 0:
        return []
    revisions = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 3 and "disabled" in parts[-1]:
            revisions.append((parts[0], parts[2]))
    return revisions


def largest_directories(run: Runner, root: Path) -> list[dict]:
    """The biggest directories two levels under root, on this filesystem only."""
    code, out = run(["du", "-x", "-B1", "--max-depth=2", str(root)], DU_TIMEOUT)
    sizes = []
    for line in out.splitlines():
        size, _, path = line.partition("\t")
        if size.isdigit() and path and Path(path) != root:
            sizes.append({"path": path, "bytes": int(size)})
    if code not in (0, 1) and not sizes:  # du exits 1 on unreadable entries
        return []
    # Keep only innermost entries: "/var/log 2 GB" names a culprit, while its
    # parent "/var 3 GB" would just count the same space twice.
    paths = [entry["path"].rstrip("/") for entry in sizes]
    leaves = [
        entry
        for entry, path in zip(sizes, paths)
        if not any(other.startswith(path + "/") for other in paths)
    ]
    leaves.sort(key=lambda entry: entry["bytes"], reverse=True)
    return leaves[:LARGEST_COUNT]


def measure(run: Runner, paths: Paths) -> list[Category]:
    """Size every cleanable category without changing anything."""
    packages = unused_packages(run)
    package_bytes = installed_bytes(run, packages)
    archive_bytes = dir_size(paths.apt_archives)
    journal_bytes = dir_size(paths.journal)
    snaps = disabled_snaps(run)
    snap_bytes = sum(
        dir_size(paths.snaps / f"{name}_{revision}.snap") for name, revision in snaps
    )
    cache_bytes = sum(dir_size(cache) for cache in paths.caches)
    return [
        Category(
            "packages",
            "Unused packages and apt cache",
            package_bytes + archive_bytes,
            f"{len(packages)} unused packages, apt cache {human(archive_bytes)}",
            packages,
        ),
        Category(
            "journal",
            "Old system logs",
            max(0, journal_bytes - JOURNAL_MAX_BYTES),
            f"journal {human(journal_bytes)}; keeps last {JOURNAL_KEEP}, "
            f"at most {JOURNAL_MAX}",
        ),
        Category(
            "snaps",
            "Old snap revisions",
            snap_bytes,
            f"{len(snaps)} disabled revisions",
            [f"{name} rev {revision}" for name, revision in snaps],
        ),
        Category(
            "caches",
            "pip and npm caches",
            cache_bytes,
            ", ".join(str(cache) for cache in paths.caches),
        ),
    ]


def report(run: Runner, paths: Paths) -> dict:
    """Everything the dashboard shows before a cleanup."""
    categories = measure(run, paths)
    disk = shutil.disk_usage(paths.root)
    return {
        "ok": True,
        "generated_at": time.time(),
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
        "categories": [category.__dict__ for category in categories],
        "reclaimable_bytes": sum(category.bytes for category in categories),
        "largest": largest_directories(run, paths.root),
    }


# ---------------------------------------------------------------- cleaning


def _remove_caches(paths: Paths) -> tuple[int, str]:
    removed = []
    errors = []
    for cache in paths.caches:
        if cache.exists():
            try:
                shutil.rmtree(cache)
            except OSError as error:
                errors.append(f"{cache}: {error}")
            else:
                removed.append(str(cache))
    message = "removed " + ", ".join(removed) if removed else "nothing to remove"
    return int(bool(errors)), "\n".join([message, *errors])


def cleanup_steps(run: Runner, paths: Paths) -> list[tuple[str, str, Callable]]:
    """The fixed cleanups, in order: (id, label, action returning (code, output))."""

    def packages() -> tuple[int, str]:
        code, out = run(["apt-get", "autoremove", "--purge", "-y"], STEP_TIMEOUT)
        clean_code, clean_out = run(["apt-get", "clean"], STEP_TIMEOUT)
        return max(code, clean_code), "\n".join(filter(None, (out, clean_out)))

    def journal() -> tuple[int, str]:
        return run(
            [
                "journalctl",
                f"--vacuum-time={JOURNAL_KEEP}",
                f"--vacuum-size={JOURNAL_MAX}",
            ],
            STEP_TIMEOUT,
        )

    def snaps() -> tuple[int, str]:
        results = [
            run(["snap", "remove", name, f"--revision={revision}"], STEP_TIMEOUT)
            for name, revision in disabled_snaps(run)
        ]
        if not results:
            return 0, "no disabled revisions"
        return max(code for code, _ in results), "\n".join(out for _, out in results)

    return [
        ("packages", "Unused packages and apt cache", packages),
        ("journal", "Old system logs", journal),
        ("snaps", "Old snap revisions", snaps),
        ("caches", "pip and npm caches", lambda: _remove_caches(paths)),
    ]


@contextlib.contextmanager
def exclusive(lock: Path) -> Iterator[bool]:
    """Yield True if this process holds the cleanup lock, False if busy."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def _tail(text: str, lines: int = 3) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def clean(run: Runner, paths: Paths) -> dict:
    """Run every cleanup, measuring the space each one actually freed."""
    started = time.time()
    before = shutil.disk_usage(paths.root).used
    steps = []
    for step_id, label, action in cleanup_steps(run, paths):
        used = shutil.disk_usage(paths.root).used
        code, out = action()
        freed = max(0, used - shutil.disk_usage(paths.root).used)
        steps.append(
            {
                "id": step_id,
                "label": label,
                "ok": code == 0,
                "freed_bytes": freed,
                "message": _tail(out) or "done",
            }
        )
    after = shutil.disk_usage(paths.root).used
    return {
        "ok": all(step["ok"] for step in steps),
        "started_at": started,
        "finished_at": time.time(),
        "disk_before": before,
        "disk_after": after,
        "freed_bytes": max(0, before - after),
        "steps": steps,
    }


def _log(paths: Paths, result: dict) -> None:
    lines = [
        f"{time.strftime('%Y-%m-%dT%H:%M:%S')} cleanup freed "
        f"{human(result['freed_bytes'])}"
    ]
    lines += [
        f"  {step['id']}: {'ok' if step['ok'] else 'FAILED'}, "
        f"{human(step['freed_bytes'])}"
        for step in result["steps"]
    ]
    with contextlib.suppress(OSError), open(paths.log, "a") as handle:
        handle.write("\n".join(lines) + "\n")


def main(argv: list[str], run: Runner = run_command, paths: Paths | None = None) -> int:
    """CLI entry: ``report`` or ``run``; prints one JSON document."""
    paths = paths or Paths()
    mode = argv[0] if argv else ""
    if mode not in ("report", "run"):
        print(json.dumps({"ok": False, "error": "usage: ai-cleanup report|run"}))
        return 2
    if os.geteuid() != 0:
        print(json.dumps({"ok": False, "error": "ai-cleanup must run as root"}))
        return 1
    if mode == "report":
        print(json.dumps(report(run, paths)))
        return 0
    with exclusive(paths.lock) as acquired:
        if not acquired:
            print(json.dumps({"ok": False, "error": "a cleanup is already running"}))
            return 1
        result = clean(run, paths)
    _log(paths, result)
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
