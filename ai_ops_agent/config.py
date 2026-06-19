"""Configuration loaded from environment variables."""

import os
import sys


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"ERROR: required environment variable '{name}' is not set.")
    return value


OPS_TELEGRAM_BOT_TOKEN = _require_env("OPS_TELEGRAM_BOT_TOKEN")

_chat_id_raw = _require_env("YOUR_CHAT_ID")
try:
    AUTHORIZED_CHAT_ID = int(_chat_id_raw)
except ValueError:
    sys.exit(f"ERROR: YOUR_CHAT_ID must be an integer, got: {_chat_id_raw!r}")

# Services this bot is allowed to manage. Whitelist only —
# never allow arbitrary service names from user input.
MANAGED_SERVICES = ["ai-agent", "ai-ops-agent"]

# Default service for /logs when none is given.
DEFAULT_SERVICE = "ai-agent"

assert DEFAULT_SERVICE in MANAGED_SERVICES, (
    f"DEFAULT_SERVICE '{DEFAULT_SERVICE}' not in MANAGED_SERVICES"
)

# Max characters per Telegram message (hard limit is 4096).
TELEGRAM_MESSAGE_LIMIT = 4000
