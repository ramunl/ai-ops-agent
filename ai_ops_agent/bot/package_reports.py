"""Render package update reports for Telegram."""

from __future__ import annotations

import html

from telegram import Update

from ai_ops_agent.bot.transport import reply_html, send_rich_message
from ai_ops_agent.packages import AptUpdate, AptUpdateReport, group_updates

_MAX_UPDATE_ITEMS_PER_SECTION = 30


def _format_update_summary_html(report: AptUpdateReport) -> str:
    if report.total == 0:
        return "✅ <b>Update check</b>\n\nNo upgradable packages found."

    action = _recommend_update_action(report)

    return "\n".join(
        [
            "📦 <b>Update check</b>",
            "",
            f"<b>Total:</b> <code>{report.total}</code> package(s)",
            f"🚨 <b>Critical/security:</b> <code>{len(report.critical)}</code>",
            f"✅ <b>Stable:</b> <code>{len(report.stable)}</code>",
            "⚠️ <b>Not tested / not recommended:</b> "
            f"<code>{len(report.not_recommended)}</code>",
            "",
            f"<b>Recommendation:</b> {html.escape(action)}",
        ]
    )


def _format_apt_item_html(item: AptUpdate) -> str:
    old = f" ← {item.old_version}" if item.old_version else ""
    return (
        f"• <code>{html.escape(item.name)}</code> "
        f"<b>{html.escape(item.version)}</b>{html.escape(old)}\n"
        f"  <i>{html.escape(item.arch)} · {html.escape(item.channel)}</i>"
    )


def _format_update_items_html(items: list[AptUpdate]) -> str:
    if not items:
        return "None."
    shown = items[:_MAX_UPDATE_ITEMS_PER_SECTION]
    lines = [_format_apt_item_html(item) for item in shown]
    hidden = len(items) - len(shown)
    if hidden > 0:
        lines.append(f"… and {hidden} more package(s)")
    return "\n".join(lines)


def _format_update_fallback_html(report: AptUpdateReport) -> str:
    if report.total == 0:
        return _format_update_summary_html(report)

    return "\n\n".join(
        [
            _format_update_summary_html(report),
            "<blockquote expandable>"
            "<b>🚨 Critical / security</b>\n"
            "Security repository updates.\n\n"
            f"{_format_update_items_html(report.critical)}"
            "</blockquote>",
            "<blockquote expandable>"
            "<b>✅ Stable</b>\n"
            "Regular distribution updates.\n\n"
            f"{_format_update_items_html(report.stable)}"
            "</blockquote>",
            "<blockquote expandable>"
            "<b>⚠️ Not tested / not recommended</b>\n"
            "Proposed, backports, testing, unstable, experimental, "
            "or devel channels.\n\n"
            f"{_format_update_items_html(report.not_recommended)}"
            "</blockquote>",
        ]
    )


def _format_update_rich_html(report: AptUpdateReport) -> str:
    if report.total == 0:
        return "<h3>Update check</h3><p>No upgradable packages found.</p>"

    action = html.escape(_recommend_update_action(report))
    critical = _format_update_items_html(report.critical).replace("\n", "<br>")
    stable = _format_update_items_html(report.stable).replace("\n", "<br>")
    review = _format_update_items_html(report.not_recommended).replace("\n", "<br>")
    return "\n".join(
        [
            "<h3>📦 Update check</h3>",
            "<ul>",
            f"<li><b>Total:</b> <code>{report.total}</code> package(s)</li>",
            "<li>🚨 <b>Critical/security:</b> "
            f"<code>{len(report.critical)}</code></li>",
            f"<li>✅ <b>Stable:</b> <code>{len(report.stable)}</code></li>",
            "<li>⚠️ <b>Not tested / not recommended:</b> "
            f"<code>{len(report.not_recommended)}</code></li>",
            "</ul>",
            f"<p><b>Recommendation:</b> {action}</p>",
            "<details open>",
            "<summary>🚨 Critical / security</summary>",
            "<p>Security repository updates.</p>",
            f"<p>{critical}</p>",
            "</details>",
            "<details>",
            "<summary>✅ Stable</summary>",
            "<p>Regular distribution updates.</p>",
            f"<p>{stable}</p>",
            "</details>",
            "<details>",
            "<summary>⚠️ Not tested / not recommended</summary>",
            "<p>Proposed, backports, testing, unstable, experimental, "
            "or devel channels.</p>",
            f"<p>{review}</p>",
            "</details>",
        ]
    )


def _recommend_update_action(report: AptUpdateReport) -> str:
    if report.critical:
        return "Install security updates soon."
    if report.stable and not report.not_recommended:
        return "Regular update looks safe."
    if report.not_recommended:
        return "Review not recommended packages before upgrading."
    return "Review only."


def _format_update_report(raw: str) -> str:
    return _format_update_fallback_html(group_updates(raw))


async def reply_update_report(update: Update, raw: str) -> None:
    """Send a rich package report or fall back to Telegram HTML."""
    report = group_updates(raw)
    rich_sent = await send_rich_message(update, _format_update_rich_html(report))
    if not rich_sent:
        await reply_html(update, _format_update_fallback_html(report))
