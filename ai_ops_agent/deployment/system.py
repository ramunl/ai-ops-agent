"""Bounded operating-system actions for deployment and health verification."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from ai_ops_agent.deployment.model import DeploymentError, Target, timestamp


class System:
    """Perform trusted Git, runtime, and systemd operations."""

    def log(self, target: Target, message: str) -> None:
        """Append sanitized progress to the existing target deployment log."""
        if target.log_file is not None:
            target.log_file.parent.mkdir(parents=True, exist_ok=True)
            with target.log_file.open("a") as stream:
                stream.write(f"[{timestamp()}] {message}\n")

    def run(
        self,
        args: list[str],
        timeout: int = 180,
        *,
        environment: dict[str, str] | None = None,
    ) -> str:
        """Run an argument vector with bounded time and sanitized errors."""
        environment = dict(os.environ) if environment is None else environment
        process = None
        try:
            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
                start_new_session=True,
            )
            stdout, _ = process.communicate(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DeploymentError(f"{Path(args[0]).name} could not complete") from error
        finally:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        if process.returncode:
            raise DeploymentError(
                f"{Path(args[0]).name} failed (exit {process.returncode})"
            )
        return stdout.strip()

    def git(self, target: Target, *args: str) -> str:
        """Run Git against only the trusted target checkout."""
        environment = dict(os.environ)
        if target.name == "ai-coding-agent":
            environment.setdefault(
                "GIT_SSH_COMMAND",
                "ssh -i /root/.ssh/ai_agent_deploy -o IdentitiesOnly=yes "
                "-o StrictHostKeyChecking=accept-new",
            )
        return self.run(["git", "-C", str(target.repo), *args], environment=environment)

    def clean(self, target: Target) -> None:
        """Refuse any tracked or untracked work, including submodule changes."""
        if self.git(target, "status", "--porcelain", "--untracked-files=all"):
            raise DeploymentError(
                f"{target.name} has local changes; deployment refused"
            )

    def resolve(self, target: Target, ref: str) -> str:
        """Fetch and resolve an explicit branch, tag, or full commit ID."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", ref):
            raise DeploymentError("Invalid deployment ref")
        self.git(target, "fetch", "--prune", "--tags", "origin")
        candidates = [f"refs/remotes/origin/{ref}", f"refs/tags/{ref}"]
        if re.fullmatch(r"[0-9a-fA-F]{40}", ref):
            candidates.append(ref)
        for candidate in candidates:
            try:
                return self.git(
                    target, "rev-parse", "--verify", f"{candidate}^{{commit}}"
                )
            except DeploymentError:
                continue
        raise DeploymentError("Unknown deployment branch, tag, or full commit")

    def checkout(self, target: Target, commit: str) -> None:
        """Detach without rewriting a named branch, then update pinned submodules."""
        self.git(target, "checkout", "--detach", commit)
        self.git(target, "submodule", "sync", "--recursive")
        self.git(target, "submodule", "update", "--init", "--recursive")

    def install(self, target: Target) -> None:
        """Install target dependencies and versioned integration artifacts."""
        self.run(
            [
                str(target.venv / "bin/pip"),
                "install",
                "-r",
                str(target.repo / "requirements.txt"),
            ],
            timeout=600,
        )
        for source, destination, mode in target.artifacts:
            source_path = target.repo / source
            if source_path.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_path, destination)
                destination.chmod(mode)
            elif not destination.name.endswith(".service"):
                # Older releases must not retain a bridge to a missing module.
                destination.unlink(missing_ok=True)
        self.run(["systemctl", "daemon-reload"])

    def process(self, target: Target) -> int:
        """Read a live service process, refusing inactive or restarting services."""
        output = self.run(
            [
                "systemctl",
                "show",
                target.service,
                "--property=ActiveState,SubState,MainPID",
                "--no-pager",
            ]
        )
        values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        try:
            pid = int(values.get("MainPID", "0"))
        except ValueError as error:
            raise DeploymentError("Invalid service health response") from error
        if (
            values.get("ActiveState") != "active"
            or values.get("SubState") != "running"
            or pid <= 0
        ):
            raise DeploymentError(f"{target.service} is not running")
        return pid

    def healthy(self, target: Target, old_pid: int | None = None) -> None:
        """Require a fresh process stable across checks and application health."""
        deadline = time.monotonic() + 30
        while True:
            try:
                pid = self.process(target)
                if old_pid is None or pid != old_pid:
                    self.application_health(target)
                    break
            except DeploymentError:
                pass
            if time.monotonic() >= deadline:
                raise DeploymentError(
                    "Service did not start a healthy fresh process within 30s; "
                    "application health unavailable"
                )
            time.sleep(1)
        if old_pid is not None and pid == old_pid:
            raise DeploymentError("Service restart did not replace the process")
        for _ in range(10):
            time.sleep(1)
            if self.process(target) != pid:
                raise DeploymentError("Service is restarting repeatedly")
            self.application_health(target)

    def application_health(self, target: Target) -> None:
        """Check application readiness separately from a running process."""
        if not target.health_url:
            return
        try:
            with urllib.request.urlopen(target.health_url, timeout=3) as response:
                health = json.load(response)
            if not isinstance(health, dict) or health.get("ok") is not True:
                raise ValueError("Unhealthy application")
        except (OSError, ValueError, urllib.error.URLError) as error:
            raise DeploymentError(
                "Dashboard application health check failed"
            ) from error

    def restart(self, target: Target) -> None:
        """Restart and verify a new stable process."""
        try:
            old_pid = self.process(target)
        except DeploymentError:
            old_pid = None
        self.run(["systemctl", "restart", target.service], timeout=60)
        self.healthy(target, old_pid)
