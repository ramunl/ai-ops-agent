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
MANAGED_SERVICES = ["ai-coding-agent", "ai-ops-agent"]

# Default service for /logs when none is given.
DEFAULT_SERVICE = "ai-coding-agent"

assert DEFAULT_SERVICE in MANAGED_SERVICES, (
    f"DEFAULT_SERVICE '{DEFAULT_SERVICE}' not in MANAGED_SERVICES"
)

# Max characters per Telegram message (hard limit is 4096).
TELEGRAM_MESSAGE_LIMIT = 4000

# AI command-line tools reported and updated by /ai_tools.
AI_SYSTEM_INSTALLS = [
    {
        "name": "codex",
        "aliases": ("openai", "openai-codex"),
        "version_cmd": ("codex", "--version"),
        "package_manager": "npm",
        "package": os.environ.get("AI_CODEX_NPM_PACKAGE", "@openai/codex"),
        "model_env_keys": ("CODEX_MODEL", "OPENAI_MODEL", "AI_MODEL", "MODEL"),
        "env_file": os.environ.get(
            "AI_CODEX_ENV", "/etc/ai-coding-agent/ai-coding-agent.env"
        ),
    },
    {
        "name": "claude",
        "aliases": ("claude-code", "anthropic"),
        "version_cmd": ("claude", "--version"),
        "package_manager": "npm",
        "package": os.environ.get(
            "AI_CLAUDE_NPM_PACKAGE",
            "@anthropic-ai/claude-code",
        ),
        "model_env_keys": ("CLAUDE_MODEL", "ANTHROPIC_MODEL", "AI_MODEL", "MODEL"),
        "env_file": os.environ.get(
            "AI_CLAUDE_ENV", "/etc/ai-coding-agent/ai-coding-agent.env"
        ),
    },
]

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
]
