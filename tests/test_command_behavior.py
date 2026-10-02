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
        "core",
        "my_agents",
        "ai_tools",
        "ai_update",
        "deployments",
        "rollback",
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


def test_self_update_only_queues_manager_operation(chat, monkeypatch):
    command = AsyncMock(
        return_value={
            "name": "ai-ops-agent",
            "status": "queued",
            "detail": "operation: op1",
            "restart": "managed",
        }
    )
    monkeypatch.setattr(agents, "update_agent", command)
    asyncio.run(agents.ai_update(chat, SimpleNamespace(args=["ops"])))
    command.assert_awaited_once()
    assert "op1" in chat.message.reply_text.await_args.args[0]


@pytest.mark.parametrize("args", [[], ["all"], ["pm", "ops"], ["pm;reboot"]])
def test_rollback_requires_one_fixed_target(chat, monkeypatch, args):
    command = AsyncMock()
    monkeypatch.setattr(agents, "rollback_agent", command)
    asyncio.run(agents.rollback(chat, SimpleNamespace(args=args)))
    command.assert_not_awaited()


def test_rollback_reports_queued_operation(chat, monkeypatch):
    command = AsyncMock(return_value={"operation": "op2", "status": "queued"})
    monkeypatch.setattr(agents, "rollback_agent", command)
    asyncio.run(agents.rollback(chat, SimpleNamespace(args=["pm"])))
    command.assert_awaited_once_with("pm")
    assert "op2" in chat.message.reply_text.await_args.args[0]


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


def test_fleet_update_submits_one_batch(chat, monkeypatch):
    command = AsyncMock(
        return_value={
            "name": "all",
            "status": "queued",
            "detail": "batch1",
            "restart": "managed",
        }
    )
    monkeypatch.setattr(agents, "update_agent", command)
    asyncio.run(agents.ai_update(chat, SimpleNamespace(args=["all"])))
    command.assert_awaited_once_with({"name": "all"}, defer_self_restart=True)


def test_deployments_escape_revision_and_failure_text(chat, monkeypatch):
    monkeypatch.setattr(
        agents,
        "deployment_status",
        AsyncMock(
            return_value=[
                {
                    "name": "pm",
                    "status": "failed",
                    "current": {"commit": "<abc>"},
                    "previous": {"commit": "good", "version": "v1"},
                    "error": "<failure>",
                }
            ]
        ),
    )
    asyncio.run(agents.deployments(chat, SimpleNamespace(args=[])))
    text = chat.message.reply_text.await_args.args[0]
    assert "&lt;abc&gt;" in text
    assert "&lt;failure&gt;" in text
    assert "good" in text


def test_unavailable_deployment_manager_is_reported(chat, monkeypatch):
    monkeypatch.setattr(
        agents,
        "deployment_status",
        AsyncMock(side_effect=agents.DeploymentError("not installed")),
    )
    asyncio.run(agents.deployments(chat, SimpleNamespace(args=[])))
    assert "unavailable: not installed" in chat.message.reply_text.await_args.args[0]
