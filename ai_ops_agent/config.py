"""Configuration loaded from environment variables."""

import os

OPS_TELEGRAM_BOT_TOKEN = os.environ["OPS_TELEGRAM_BOT_TOKEN"]
AUTHORIZED_CHAT_ID = int(os.environ["YOUR_CHAT_ID"])

# Services this bot is allowed to manage. Whitelist only —
# never allow arbitrary service names from user input.
MANAGED_SERVICES = ["ai-agent", "ai-ops-agent"]

# Default service for /logs and /errors when none is given.
DEFAULT_SERVICE = "ai-agent"

# Max characters per Telegram message (hard limit is 4096).
TELEGRAM_MESSAGE_LIMIT = 4000
