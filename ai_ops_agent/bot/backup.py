"""Nightly backup sent to the owner's chat, and /backup to make one now.

The archive is made by ai-backup (ai_ops_agent/backup.py), encrypted with the
key in /etc/ai-backup/key; the bot only sends the finished file, so a copy
lives off the server. Telegram keeps the documents; the server keeps the
last seven.
"""

from __future__ import annotations

import asyncio
import html
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
from ai_ops_agent.backup_info import format_backup_info
from ai_ops_agent.bot.transport import is_authorized, reply
from ai_ops_agent.command_output import command_failed, run_command, strip_exit_prefix

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
    if command_failed(output) and isinstance(result, dict) and result.get("ok"):
        return {"ok": False, "error": "backup command failed"}
    return result if isinstance(result, dict) else {"ok": False, "error": "bad output"}


def caption(result: dict) -> str:
    """Keep the document caption short; source details follow in messages."""
    size = max(1, round(result.get("bytes", 0) / 1024))
    return (
        f"🗄 Backup · {len(result.get('included', []))} sources · {size} KB\n"
        f"Skipped: {len(result.get('skipped', []))} sources. Details follow."
    )


async def send_details(bot: Any, chat_id: int, text: str) -> None:
    """Send all plain text as escaped HTML chunks bounded even after escaping."""
    chunk = ""
    units = 0
    for character in text:
        escaped = html.escape(character)
        width = len(escaped.encode("utf-16-le")) // 2
        if units + width > config.TELEGRAM_MESSAGE_LIMIT:
            await bot.send_message(chat_id, chunk, parse_mode="HTML")
            chunk, units = "", 0
        chunk += escaped
        units += width
    if chunk:
        await bot.send_message(chat_id, chunk, parse_mode="HTML")


async def send_backup(bot: Any, chat_id: int, result: dict) -> None:
    """Send the archive and complete source details only to the owner."""
    if chat_id != config.AUTHORIZED_CHAT_ID:
        return
    if not result.get("ok"):
        await send_details(bot, chat_id, format_backup_info(result))
        return
    path = Path(result["file"])
    with path.open("rb") as handle:
        await bot.send_document(
            chat_id, document=handle, filename=path.name, caption=caption(result)
        )

    await send_details(
        bot, chat_id, f"{format_backup_info(result)}\n\nRestore: {RESTORE}"
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
