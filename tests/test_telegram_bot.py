"""Focused tests for Telegram command hint registration."""

import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

os.environ.setdefault("OPS_TELEGRAM_BOT_TOKEN", "123456:test-token")
os.environ.setdefault("YOUR_CHAT_ID", "123")

from ai_ops_agent.telegram_bot import (
    BOT_COMMANDS,
    build_application,
    register_bot_commands,
)


class TelegramCommandHintsTest(unittest.IsolatedAsyncioTestCase):
    async def test_register_bot_commands_publishes_full_catalog(self) -> None:
        bot = SimpleNamespace(set_my_commands=AsyncMock())
        application = SimpleNamespace(bot=bot)

        await register_bot_commands(application)

        bot.set_my_commands.assert_awaited_once_with(BOT_COMMANDS)

    def test_catalog_matches_registered_command_handlers(self) -> None:
        application = build_application()
        handler_commands = {
            command
            for handlers in application.handlers.values()
            for handler in handlers
            for command in getattr(handler, "commands", ())
        }

        self.assertEqual({command.command for command in BOT_COMMANDS}, handler_commands)
        self.assertIs(application.post_init, register_bot_commands)


if __name__ == "__main__":
    unittest.main()
