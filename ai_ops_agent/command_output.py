"""Run blocking commands asynchronously and interpret command output."""

from __future__ import annotations

import asyncio
from typing import Any

from ai_ops_agent.shell import run


async def run_command(cmd: list[str], **kwargs: Any) -> str:
    """Run a blocking argument-list command outside the event loop."""
    return await asyncio.to_thread(run, cmd, **kwargs)


def strip_exit_prefix(raw: str) -> str:
    """'[exit 3] inactive' → 'inactive'"""
    if raw.startswith("[exit") and "] " in raw:
        return raw.split("] ", 1)[1]
    return raw


def command_failed(output: str) -> bool:
    """Recognize process errors and timeouts in command output."""
    return output.startswith(("[exit", "Error:", "Command timed out"))
