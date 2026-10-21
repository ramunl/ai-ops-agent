"""Verify owner authorization and service command boundaries."""

import asyncio
import fcntl
import os
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ai_ops_agent import config, package_updates, telegram_bot
from ai_ops_agent.bot import agents, packages, services
from ai_ops_agent.command_output import command_failed


@pytest.mark.parametrize("code", [0, 1, 2, -15])
def test_silent_command_failure_depends_on_exit_code(monkeypatch, code):
    from ai_ops_agent import shell

    proc = Mock(returncode=code)
    proc.communicate.return_value = ("", "")
    monkeypatch.setattr(shell.subprocess, "Popen", Mock(return_value=proc))

    result = shell.run(["command"])

    assert result == f"(exit {code})"
    assert command_failed(result) is (code != 0)


@pytest.mark.parametrize("result", ["(exit 1)", "Command timed out after 7s"])
def test_restart_failure_stops_before_polling(result, chat, monkeypatch):
    command = AsyncMock(return_value=result)
    monkeypatch.setattr(services, "run_command", command)
    service = config.MANAGED_SERVICES[0]

    asyncio.run(services.restart(chat, SimpleNamespace(args=[service])))

    command.assert_awaited_once_with(["systemctl", "restart", service])
    assert "Restart failed" in chat.message.reply_text.await_args.args[0]


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


@pytest.fixture
def upgrade_environment(tmp_path, monkeypatch):
    """Fake the systemd start and the package lock; apt-get must never run here."""
    lock = tmp_path / "ai-packages.lock"
    monkeypatch.setattr(package_updates, "LOCK", lock)
    command = Mock(return_value=(0, ""))
    monkeypatch.setattr(package_updates, "run_command", command)
    legacy = AsyncMock(side_effect=AssertionError("apt-get ran in the bot process"))
    monkeypatch.setattr(packages, "run_command", legacy)
    return SimpleNamespace(lock=lock, command=command, legacy=legacy)


def test_upgrade_starts_detached_unit_without_direct_apt(upgrade_environment, chat):
    asyncio.run(packages.upgrade(chat, SimpleNamespace(args=[])))
    upgrade_environment.legacy.assert_not_awaited()
    upgrade_environment.command.assert_called_once()
    args, timeout = upgrade_environment.command.call_args.args
    assert args == package_updates.start_command(upgrade_environment.lock)
    assert args[0] == "systemd-run"
    assert "--setenv=DEBIAN_FRONTEND=noninteractive" in args
    assert "--setenv=LC_ALL=C" in args
    assert timeout == package_updates.START_TIMEOUT
    text = chat.message.reply_text.await_args.args[0]
    assert "ai-packages-upgrade.service" in text
    assert chat.message.reply_text.await_count == 1


def test_upgrade_start_failure_is_never_reported_as_started(upgrade_environment, chat):
    upgrade_environment.command.return_value = (
        124,
        "systemd-run: timed out after 30 s",
    )
    asyncio.run(packages.upgrade(chat, SimpleNamespace(args=[])))
    text = chat.message.reply_text.await_args.args[0]
    assert "Upgrade not started" in text
    assert "timed out after 30 s" in text
    assert "Upgrade started" not in text


def test_second_upgrade_is_rejected_while_lock_held(upgrade_environment, chat):
    with upgrade_environment.lock.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        asyncio.run(packages.upgrade(chat, SimpleNamespace(args=[])))
    text = chat.message.reply_text.await_args.args[0]
    assert "Upgrade not started" in text
    assert "a package run is already active" in text
    upgrade_environment.command.assert_not_called()
    upgrade_environment.legacy.assert_not_awaited()


def test_upgrade_can_start_again_once_lock_is_released(upgrade_environment, chat):
    with upgrade_environment.lock.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
    # The lock is released by the kernel when its holder exits, so a crashed
    # run cannot leave upgrades blocked.
    asyncio.run(packages.upgrade(chat, SimpleNamespace(args=[])))
    upgrade_environment.command.assert_called_once()
    assert "ai-packages-upgrade.service" in chat.message.reply_text.await_args.args[0]


def test_ops_restart_mid_upgrade_leaves_detached_unit_running(
    upgrade_environment, chat
):
    entered = threading.Event()
    release = threading.Event()

    def run(args, timeout):
        entered.set()
        release.wait(5)
        return 0, ""

    upgrade_environment.command.side_effect = run

    async def restart_ops():
        task = asyncio.create_task(packages.upgrade(chat, SimpleNamespace(args=[])))
        for _ in range(500):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        # What an Ops restart does to the bot's handler tasks.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()

    asyncio.run(restart_ops())
    assert entered.is_set()
    calls = [call.args[0] for call in upgrade_environment.command.call_args_list]
    assert calls == [package_updates.start_command(upgrade_environment.lock)]
    assert all(args[0] == "systemd-run" for args in calls)
    chat.message.reply_text.assert_not_awaited()


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


@pytest.mark.parametrize("parent_exits", [False, True])
def test_shell_timeout_kills_child_retaining_pipes(tmp_path, monkeypatch, parent_exits):
    from ai_ops_agent import shell

    marker = tmp_path / "delayed-write"
    child_pid = tmp_path / "child-pid"
    processes = []
    popen = subprocess.Popen

    def launch(*args, **kwargs):
        proc = popen(*args, **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(shell.subprocess, "Popen", launch)
    script = """
import os
import pathlib
import sys
import time

pid = os.fork()
if pid == 0:
    time.sleep(3)
    pathlib.Path(sys.argv[1]).write_text('unexpected write')
    os._exit(0)
pathlib.Path(sys.argv[2]).write_text(str(pid))
if sys.argv[3] == 'True':
    os._exit(0)
time.sleep(30)
"""
    results = []
    worker = threading.Thread(
        target=lambda: results.append(
            shell.run(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(marker),
                    str(child_pid),
                    str(parent_exits),
                ],
                timeout=1,
            )
        ),
        daemon=True,
    )
    started = time.monotonic()
    try:
        worker.start()
        worker.join(timeout=2.5)
        assert not worker.is_alive(), "Timeout cleanup blocked on inherited pipes"
        assert results == ["Command timed out after 1s"]
        assert child_pid.exists(), "Child was not launched before the timeout"
        time.sleep(max(0, started + 3.5 - time.monotonic()))
        assert not marker.exists()
    finally:
        # Kill individually as well: the old implementation has no isolated group.
        if child_pid.exists():
            try:
                os.kill(int(child_pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        for proc in processes:
            proc.kill()
            proc.wait(timeout=2)
        worker.join(timeout=2)


@pytest.mark.parametrize(
    "failure", [None, "missing_group", "kill", "close", "wait", "wait_timeout"]
)
def test_shell_timeout_cleanup_is_bounded_and_preserves_failure(
    monkeypatch, caplog, failure
):
    from ai_ops_agent import shell

    proc = Mock(pid=12345, returncode=0)
    proc.communicate.side_effect = subprocess.TimeoutExpired(["command"], 7)
    killpg = Mock()
    if failure == "missing_group":
        killpg.side_effect = ProcessLookupError("already gone")
    elif failure == "kill":
        killpg.side_effect = PermissionError("denied")
    elif failure == "close":
        proc.stdout.close.side_effect = OSError("close failed")
    elif failure == "wait":
        proc.wait.side_effect = OSError("wait failed")
    elif failure == "wait_timeout":
        proc.wait.side_effect = subprocess.TimeoutExpired(["command"], 1)
    launch = Mock(return_value=proc)
    monkeypatch.setattr(shell.subprocess, "Popen", launch)
    monkeypatch.setattr(shell.os, "killpg", killpg)

    result = shell.run(["command"], timeout=7)
    assert result == "Command timed out after 7s"
    assert command_failed(result)

    assert launch.call_args.kwargs["start_new_session"] is True
    killpg.assert_called_once_with(proc.pid, signal.SIGKILL)
    proc.communicate.assert_called_once_with(timeout=7)
    proc.stdout.close.assert_called_once_with()
    proc.stderr.close.assert_called_once_with()
    proc.wait.assert_called_once_with(timeout=shell.TIMEOUT_CLEANUP_TIMEOUT)
    assert 0 < shell.TIMEOUT_CLEANUP_TIMEOUT <= 1
    if failure in {"kill", "close", "wait", "wait_timeout"}:
        assert "Failed to" in caplog.text
    else:
        assert "Failed to" not in caplog.text


@pytest.mark.parametrize(
    "code,stdout,stderr,expected",
    [
        (0, " out ", "err\n", "out err"),
        (2, "out", "err\n", "[exit 2] outerr"),
        (0, "", "", "(exit 0)"),
        (2, "", "", "(exit 2)"),
    ],
)
def test_shell_preserves_output_and_environment(
    monkeypatch, code, stdout, stderr, expected
):
    from ai_ops_agent import shell

    monkeypatch.setenv("SHELL_TEST_INHERITED", "inherited")
    monkeypatch.setenv("SHELL_TEST_OVERRIDE", "original")
    script = (
        "import os, sys; "
        "assert os.environ['SHELL_TEST_INHERITED'] == 'inherited'; "
        "assert os.environ['SHELL_TEST_OVERRIDE'] == 'override'; "
        "sys.stdout.write(sys.argv[1]); sys.stderr.write(sys.argv[2]); "
        "sys.exit(int(sys.argv[3]))"
    )
    assert (
        shell.run(
            [sys.executable, "-c", script, stdout, stderr, str(code)],
            env={"SHELL_TEST_OVERRIDE": "override"},
        )
        == expected
    )
    assert os.environ["SHELL_TEST_OVERRIDE"] == "original"
