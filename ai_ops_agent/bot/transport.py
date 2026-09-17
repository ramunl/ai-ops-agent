"""Authorize messages and render Telegram replies."""

from __future__ import annotations

import asyncio
import html
import json
import logging
from urllib import error, request

from telegram import Update

from ai_ops_agent import config

logger = logging.getLogger(__name__)


def is_authorized(update: Update) -> bool:
    """Check the owner chat and log unauthorized messages."""
    is_authorized_chat = (
        update.message is not None
        and update.message.chat_id == config.AUTHORIZED_CHAT_ID
    )
    if not is_authorized_chat:
        logger.warning(
            "Ignored message from unauthorized chat: %s",
            update.message.chat_id if update.message else "unknown",
        )
    return is_authorized_chat


_SUFFIX = "\n... (truncated)"


async def reply(update: Update, text: str) -> None:
    """Reply with plain text, truncated to fit Telegram's limit."""
    limit = config.TELEGRAM_MESSAGE_LIMIT - len(_SUFFIX)
    if len(text) > limit:
        text = text[:limit] + _SUFFIX
    await update.message.reply_text(text)


async def reply_code(update: Update, header: str, body: str) -> None:
    """Reply with a header line followed by body in a monospace code block."""
    overhead = len(header) + 1 + len("<pre></pre>")
    limit = config.TELEGRAM_MESSAGE_LIMIT - overhead - len(_SUFFIX)
    if len(body) > limit:
        body = body[:limit] + _SUFFIX
    text = f"{header}\n<pre>{html.escape(body)}</pre>"
    await update.message.reply_text(text, parse_mode="HTML")


async def reply_expandable(update: Update, header: str, body: str) -> None:
    """Reply with header + collapsible body (tap to expand). Use for long outputs."""
    overhead = len(header) + 1 + len("<blockquote expandable></blockquote>")
    limit = config.TELEGRAM_MESSAGE_LIMIT - overhead - len(_SUFFIX)
    if len(body) > limit:
        body = body[:limit] + _SUFFIX
    text = f"{header}\n<blockquote expandable>{html.escape(body)}</blockquote>"
    await update.message.reply_text(text, parse_mode="HTML")


async def reply_html(update: Update, text: str) -> None:
    """Reply with HTML-formatted text, truncated to fit Telegram's limit."""
    if len(text) > config.TELEGRAM_MESSAGE_LIMIT:
        text = text[: config.TELEGRAM_MESSAGE_LIMIT - len(_SUFFIX)] + _SUFFIX
    await update.message.reply_text(text, parse_mode="HTML")


async def send_rich_message(update: Update, html_text: str) -> bool:
    """Send a Bot API 10.1 rich message. Return False if unsupported/unavailable."""
    if update.message is None:
        return False

    payload = {
        "chat_id": update.message.chat_id,
        "rich_message": {"html": html_text},
    }
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        f"https://api.telegram.org/bot{config.OPS_TELEGRAM_BOT_TOKEN}/sendRichMessage",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    def _post() -> bool:
        try:
            with request.urlopen(req, timeout=15) as response:
                body = json.loads(response.read().decode("utf-8"))
                return bool(body.get("ok"))
        except (OSError, error.HTTPError, json.JSONDecodeError):
            logger.info(
                "sendRichMessage unavailable; falling back to HTML", exc_info=True
            )
            return False

    return await asyncio.to_thread(_post)
