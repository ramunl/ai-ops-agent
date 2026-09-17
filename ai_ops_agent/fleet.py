"""Inspect agent checkouts and coordinate updates and service restarts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from ai_ops_agent.command_output import command_failed, run_command, strip_exit_prefix
from ai_ops_agent.inventory import read_agent_model, read_agent_version


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


async def update_agent(agent: dict, *, defer_self_restart: bool) -> dict[str, str]:
    """Fast-forward an agent checkout and coordinate its service restart."""
    path = Path(agent["path"])
    name = agent["name"]
    service = agent.get("service") or ""
    if not path.exists():
        return {
            "name": name,
            "status": "skipped",
            "detail": f"not installed at {path}",
            "restart": "not run",
        }

    try:
        checkout = await _fast_forward(path)
    except AgentUpdateError as error:
        return {
            "name": name,
            "status": "failed",
            "detail": str(error),
            "restart": "not run",
        }
    before, after = checkout.before, checkout.after
    changed = after != before
    status = "updated" if changed else "already current"
    detail = f"{before} → {after}" if changed else before

    if not changed:
        restart = "not needed"
    elif name == "ai-ops-agent" and defer_self_restart:
        restart = "deferred until after report"
    else:
        restart = await restart_agent_service(service)

    return {
        "name": name,
        "status": status,
        "detail": detail,
        "restart": restart,
    }


class AgentUpdateError(RuntimeError):
    """An agent checkout could not be read or fast-forwarded."""


@dataclass(frozen=True)
class _CheckoutUpdate:
    """Capture revisions on either side of a successful fast-forward."""

    before: str
    after: str


async def _fast_forward(path: Path) -> _CheckoutUpdate:
    """Fast-forward a checkout and return its verified before and after commits."""
    command = ["git", "-C", str(path), "rev-parse", "--short", "HEAD"]
    before = await run_command(command)
    if command_failed(before) or not before:
        raise AgentUpdateError(f"cannot read current commit: {before}")
    output = await run_command(
        ["git", "-C", str(path), "pull", "--ff-only"], timeout=180
    )
    if command_failed(output):
        raise AgentUpdateError(output)
    after = await run_command(command)
    if command_failed(after) or not after:
        raise AgentUpdateError(f"cannot read updated commit: {after}")
    return _CheckoutUpdate(before, after)
