"""ai-tools: the fixed check/update command the dashboard uses."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_ops_agent import config, tool_updates
from ai_ops_agent.tools_list import AI_TOOLS

REPO = Path(__file__).resolve().parent.parent


class FakeRun:
    """Answer by the first two words of the command; record every call."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = {
            "claude --version": (0, "2.1.0 (Claude Code)"),
            "codex --version": (127, "codex: not installed"),
            "npm view": (0, "2.2.0"),
            **(answers or {}),
        }

    def __call__(self, args, timeout):
        self.calls.append(args)
        return self.answers.get(" ".join(args[:2]), (0, ""))


@pytest.fixture
def cli(tmp_path):
    def call(argv, run, uid=0):
        with patch.object(tool_updates.os, "geteuid", return_value=uid):
            return tool_updates.main(
                argv, run, lock=tmp_path / "lock", log=tmp_path / "log"
            )

    return call


def test_the_bot_uses_the_same_tool_list():
    for system in config.AI_SYSTEM_INSTALLS:
        tool = AI_TOOLS[system["name"]]
        assert system["package"] == tool.package
        assert system["version_cmd"] == tool.version_cmd
    assert len(config.AI_SYSTEM_INSTALLS) == len(AI_TOOLS)


def test_check_reports_versions_and_what_can_be_updated():
    tools = {tool["name"]: tool for tool in tool_updates.check(FakeRun())["tools"]}
    assert tools["claude"]["version"] == "2.1.0 (Claude Code)"
    assert tools["claude"]["latest"] == "2.2.0"
    assert tools["claude"]["update_available"] is True
    assert tools["codex"]["version"] is None
    assert tools["codex"]["update_available"] is False


def test_current_or_unknown_latest_is_not_an_update():
    current = FakeRun({"npm view": (0, "2.1.0")})
    assert tool_updates.inspect(current, "claude")["update_available"] is False
    offline = FakeRun({"npm view": (1, "npm ERR! network")})
    result = tool_updates.inspect(offline, "claude")
    assert result["latest"] is None
    assert result["update_available"] is False


def test_update_installs_only_the_listed_package():
    run = FakeRun()
    result = tool_updates.update(run, "claude")
    assert result["ok"] is True
    assert ["npm", "install", "-g", "@anthropic-ai/claude-code@latest"] in run.calls
    assert result["before"] == result["after"] == "2.1.0 (Claude Code)"


def test_update_failure_keeps_the_last_lines():
    run = FakeRun({"npm install": (1, "a\nnpm ERR! EACCES")})
    result = tool_updates.update(run, "claude")
    assert result["ok"] is False
    assert result["error"].endswith("npm ERR! EACCES")


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["update"],
        ["update", "left-pad"],
        ["update", "--global"],
        ["update", "claude", "x"],
        ["check", "claude"],
        ["install", "claude"],
    ],
)
def test_unknown_or_crafted_arguments_never_reach_npm(cli, capsys, argv):
    run = FakeRun()
    assert cli(argv, run) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert run.calls == []
    assert tool_updates.update(run, "left-pad")["ok"] is False
    assert run.calls == []


def test_update_requires_root_but_check_does_not(cli, capsys):
    run = FakeRun()
    assert cli(["update", "claude"], run, uid=1000) == 1
    assert "root" in json.loads(capsys.readouterr().out)["error"]
    assert run.calls == []
    assert cli(["check"], run, uid=1000) == 0


def test_cli_updates_logs_and_refuses_a_second_run(cli, capsys, tmp_path):
    assert cli(["update", "claude"], FakeRun()) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "claude"
    assert "update claude:" in (tmp_path / "log").read_text()
    run = FakeRun()
    with tool_updates.exclusive(tmp_path / "lock") as held:
        assert held
        assert cli(["update", "claude"], run) == 1
    assert "already active" in json.loads(capsys.readouterr().out)["error"]
    assert run.calls == []


def test_wrapper_runs_without_the_bot_environment():
    done = subprocess.run(
        [str(REPO / "deploy" / "ai-tools"), "bogus"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "AI_OPS_AGENT_DIR": str(REPO)},
        check=False,
    )
    assert done.returncode == 2
    assert "usage" in json.loads(done.stdout)["error"]


@pytest.mark.parametrize(
    "installed, latest, expected",
    [
        ("codex-cli 11.2.3", "1.2.3", False),
        ("2.10.0 (Claude Code)", "2.9.0", False),
        ("codex-cli 1.2.3", "1.2.4", True),
    ],
)
def test_only_newer_releases_offer_an_update(installed, latest, expected):
    assert tool_updates.newer_version(installed, latest) is expected
