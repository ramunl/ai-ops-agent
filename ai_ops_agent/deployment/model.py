"""Fixed deployment targets and private persistent state."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class DeploymentError(RuntimeError):
    """A deployment cannot safely proceed."""


@dataclass(frozen=True)
class Target:
    """Trusted server paths for a single managed service."""

    name: str
    repo: Path
    venv: Path
    artifacts: tuple[tuple[str, Path, int], ...] = ()
    health_url: str | None = None
    log_file: Path | None = None

    @property
    def service(self) -> str:
        """Return the fixed systemd service name."""
        return f"{self.name}.service"


def targets() -> dict[str, Target]:
    """Return the fixed fleet; caller input never supplies server paths."""
    result = {}
    for name, runtime in (
        ("ai-coding-agent", "ai_coding_venv"),
        ("ai-pm-agent", "ai_pm_venv"),
        ("ai-ops-agent", "ai_ops_venv"),
        ("ai-dashboard", "ai_dashboard_venv"),
    ):
        artifacts = [
            (
                f"deploy/{name}.service"
                if name in {"ai-dashboard", "ai-coding-agent"}
                else f"{name}.service",
                Path(f"/etc/systemd/system/{name}.service"),
                0o644,
            )
        ]
        if name == "ai-pm-agent":
            artifacts.append(
                ("deploy/ai-pm-todos", Path("/usr/local/sbin/ai-pm-todos"), 0o755)
            )
        if name == "ai-ops-agent":
            artifacts.append(
                ("deploy/ai-cleanup", Path("/usr/local/sbin/ai-cleanup"), 0o755)
            )
        result[name] = Target(
            name,
            Path("/opt") / name,
            Path("/opt") / runtime,
            tuple(artifacts),
            "http://127.0.0.1:8787/healthz" if name == "ai-dashboard" else None,
            Path(f"/var/log/{name}/update.log"),
        )
    return result


def timestamp() -> str:
    """Return an unambiguous UTC transaction timestamp."""
    return datetime.now(UTC).isoformat()


def atomic_json(path: Path, value: dict) -> None:
    """Replace private state atomically without exposing partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Store:
    """Serialize and persist manager state independently of agent checkouts."""

    def __init__(self, root: Path = Path("/var/lib/ai-deploy")) -> None:
        """Create the owner-only state directory."""
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Refuse competing state changes instead of waiting behind them."""
        with (self.root / "lock").open("a") as stream:
            os.chmod(stream.name, 0o600)
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise DeploymentError(
                    "Another deployment transaction is running"
                ) from error
            yield

    def read(self) -> dict:
        """Read state, refusing to discard a corrupted deployment journal."""
        path = self.root / "state.json"
        if not path.exists():
            return {"targets": {}, "active": None}
        try:
            value = json.loads(path.read_text())
            if not isinstance(value.get("targets"), dict):
                raise ValueError("Invalid targets")
            return value
        except (ValueError, AttributeError) as error:
            raise DeploymentError(
                "Deployment state is corrupt; inspect it manually"
            ) from error

    def write(self, state: dict) -> None:
        """Persist a journal update."""
        atomic_json(self.root / "state.json", state)
