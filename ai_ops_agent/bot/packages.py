"""Handle system package checks and upgrades."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

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
    """Upgrade system packages and summarize command success or failure."""
    if not is_authorized(update):
        return
    await reply(update, "⬆️ Upgrading packages, this may take a while...")
    output = await run_command(["apt-get", "upgrade", "-y", "-q"], timeout=900)
    failed = command_failed(output)
    if failed:
        # Show the beginning (where errors appear) + tail
        summary = (
            output[:1500] + "\n...\n" + output[-500:] if len(output) > 2000 else output
        )
        await reply_expandable(update, "❌ Upgrade failed", summary)
    else:
        tail = output[-2000:] if len(output) > 2000 else output
        await reply_expandable(update, "✅ Upgrade complete", tail)
