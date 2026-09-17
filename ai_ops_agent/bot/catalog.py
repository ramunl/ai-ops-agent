"""Define command names and autocomplete descriptions."""

from __future__ import annotations

from telegram import BotCommand

BOT_COMMANDS = (
    BotCommand("start", "Start the ops bot"),
    BotCommand("help", "Show the command list"),
    BotCommand("health", "Show CPU, RAM, and disk summary"),
    BotCommand("disk", "Show disk usage details"),
    BotCommand("memory", "Show memory details"),
    BotCommand("uptime", "Show uptime and load"),
    BotCommand("services", "Show managed service status"),
    BotCommand("logs", "Show recent service logs"),
    BotCommand("errors", "Show recent service errors"),
    BotCommand("restart", "Restart a managed service"),
    BotCommand("reboot", "Reboot the whole system"),
    BotCommand("update", "Check available system updates"),
    BotCommand("upgrade", "Install system updates"),
    BotCommand("version", "Show the running bot version"),
    BotCommand("my_agents", "Show installed AI agents"),
    BotCommand("ai_tools", "Show or update installed AI tools"),
    BotCommand("ai_update", "Update installed AI agents"),
)
