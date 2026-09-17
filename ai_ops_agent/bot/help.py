"""Show help and the running agent version."""

from __future__ import annotations

import asyncio
import html

from telegram import Update
from telegram.ext import ContextTypes

from ai_ops_agent.bot.transport import is_authorized, reply_html
from ai_ops_agent.version import get_git_branch, get_git_commit, get_version


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the available commands and their usage."""
    if not is_authorized(update):
        return
    await reply_html(
        update,
        "🛠 <b>Ops Agent</b> — ready\n\n"
        "/help — show this command list\n\n"
        "<b>📊 Server info</b>\n"
        "/health — CPU, RAM, disk summary\n"
        "/disk — disk usage details\n"
        "/memory — memory details\n"
        "/uptime — uptime and load\n\n"
        "<b>🔧 Services</b>\n"
        "/services — status of managed services\n"
        "/logs [service] — recent logs\n"
        "/errors [service] — recent errors, all services by default\n"
        "/restart &lt;service&gt; — restart a service\n\n"
        "<b>⚙️ System</b>\n"
        "/reboot — reboot the whole system\n\n"
        "<b>📦 Updates</b>\n"
        "/update — check available updates\n"
        "/upgrade — install updates\n\n"
        "<b>🤖 Agent</b>\n"
        "/version — running bot version, branch, and commit\n"
        "/my_agents — installed AI agents, versions, models, and latest status\n"
        "/ai_tools — installed AI tools, versions, models, and update status\n"
        "/ai_tools update &lt;codex|claude|all&gt; — update AI tools\n"
        "/ai_update [agent] — update all AI agents or one by name",
    )


async def version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report the ops agent version, branch, and commit."""
    if not is_authorized(update):
        return
    ver, branch, commit = await asyncio.to_thread(
        lambda: (get_version(), get_git_branch(), get_git_commit())
    )
    text = (
        f"🤖 <b>ai_ops_agent</b> <code>v{html.escape(ver)}</code>\n"
        f"<b>branch:</b> <code>{html.escape(branch)}</code>\n"
        f"<b>commit:</b> <code>{html.escape(commit)}</code>"
    )
    await reply_html(update, text)
