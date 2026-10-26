"""Handle whitelisted service operations and system reboot."""

from __future__ import annotations

import asyncio
import html
import logging

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ai_agent_common.buttons import keyboard, make_callback_data, parse_callback_data
from ai_ops_agent import config, service_control
from ai_ops_agent.bot.transport import (
    expandable_html,
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
    await reply_html(update, "\n".join(lines), reply_markup=service_keyboard())


def service_keyboard() -> InlineKeyboardMarkup:
    """A Start and a Stop button for each whitelisted service."""
    options = []
    for name in config.MANAGED_SERVICES:
        options.append((f"▶ Start {name}", make_callback_data("svc_start", name)))
        options.append((f"■ Stop {name}", make_callback_data("svc_ask_stop", name)))
    return keyboard(options, columns=2)


def _stop_question(service: str) -> str:
    """Ask before stopping: stopping drops clients and closes the port."""
    port = config.SERVICE_PORTS.get(service)
    if port is None:
        firewall = "No firewall port is configured for it."
    else:
        firewall = f"Its port {port.port}/{port.proto} will be closed."
    return (
        f"⚠️ Stop <code>{html.escape(service)}</code>?\n{firewall} "
        "Clients connected to it will be disconnected."
    )


def _result_text(service: str, result: dict) -> str:
    """HTML for a start or stop result, with the port state it verified."""
    port = result.get("port")
    if port is None:
        where = "no firewall port"
    else:
        port_open = result.get("port_open")
        state = {True: "open", False: "closed", None: "unknown"}[port_open]
        where = f"port {port} {state}"
    svc = html.escape(service)
    if result.get("ok"):
        unit = html.escape(result["unit"])
        return f"✅ <code>{svc}</code> is now <b>{unit}</b> — {where}"
    unit = html.escape(result.get("unit") or "unknown")
    header = f"⚠️ <code>{svc}</code> failed — unit {unit}, {where}"
    return expandable_html(header, result.get("error") or "No details.")


async def service_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a service, or ask first and then stop it, from the service buttons."""
    if not is_authorized(update):
        return
    query = update.callback_query
    await query.answer()
    action, service = parse_callback_data(query.data or "")
    if action == "svc_cancel":
        await query.edit_message_text("Cancelled. Nothing was changed.")
        return
    if service not in config.MANAGED_SERVICES:
        await query.edit_message_text("Unknown service. Nothing was changed.")
        return
    if action == "svc_ask_stop":
        confirm = keyboard(
            [
                ("Confirm stop", make_callback_data("svc_stop", service)),
                ("Cancel", make_callback_data("svc_cancel")),
            ]
        )
        await query.edit_message_text(
            _stop_question(service), parse_mode="HTML", reply_markup=confirm
        )
        return
    if action == "svc_start":
        verb, call = "Starting", service_control.start
    elif action == "svc_stop":
        verb, call = "Stopping", service_control.stop
    else:
        return
    await query.edit_message_text(
        f"{verb} <code>{html.escape(service)}</code>...", parse_mode="HTML"
    )
    result = await asyncio.to_thread(call, service)
    await query.edit_message_text(_result_text(service, result), parse_mode="HTML")


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
    """Queue a guarded system reboot after checking the owner chat."""
    if not is_authorized(update):
        return
    result = await asyncio.to_thread(service_control.reboot)
    if result.get("ok") and result.get("queued"):
        await reply(update, "🔁 Reboot queued — scheduled in 5 seconds.")
        return
    header = "⚠️ Reboot refused" if result.get("busy") else "⚠️ Reboot failed"
    await reply_expandable(
        update, header, result.get("error") or "Reboot was not queued."
    )
