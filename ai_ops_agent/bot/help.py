"""Show help, runtime versions, and shared-core status."""

import asyncio
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from ai_agent_common import CoreCommand
from ai_ops_agent.bot.transport import is_authorized, reply, reply_html
from ai_ops_agent.version import get_runtime_version

ROOT_DIR = Path(__file__).resolve().parents[2]
_CORE_COMMAND = CoreCommand(
    submodule_dir=ROOT_DIR / "ai_agent_common",
    superproject_dir=ROOT_DIR,
    submodule_path="ai_agent_common",
    agent_name="ai-ops-agent",
)


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
        "/core — shared core version\n"
        "/my_agents — installed AI agents, versions, models, and latest status\n"
        "/ai_tools — installed AI tools, versions, models, and update status\n"
        "/ai_tools update &lt;codex|claude|all&gt; — update AI tools\n"
        "/ai_update [agent] — update all AI agents or one by name",
    )


async def version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report the runtime and shared-core versions."""
    if not is_authorized(update):
        return
    text = await asyncio.to_thread(get_runtime_version)
    await reply(update, f"{text}\n{_CORE_COMMAND.short_line()}")


async def core(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report this bot's pinned shared-core version."""
    if not is_authorized(update):
        return
    text = await asyncio.to_thread(_CORE_COMMAND.status_text)
    await reply(update, text)
