"""Wire Telegram commands and startup registration."""

from __future__ import annotations

import logging

from telegram.ext import Application, CommandHandler

from ai_ops_agent import config
from ai_ops_agent.bot.agents import ai_tools, ai_update, my_agents
from ai_ops_agent.bot.catalog import BOT_COMMANDS
from ai_ops_agent.bot.help import start, version
from ai_ops_agent.bot.packages import update_cmd, upgrade
from ai_ops_agent.bot.services import errors, logs, reboot, restart, services
from ai_ops_agent.bot.system import disk, health, memory, uptime

logger = logging.getLogger(__name__)


async def register_bot_commands(application: Application) -> None:
    """Publish commands so Telegram clients show suggestions after typing `/`."""
    await application.bot.set_my_commands(BOT_COMMANDS)
    logger.info("Registered %d Telegram command hints", len(BOT_COMMANDS))


def build_application() -> Application:
    """Build the application with command handlers and startup hooks."""
    app = (
        Application.builder()
        .token(config.OPS_TELEGRAM_BOT_TOKEN)
        .post_init(register_bot_commands)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CommandHandler("disk", disk))
    app.add_handler(CommandHandler("memory", memory))
    app.add_handler(CommandHandler("uptime", uptime))
    app.add_handler(CommandHandler("services", services))
    app.add_handler(CommandHandler("logs", logs))
    app.add_handler(CommandHandler("errors", errors))
    app.add_handler(CommandHandler("restart", restart))
    app.add_handler(CommandHandler("reboot", reboot))
    app.add_handler(CommandHandler("update", update_cmd))
    app.add_handler(CommandHandler("upgrade", upgrade))
    app.add_handler(CommandHandler("version", version))
    app.add_handler(CommandHandler("my_agents", my_agents))
    app.add_handler(CommandHandler("ai_tools", ai_tools))
    app.add_handler(CommandHandler("ai_update", ai_update))
    return app
