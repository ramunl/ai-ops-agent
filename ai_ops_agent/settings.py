"""Define locale and model metadata settings."""

from __future__ import annotations

C_LOCALE_ENV = {"LC_ALL": "C"}


MODEL_ENV_KEYS = (
    "AI_MODEL",
    "MODEL",
    "OPENAI_MODEL",
    "CODEX_MODEL",
    "PM_MODEL",
    "ANTHROPIC_MODEL",
    "CLAUDE_MODEL",
    "LLM_MODEL",
)
