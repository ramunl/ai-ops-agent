"""Nightly backup sent to the owner's chat, and /backup to make one now.

The archive is made by ai-backup (ai_ops_agent/backup.py), encrypted with the
key in /etc/ai-backup/key; the bot only sends the finished file, so a copy
lives off the server. Telegram keeps the documents; the server keeps the
last seven.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.ext import Application, ContextTypes

from ai_ops_agent import config
from ai_ops_agent.backup import CIPHER
from ai_ops_agent.bot.transport import is_authorized, reply
from ai_ops_agent.command_output import run_command, strip_exit_prefix

logger = logging.getLogger(__name__)

BACKUP_COMMAND = "/usr/local/sbin/ai-backup"
BACKUP_HOUR = int(os.environ.get("OPS_BACKUP_HOUR", "4"))  # server local time
RESTORE = (
    f"openssl enc -d {' '.join(CIPHER[:-1])} -pass file:KEYFILE -in FILE | tar -xz"
)


async def make_backup() -> dict:
    """Run ai-backup and return its JSON result (or an error result)."""
    output = await run_command([BACKUP_COMMAND, "run"], timeout=300)
    try:
        result = json.loads(strip_exit_prefix(output))
    except ValueError:
        return {"ok": False, "error": output.strip()[:300] or "no output"}
    return result if isinstance(result, dict) else {"ok": False, "error": "bad output"}


def caption(result: dict) -> str:
    """What the document message says: contents, gaps and how to restore."""
    size = max(1, round(result.get("bytes", 0) / 1024))
    lines = [f"🗄 Backup · {len(result.get('included', []))} files · {size} KB"]
    if result.get("skipped"):
        lines.append("Not on this server: " + ", ".join(result["skipped"]))
    lines.append(f"Restore: {RESTORE}")
    return "\n".join(lines)[:1024]  # Telegram's caption limit


async def send_backup(bot: Any, chat_id: int, result: dict) -> None:
    """Send the archive, or say why there is none."""
    if not result.get("ok"):
        await bot.send_message(chat_id, f"⚠️ Backup failed: {result.get('error')}")
        return
    path = Path(result["file"])
    with path.open("rb") as handle:
        await bot.send_document(
            chat_id, document=handle, filename=path.name, caption=caption(result)
        )


async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Make a backup now and send it here."""
    if not is_authorized(update):
        return
    await reply(update, "🗄 Backing up…")
    await send_backup(context.bot, update.effective_chat.id, await make_backup())


def seconds_until(hour: int, now: datetime) -> float:
    """Seconds from now until the next time the clock shows hour:00."""
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def backup_forever(application: Application, hour: int = BACKUP_HOUR) -> None:
    """Every night at hour, back up and send the archive to the owner."""
    while True:
        await asyncio.sleep(seconds_until(hour, datetime.now()))
        try:
            await send_backup(
                application.bot, config.AUTHORIZED_CHAT_ID, await make_backup()
            )
        except Exception as error:  # the next night must still run
            logger.warning("Nightly backup failed: %s", error)
