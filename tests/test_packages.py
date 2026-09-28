"""Verify update parsing, channel grouping, and Telegram report fallbacks."""

import asyncio
from unittest.mock import AsyncMock

from ai_ops_agent.bot import package_reports
from ai_ops_agent.packages import group_updates


def test_update_parser_ignores_notices_and_groups_release_channels():
    report = group_updates("""Listing...
WARNING: apt has no stable CLI interface
N: ignored notice
openssl/noble-security,noble-updates 2.0 amd64 [upgradable from: 1.0]
regular/noble-updates 3.0 all
preview/noble-proposed 4.0 amd64
invalid row
""")
    assert report.total == 3
    assert report.critical[0].name == "openssl"
    assert report.critical[0].old_version == "1.0"
    assert report.stable[0].name == "regular"
    assert report.not_recommended[0].name == "preview"


def test_rich_message_failure_falls_back_to_escaped_html(chat, monkeypatch):
    monkeypatch.setattr(
        package_reports, "send_rich_message", AsyncMock(return_value=False)
    )
    asyncio.run(package_reports.reply_update_report(chat, "pkg<&/stable 2.0 amd64"))
    text = chat.message.reply_text.await_args.args[0]
    assert "pkg&lt;&amp;" in text
    assert "<blockquote expandable>" in text
    assert chat.message.reply_text.await_args.kwargs["parse_mode"] == "HTML"


def test_successful_rich_report_does_not_send_duplicate_reply(chat, monkeypatch):
    rich = AsyncMock(return_value=True)
    monkeypatch.setattr(package_reports, "send_rich_message", rich)
    asyncio.run(package_reports.reply_update_report(chat, ""))
    assert "No upgradable packages" in rich.await_args.args[1]
    chat.message.reply_text.assert_not_awaited()
