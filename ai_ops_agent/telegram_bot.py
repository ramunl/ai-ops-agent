"""Telegram bot exposing server operations commands."""

import asyncio
import html
import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from . import config
from .shell import run
from .version import get_git_branch, get_git_commit, get_version

logger = logging.getLogger(__name__)


async def arun(cmd: list[str], **kwargs) -> str:
    return await asyncio.to_thread(run, cmd, **kwargs)


def is_authorized(update: Update) -> bool:
    isAuthorized = (
        update.message is not None
        and update.message.chat_id == config.AUTHORIZED_CHAT_ID
    )
    if not isAuthorized:
        logger.warning(
            "Ignored message from unauthorized chat: %s",
            update.message.chat_id if update.message else "unknown",
        )
    return isAuthorized


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


def _usage_bar(pct: float, width: int = 10) -> str:
    """Render a Unicode progress bar for a 0-100 percentage."""
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100 * width))
    return "█" * filled + "░" * (width - filled)


def _fmt_bytes(n: float) -> str:
    """Human-readable size, e.g. 4.0G."""
    for unit in ("B", "K", "M", "G", "T", "P"):
        if n < 1024 or unit == "P":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}P"


def _mem_usage(free_b: str) -> tuple[int, int] | None:
    """(used_bytes, total_bytes) parsed from `free -b`, or None on failure."""
    for line in free_b.splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            try:
                total = int(parts[1])
                available = int(parts[6]) if len(parts) > 6 else None
                used = total - available if available is not None else int(parts[2])
                return used, total
            except (ValueError, IndexError):
                return None
    return None


def _disk_usage(df_hp: str) -> tuple[str, str, float] | None:
    """(used, size, percent) parsed from `df -hP /`, or None on failure."""
    lines = df_hp.splitlines()
    if len(lines) < 2:
        return None
    parts = lines[1].split()
    try:
        return parts[2], parts[1], float(parts[4].rstrip("%"))
    except (ValueError, IndexError):
        return None


def _strip_exit_prefix(raw: str) -> str:
    """'[exit 3] inactive' → 'inactive'"""
    if raw.startswith("[exit") and "] " in raw:
        return raw.split("] ", 1)[1]
    return raw


def _filter_disk(raw: str) -> str:
    """Keep only real block-device lines from df output, dropping overlay/tmpfs noise."""
    lines = raw.splitlines()
    if not lines:
        return raw
    header = lines[0]
    real = [l for l in lines[1:] if l.startswith("/dev/")]
    return "\n".join([header] + real) if real else raw


def resolve_service(args: list[str] | None) -> str | None:
    """Return a whitelisted service name from args, or None if invalid."""
    if not args:
        return config.DEFAULT_SERVICE
    name = args[0]
    if name in config.MANAGED_SERVICES:
        return name
    logger.info("Rejected non-whitelisted service name: %s", name)
    return None


# ---------------------------------------------------------------- commands


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        await reply_html(
            update,
            "🛠 <b>Ops Agent</b> — ready\n\n"
            "<b>📊 Server info</b>\n"
            "/health — CPU, RAM, disk summary\n"
            "/disk — disk usage details\n"
            "/memory — memory details\n"
            "/uptime — uptime and load\n\n"
            "<b>🔧 Services</b>\n"
            "/services — status of managed services\n"
            "/logs [service] — recent logs\n"
            "/errors [service] — recent errors\n"
            "/restart &lt;service&gt; — restart a service\n\n"
            "<b>📦 Updates</b>\n"
            "/update — check available updates\n"
            "/upgrade — install updates\n\n"
            "<b>🤖 Agent</b>\n"
            "/version — running bot version, branch, and commit",
        )


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        load_raw, memory_raw, disk_raw = await asyncio.gather(
            arun(["cat", "/proc/loadavg"]),
            arun(["free", "-b"]),
            arun(["df", "-hP", "/"]),
        )
        parts = load_raw.split()
        load_str = " · ".join(parts[:3]) if len(parts) >= 3 else load_raw
        lines = [
            "❤️ <b>Health</b>",
            "",
            f"<b>Load</b>  {html.escape(load_str)}   <i>1m·5m·15m</i>",
        ]
        mem = _mem_usage(memory_raw)
        if mem:
            used, total = mem
            pct = used / total * 100 if total else 0
            lines.append(
                f"<b>RAM </b> <code>{_usage_bar(pct)}</code> {pct:.0f}%   "
                f"{_fmt_bytes(used)} / {_fmt_bytes(total)}"
            )
        disk = _disk_usage(disk_raw)
        if disk:
            used, size, pct = disk
            lines.append(
                f"<b>Disk</b> <code>{_usage_bar(pct)}</code> {pct:.0f}%   "
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
    if is_authorized(update):
        raw = await arun(["df", "-h"])
        await reply_code(update, "💾 Disk usage", _filter_disk(raw))


async def memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        raw = await arun(["free", "-h"])
        await reply_code(update, "🧠 Memory", raw)


async def uptime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        pretty, load_raw = await asyncio.gather(
            arun(["uptime", "-p"]),
            arun(["cat", "/proc/loadavg"]),
        )
        parts = load_raw.split()
        load_str = " · ".join(parts[:3]) if len(parts) >= 3 else load_raw
        text = (
            "⏱ <b>Uptime</b>\n\n"
            f"{html.escape(pretty)}\n"
            f"<b>load:</b> {html.escape(load_str)}   <i>1m·5m·15m</i>"
        )
        await reply_html(update, text)


async def services(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        results = await asyncio.gather(
            *(arun(["systemctl", "is-active", name]) for name in config.MANAGED_SERVICES)
        )
        lines = ["🔧 <b>Services</b>", ""]
        for name, raw in zip(config.MANAGED_SERVICES, results):
            state = _strip_exit_prefix(raw).strip()
            icon = "✅" if state == "active" else "❌"
            lines.append(f"{icon} <code>{html.escape(name)}</code> — {html.escape(state)}")
        await reply_html(update, "\n".join(lines))


async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        service = resolve_service(context.args)
        if service is not None:
            output = await arun(
                ["journalctl", "-u", service, "-n", "30", "--no-pager"]
            )
            await reply_expandable(
                update, f"📋 Logs — <code>{html.escape(service)}</code>", output
            )
        else:
            await reply(
                update,
                "Unknown service. Allowed: " + ", ".join(config.MANAGED_SERVICES),
            )


async def errors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        service = resolve_service(context.args)
        if service is not None:
            output = await arun(
                ["journalctl", "-u", service, "-n", "300", "--no-pager", "-p", "err"]
            )
            await reply_expandable(
                update, f"🚨 Errors — <code>{html.escape(service)}</code>", output
            )
        else:
            await reply(
                update,
                "Unknown service. Allowed: " + ", ".join(config.MANAGED_SERVICES),
            )


async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        if not context.args:
            await reply(update, "Usage: /restart <service>")
            return
        service = resolve_service(context.args)
        if service is None:
            await reply(
                update,
                "Unknown service. Allowed: " + ", ".join(config.MANAGED_SERVICES),
            )
            return
        svc = html.escape(service)
        await update.message.reply_text(f"♻️ Restarting <code>{svc}</code>...", parse_mode="HTML")
        result = await arun(["systemctl", "restart", service])
        if result.startswith("[exit") or result.startswith("Error:"):
            await reply_expandable(update, f"⚠️ Restart failed — <code>{svc}</code>", result)
            return
        # Poll up to 10 s for a stable state
        state = "activating"
        for _ in range(10):
            await asyncio.sleep(1)
            state = _strip_exit_prefix(
                await arun(["systemctl", "is-active", service])
            ).strip()
            if state not in ("activating", "deactivating"):
                break
        icon = "✅" if state == "active" else "❌"
        await update.message.reply_text(
            f"{icon} <code>{svc}</code> is now: <b>{html.escape(state)}</b>",
            parse_mode="HTML",
        )


async def update_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        await reply(update, "🔍 Checking for updates...")
        update_result = await arun(["apt-get", "update", "-q"], timeout=300)
        if update_result.startswith("[exit") or update_result.startswith("Error:"):
            await reply_expandable(update, "⚠️ apt-get update failed", update_result)
            return
        output = await arun(["apt", "list", "--upgradable"], timeout=120)
        await reply_expandable(update, "📦 Upgradable packages", output)


async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        await reply(update, "⬆️ Upgrading packages, this may take a while...")
        output = await arun(["apt-get", "upgrade", "-y", "-q"], timeout=900)
        failed = output.startswith("[exit") or output.startswith("Error:")
        if failed:
            # Show the beginning (where errors appear) + tail
            summary = (
                output[:1500] + "\n...\n" + output[-500:]
                if len(output) > 2000
                else output
            )
            await reply_expandable(update, "❌ Upgrade failed", summary)
        else:
            tail = output[-2000:] if len(output) > 2000 else output
            await reply_expandable(update, "✅ Upgrade complete", tail)


async def version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        ver, branch, commit = await asyncio.to_thread(
            lambda: (get_version(), get_git_branch(), get_git_commit())
        )
        text = (
            f"🤖 <b>ai_ops_agent</b> <code>v{html.escape(ver)}</code>\n"
            f"<b>branch:</b> <code>{html.escape(branch)}</code>\n"
            f"<b>commit:</b> <code>{html.escape(commit)}</code>"
        )
        await reply_html(update, text)


def build_application() -> Application:
    app = Application.builder().token(config.OPS_TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CommandHandler("disk", disk))
    app.add_handler(CommandHandler("memory", memory))
    app.add_handler(CommandHandler("uptime", uptime))
    app.add_handler(CommandHandler("services", services))
    app.add_handler(CommandHandler("logs", logs))
    app.add_handler(CommandHandler("errors", errors))
    app.add_handler(CommandHandler("restart", restart))
    app.add_handler(CommandHandler("update", update_cmd))
    app.add_handler(CommandHandler("upgrade", upgrade))
    app.add_handler(CommandHandler("version", version))
    return app
