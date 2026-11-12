"""Journaled deployment and verified recovery transactions."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from ai_ops_agent.deployment.backup import restore, snapshot
from ai_ops_agent.deployment.failure_reporting import FailureReporting
from ai_ops_agent.deployment.model import DeploymentError, Store, Target, timestamp
from ai_ops_agent.deployment.system import CommandError, System


class Manager:
    """Coordinate fixed targets with one fleet-wide transaction reservation."""

    def __init__(self, store: Store, fleet: dict[str, Target], system: System) -> None:
        """Inject storage and OS operations for isolated integration testing."""
        self.store, self.fleet, self.system = store, fleet, system
        self.reporting = FailureReporting(store)

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
            self.reporting.result(record)
            record["history"] = [dict(item) for item in record.get("history", [])]
            for item in record["history"]:
                self.reporting.result(item)
            results.append(record)
        return {"targets": results, "active": state.get("active")}

    def remote_main(self) -> dict:
        """The commit main points to on each target's origin (read-only).

        Lets a caller tell whether "deploy latest main" would change anything.
        A target whose remote cannot be reached reports an error, not a guess.
        """
        results = []
        for name, target in self.fleet.items():
            try:
                output = self.system.git(
                    target, "ls-remote", "origin", "refs/heads/main"
                )
                commit = output.split()[0] if output.strip() else None
                results.append({"name": name, "main": commit, "error": None})
            except DeploymentError as error:
                results.append({"name": name, "main": None, "error": str(error)})
        return {"targets": results}

    def reserve(
        self, action: str, name: str, ref: str | None, expected: str | None = None
    ) -> dict:
        """Validate and reserve a single job before systemd starts it."""
        try:
            return self._reserve(action, name, ref, expected)
        finally:
            self.reporting.flush()

    def _reserve(
        self, action: str, name: str, ref: str | None, expected: str | None
    ) -> dict:
        """Reserve under the transaction lock and journal preflight failures."""
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
            job = {
                "operation": uuid.uuid4().hex,
                "action": action,
                "requested_ref": ref,
                "commits": commits,
            }
            for item in selected:
                target = self.fleet[item]
                try:
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
                except (DeploymentError, OSError, ValueError) as error:
                    record = state["targets"].setdefault(item, {})
                    record.update(status="failed", error=str(error))
                    self.reporting.capture(
                        state,
                        job,
                        target,
                        "preflight",
                        error,
                        self._evidence(target),
                        "not_required",
                    )
                    self._history(record, job, "failed")
                    self.store.write(state)
                    raise
            job = {
                "operation": job["operation"],
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
                for key in (
                    "failure_identity",
                    "failure_stage",
                    "failure_exit_code",
                    "original_error",
                    "rollback_outcome",
                    "recovery_error",
                    "reporting_error",
                ):
                    record.pop(key, None)
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

    def cancel_reservation(
        self, operation: str, error: str, exit_code: int | None = None
    ) -> None:
        """Expose scheduler failure without leaving a permanent queued reservation."""
        with self.store.lock():
            state = self.store.read()
            if (state.get("active") or {}).get("operation") != operation:
                return
            for name in state["active"]["targets"]:
                record = state["targets"][name]
                record.update(status="failed", error=error)
                self.reporting.capture(
                    state,
                    state["active"],
                    self.fleet[name],
                    "scheduling",
                    CommandError(error, exit_code),
                    outcome="not_required",
                )
                self._history(record, state["active"], "failed")
            state["active"] = None
            self.store.write(state)

        self.reporting.flush()

    def execute(self, operation: str) -> bool:
        """Flush failure handoff only after releasing the transaction lock."""
        try:
            return self._execute(operation)
        finally:
            self.reporting.flush()

    def _evidence(self, target: Target) -> tuple[str, str]:
        """Evidence collection must never prevent recovery."""
        try:
            return self.system.evidence(target)
        except (DeploymentError, OSError, ValueError):
            return "", ""

    def _execute(self, operation: str) -> bool:
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

    def _checked_commit(self, state: dict, target: Target) -> None:
        record = state["targets"][target.name]
        commit = self.system.git(target, "rev-parse", "HEAD")
        if record.get("current") and record["current"]["commit"] != commit:
            raise DeploymentError(
                "Checkout drifted from verified deployment; restore it manually"
            )

    def _verified_current(self, state: dict, target: Target) -> dict:
        record = state["targets"][target.name]
        current = self._identity(
            target, (record.get("current") or {}).get("ref", "bootstrap")
        )
        record["current"] = current
        self.store.write(state)
        return current

    def _baseline(self, state: dict, target: Target) -> dict:
        """Gate a deployment on the live release starting and verifying."""
        self._checked_commit(state, target)
        # Reverification also bootstraps a pre-manager checkout, proving its process.
        self.system.restart(target)
        return self._verified_current(state, target)

    def _probe_current(self, state: dict, target: Target) -> DeploymentError | None:
        """Check the live release for rollback without gating recovery on it.

        Returns the startup failure, or None after recording a verified identity.
        """
        self._checked_commit(state, target)
        try:
            self.system.restart(target)
        except DeploymentError as error:
            return error
        self._verified_current(state, target)
        return None

    def _transaction(self, state: dict, job: dict, name: str) -> bool:
        target = self.fleet[name]
        record = state["targets"][name]
        backup_path = self.store.root / f"{name}-{uuid.uuid4().hex}"
        startup_failure = None
        stage = "baseline"
        evidence = self._evidence(target)
        try:
            self.system.log(target, f"{job['operation']} {job['action']} started")
            self.system.clean(target)
            if job["action"] == "rollback":
                # A broken current release must not block restoring its predecessor.
                startup_failure = self._probe_current(state, target)
                if startup_failure:
                    self.reporting.capture(
                        state,
                        job,
                        target,
                        "baseline",
                        startup_failure,
                        self._evidence(target),
                        prior_evidence=evidence,
                    )
                if startup_failure and not record.get("current"):
                    raise DeploymentError(
                        "Current release cannot start and has no verified record"
                    )
                # Unverified failures keep the last verified identity as the baseline.
                baseline = record.get("current")
            else:
                baseline = self._baseline(state, target)
            stage = "snapshot"
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
            stage = "stop"
            self.system.run(["systemctl", "stop", target.service], timeout=60)
            stage = "checkout"
            self.system.checkout(target, job["commits"][name])
            if job["action"] == "rollback":
                requested_ref = record["previous"]["ref"]
                stage = "rollback"
                restore(target, Path(record["previous"]["backup"]))
                self.system.run(["systemctl", "daemon-reload"])
            else:
                requested_ref = job["requested_ref"] or "main"
                stage = "install"
                self.system.install(target)
            stage = "health"
            self.system.restart(target)
            if startup_failure:
                # Only the restored predecessor is verified; the failed release keeps
                # no rollback pointer and its snapshot is discarded.
                reason = (
                    f"Current release failed to start ({startup_failure}); "
                    "restored previous verified release"
                )
                record.update(
                    current=self._identity(target, requested_ref),
                    status="healthy",
                    error=None,
                    recovery={
                        "reason": reason,
                        "operation": job["operation"],
                        "failed_commit": baseline["commit"],
                        "restored_commit": job["commits"][name],
                        "at": timestamp(),
                    },
                    updated_at=timestamp(),
                )
                job.pop("recovery")
                self.reporting.finish(state, job, name, "succeeded")
                self._history(record, job, "recovered")
                self.store.write(state)
                self.system.log(
                    target,
                    f"{job['operation']} recovered to verified "
                    f"{record['current']['commit']}: {reason}",
                )
                shutil.rmtree(backup_path, ignore_errors=True)
                return True
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
            self.reporting.capture(
                state,
                job,
                target,
                stage,
                error,
                self._evidence(target),
                "pending" if job.get("recovery") else "not_required",
                prior_evidence=evidence if stage == "baseline" else ("", ""),
            )
            record["error"] = str(error)
            if startup_failure:
                record["error"] = (
                    f"Current release failed to start ({startup_failure}); {error}"
                )
            if job.get("recovery"):
                return self._recover_interrupted(state, job)
            self.reporting.finish(state, job, name, "not_required")
            record.update(status="failed", updated_at=timestamp())
            shutil.rmtree(backup_path, ignore_errors=True)
            self._history(record, job, "failed")
            self.store.write(state)
            return False

    def _recover_interrupted(self, state: dict, job: dict) -> bool:
        recovery = job["recovery"]
        target = self.fleet[recovery["target"]]
        record = state["targets"][target.name]
        original_error = (
            record.get("error")
            or record.get("original_error")
            or "Interrupted deployment"
        )
        if not record.get("failure_identity"):
            self.reporting.capture(
                state,
                job,
                target,
                "interrupted",
                DeploymentError("Interrupted deployment"),
                self._evidence(target),
            )
        try:
            self.system.run(["systemctl", "stop", target.service], timeout=60)
            self.system.checkout(target, recovery["baseline"]["commit"])
            restore(target, Path(recovery["backup"]))
            self.system.run(["systemctl", "daemon-reload"])
            self.system.restart(target)
            record.update(
                current=recovery["baseline"], status="failed", updated_at=timestamp()
            )
            # A rollback's baseline is the release it left, not its predecessor.
            restored = (
                "previous version"
                if job["action"] == "deploy"
                else "pre-rollback release"
            )
            record["error"] = original_error + f"; {restored} restored and verified"
            self.reporting.finish(state, job, target.name, "succeeded")
            record["recovery_error"] = None
            job.pop("recovery")
            # Commit restored verification before discarding its recovery bundle.
            self.store.write(state)
            shutil.rmtree(recovery["backup"], ignore_errors=True)
        except (DeploymentError, OSError, ValueError, KeyError) as error:
            self.reporting.capture(
                state, job, target, "recovery", error, self._evidence(target)
            )
            self.reporting.finish(state, job, target.name, "failed")
            record["recovery_error"] = str(error)
            record.update(
                status="rollback_failed",
                error=(f"{original_error}; ROLLBACK FAILED: {error}"),
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
                **{
                    key: record.get(key)
                    for key in (
                        "failure_identity",
                        "failure_stage",
                        "failure_exit_code",
                        "original_error",
                        "rollback_outcome",
                        "recovery_error",
                    )
                },
            }
        )
        del history[:-20]
