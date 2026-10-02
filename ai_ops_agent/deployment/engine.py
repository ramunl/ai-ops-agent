"""Journaled deployment and verified recovery transactions."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from ai_ops_agent.deployment.backup import restore, snapshot
from ai_ops_agent.deployment.model import DeploymentError, Store, Target, timestamp
from ai_ops_agent.deployment.system import System


class Manager:
    """Coordinate fixed targets with one fleet-wide transaction reservation."""

    def __init__(self, store: Store, fleet: dict[str, Target], system: System) -> None:
        """Inject storage and OS operations for isolated integration testing."""
        self.store, self.fleet, self.system = store, fleet, system

    def status(self) -> dict:
        """Report verified state separately from an unverified observed checkout."""
        state = self.store.read()
        results = []
        for name, target in self.fleet.items():
            record = dict(state["targets"].get(name, {}))
            record.setdefault("status", "untracked")
            record.setdefault("current", None)
            record.setdefault("previous", None)
            record.setdefault("error", None)
            record["name"] = name
            try:
                record["observed_commit"] = self.system.git(target, "rev-parse", "HEAD")
            except DeploymentError:
                record["observed_commit"] = None
            if (
                record["status"] == "healthy"
                and record["current"]
                and record["observed_commit"] != record["current"]["commit"]
            ):
                record["status"] = "drifted"
            results.append(record)
        return {"targets": results, "active": state.get("active")}

    def reserve(
        self, action: str, name: str, ref: str | None, expected: str | None = None
    ) -> dict:
        """Validate and reserve a single job before systemd starts it."""
        name = {
            "coding": "ai-coding-agent",
            "pm": "ai-pm-agent",
            "ops": "ai-ops-agent",
            "dashboard": "ai-dashboard",
        }.get(name, name)
        if action not in {"deploy", "rollback"} or (
            name not in self.fleet and name != "all"
        ):
            raise DeploymentError("Unknown deployment action or target")
        if name == "all" and action != "deploy":
            raise DeploymentError("Rollback requires one explicit target")
        with self.store.lock():
            state = self.store.read()
            if state.get("active"):
                raise DeploymentError("Another deployment is queued or running")
            self._prune_backups(state)
            selected = list(self.fleet) if name == "all" else [name]
            commits = {}
            for item in selected:
                target = self.fleet[item]
                self.system.clean(target)
                if action == "deploy":
                    commits[item] = self.system.resolve(target, ref or "main")
                else:
                    previous = state["targets"].get(item, {}).get("previous")
                    if not previous or not previous.get("verified_at"):
                        raise DeploymentError(
                            "No verified previous deployment is available"
                        )
                    if expected is not None and previous["commit"] != expected:
                        raise DeploymentError(
                            "Rollback target changed; refresh and confirm again"
                        )
                    commits[item] = previous["commit"]
            job = {
                "operation": uuid.uuid4().hex,
                "target": name,
                "action": action,
                "status": "queued",
                "requested_ref": ref,
                "commits": commits,
                "targets": selected,
                "queued_at": timestamp(),
            }
            state["active"] = job
            for item in selected:
                record = state["targets"].setdefault(item, {})
                record.update(status="queued", operation=job["operation"], error=None)
            self.store.write(state)
            return job

    def _prune_backups(self, state: dict) -> None:
        retained = {
            record["previous"]["backup"]
            for record in state["targets"].values()
            if record.get("previous") and record["previous"].get("backup")
        }
        for name in self.fleet:
            for path in self.store.root.glob(f"{name}-*"):
                if str(path) not in retained and path.is_dir():
                    shutil.rmtree(path)

    def cancel_reservation(self, operation: str, error: str) -> None:
        """Expose scheduler failure without leaving a permanent queued reservation."""
        with self.store.lock():
            state = self.store.read()
            if (state.get("active") or {}).get("operation") != operation:
                return
            for name in state["active"]["targets"]:
                state["targets"][name].update(status="failed", error=error)
            state["active"] = None
            self.store.write(state)

    def execute(self, operation: str) -> bool:
        """Execute a reserved job; report recovery failure explicitly."""
        with self.store.lock():
            state = self.store.read()
            job = state.get("active")
            if not job or job["operation"] != operation:
                raise DeploymentError("Deployment reservation does not match")
            if job.get("recovery"):
                success = self._recover_interrupted(state, job)
            else:
                success = True
                for name in job["targets"]:
                    if not self._transaction(state, job, name):
                        success = False
                        break
            for name in job["targets"]:
                if state["targets"][name]["status"] == "queued":
                    state["targets"][name].update(
                        status="not_run", error="Earlier fleet deployment failed"
                    )
            if not job.get("recovery"):
                state["active"] = None
            self.store.write(state)
            return success

    def _identity(self, target: Target, ref: str) -> dict:
        version = target.repo / "VERSION"
        return {
            "commit": self.system.git(target, "rev-parse", "HEAD"),
            "version": version.read_text().strip() if version.is_file() else "unknown",
            "ref": ref,
            "verified_at": timestamp(),
        }

    def _baseline(self, state: dict, target: Target) -> dict:
        record = state["targets"][target.name]
        commit = self.system.git(target, "rev-parse", "HEAD")
        if record.get("current") and record["current"]["commit"] != commit:
            raise DeploymentError(
                "Checkout drifted from verified deployment; restore it manually"
            )
        # Reverification also bootstraps a pre-manager checkout, proving its process.
        self.system.restart(target)
        current = self._identity(
            target, (record.get("current") or {}).get("ref", "bootstrap")
        )
        record["current"] = current
        self.store.write(state)
        return current

    def _transaction(self, state: dict, job: dict, name: str) -> bool:
        target = self.fleet[name]
        record = state["targets"][name]
        backup_path = self.store.root / f"{name}-{uuid.uuid4().hex}"
        try:
            self.system.log(target, f"{job['operation']} {job['action']} started")
            self.system.clean(target)
            baseline = self._baseline(state, target)
            snapshot(target, backup_path)
            job["recovery"] = {
                "target": name,
                "baseline": baseline,
                "backup": str(backup_path),
            }
            record.update(
                status="deploying" if job["action"] == "deploy" else "rolling_back",
                updated_at=timestamp(),
            )
            self.store.write(state)
            self.system.run(["systemctl", "stop", target.service], timeout=60)
            self.system.checkout(target, job["commits"][name])
            if job["action"] == "rollback":
                requested_ref = record["previous"]["ref"]
                restore(target, Path(record["previous"]["backup"]))
                self.system.run(["systemctl", "daemon-reload"])
            else:
                requested_ref = job["requested_ref"] or "main"
                self.system.install(target)
            self.system.restart(target)
            previous = dict(baseline, backup=str(backup_path))
            old_backup = (record.get("previous") or {}).get("backup")
            record.update(
                previous=previous,
                current=self._identity(target, requested_ref),
                status="healthy",
                error=None,
                updated_at=timestamp(),
            )
            job.pop("recovery")
            self._history(record, job, "healthy")
            self.store.write(state)
            self.system.log(
                target,
                f"{job['operation']} verified healthy at {record['current']['commit']}",
            )
            if old_backup and old_backup != str(backup_path):
                shutil.rmtree(old_backup, ignore_errors=True)
            return True
        except (DeploymentError, OSError, ValueError, KeyError) as error:
            record["error"] = str(error)
            if job.get("recovery"):
                return self._recover_interrupted(state, job)
            record.update(status="failed", updated_at=timestamp())
            shutil.rmtree(backup_path, ignore_errors=True)
            self._history(record, job, "failed")
            self.store.write(state)
            return False

    def _recover_interrupted(self, state: dict, job: dict) -> bool:
        recovery = job["recovery"]
        target = self.fleet[recovery["target"]]
        record = state["targets"][target.name]
        try:
            self.system.run(["systemctl", "stop", target.service], timeout=60)
            self.system.checkout(target, recovery["baseline"]["commit"])
            restore(target, Path(recovery["backup"]))
            self.system.run(["systemctl", "daemon-reload"])
            self.system.restart(target)
            record.update(
                current=recovery["baseline"], status="failed", updated_at=timestamp()
            )
            record["error"] = (
                record.get("error") or "Interrupted deployment"
            ) + "; previous version restored and verified"
            job.pop("recovery")
            # Commit restored verification before discarding its recovery bundle.
            self.store.write(state)
            shutil.rmtree(recovery["backup"], ignore_errors=True)
        except (DeploymentError, OSError, ValueError, KeyError) as error:
            record.update(
                status="rollback_failed",
                error=(
                    f"{record.get('error') or 'Deployment interrupted'}; "
                    f"ROLLBACK FAILED: {error}"
                ),
            )
        self._history(record, job, record["status"])
        self.store.write(state)
        self.system.log(
            target, f"{job['operation']} {record['status']}: {record['error']}"
        )
        return False

    @staticmethod
    def _history(record: dict, job: dict, status: str) -> None:
        history = record.setdefault("history", [])
        history.append(
            {
                "operation": job["operation"],
                "action": job["action"],
                "status": status,
                "at": timestamp(),
                "error": record.get("error"),
            }
        )
        del history[:-20]
