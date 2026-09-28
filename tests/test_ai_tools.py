"""Verify configured CLI updates without invoking package managers."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from ai_ops_agent import ai_tools


@pytest.mark.parametrize(
    "output", ["[exit 1] denied", "Error: missing npm", "Command timed out after 300s"]
)
def test_failed_tool_install_is_reported_without_version_recheck(monkeypatch, output):
    command = AsyncMock(side_effect=["tool 1.0", output])
    monkeypatch.setattr(ai_tools, "run_command", command)
    result = asyncio.run(
        ai_tools.update_tool(
            {
                "name": "tool",
                "package_manager": "npm",
                "package": "@owner/tool",
                "version_cmd": ["tool", "--version"],
            }
        )
    )
    assert result["status"] == "failed"
    assert result["detail"] == output
    assert command.await_count == 2


def test_unsupported_package_manager_never_runs_commands(monkeypatch):
    command = AsyncMock()
    monkeypatch.setattr(ai_tools, "run_command", command)
    result = asyncio.run(
        ai_tools.update_tool({"name": "tool", "package_manager": "unknown"})
    )
    assert result["status"] == "failed"
    command.assert_not_awaited()


def test_successful_cli_update_reports_before_and_after_versions(monkeypatch):
    monkeypatch.setattr(
        ai_tools,
        "run_command",
        AsyncMock(side_effect=["tool 1", "installed", "tool 2"]),
    )
    result = asyncio.run(
        ai_tools.update_tool(
            {
                "name": "tool",
                "package_manager": "npm",
                "package": "tool",
                "version_cmd": ["tool", "--version"],
            }
        )
    )
    assert result["status"] == "updated"
    assert result["detail"] == "tool 1 → tool 2"
