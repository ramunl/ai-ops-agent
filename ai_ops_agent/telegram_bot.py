"""Telegram bot exposing server operations commands."""

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from . import config
from .shell import run

logger = logging.getLogger(__name__)


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


async def reply(update: Update, text: str) -> None:
    """Reply, truncating to Telegram's message size limit."""
    isTooLong = len(text) > config.TELEGRAM_MESSAGE_LIMIT
    if isTooLong:
        text = text[: config.TELEGRAM_MESSAGE_LIMIT] + "\n... (truncated)"
    await update.message.reply_text(text)


def resolve_service(args: list[str]) -> str | None:
    """Return a whitelisted service name from args, or None if invalid."""
    isDefault = not args
    if isDefault:
        return config.DEFAULT_SERVICE
    name = args[0]
    isAllowed = name in config.MANAGED_SERVICES
    if isAllowed:
        return name
    logger.info("Rejected non-whitelisted service name: %s", name)
    return None


# ---------------------------------------------------------------- commands


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        await reply(
            update,
            "🛠 Ops Agent ready!\n\n"
            "Server info:\n"
            "/health - CPU, RAM, disk summary\n"
            "/disk - disk usage details\n"
            "/memory - memory details\n"
            "/uptime - uptime and load\n\n"
            "Services:\n"
            "/services - status of managed services\n"
            "/logs [service] - recent logs\n"
            "/errors [service] - recent errors\n"
            "/restart <service> - restart a service\n\n"
            "Updates:\n"
            "/update - check available updates\n"
            "/upgrade - install updates\n",
        )


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        load = run(["cat", "/proc/loadavg"])
        memory = run(["free", "-h"])
        disk = run(["df", "-h", "/"])
        await reply(
            update,
            f"❤️ Health\n\nLoad average:\n{load}\n\n"
            f"Memory:\n{memory}\n\nDisk:\n{disk}",
        )


async def disk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        await reply(update, "💾 Disk usage:\n" + run(["df", "-h"]))


async def memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        await reply(update, "🧠 Memory:\n" + run(["free", "-h"]))


async def uptime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        await reply(update, "⏱ Uptime:\n" + run(["uptime"]))


async def services(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        lines = []
        for name in config.MANAGED_SERVICES:
            state = run(["systemctl", "is-active", name]).strip()
            icon = "✅" if state == "active" else "❌"
            lines.append(f"{icon} {name}: {state}")
        await reply(update, "🔧 Services:\n" + "\n".join(lines))


async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        service = resolve_service(context.args)
        isValid = service is not None
        if isValid:
            output = run(
                ["journalctl", "-u", service, "-n", "30", "--no-pager"]
            )
            await reply(update, f"📋 Logs for {service}:\n{output}")
        else:
            await reply(
                update,
                "Unknown service. Allowed: "
                + ", ".join(config.MANAGED_SERVICES),
            )


async def errors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        service = resolve_service(context.args)
        isValid = service is not None
        if isValid:
            output = run(
                [
                    "journalctl", "-u", service, "-n", "300",
                    "--no-pager", "-p", "err",
                ]
            )
            await reply(update, f"🚨 Errors for {service}:\n{output}")
        else:
            await reply(
                update,
                "Unknown service. Allowed: "
                + ", ".join(config.MANAGED_SERVICES),
            )


async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        isServiceGiven = bool(context.args)
        if isServiceGiven:
            service = resolve_service(context.args)
            isValid = service is not None
            if isValid:
                await reply(update, f"♻️ Restarting {service}...")
                run(["systemctl", "restart", service])
                await asyncio.sleep(3)
                state = run(["systemctl", "is-active", service]).strip()
                await reply(update, f"Service {service} is now: {state}")
            else:
                await reply(
                    update,
                    "Unknown service. Allowed: "
                    + ", ".join(config.MANAGED_SERVICES),
                )
        else:
            await reply(update, "Usage: /restart <service>")


async def update_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        await reply(update, "🔍 Checking for updates...")
        run(["apt-get", "update", "-q"], timeout=300)
        output = run(["apt", "list", "--upgradable"], timeout=120)
        await reply(update, "📦 Upgradable packages:\n" + output)


async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        await reply(update, "⬆️ Upgrading packages, this may take a while...")
        output = run(
            ["apt-get", "upgrade", "-y", "-q"],
            timeout=900,
        )
        await reply(update, "Done:\n" + output[-1500:])


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
    return app
