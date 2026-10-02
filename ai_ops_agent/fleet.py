"""Inspect agent checkouts and coordinate updates and service restarts."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from ai_ops_agent.command_output import command_failed, run_command, strip_exit_prefix
from ai_ops_agent.inventory import read_agent_model, read_agent_version

logger = logging.getLogger(__name__)


async def agent_latest_status(path: Path) -> str:
    """Compare the local checkout with its fetched upstream branch."""
    upstream = await run_command(
        [
            "git",
            "-C",
            str(path),
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{u}",
        ]
    )
    if command_failed(upstream) or not upstream:
        return "unknown (no upstream)"

    fetch = await run_command(
        ["git", "-C", str(path), "fetch", "--quiet", "--prune"],
        timeout=60,
    )
    if command_failed(fetch):
        return "unknown (fetch failed)"

    counts = await run_command(
        [
            "git",
            "-C",
            str(path),
            "rev-list",
            "--left-right",
            "--count",
            "HEAD...@{u}",
        ]
    )
    if command_failed(counts):
        return "unknown (compare failed)"

    try:
        ahead, behind = (int(part) for part in counts.split()[:2])
    except (ValueError, IndexError):
        logger.warning("Could not parse upstream revision counts for %s", path)
        return "unknown (bad compare output)"

    if ahead == 0 and behind == 0:
        return f"up to date with {upstream}"
    if ahead == 0:
        return f"behind {upstream} by {behind} commit(s)"
    if behind == 0:
        return f"ahead of {upstream} by {ahead} commit(s)"
    return f"diverged from {upstream}: ahead {ahead}, behind {behind}"


async def inspect_agent(agent: dict[str, str]) -> dict[str, str]:
    """Collect version, service, model, and Git details for one agent."""
    path = Path(agent["path"])
    installed = path.exists()
    version, model = await asyncio.to_thread(
        lambda: (
            read_agent_version(path) if installed else "not installed",
            read_agent_model(agent.get("env_file")) if installed else "unknown",
        )
    )

    branch = commit = latest = "unknown"
    if installed:
        branch, commit, latest = await asyncio.gather(
            run_command(["git", "-C", str(path), "branch", "--show-current"]),
            run_command(["git", "-C", str(path), "rev-parse", "--short", "HEAD"]),
            agent_latest_status(path),
        )
        if command_failed(branch) or not branch:
            branch = "unknown"
        if command_failed(commit) or not commit:
            commit = "unknown"

    service = agent.get("service") or ""
    service_state = "not configured"
    if service:
        service_state = strip_exit_prefix(
            await run_command(["systemctl", "is-active", service])
        ).strip()

    return {
        "name": agent["name"],
        "installed": "yes" if installed else "no",
        "path": str(path),
        "service": service or "none",
        "service_state": service_state,
        "version": version,
        "model": model,
        "branch": branch,
        "commit": commit,
        "latest": latest,
    }


async def restart_agent_service(
    service: str,
    *,
    no_block: bool = False,
) -> str:
    """Restart a configured service, optionally without blocking."""
    if not service:
        return "not configured"
    command = ["systemctl", "restart"]
    if no_block:
        command.append("--no-block")
    command.append(service)
    result = await run_command(command, timeout=30)
    if command_failed(result):
        return f"restart failed: {result}"
    if no_block:
        return "restart queued"
    state = strip_exit_prefix(
        await run_command(["systemctl", "is-active", service])
    ).strip()
    return f"restarted, state: {state}"


DEPLOY_COMMAND = "/usr/local/sbin/ai-deploy"
DEPLOY_TARGETS = {
    "ai-coding-agent": "coding",
    "ai-pm-agent": "pm",
    "ai-ops-agent": "ops",
    "ai-dashboard": "dashboard",
}


async def deployment_request(arguments: list[str]) -> dict:
    """Call the fixed deployment CLI and reject failed or malformed responses."""
    output = await run_command([DEPLOY_COMMAND, *arguments], timeout=30)
    if command_failed(output):
        raise DeploymentError(output)
    try:
        result = json.loads(output)
    except (ValueError, TypeError) as error:
        raise DeploymentError("Deployment manager returned invalid JSON") from error
    if not isinstance(result, dict) or result.get("error"):
        raise DeploymentError(
            str(result.get("error", "Invalid response"))
            if isinstance(result, dict)
            else "Invalid response"
        )
    return result


class DeploymentError(RuntimeError):
    """The deployment manager could not satisfy the request."""


async def deployment_status() -> list[dict]:
    """Read persisted deployment status for the fixed fleet."""
    result = await deployment_request(["status"])
    targets = result.get("targets")
    if not isinstance(targets, list) or not all(isinstance(t, dict) for t in targets):
        raise DeploymentError("Deployment manager returned invalid targets")
    return targets


async def rollback_agent(target: str) -> dict:
    """Queue rollback of exactly one fixed deployment target."""
    if target not in DEPLOY_TARGETS.values():
        raise DeploymentError("Unknown deployment target")
    result = await deployment_request(["submit", "rollback", target])
    if result.get("status") != "queued" or not result.get("operation"):
        raise DeploymentError("Deployment manager did not queue the operation")
    return result


async def update_agent(
    agent: dict, *, defer_self_restart: bool = True
) -> dict[str, str]:
    """Queue a supervised deployment, including self updates, outside this bot."""
    name = agent["name"]
    target = "all" if name == "all" else DEPLOY_TARGETS.get(name)
    if target is None:
        return {
            "name": name,
            "status": "failed",
            "detail": "Unknown target",
            "restart": "not run",
        }
    try:
        result = await deployment_request(["submit", "deploy", target, "main"])
        if result.get("status") != "queued" or not result.get("operation"):
            raise DeploymentError("Deployment manager did not queue the operation")
    except DeploymentError as error:
        return {
            "name": name,
            "status": "failed",
            "detail": str(error),
            "restart": "not run",
        }
    return {
        "name": name,
        "status": str(result.get("status", "unknown")),
        "detail": f"operation: {result.get('operation', 'unknown')}",
        "restart": "managed by deployment job",
    }
