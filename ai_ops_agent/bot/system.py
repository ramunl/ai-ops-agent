"""Handle server resource inspection commands."""

from __future__ import annotations

import asyncio
import html

from telegram import Update
from telegram.ext import ContextTypes

from ai_ops_agent.bot.transport import is_authorized, reply_code, reply_html
from ai_ops_agent.command_output import run_command
from ai_ops_agent.metrics import (
    filter_disk_rows,
    format_bytes,
    parse_disk_rows,
    parse_disk_usage,
    parse_memory_usage,
    usage_bar,
)
from ai_ops_agent.settings import C_LOCALE_ENV


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show CPU load, memory usage, and root filesystem usage."""
    if not is_authorized(update):
        return
    load_raw, memory_raw, disk_raw = await asyncio.gather(
        run_command(["cat", "/proc/loadavg"]),
        run_command(["free", "-b"], env=C_LOCALE_ENV),
        run_command(["df", "-hP", "/"], env=C_LOCALE_ENV),
    )
    parts = load_raw.split()
    load_str = " · ".join(parts[:3]) if len(parts) >= 3 else load_raw
    lines = [
        "❤️ <b>Health</b>",
        "",
        f"<b>Load</b>  {html.escape(load_str)}   <i>1m·5m·15m</i>",
    ]
    mem = parse_memory_usage(memory_raw)
    if mem:
        used, total = mem
        pct = used / total * 100 if total else 0
        lines.append(
            f"<b>RAM </b> <code>{usage_bar(pct)}</code> {pct:.0f}%   "
            f"{format_bytes(used)} / {format_bytes(total)}"
        )
    disk = parse_disk_usage(disk_raw)
    if disk:
        used, size, pct = disk
        lines.append(
            f"<b>Disk</b> <code>{usage_bar(pct)}</code> {pct:.0f}%   "
            f"{html.escape(used)} / {html.escape(size)}"
        )
    if not mem or not disk:
        # Parsing failed for at least one metric — fall back to raw tables.
        lines += [
            "",
            f"<pre>{html.escape(memory_raw)}</pre>",
            f"<pre>{html.escape(disk_raw)}</pre>",
        ]
    await reply_html(update, "\n".join(lines))


async def disk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show block-device disk usage with raw output as a parsing fallback."""
    if not is_authorized(update):
        return
    raw = await run_command(["df", "-hP"], env=C_LOCALE_ENV)
    rows = parse_disk_rows(raw)
    if not rows:
        # Parsing failed / no block devices — fall back to the raw table.
        await reply_code(update, "💾 Disk usage", filter_disk_rows(raw))
        return
    lines = ["💾 <b>Disk usage</b>", ""]
    for mount, used, size, pct in rows:
        lines.append(
            f"<b>{html.escape(mount)}</b> <code>{usage_bar(pct)}</code> "
            f"{pct:.0f}%   {html.escape(used)} / {html.escape(size)}"
        )
    await reply_html(update, "\n".join(lines))


async def memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show memory and swap usage with a raw-output fallback."""
    if not is_authorized(update):
        return
    raw = await run_command(["free", "-b"], env=C_LOCALE_ENV)
    mem = parse_memory_usage(raw, "Mem:")
    if not mem:
        await reply_code(update, "🧠 Memory", raw)
        return
    used, total = mem
    pct = used / total * 100 if total else 0
    lines = [
        "🧠 <b>Memory</b>",
        "",
        f"<b>RAM </b> <code>{usage_bar(pct)}</code> {pct:.0f}%   "
        f"{format_bytes(used)} / {format_bytes(total)}",
    ]
    swap = parse_memory_usage(raw, "Swap:")
    if swap and swap[1] > 0:
        sused, stotal = swap
        spct = sused / stotal * 100
        lines.append(
            f"<b>Swap</b> <code>{usage_bar(spct)}</code> {spct:.0f}%   "
            f"{format_bytes(sused)} / {format_bytes(stotal)}"
        )
    elif swap:
        lines.append("<b>Swap</b> <i>none configured</i>")
    await reply_html(update, "\n".join(lines))


async def uptime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show server uptime and recent load averages."""
    if not is_authorized(update):
        return
    pretty, load_raw = await asyncio.gather(
        run_command(["uptime", "-p"]),
        run_command(["cat", "/proc/loadavg"]),
    )
    parts = load_raw.split()
    load_str = " · ".join(parts[:3]) if len(parts) >= 3 else load_raw
    text = (
        "⏱ <b>Uptime</b>\n\n"
        f"{html.escape(pretty)}\n"
        f"<b>load:</b> {html.escape(load_str)}   <i>1m·5m·15m</i>"
    )
    await reply_html(update, text)
