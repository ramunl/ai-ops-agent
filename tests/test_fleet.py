"""Verify supervised deployment requests and upstream inspection."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from ai_ops_agent import fleet


def test_update_queues_fixed_target_without_checkout_mutation(monkeypatch):
    command = AsyncMock(return_value='{"status":"queued","operation":"op1"}')
    monkeypatch.setattr(fleet, "run_command", command)
    result = asyncio.run(fleet.update_agent({"name": "ai-ops-agent"}))
    assert result["status"] == "queued"
    command.assert_awaited_once_with(
        [fleet.DEPLOY_COMMAND, "submit", "deploy", "ops", "main"], timeout=30
    )


@pytest.mark.parametrize(
    "output", ["[exit 1] busy", "not JSON", "[]", '{"error":"busy"}']
)
def test_failed_manager_response_never_falls_back_to_git(monkeypatch, output):
    command = AsyncMock(return_value=output)
    monkeypatch.setattr(fleet, "run_command", command)
    result = asyncio.run(fleet.update_agent({"name": "ai-pm-agent"}))
    assert result["status"] == "failed"
    assert command.await_count == 1


def test_rollback_rejects_unknown_target_before_running(monkeypatch):
    command = AsyncMock()
    monkeypatch.setattr(fleet, "run_command", command)
    with pytest.raises(fleet.DeploymentError):
        asyncio.run(fleet.rollback_agent("pm;reboot"))
    command.assert_not_awaited()


def test_status_rejects_missing_targets(monkeypatch):
    monkeypatch.setattr(fleet, "run_command", AsyncMock(return_value="{}"))
    with pytest.raises(fleet.DeploymentError):
        asyncio.run(fleet.deployment_status())


@pytest.mark.parametrize(
    "counts,expected",
    [
        ("0 0", "up to date"),
        ("0 3", "behind"),
        ("2 0", "ahead"),
        ("2 3", "diverged"),
        ("bad data", "bad compare output"),
    ],
)
def test_upstream_status_describes_ahead_and_behind_counts(
    tmp_path, monkeypatch, counts, expected
):
    monkeypatch.setattr(
        fleet, "run_command", AsyncMock(side_effect=["origin/main", "", counts])
    )
    assert expected in asyncio.run(fleet.agent_latest_status(tmp_path))


def test_nonblocking_restart_does_not_poll_service(monkeypatch):
    command = AsyncMock(return_value="")
    monkeypatch.setattr(fleet, "run_command", command)
    assert (
        asyncio.run(fleet.restart_agent_service("ops", no_block=True))
        == "restart queued"
    )
    command.assert_awaited_once_with(
        ["systemctl", "restart", "--no-block", "ops"], timeout=30
    )
