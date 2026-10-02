"""Focused tests for Telegram command hint registration."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("OPS_TELEGRAM_BOT_TOKEN", "123456:test-token")
os.environ.setdefault("YOUR_CHAT_ID", "123")

from ai_ops_agent import config
from ai_ops_agent.telegram_bot import (
    BOT_COMMANDS,
    build_application,
    register_bot_commands,
    version,
)


class TelegramCommandHintsTest(unittest.IsolatedAsyncioTestCase):
    async def test_register_bot_commands_publishes_full_catalog(self) -> None:
        bot = SimpleNamespace(set_my_commands=AsyncMock())
        application = SimpleNamespace(bot=bot)

        await register_bot_commands(application)

        bot.set_my_commands.assert_awaited_once_with(BOT_COMMANDS)

    def test_shared_base_commands_are_present(self) -> None:
        names = [command.command for command in BOT_COMMANDS]

        self.assertEqual(names[:2], ["help", "version"])

    async def test_version_uses_shared_runtime_report(self) -> None:
        message = SimpleNamespace(
            chat_id=config.AUTHORIZED_CHAT_ID, reply_text=AsyncMock()
        )
        update = SimpleNamespace(message=message, effective_chat=None)

        with (
            patch(
                "ai_ops_agent.bot.help.asyncio.to_thread",
                new=AsyncMock(
                    return_value="ai-ops-agent v1\nbranch: main\ncommit: abc123"
                ),
            ),
            patch(
                "ai_ops_agent.bot.help._CORE_COMMAND.short_line",
                return_value="core: v1.1",
            ),
        ):
            await version(update, SimpleNamespace())

        message.reply_text.assert_awaited_once_with(
            "ai-ops-agent v1\nbranch: main\ncommit: abc123\ncore: v1.1"
        )

    def test_catalog_matches_registered_command_handlers(self) -> None:
        application = build_application()
        handler_commands = {
            command
            for handlers in application.handlers.values()
            for handler in handlers
            for command in getattr(handler, "commands", ())
        }

        self.assertEqual(
            {command.command for command in BOT_COMMANDS}, handler_commands
        )
        self.assertIs(application.post_init, register_bot_commands)


if __name__ == "__main__":
    unittest.main()
