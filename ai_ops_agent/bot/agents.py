"""Handle agent and AI tool inspection and updates."""

from __future__ import annotations

import asyncio
import html

from telegram import Update
from telegram.ext import ContextTypes

from ai_ops_agent import config
from ai_ops_agent.ai_tools import inspect_tool, update_tool
from ai_ops_agent.bot.transport import is_authorized, reply, reply_html
from ai_ops_agent.fleet import (
    DEPLOY_TARGETS,
    DeploymentError,
    deployment_status,
    inspect_agent,
    rollback_agent,
    update_agent,
)
from ai_ops_agent.inventory import (
    agent_names,
    resolve_ai_agents,
    resolve_ai_systems,
    tool_names,
)


def _format_ai_system_info(system: dict[str, str]) -> list[str]:
    return [
        f"<b>{html.escape(system['name'])}</b>",
        f"installed: <code>{html.escape(system['installed'])}</code>",
        f"version: <code>{html.escape(system['version'])}</code>",
        f"model: <code>{html.escape(system['model'])}</code>",
        f"package: <code>{html.escape(system['package_manager'])}</code> "
        f"<code>{html.escape(system['package'])}</code>",
        f"latest: <code>{html.escape(system['latest'])}</code>",
        f"status: <code>{html.escape(system['status'])}</code>",
        f"update: <code>{html.escape(system['update_command'])}</code>",
    ]


def _format_ai_system_update_result(result: dict[str, str]) -> list[str]:
    return [
        f"<b>{html.escape(result['name'])}</b>",
        f"status: <code>{html.escape(result['status'])}</code>",
        f"detail: <code>{html.escape(result['detail'])}</code>",
    ]


def _format_ai_agent_info(agent: dict[str, str]) -> list[str]:
    return [
        f"<b>{html.escape(agent['name'])}</b>",
        f"installed: <code>{html.escape(agent['installed'])}</code>",
        f"path: <code>{html.escape(agent['path'])}</code>",
        f"service: <code>{html.escape(agent['service'])}</code> "
        f"({html.escape(agent['service_state'])})",
        f"version: <code>{html.escape(agent['version'])}</code>",
        f"model: <code>{html.escape(agent['model'])}</code>",
        f"git: <code>{html.escape(agent['branch'])}</code> "
        f"<code>{html.escape(agent['commit'])}</code>",
        f"latest: <code>{html.escape(agent['latest'])}</code>",
    ]


def _format_ai_update_result(result: dict[str, str]) -> list[str]:
    return [
        f"<b>{html.escape(result['name'])}</b>",
        f"status: <code>{html.escape(result['status'])}</code>",
        f"detail: <code>{html.escape(result['detail'])}</code>",
        f"service: <code>{html.escape(result['restart'])}</code>",
    ]


async def my_agents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show installed agents and their current status."""
    if not is_authorized(update):
        return
    agents = await asyncio.gather(
        *(inspect_agent(agent) for agent in config.AI_AGENT_INSTALLS)
    )
    try:
        deployed = {entry["name"]: entry for entry in await deployment_status()}
    except DeploymentError:
        deployed = {}
    lines = ["🤖 <b>AI agents</b>", ""]
    for idx, agent in enumerate(agents):
        if idx:
            lines.append("")
        lines.extend(_format_ai_agent_info(agent))
        entry = deployed.get(agent["name"]) or deployed.get(
            DEPLOY_TARGETS.get(agent["name"])
        )
        if entry:
            lines.extend(_deployment_lines(entry))
    await reply_html(update, "\n".join(lines))


async def ai_tools(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show installed CLI tools or update the selected configured tools."""
    if not is_authorized(update):
        return
    if context.args and context.args[0].lower() == "update":
        systems = resolve_ai_systems(context.args[1:])
        if systems is None:
            await reply(
                update,
                "Unknown AI tool. Allowed: " + tool_names(),
            )
            return

        target = "all AI tools" if len(systems) > 1 else systems[0]["name"]
        await reply(update, f"⬇️ Updating {target}...")
        results = []
        for system in systems:
            results.append(await update_tool(system))
        lines = ["🤖 <b>AI tool update</b>", ""]
        for idx, result in enumerate(results):
            if idx:
                lines.append("")
            lines.extend(_format_ai_system_update_result(result))
        await reply_html(update, "\n".join(lines))
        return

    systems = await asyncio.gather(
        *(inspect_tool(system) for system in config.AI_SYSTEM_INSTALLS)
    )
    lines = ["🤖 <b>AI tools</b>", ""]
    for idx, system in enumerate(systems):
        if idx:
            lines.append("")
        lines.extend(_format_ai_system_info(system))
    lines += [
        "",
        "Use <code>/ai_tools update codex</code>, "
        "<code>/ai_tools update claude</code>, or "
        "<code>/ai_tools update all</code>.",
    ]
    await reply_html(update, "\n".join(lines))


async def ai_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Queue selected deployments without restarting the bot process itself."""
    if not is_authorized(update):
        return
    if len(context.args) > 1:
        await reply(update, "Usage: /ai_update [coding|pm|ops|dashboard|all]")
        return
    agents = resolve_ai_agents([] if context.args == ["all"] else context.args)
    if agents is None:
        await reply(
            update,
            "Unknown AI agent. Allowed: " + agent_names(),
        )
        return

    target = "all AI agents" if not context.args else agents[0]["name"]
    await reply(update, f"⬇️ Updating {target}...")
    results = []
    if len(agents) > 1:
        agents = [{"name": "all"}]
    for agent in agents:
        result = await update_agent(agent, defer_self_restart=True)
        results.append(result)

    lines = ["🤖 <b>AI update</b>", ""]
    for idx, result in enumerate(results):
        if idx:
            lines.append("")
        lines.extend(_format_ai_update_result(result))
    await reply_html(update, "\n".join(lines))


def _deployment_lines(entry: dict) -> list[str]:
    lines = [f"deployment: {html.escape(str(entry.get('status', 'unknown')))}"]
    for key in ("current", "previous"):
        revision = entry.get(key) or {}
        lines.append(
            f"{key}: <code>{html.escape(str(revision.get('commit', 'none')))}</code> "
            f"{html.escape(str(revision.get('version', '')))}"
        )
    if entry.get("error"):
        lines.append(f"error: {html.escape(str(entry['error']))}")
    return lines


async def deployments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report current and previously verified deployments to the owner."""
    if not is_authorized(update):
        return
    try:
        targets = await deployment_status()
    except DeploymentError as error:
        await reply(update, f"Deployment status unavailable: {error}")
        return
    lines = ["<b>Deployments</b>"]
    for entry in targets:
        lines.append(f"\n<b>{html.escape(str(entry.get('name', 'unknown')))}</b>")
        lines.extend(_deployment_lines(entry))
    await reply_html(update, "\n".join(lines))


async def rollback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Queue rollback of one allowlisted target to its previous verified release."""
    if not is_authorized(update):
        return
    if len(context.args) != 1 or context.args[0] not in DEPLOY_TARGETS.values():
        await reply(update, "Usage: /rollback <coding|pm|ops|dashboard>")
        return
    try:
        result = await rollback_agent(context.args[0])
    except DeploymentError as error:
        await reply(update, f"Rollback failed: {error}")
        return
    await reply(
        update,
        f"Rollback queued: {result.get('operation', 'unknown')}. "
        "Use /deployments to check the result.",
    )
