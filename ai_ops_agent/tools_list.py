"""The AI command-line tools the ops tools may inspect and update.

Kept free of bot settings so the ai-tools command (dashboard) can import it
without credentials. The ops bot's /ai_tools uses the same list.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AiTool:
    """One npm-installed AI tool: how to ask its version, what to install."""

    version_cmd: tuple[str, ...]
    package: str


AI_TOOLS = {
    "codex": AiTool(
        ("codex", "--version"),
        os.environ.get("AI_CODEX_NPM_PACKAGE", "@openai/codex"),
    ),
    "claude": AiTool(
        ("claude", "--version"),
        os.environ.get("AI_CLAUDE_NPM_PACKAGE", "@anthropic-ai/claude-code"),
    ),
}
