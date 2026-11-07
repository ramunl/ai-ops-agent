"""The repo-reviewed allowlist of installable AI command-line tools.

Kept free of bot settings so the ai-tools command (dashboard) can import it
without credentials. The ops bot's /ai_tools uses the same allowlist, merged
with the Coding installed-tool manifest by ``tool_manifest``.

Only these reviewed specs supply version commands, install packages and install
commands. The manifest is untrusted snapshot content and can never provide any
of those: a newly discovered tool is read-only until a reviewed spec is added
here.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class AiTool:
    """One reviewed, installable AI tool: how to ask its version, what to install.

    ``name`` is the key in the allowlist, so it is not duplicated here.
    """

    version_cmd: tuple[str, ...]
    package: str
    aliases: tuple[str, ...] = ()
    package_manager: str = "npm"
    model_env_keys: tuple[str, ...] = ()
    env_file: str | None = None


_ALLOWLIST: dict[str, AiTool] = {
    "codex": AiTool(
        version_cmd=("codex", "--version"),
        package=os.environ.get("AI_CODEX_NPM_PACKAGE", "@openai/codex"),
        aliases=("openai", "openai-codex"),
        package_manager="npm",
        model_env_keys=("CODEX_MODEL", "OPENAI_MODEL", "AI_MODEL", "MODEL"),
        env_file=os.environ.get(
            "AI_CODEX_ENV", "/etc/ai-coding-agent/ai-coding-agent.env"
        ),
    ),
    "claude": AiTool(
        version_cmd=("claude", "--version"),
        package=os.environ.get("AI_CLAUDE_NPM_PACKAGE", "@anthropic-ai/claude-code"),
        aliases=("claude-code", "anthropic"),
        package_manager="npm",
        model_env_keys=("CLAUDE_MODEL", "ANTHROPIC_MODEL", "AI_MODEL", "MODEL"),
        env_file=os.environ.get(
            "AI_CLAUDE_ENV", "/etc/ai-coding-agent/ai-coding-agent.env"
        ),
    ),
}

# Immutable reviewed install specs keyed by tool name. ``ai-tools`` and the bot
# read only these; a manifest entry cannot mutate them or add an executable tool.
ALLOWLIST: Mapping[str, AiTool] = MappingProxyType(_ALLOWLIST)

# Backwards-compatible name for the executable tool surface used by ai-tools.
AI_TOOLS = ALLOWLIST
