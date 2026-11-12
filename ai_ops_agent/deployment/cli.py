"""JSON CLI and independent systemd job scheduling."""

from __future__ import annotations

import argparse
import json
import sys

from ai_ops_agent.deployment.engine import Manager
from ai_ops_agent.deployment.model import DeploymentError, Store, targets
from ai_ops_agent.deployment.system import System


class ScheduledFailureError(DeploymentError):
    """A waited worker failure carrying the existing structured status result."""

    def __init__(self, message: str, result: dict) -> None:
        """Preserve the CLI error field alongside deployment results."""
        super().__init__(message)
        self.result = result


def schedule(manager: Manager, job: dict, *, wait: bool = False) -> dict:
    """Launch a reserved transaction outside the service being restarted."""
    command = [
        "systemd-run",
        "--collect",
        f"--unit=ai-deploy-{job['operation']}",
        "--property=Type=exec",
        "--property=TimeoutStartSec=1800",
        "--property=RuntimeMaxSec=1800",
    ]
    if wait:
        command.extend(["--wait", "--pipe"])
    command.extend(["/usr/local/sbin/ai-deploy", "execute", job["operation"]])
    try:
        manager.system.run(command, timeout=1900 if wait else 30)
    except DeploymentError as error:
        if wait:
            # Completed failures have released their reservation. Failed recovery
            # retains it, but its target status distinguishes it from a queue.
            state = manager.store.read()
            completed = any(
                item.get("operation") == job["operation"]
                and item.get("status") in {"failed", "rollback_failed"}
                for item in state["targets"].values()
            )
            if completed:
                raise ScheduledFailureError(str(error), manager.status()) from error
        # An uncertain scheduler result must not free a running worker's slot.
        try:
            manager.system.run(
                [
                    "systemctl",
                    "is-active",
                    "--quiet",
                    f"ai-deploy-{job['operation']}.service",
                ]
            )
        except DeploymentError:
            manager.cancel_reservation(
                job["operation"],
                "Independent deployment job could not start",
                getattr(error, "exit_code", None),
            )
        if wait:
            raise ScheduledFailureError(str(error), manager.status()) from error
        raise
    return {
        key: job[key]
        for key in ("operation", "target", "action", "status", "requested_ref")
    }


def parser() -> argparse.ArgumentParser:
    """Define a narrow CLI without arbitrary paths or executable arguments."""
    result = argparse.ArgumentParser(description="Verified fixed-fleet deployments")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("remote")
    for command in ("submit", "deploy", "rollback"):
        child = sub.add_parser(command)
        if command == "submit":
            child.add_argument("action", choices=("deploy", "rollback"))
        child.add_argument("target")
        child.add_argument("ref", nargs="?")
        child.add_argument("--expected-commit")
        child.add_argument("--no-restart", action="store_true")
    child = sub.add_parser("execute")
    child.add_argument("operation")
    sub.add_parser("recover")
    sub.add_parser("retry-tickets")
    return result


def recover(manager: Manager) -> dict:
    """Flush any interrupted queue failure after releasing its lock."""
    try:
        return _recover(manager)
    finally:
        manager.reporting.flush()


def _recover(manager: Manager) -> dict:
    """Resume restoration only after an independent worker has stopped."""
    with manager.store.lock():
        state = manager.store.read()
        job = state.get("active")
        if not job:
            raise DeploymentError("No interrupted deployment needs recovery")
        try:
            manager.system.run(
                [
                    "systemctl",
                    "is-active",
                    "--quiet",
                    f"ai-deploy-{job['operation']}.service",
                ]
            )
        except DeploymentError:
            pass
        else:
            raise DeploymentError("Deployment worker is still active")
        if not job.get("recovery"):
            for name in job["targets"]:
                if state["targets"][name].get("status") == "queued":
                    manager.reporting.capture(
                        state,
                        job,
                        manager.fleet[name],
                        "interrupted",
                        DeploymentError("Fleet job interrupted before this target"),
                        manager._evidence(manager.fleet[name]),
                        "not_required",
                    )
                    state["targets"][name].update(
                        status="not_run",
                        error="Fleet job interrupted before this target",
                    )
                    manager._history(state["targets"][name], job, "not_run")
            state["active"] = None
            manager.store.write(state)
            return {
                "status": "failed",
                "error": "Interrupted queue cleared; completed targets retained",
            }
    return schedule(manager, job)


def main(argv: list[str] | None = None) -> int:
    """Print one JSON result and return failure for unsuccessful transactions."""
    args = parser().parse_args(argv)
    try:
        manager = Manager(Store(), targets(), System())
        if args.command == "status":
            result = manager.status()
        elif args.command == "retry-tickets":
            result = manager.reporting.retry()
            print(json.dumps(result))
            return 0 if result["ok"] else 1
        elif args.command == "remote":
            result = manager.remote_main()
        elif args.command == "execute":
            success = manager.execute(args.operation)
            print(json.dumps(manager.status()))
            return 0 if success else 1
        elif args.command == "recover":
            result = recover(manager)
        else:
            if args.no_restart:
                raise DeploymentError(
                    "--no-restart cannot verify deployment; "
                    "use submit for an independent restart"
                )
            action = args.action if args.command == "submit" else args.command
            if action == "rollback" and args.ref:
                raise DeploymentError("Rollback does not accept an arbitrary ref")
            try:
                job = manager.reserve(
                    action, args.target, args.ref, args.expected_commit
                )
            except (DeploymentError, OSError) as error:
                if args.command != "submit":
                    raise ScheduledFailureError(str(error), manager.status()) from error
                raise
            result = schedule(manager, job, wait=args.command != "submit")
            if args.command != "submit":
                result = manager.status()
        print(json.dumps(result))
        return 0
    except ScheduledFailureError as error:
        print(json.dumps(dict(error.result, error=str(error))))
        return 1
    except (DeploymentError, OSError) as error:
        print(json.dumps({"error": str(error)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
