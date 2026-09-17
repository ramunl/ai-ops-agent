"""Verify owner authorization and service command boundaries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai_ops_agent import config, telegram_bot
from ai_ops_agent.bot import agents, packages, services


@pytest.mark.parametrize(
    "name",
    [
        "start",
        "health",
        "disk",
        "memory",
        "uptime",
        "services",
        "logs",
        "errors",
        "restart",
        "reboot",
        "update_cmd",
        "upgrade",
        "version",
        "my_agents",
        "ai_tools",
        "ai_update",
    ],
)
def test_unauthorized_commands_do_not_run_or_reply(name, chat, monkeypatch):
    chat.message.chat_id = config.AUTHORIZED_CHAT_ID + 1

    async def unexpected(*args, **kwargs):
        raise AssertionError("Unauthorized command performed an operation")

    for module in ("system", "services", "packages"):
        monkeypatch.setattr(f"ai_ops_agent.bot.{module}.run_command", unexpected)
    monkeypatch.setattr(agents, "inspect_agent", unexpected)
    monkeypatch.setattr(agents, "inspect_tool", unexpected)
    monkeypatch.setattr(agents, "update_agent", unexpected)
    monkeypatch.setattr(agents, "update_tool", unexpected)

    asyncio.run(getattr(telegram_bot, name)(chat, SimpleNamespace(args=["all"])))

    chat.message.reply_text.assert_not_awaited()


@pytest.mark.parametrize("name", ["logs", "errors", "restart"])
def test_unknown_services_never_reach_systemctl_or_journalctl(name, chat, monkeypatch):
    command = AsyncMock()
    monkeypatch.setattr(services, "run_command", command)

    asyncio.run(
        getattr(services, name)(chat, SimpleNamespace(args=["not-whitelisted"]))
    )

    command.assert_not_awaited()
    assert "Unknown service" in chat.message.reply_text.await_args.args[0]


def test_restart_requires_explicit_service(chat, monkeypatch):
    command = AsyncMock()
    monkeypatch.setattr(services, "run_command", command)
    asyncio.run(services.restart(chat, SimpleNamespace(args=[])))
    command.assert_not_awaited()
    assert "Usage: /restart" in chat.message.reply_text.await_args.args[0]


def test_restart_polls_service_until_active(chat, monkeypatch):
    command = AsyncMock(side_effect=["", "activating", "active"])
    monkeypatch.setattr(services, "run_command", command)
    monkeypatch.setattr(services.asyncio, "sleep", AsyncMock())
    service = config.MANAGED_SERVICES[0]

    asyncio.run(services.restart(chat, SimpleNamespace(args=[service])))

    assert command.await_args_list[0].args[0] == ["systemctl", "restart", service]
    assert command.await_count == 3
    assert "is now: <b>active</b>" in chat.message.reply_text.await_args.args[0]


def test_update_stops_when_refresh_fails(chat, monkeypatch):
    command = AsyncMock(return_value="[exit 100] repository unavailable")
    monkeypatch.setattr(packages, "run_command", command)

    asyncio.run(packages.update_cmd(chat, SimpleNamespace(args=[])))

    command.assert_awaited_once_with(["apt-get", "update", "-q"], timeout=300)
    assert "apt-get update failed" in chat.message.reply_text.await_args.args[0]


def test_upgrade_timeout_is_reported_as_failure(chat, monkeypatch):
    monkeypatch.setattr(
        packages, "run_command", AsyncMock(return_value="Command timed out after 900s")
    )
    asyncio.run(packages.upgrade(chat, SimpleNamespace(args=[])))
    assert "Upgrade failed" in chat.message.reply_text.await_args.args[0]


def test_self_update_reports_results_before_queuing_restart(chat, monkeypatch):
    events = []

    async def record_reply(text, **kwargs):
        events.append(text)

    async def record_restart(service, **kwargs):
        events.append((service, kwargs))

    chat.message.reply_text.side_effect = record_reply
    monkeypatch.setattr(
        agents, "resolve_ai_agents", lambda args: [{"name": "ai-ops-agent"}]
    )
    monkeypatch.setattr(
        agents,
        "update_agent",
        AsyncMock(
            return_value={
                "name": "ai-ops-agent",
                "status": "updated",
                "detail": "old → new",
                "restart": "deferred until after report",
            }
        ),
    )
    monkeypatch.setattr(agents, "self_service", lambda: "ai-ops-agent")
    monkeypatch.setattr(agents, "restart_agent_service", record_restart)

    asyncio.run(agents.ai_update(chat, SimpleNamespace(args=["ops"])))

    assert "old → new" in events[-2]
    assert events[-1] == ("ai-ops-agent", {"no_block": True})


@pytest.mark.parametrize(
    "name,args", [("ai_update", ["invalid"]), ("ai_tools", ["update", "invalid"])]
)
def test_unknown_agent_or_tool_does_not_start_updates(name, args, chat, monkeypatch):
    agent_update, tool_update = AsyncMock(), AsyncMock()
    monkeypatch.setattr(agents, "update_agent", agent_update)
    monkeypatch.setattr(agents, "update_tool", tool_update)
    asyncio.run(getattr(agents, name)(chat, SimpleNamespace(args=args)))
    agent_update.assert_not_awaited()
    tool_update.assert_not_awaited()
    assert "Unknown AI" in chat.message.reply_text.await_args.args[0]
