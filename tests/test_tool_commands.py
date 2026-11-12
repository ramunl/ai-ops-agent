"""Version detection works with systemd's PATH and native installations."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from ai_ops_agent import ai_tools, tool_commands, tool_updates
from ai_ops_agent.tool_manifest import load_merged_tools
from ai_ops_agent.tools_list import AI_TOOLS


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Never inspect the developer's own command installations."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    return tmp_path


def executable(path):
    """Create a harmless version executable at a fake user location."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho '2.1.317 (Claude Code)'\n")
    path.chmod(0o755)
    return path


def test_native_claude_detected_from_minimal_service_path(home, monkeypatch):
    binary = executable(home / ".local/share/claude/versions/2.1.317")
    launcher = home / ".local/bin/claude"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(binary)
    assert (
        tool_updates.installed_version(tool_updates.run_command, AI_TOOLS["claude"])
        == "2.1.317 (Claude Code)"
    )
    assert tool_updates.install_command(AI_TOOLS["claude"]) == [str(launcher), "update"]
    system = next(
        s for s in load_merged_tools(path=home / "absent") if s["name"] == "claude"
    )
    command = AsyncMock(side_effect=["2.1.317 (Claude Code)", "2.1.317"])
    monkeypatch.setattr(ai_tools, "run_command", command)
    monkeypatch.setattr(ai_tools, "read_tool_model", lambda _: "unknown")
    result = asyncio.run(ai_tools.inspect_tool(system))
    assert result["installed"] == "yes"
    assert command.await_args_list[0].args[0] == [str(launcher), "--version"]
    command.reset_mock(side_effect=True)
    command.side_effect = ["2.1.317", "updated", "2.1.318"]
    asyncio.run(ai_tools.update_tool(system))
    assert command.await_args_list[1].args[0] == [str(launcher), "update"]


def test_path_installation_keeps_precedence(home, monkeypatch):
    preferred = executable(home / "system/claude")
    executable(home / ".local/bin/claude")
    monkeypatch.setenv("PATH", str(preferred.parent))
    assert tool_commands.version_command(("claude", "--version")) == [
        "claude",
        "--version",
    ]
    assert tool_updates.install_command(AI_TOOLS["claude"])[0] == "npm"


def test_opencode_is_listed_even_when_absent_and_found_in_user_install(home):
    report = tool_updates.check(lambda args, timeout: (127, "missing"))
    tools = {item["name"]: item for item in report["tools"]}
    assert tools["opencode"]["version"] is None
    assert tools["opencode"]["update_available"] is False
    launcher = executable(home / ".opencode/bin/opencode")
    assert tool_commands.version_command(AI_TOOLS["opencode"].version_cmd) == [
        str(launcher),
        "--version",
    ]
    assert AI_TOOLS["opencode"].package == "@opencode/cli"


def test_missing_broken_or_nonexecutable_install_is_not_selected(home):
    launcher = home / ".local/bin/claude"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(home / "missing")
    assert tool_commands.version_command(("claude", "--version")) == [
        "claude",
        "--version",
    ]
    launcher.unlink()
    launcher.write_text("not executable")
    assert tool_commands.version_command(("claude", "--version")) == [
        "claude",
        "--version",
    ]


def test_manifest_discoveries_never_get_user_command_resolution(home):
    executable(home / ".local/bin/unreviewed")
    assert tool_commands.version_command(("unreviewed", "--version")) == [
        "unreviewed",
        "--version",
    ]
