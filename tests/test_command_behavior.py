"""Verify owner authorization and service command boundaries."""

import asyncio
import fcntl
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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


@pytest.fixture
def reboot_environment(tmp_path, monkeypatch):
    """Isolate shared checks and intercept both old and shared subprocess paths."""
    from ai_ops_agent import service_control

    locks = {
        tmp_path / path.name: label
        for path, label in service_control.BUSY_LOCKS.items()
    }
    monkeypatch.setattr(service_control, "BUSY_LOCKS", locks)
    state = tmp_path / "state.json"
    monkeypatch.setattr(service_control, "DEPLOYMENT_STATE", state)
    command = Mock(
        return_value=SimpleNamespace(returncode=3, stdout="inactive", stderr="")
    )
    monkeypatch.setattr(service_control.subprocess, "run", command)
    legacy = AsyncMock(return_value="")
    monkeypatch.setattr(services, "run_command", legacy)
    return SimpleNamespace(locks=locks, state=state, command=command, legacy=legacy)


@pytest.mark.parametrize(
    "label",
    ["a deployment", "a package upgrade", "a disk cleanup", "an AI tool update"],
)
def test_reboot_refuses_active_lock(label, reboot_environment, chat):
    env = reboot_environment
    path = next(path for path, what in env.locks.items() if what == label)
    with path.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        asyncio.run(services.reboot(chat, SimpleNamespace(args=[])))
    text = chat.message.reply_text.await_args.args[0]
    assert "Reboot refused" in text
    assert f"{label} is running" in text
    env.command.assert_not_called()
    env.legacy.assert_not_awaited()
    assert chat.message.reply_text.await_count == 1


@pytest.mark.parametrize("activity", ["deployment", "detached upgrade"])
def test_reboot_refuses_activity_without_lock(activity, reboot_environment, chat):
    env = reboot_environment
    if activity == "deployment":
        env.state.write_text('{"active": "job1"}')
    else:
        env.command.return_value = SimpleNamespace(
            returncode=0, stdout="active", stderr=""
        )
    asyncio.run(services.reboot(chat, SimpleNamespace(args=[])))
    assert "Reboot refused" in chat.message.reply_text.await_args.args[0]
    assert all(call.args[0][0] == "systemctl" for call in env.command.call_args_list)
    env.legacy.assert_not_awaited()


@pytest.mark.parametrize("outcome", ["queued", "failure", "timeout"])
def test_reboot_reports_scheduling_result(outcome, reboot_environment, chat):
    env = reboot_environment
    main_thread = threading.get_ident()

    def run(args, **kwargs):
        assert threading.get_ident() != main_thread
        chat.message.reply_text.assert_not_awaited()
        if args[0] == "systemctl":
            return SimpleNamespace(returncode=3, stdout="inactive", stderr="")
        assert args == [
            "systemd-run",
            "--quiet",
            "--collect",
            "--unit=ai-server-reboot",
            "--on-active=5s",
            "/usr/bin/systemctl",
            "reboot",
        ]
        assert kwargs["timeout"] == 30
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(args, 30)
        return SimpleNamespace(
            returncode=0 if outcome == "queued" else 1,
            stdout="",
            stderr="<scheduling> & failed",
        )

    env.command.side_effect = run
    asyncio.run(services.reboot(chat, SimpleNamespace(args=[])))
    text = chat.message.reply_text.await_args.args[0]
    assert chat.message.reply_text.await_count == 1
    assert env.command.call_count == 2
    env.legacy.assert_not_awaited()
    if outcome == "queued":
        assert "Reboot queued" in text
        assert "5 seconds" in text
    else:
        assert "Reboot failed" in text
        assert "Reboot queued" not in text
        assert (
            "timed out after 30 s"
            if outcome == "timeout"
            else "&lt;scheduling&gt; &amp; failed"
        ) in text


@pytest.mark.parametrize("result", [{"ok": True}, {"ok": False, "error": "x" * 10000}])
def test_reboot_bounds_errors_and_requires_queued(result, chat, monkeypatch):
    from ai_ops_agent import service_control

    monkeypatch.setattr(service_control, "reboot", Mock(return_value=result))
    asyncio.run(services.reboot(chat, SimpleNamespace(args=[])))
    text = chat.message.reply_text.await_args.args[0]
    assert "Reboot failed" in text
    assert len(text) <= config.TELEGRAM_MESSAGE_LIMIT


def test_unauthorized_reboot_never_calls_shared_entrypoint(chat, monkeypatch):
    from ai_ops_agent import service_control

    chat.message.chat_id = config.AUTHORIZED_CHAT_ID + 1
    reboot = Mock(side_effect=AssertionError("Unauthorized shared reboot"))
    monkeypatch.setattr(service_control, "reboot", reboot)
    asyncio.run(services.reboot(chat, SimpleNamespace(args=[])))
    reboot.assert_not_called()
    chat.message.reply_text.assert_not_awaited()
