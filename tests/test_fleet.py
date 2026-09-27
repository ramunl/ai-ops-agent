"""Verify fleet updates, upstream comparisons, and service restart sequencing."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from ai_ops_agent import fleet


@pytest.mark.parametrize(
    "pull_output",
    ["[exit 1] conflict", "Error: unavailable", "Command timed out after 180s"],
)
def test_failed_pull_never_restarts_an_agent(tmp_path, monkeypatch, pull_output):
    command = AsyncMock(side_effect=["old", pull_output])
    restart = AsyncMock()
    monkeypatch.setattr(fleet, "run_command", command)
    monkeypatch.setattr(fleet, "restart_agent_service", restart)

    result = asyncio.run(
        fleet.update_agent(
            {"name": "ai-pm-agent", "path": str(tmp_path), "service": "pm"},
            defer_self_restart=True,
        )
    )

    assert result["status"] == "failed"
    assert result["restart"] == "not run"
    assert command.await_count == 2
    restart.assert_not_awaited()


@pytest.mark.parametrize(
    "name,before,after,expected_restart",
    [
        ("ai-pm-agent", "old", "old", "not needed"),
        ("ai-ops-agent", "old", "new", "deferred until after report"),
        ("ai-pm-agent", "old", "new", "restarted, state: active"),
    ],
)
def test_only_changed_nonself_agents_restart_immediately(
    tmp_path, monkeypatch, name, before, after, expected_restart
):
    monkeypatch.setattr(
        fleet, "run_command", AsyncMock(side_effect=[before, "updated", "", after])
    )
    restart = AsyncMock(return_value="restarted, state: active")
    monkeypatch.setattr(fleet, "restart_agent_service", restart)

    result = asyncio.run(
        fleet.update_agent(
            {"name": name, "path": str(tmp_path), "service": "agent-service"},
            defer_self_restart=True,
        )
    )

    assert result["restart"] == expected_restart
    assert restart.await_count == int(name != "ai-ops-agent" and before != after)


def test_missing_checkout_is_skipped_without_commands(tmp_path, monkeypatch):
    command = AsyncMock()
    monkeypatch.setattr(fleet, "run_command", command)
    result = asyncio.run(
        fleet.update_agent(
            {"name": "missing", "path": str(tmp_path / "missing")},
            defer_self_restart=True,
        )
    )
    assert result["status"] == "skipped"
    command.assert_not_awaited()


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


@pytest.mark.parametrize(
    "outputs,expected",
    [
        (["[exit 128] not a repository"], "cannot read current commit"),
        (
            ["old", "pulled", "", "Command timed out after 30s"],
            "cannot read updated commit",
        ),
    ],
)
def test_unreadable_revision_stops_update_without_restart(
    tmp_path, monkeypatch, outputs, expected
):
    monkeypatch.setattr(fleet, "run_command", AsyncMock(side_effect=outputs))
    restart = AsyncMock()
    monkeypatch.setattr(fleet, "restart_agent_service", restart)
    result = asyncio.run(
        fleet.update_agent(
            {"name": "pm", "path": str(tmp_path), "service": "pm"},
            defer_self_restart=True,
        )
    )
    assert result["status"] == "failed"
    assert expected in result["detail"]
    restart.assert_not_awaited()


@pytest.mark.parametrize(
    "failure", ["[exit 1] unavailable", "Error: denied", "Command timed out after 180s"]
)
def test_failed_submodule_sync_never_restarts_agent(tmp_path, monkeypatch, failure):
    command = AsyncMock(side_effect=["old", "pulled", failure])
    restart = AsyncMock()
    monkeypatch.setattr(fleet, "run_command", command)
    monkeypatch.setattr(fleet, "restart_agent_service", restart)

    result = asyncio.run(
        fleet.update_agent(
            {"name": "pm", "path": str(tmp_path), "service": "pm"},
            defer_self_restart=False,
        )
    )

    assert result["status"] == "failed"
    assert "submodule sync failed" in result["detail"]
    assert command.await_count == 3
    restart.assert_not_awaited()
