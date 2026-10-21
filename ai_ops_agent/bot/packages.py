"""Handle system package checks and upgrades."""

from __future__ import annotations

import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from ai_ops_agent import package_updates
from ai_ops_agent.bot.package_reports import reply_update_report
from ai_ops_agent.bot.transport import is_authorized, reply, reply_expandable
from ai_ops_agent.command_output import command_failed, run_command


async def update_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Refresh package indexes and report available updates."""
    if not is_authorized(update):
        return
    await reply(update, "🔍 Checking for updates...")
    update_result = await run_command(["apt-get", "update", "-q"], timeout=300)
    if command_failed(update_result):
        await reply_expandable(update, "⚠️ apt-get update failed", update_result)
        return
    output = await run_command(["apt", "list", "--upgradable"], timeout=120)
    if command_failed(output):
        await reply_expandable(update, "⚠️ apt list failed", output)
        return
    await reply_update_report(update, output)


async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start the protected package upgrade in its own unit and report the start.

    apt runs in the detached unit, so the reply only says whether the unit was
    started or why it was not; progress is in its journal (ai-packages-upgrade).
    """
    if not is_authorized(update):
        return
    result = await asyncio.to_thread(package_updates.start_upgrade)
    if result.get("ok"):
        await reply(update, f"⬆️ Upgrade started: {result['unit']}")
    elif result.get("busy"):
        await reply(update, f"⏳ Upgrade not started: {result['error']}")
    else:
        await reply_expandable(
            update,
            "❌ Upgrade not started",
            result.get("error") or "systemd-run did not start the upgrade.",
        )
