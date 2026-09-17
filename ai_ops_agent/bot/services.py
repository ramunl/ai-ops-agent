"""Handle whitelisted service operations and system reboot."""

from __future__ import annotations

import asyncio
import html
import logging

from telegram import Update
from telegram.ext import ContextTypes

from ai_ops_agent import config
from ai_ops_agent.bot.transport import (
    is_authorized,
    reply,
    reply_expandable,
    reply_html,
)
from ai_ops_agent.command_output import command_failed, run_command, strip_exit_prefix

logger = logging.getLogger(__name__)


def resolve_service(args: list[str] | None) -> str | None:
    """Return a whitelisted service name from args, or None if invalid."""
    if not args:
        return config.DEFAULT_SERVICE
    name = args[0]
    if name in config.MANAGED_SERVICES:
        return name
    logger.info("Rejected non-whitelisted service name: %s", name)
    return None


def _journalctl_unit_args(services: list[str]) -> list[str]:
    args = []
    for service in services:
        args.extend(["-u", service])
    return args


async def services(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report the state of each whitelisted system service."""
    if not is_authorized(update):
        return
    results = await asyncio.gather(
        *(
            run_command(["systemctl", "is-active", name])
            for name in config.MANAGED_SERVICES
        )
    )
    lines = ["🔧 <b>Services</b>", ""]
    for name, raw in zip(config.MANAGED_SERVICES, results):
        state = strip_exit_prefix(raw).strip()
        icon = "✅" if state == "active" else "❌"
        lines.append(f"{icon} <code>{html.escape(name)}</code> — {html.escape(state)}")
    await reply_html(update, "\n".join(lines))


async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show recent logs for a whitelisted service."""
    if not is_authorized(update):
        return
    service = resolve_service(context.args)
    if service is not None:
        output = await run_command(
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
    """Show recent errors for one or all whitelisted services."""
    if not is_authorized(update):
        return
    if context.args:
        service = resolve_service(context.args)
        if service is None:
            await reply(
                update,
                "Unknown service. Allowed: " + ", ".join(config.MANAGED_SERVICES),
            )
            return
        services = [service]
        header = f"🚨 Errors — <code>{html.escape(service)}</code>"
    else:
        services = config.MANAGED_SERVICES
        header = "🚨 Errors — all managed services"

    output = await run_command(
        [
            "journalctl",
            *_journalctl_unit_args(services),
            "-n",
            "300",
            "--no-pager",
            "-p",
            "err",
        ]
    )
    await reply_expandable(update, header, output)


async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Restart a whitelisted service and report its resulting state."""
    if not is_authorized(update):
        return
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
    await update.message.reply_text(
        f"♻️ Restarting <code>{svc}</code>...", parse_mode="HTML"
    )
    result = await run_command(["systemctl", "restart", service])
    if command_failed(result):
        await reply_expandable(update, f"⚠️ Restart failed — <code>{svc}</code>", result)
        return
    # Poll up to 10 s for a stable state
    state = "activating"
    for _ in range(10):
        await asyncio.sleep(1)
        state = strip_exit_prefix(
            await run_command(["systemctl", "is-active", service])
        ).strip()
        if state not in ("activating", "deactivating"):
            break
    icon = "✅" if state == "active" else "❌"
    await update.message.reply_text(
        f"{icon} <code>{svc}</code> is now: <b>{html.escape(state)}</b>",
        parse_mode="HTML",
    )


async def reboot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Request a system reboot after checking the owner chat."""
    if not is_authorized(update):
        return
    await reply(update, "🔁 Rebooting the whole system now...")
    result = await run_command(["systemctl", "reboot"], timeout=15)
    if command_failed(result):
        await reply_expandable(update, "⚠️ Reboot failed", result)
