"""Define the shared command catalog and Telegram hints."""

from ai_agent_common import Command, build_command_list, to_bot_commands

COMMANDS = build_command_list(
    [
        Command("start", "Start the ops bot"),
        Command("health", "Show CPU, RAM, and disk summary"),
        Command("disk", "Show disk usage details"),
        Command("memory", "Show memory details"),
        Command("uptime", "Show uptime and load"),
        Command("services", "Show managed service status"),
        Command("logs", "Show recent service logs"),
        Command("errors", "Show recent service errors"),
        Command("restart", "Restart a managed service"),
        Command("reboot", "Reboot the whole system"),
        Command("update", "Check available system updates"),
        Command("upgrade", "Install system updates"),
        Command("core", "Show the shared core version"),
        Command("my_agents", "Show installed AI agents"),
        Command("ai_tools", "Show or update installed AI tools"),
        Command("ai_update", "Update installed AI agents"),
    ]
)
BOT_COMMANDS = tuple(to_bot_commands(COMMANDS))
