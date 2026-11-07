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

# Services this bot is allowed to manage. Whitelist only — never allow
# arbitrary service names from user input. Defined once, shared with the
# dashboard's ai-service command.
from ai_ops_agent.services_list import (  # noqa: E402
    MANAGED_SERVICES,
    SERVICE_PORTS,
    validate_service_ports,
)
from ai_ops_agent.tool_manifest import load_merged_tools  # noqa: E402
from ai_ops_agent.tools_list import ALLOWLIST  # noqa: E402

try:
    validate_service_ports(MANAGED_SERVICES, SERVICE_PORTS)
except ValueError as e:
    sys.exit(f"ERROR: invalid service port settings: {e}")

# Default service for /logs when none is given.
DEFAULT_SERVICE = "ai-coding-agent"

assert DEFAULT_SERVICE in MANAGED_SERVICES, (
    f"DEFAULT_SERVICE '{DEFAULT_SERVICE}' not in MANAGED_SERVICES"
)

# Max characters per Telegram message (hard limit is 4096).
TELEGRAM_MESSAGE_LIMIT = 4000

# AI command-line tools reported and updated by /ai_tools. Built once from the
# reviewed allowlist merged with the validated Coding installed-tool manifest;
# a missing or invalid manifest falls back to the allowlist alone. Executable
# specs (version_cmd, package, install command) come only from the allowlist.
AI_SYSTEM_INSTALLS = load_merged_tools(ALLOWLIST)

# AI agents reported by /my_agents.
AI_AGENT_INSTALLS = [
    {
        "name": "ai-coding-agent",
        "aliases": ("coding", "coder", "ai-agent"),
        "path": os.environ.get("AI_CODING_AGENT_DIR", "/opt/ai-coding-agent"),
        "service": os.environ.get("AI_CODING_AGENT_SERVICE", "ai-coding-agent"),
        "env_file": os.environ.get(
            "AI_CODING_AGENT_ENV", "/etc/ai-coding-agent/ai-coding-agent.env"
        ),
    },
    {
        "name": "ai-pm-agent",
        "aliases": ("pm", "project-manager"),
        "path": os.environ.get("AI_PM_AGENT_DIR", "/opt/ai-pm-agent"),
        "service": os.environ.get("AI_PM_AGENT_SERVICE", "ai-pm-agent"),
        "env_file": os.environ.get("AI_PM_AGENT_ENV", "/etc/ai-pm-agent.env"),
    },
    {
        "name": "ai-ops-agent",
        "aliases": ("ops", "bot"),
        "path": os.environ.get("AI_OPS_AGENT_DIR", "/opt/ai-ops-agent"),
        "service": os.environ.get("AI_OPS_AGENT_SERVICE", "ai-ops-agent"),
        "env_file": os.environ.get("AI_OPS_AGENT_ENV", "/etc/ai-ops-agent.env"),
    },
    {
        "name": "ai-dashboard",
        "aliases": ("dashboard",),
        "path": os.environ.get("AI_DASHBOARD_DIR", "/opt/ai-dashboard"),
        "service": "ai-dashboard",
    },
]
