"""Exact dependency and installed-artifact snapshots for rollback."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ai_ops_agent.deployment.model import DeploymentError, Target, atomic_json


def tree_size(path: Path) -> int:
    """Measure regular runtime files without following symlinks."""
    return sum(
        item.stat().st_size
        for item in path.rglob("*")
        if item.is_file() and not item.is_symlink()
    )


def snapshot(target: Target, destination: Path) -> None:
    """Snapshot the runtime and the exact previously installed artifacts."""
    if not target.venv.is_dir():
        raise DeploymentError(
            "Existing runtime is missing; install it before migration"
        )
    needed = tree_size(target.venv)
    if shutil.disk_usage(destination.parent).free < needed * 2 + 100 * 1024 * 1024:
        raise DeploymentError("Insufficient space for runtime snapshot and restore")
    destination.mkdir(mode=0o700)
    shutil.copytree(target.venv, destination / "venv", symlinks=True)
    artifacts = []
    for index, (_, installed, _) in enumerate(target.artifacts):
        exists = installed.is_file()
        entry = {
            "path": str(installed),
            "exists": exists,
            "mode": installed.stat().st_mode & 0o777 if exists else None,
        }
        if exists:
            shutil.copyfile(installed, destination / f"artifact-{index}")
        artifacts.append(entry)
    atomic_json(destination / "artifacts.json", {"artifacts": artifacts})


def restore(target: Target, source: Path) -> None:
    """Restore dependencies and artifacts, removing artifacts absent at snapshot."""
    if not (source / "venv").is_dir():
        raise DeploymentError("Rollback runtime snapshot is missing")
    entries = json.loads((source / "artifacts.json").read_text())["artifacts"]
    expected = [str(path) for _, path, _ in target.artifacts]
    if [entry["path"] for entry in entries] != expected:
        raise DeploymentError("Rollback artifact manifest does not match target")
    temporary = target.venv.with_name(f"{target.venv.name}.ai-deploy-restore")
    shutil.rmtree(temporary, ignore_errors=True)
    shutil.copytree(source / "venv", temporary, symlinks=True)
    shutil.rmtree(target.venv)
    temporary.rename(target.venv)
    for index, entry in enumerate(entries):
        path = Path(entry["path"])
        if entry["exists"]:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / f"artifact-{index}", path)
            path.chmod(entry["mode"])
        else:
            path.unlink(missing_ok=True)
