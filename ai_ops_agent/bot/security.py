"""/security and the daily security report, from ai-audit.

ai-audit (ai_ops_agent/audit.py) reads the stats and findings as root; the
bot only formats them. The daily report goes to the owner at OPS_AUDIT_HOUR.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any

from telegram import Update
from telegram.ext import Application, ContextTypes

from ai_ops_agent import config
from ai_ops_agent.bot.backup import seconds_until
from ai_ops_agent.bot.transport import is_authorized, reply
from ai_ops_agent.command_output import run_command, strip_exit_prefix

logger = logging.getLogger(__name__)

AUDIT_COMMAND = "/usr/local/sbin/ai-audit"
AUDIT_HOUR = int(os.environ.get("OPS_AUDIT_HOUR", "9"))  # server local time
ICONS = {"high": "🔴", "medium": "🟠", "low": "🟡"}
MESSAGE_LIMIT = 4000


async def run_audit() -> dict:
    """Run ai-audit and return its report (or an error report)."""
    output = await run_command([AUDIT_COMMAND, "check"], timeout=90)
    try:
        report = json.loads(strip_exit_prefix(output))
    except ValueError:
        return {"ok": False, "error": output.strip()[:300] or "no output"}
    return report if isinstance(report, dict) else {"ok": False, "error": "bad output"}


def _stat_lines(stats: dict) -> list[str]:
    ssh = stats.get("ssh", {})
    lines = []
    if ssh.get("available"):
        lines.append(
            f"SSH, 24 h: {ssh['failed']} failed logins from {ssh['failed_ips']} "
            f"IPs · {ssh['accepted']} successful"
            + (
                f" ({ssh['password_logins']} by password)"
                if ssh["password_logins"]
                else ""
            )
        )
        for item in ssh.get("login_groups", []):
            key = f", key {item['key']}" if item["key"] else ""
            lines.append(
                f"  ✓ {item['user']} from {item['from']} ×{item['count']} "
                f"({item['method']}{key})"
            )
        if ssh["top_attackers"]:
            lines.append(
                "Top attackers: "
                + ", ".join(
                    f"{item['ip']} ×{item['count']}" for item in ssh["top_attackers"]
                )
            )
        if ssh["top_users"]:
            lines.append(
                "Tried users: "
                + ", ".join(
                    f"{item['user']} ×{item['count']}" for item in ssh["top_users"]
                )
            )
    jails = stats.get("fail2ban", {})
    if jails.get("installed"):
        lines.append(
            "fail2ban: "
            + (
                ", ".join(
                    f"{jail['name']} {jail['banned_now']} banned now, "
                    f"{jail['banned_total']} in total"
                    for jail in jails["jails"]
                )
                or "no jails"
            )
        )
    sessions = stats.get("sessions", [])
    lines.append(
        f"Logged in now: {len(sessions)}"
        + (
            " — "
            + ", ".join(f"{item['user']} from {item['from']}" for item in sessions)
            if sessions
            else ""
        )
    )
    ports = stats.get("ports", [])
    if ports:
        lines.append(
            "Public ports: "
            + ", ".join(
                f"{item['port']}/{item['proto']} {item['process']}" for item in ports
            )
        )
    lines.append(f"Firewall: {stats.get('firewall', '?')}")
    return lines


def render(report: dict) -> str:
    """The report as one chat message: stats first, then what to fix."""
    if not report.get("ok"):
        return f"⚠️ Security check failed: {report.get('error')}"
    findings = report.get("findings", [])
    head = "🛡 Security" + (
        f" · {len(findings)} to fix" if findings else " · nothing to fix"
    )
    lines = [head, "", *_stat_lines(report.get("stats", {}))]
    for item in findings:
        lines += [
            "",
            f"{ICONS.get(item['level'], '•')} {item['title']}",
            item["detail"],
            f"Fix: {item['fix']}",
        ]
    return "\n".join(lines)[:MESSAGE_LIMIT]


async def security_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the security stats and findings now."""
    if not is_authorized(update):
        return
    await reply(update, "🛡 Checking…")
    await reply(update, render(await run_audit()))


async def send_report(bot: Any, chat_id: int) -> None:
    """Send today's report to the owner."""
    await bot.send_message(chat_id, render(await run_audit()))


async def audit_forever(application: Application, hour: int = AUDIT_HOUR) -> None:
    """Every day at hour, send the security report to the owner."""
    while True:
        await asyncio.sleep(seconds_until(hour, datetime.now()))
        try:
            await send_report(application.bot, config.AUTHORIZED_CHAT_ID)
        except Exception as error:  # tomorrow's report must still run
            logger.warning("Daily security report failed: %s", error)
