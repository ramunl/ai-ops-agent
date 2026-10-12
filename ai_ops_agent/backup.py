"""Back up what is not in git: secrets, agent state, small configs.

Run as ``ai-backup run`` or ``ai-backup status``; prints one JSON document.
``run`` packs the files below (plus any listed in EXTRA_PATHS) into a tar.gz,
encrypts it with openssl (AES-256, key file never on a command line), keeps
the last KEEP archives and records the result for ``status``. The ops bot
sends each archive to the owner's chat, so a copy lives off the server.

Standard library plus the system openssl; no bot credentials.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ai_ops_agent.backup_containers import add_container, is_container_path
from ai_ops_agent.cleanup import exclusive

# Not in any repository, and painful or impossible to recreate.
FILES = (
    "/etc/ai-coding-agent/ai-coding-agent.env",
    "/etc/ai-pm-agent.env",
    "/etc/ai-ops-agent.env",
    "/etc/ai-dashboard/ai-dashboard.env",
    "/etc/ai-agent-projects.json",
    "/var/lib/ai-coding-agent/state.json",
    "/var/lib/ai-deploy/state.json",
    "/opt/ai-todos-active.txt",
    "/etc/caddy/Caddyfile",
)
KEEP = 7
MAX_BYTES = 20 * 1024**2  # far above the real size; stops a wrong extra path
CIPHER = ("-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt")

Encrypter = Callable[[bytes, Path, Path], None]


@dataclass(frozen=True)
class Paths:
    """Where the key, archives, status and lock live (tests use a temp dir)."""

    key: Path = Path("/etc/ai-backup/key")
    extra: Path = Path("/etc/ai-backup/extra-paths")
    archives: Path = Path("/var/backups/ai-agents")
    status: Path = Path("/var/lib/ai-backup/status.json")
    lock: Path = Path("/run/lock/ai-backup.lock")
    files: tuple[str, ...] = field(default=FILES)


def openssl_encrypt(data: bytes, key: Path, target: Path) -> None:
    """Encrypt data into target with the passphrase in key."""
    done = subprocess.run(
        ["openssl", "enc", *CIPHER, "-pass", f"file:{key}", "-out", str(target)],
        input=data,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if done.returncode != 0:
        detail = done.stderr.decode(errors="replace").strip().splitlines()[-1:]
        raise OSError(f"openssl failed: {detail[0] if detail else done.returncode}")


def wanted_paths(paths: Paths) -> list[str]:
    """The fixed files plus the owner's extra paths (one per line, # comments)."""
    extra = []
    with contextlib.suppress(FileNotFoundError):
        for line in paths.extra.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("/") or is_container_path(line):
                extra.append(line)
    return list(dict.fromkeys([*paths.files, *extra]))


def pack(wanted: list[str]) -> tuple[bytes, list[str], list[str]]:
    """A tar.gz of the existing paths; returns it with what was included/skipped."""
    buffer = io.BytesIO()
    included, skipped = [], []
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in wanted:
            if is_container_path(name):
                add_container(archive, name)
                included.append(name)
                continue
            if not os.path.exists(name):
                skipped.append(name)
                continue
            archive.add(name, arcname=name.lstrip("/"))
            included.append(name)
    return buffer.getvalue(), included, skipped


def prune(folder: Path, keep: int = KEEP) -> None:
    """Keep only the newest archives."""
    archives = sorted(folder.glob("ai-backup-*.tar.gz.enc"))
    for old in archives[:-keep] if keep else archives:
        old.unlink(missing_ok=True)


def run(paths: Paths, encrypt: Encrypter = openssl_encrypt) -> dict:
    """Make one encrypted archive; return what happened."""
    if not paths.key.is_file():
        return {"ok": False, "error": f"no key at {paths.key}; run install-ai-backup"}
    data, included, skipped = pack(wanted_paths(paths))
    if len(data) > MAX_BYTES:
        return {"ok": False, "error": f"archive too large ({len(data)} bytes)"}
    paths.archives.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = paths.archives / f"ai-backup-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz.enc"
    old_mask = os.umask(0o077)
    try:
        encrypt(data, paths.key, target)
    finally:
        os.umask(old_mask)
    prune(paths.archives)
    return {
        "ok": True,
        "file": str(target),
        "bytes": target.stat().st_size,
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "included": included,
        "skipped": skipped,
        "at": time.time(),
    }


def save_status(paths: Paths, result: dict) -> None:
    """Remember the last run, and the last successful one, for ``status``."""
    previous = status(paths)
    record = {
        "last": result,
        "last_ok": result if result["ok"] else previous.get("last_ok"),
    }
    paths.status.parent.mkdir(parents=True, exist_ok=True)
    tmp = paths.status.with_suffix(".tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    os.replace(tmp, paths.status)


def status(paths: Paths) -> dict:
    """The saved status, or an empty one before the first run."""
    try:
        saved = json.loads(paths.status.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"last": None, "last_ok": None}
    return saved if isinstance(saved, dict) else {"last": None, "last_ok": None}


def main(
    argv: list[str], paths: Paths | None = None, encrypt: Encrypter = openssl_encrypt
) -> int:
    """CLI entry: ``run`` or ``status``; prints one JSON document."""
    paths = paths or Paths()
    if argv == ["status"]:
        print(json.dumps({"ok": True, **status(paths)}))
        return 0
    if argv != ["run"]:
        print(json.dumps({"ok": False, "error": "usage: ai-backup run|status"}))
        return 2
    if os.geteuid() != 0:
        print(json.dumps({"ok": False, "error": "ai-backup must run as root"}))
        return 1
    with exclusive(paths.lock) as acquired:
        if not acquired:
            print(json.dumps({"ok": False, "error": "a backup is already running"}))
            return 1
        try:
            result = run(paths, encrypt)
        except (OSError, tarfile.TarError) as error:
            result = {"ok": False, "error": str(error), "at": time.time()}
        result.setdefault("at", time.time())
        save_status(paths, result)
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
