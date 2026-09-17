"""Inspect and update installed AI command-line tools."""

from __future__ import annotations

import asyncio

from ai_ops_agent.command_output import command_failed, run_command
from ai_ops_agent.inventory import read_tool_model


def parse_tool_version(raw: str) -> str:
    """Extract the first version line or report a missing command."""
    if command_failed(raw) or raw.startswith("Command timed out"):
        return "not installed"
    return raw.splitlines()[0].strip() if raw.strip() else "unknown"


async def latest_npm_version(package: str) -> str:
    """Query the published version of a configured npm package."""
    latest = await run_command(["npm", "view", package, "version"], timeout=60)
    if command_failed(latest) or latest.startswith("Command timed out"):
        return "unknown"
    return latest.splitlines()[-1].strip() if latest.strip() else "unknown"


def version_status(installed: str, latest: str) -> str:
    """Describe whether an installed tool matches its published version."""
    if installed == "not installed":
        return "not installed"
    if latest == "unknown":
        return "unknown"
    if latest and latest in installed:
        return "up to date"
    return f"update available: {latest}"


async def inspect_tool(system: dict) -> dict[str, str]:
    """Collect the configured tool version, model, and update status."""
    version_cmd = list(system["version_cmd"])
    version_raw, latest, model = await asyncio.gather(
        run_command(version_cmd, timeout=30),
        latest_npm_version(system["package"])
        if system.get("package_manager") == "npm" and system.get("package")
        else asyncio.to_thread(lambda: "unknown"),
        asyncio.to_thread(lambda: read_tool_model(system)),
    )
    installed = parse_tool_version(version_raw)
    return {
        "name": system["name"],
        "installed": "yes" if installed != "not installed" else "no",
        "version": installed,
        "model": model,
        "package_manager": system.get("package_manager", "unknown"),
        "package": system.get("package", "unknown"),
        "latest": latest,
        "status": version_status(installed, latest),
        "update_command": f"/ai_tools update {system['name']}",
    }


async def update_tool(system: dict) -> dict[str, str]:
    """Update a configured npm CLI and compare its before and after versions."""
    name = system["name"]
    package_manager = system.get("package_manager")
    package = system.get("package")
    if package_manager != "npm" or not package:
        return {
            "name": name,
            "status": "failed",
            "detail": "no supported update command configured",
        }

    before = parse_tool_version(
        await run_command(list(system["version_cmd"]), timeout=30)
    )
    update = await run_command(
        ["npm", "install", "-g", f"{package}@latest"], timeout=300
    )
    if command_failed(update) or update.startswith("Command timed out"):
        return {
            "name": name,
            "status": "failed",
            "detail": update,
        }

    after = parse_tool_version(
        await run_command(list(system["version_cmd"]), timeout=30)
    )
    if before == after:
        status = "already current"
        detail = after
    else:
        status = "updated"
        detail = f"{before} → {after}"
    return {
        "name": name,
        "status": status,
        "detail": detail,
    }
