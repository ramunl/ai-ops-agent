"""The validated Coding manifest merged with the reviewed tool allowlist."""

import json
from unittest.mock import patch

import pytest

from ai_ops_agent import tool_manifest, tool_updates
from ai_ops_agent.tools_list import ALLOWLIST, AiTool


def _write(tmp_path, data):
    path = tmp_path / "tool-manifest.json"
    path.write_text(json.dumps(data))
    return path


def _manifest(*tools):
    return {"schema_version": 1, "tools": list(tools)}


class FakeRun:
    """Record every command without running it."""

    def __init__(self):
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append(args)
        return 0, ""


def test_valid_manifest_loads_identity_entries(tmp_path):
    path = _write(
        tmp_path,
        _manifest(
            {"name": "codex", "package": "@openai/codex"},
            {"name": "claude", "package": "@anthropic-ai/claude-code"},
        ),
    )
    assert tool_manifest.load_manifest(path) == [
        {"name": "codex", "package": "@openai/codex"},
        {"name": "claude", "package": "@anthropic-ai/claude-code"},
    ]


def test_third_tool_discovery_is_read_only():
    entries = [{"name": "gemini", "package": "@google/gemini-cli"}]
    merged = tool_manifest.merged_tools(ALLOWLIST, entries)
    by_name = {entry["name"]: entry for entry in merged}
    assert "gemini" in by_name
    assert by_name["gemini"]["executable"] is False
    assert by_name["gemini"]["package"] == "@google/gemini-cli"
    assert "version_cmd" not in by_name["gemini"]
    assert "package_manager" not in by_name["gemini"]
    executable = {name for name, entry in by_name.items() if entry["executable"]}
    assert executable == set(ALLOWLIST)


@pytest.mark.parametrize(
    "tool",
    [
        {"name": "evil", "package": "@evil/tool", "version_cmd": ["touch", "/tmp/x"]},
        {"name": "evil", "package": "@evil/tool", "install": "rm -rf /"},
        {"name": "evil", "package": "@evil/tool", "install_command": "rm -rf /"},
        {"name": "evil", "package": "@evil/tool", "command": "rm -rf /"},
        {"name": "evil", "package": "@evil/tool", "args": ["--global"]},
        {"name": "evil", "package": "@evil/tool @latest"},
        {"name": "evil", "package": "@evil/tool; rm -rf /"},
        {"name": "evil", "package": ""},
        {"name": "evil", "package": ["@evil/tool"]},
        {"name": 123, "package": "@evil/tool"},
        {"name": "evil"},
        {"package": "@evil/tool"},
        {"name": "", "package": "@evil/tool"},
        {"name": "evil tool", "package": "@evil/tool"},
    ],
)
def test_rejected_malicious_metadata(tmp_path, tool):
    path = _write(tmp_path, _manifest(tool))
    assert tool_manifest.load_manifest(path) is None
    merged = tool_manifest.load_merged_tools(ALLOWLIST, path)
    assert [entry["name"] for entry in merged] == list(ALLOWLIST)
    assert all(entry["executable"] for entry in merged)


@pytest.mark.parametrize(
    "data",
    [
        {"tools": []},
        {"schema_version": 2, "tools": []},
        {"schema_version": "1", "tools": []},
        {"schema_version": 1},
        {"schema_version": 1, "tools": "codex"},
        ["not", "a", "dict"],
    ],
)
def test_rejected_manifest_shape(tmp_path, data):
    path = _write(tmp_path, data)
    assert tool_manifest.load_manifest(path) is None


def test_duplicate_tool_names_are_rejected(tmp_path):
    path = _write(
        tmp_path,
        _manifest(
            {"name": "gemini", "package": "@google/gemini-cli"},
            {"name": "gemini", "package": "@google/gemini-cli"},
        ),
    )
    assert tool_manifest.load_manifest(path) is None


def test_offline_fallback_when_manifest_missing(tmp_path):
    assert tool_manifest.load_manifest(tmp_path / "missing.json") is None
    merged = tool_manifest.load_merged_tools(ALLOWLIST, tmp_path / "missing.json")
    assert [entry["name"] for entry in merged] == list(ALLOWLIST)
    assert all(entry["executable"] for entry in merged)


def test_invalid_json_falls_back_to_local_inventory(tmp_path):
    path = tmp_path / "tool-manifest.json"
    path.write_text("{not valid json")
    assert tool_manifest.load_manifest(path) is None
    merged = tool_manifest.load_merged_tools(ALLOWLIST, path)
    assert [entry["name"] for entry in merged] == list(ALLOWLIST)


def test_allowlist_spec_wins_over_manifest_package():
    entries = [{"name": "codex", "package": "@attacker/evil"}]
    merged = tool_manifest.merged_tools(ALLOWLIST, entries)
    codex = next(entry for entry in merged if entry["name"] == "codex")
    assert codex["package"] == ALLOWLIST["codex"].package
    assert codex["version_cmd"] == ALLOWLIST["codex"].version_cmd
    assert codex["executable"] is True


def test_manifest_tool_stays_out_of_the_root_cli(tmp_path):
    entries = [{"name": "gemini", "package": "@google/gemini-cli"}]
    merged = tool_manifest.merged_tools(ALLOWLIST, entries)
    assert any(entry["name"] == "gemini" for entry in merged)
    assert "gemini" not in tool_updates.AI_TOOLS
    run = FakeRun()
    assert (
        tool_updates.main(
            ["update", "gemini"], run, lock=tmp_path / "lock", log=tmp_path / "log"
        )
        == 2
    )
    assert run.calls == []


def test_ai_tools_cli_preserves_root_check_without_a_manifest(tmp_path):
    run = FakeRun()
    with patch.object(tool_updates.os, "geteuid", return_value=1000):
        assert (
            tool_updates.main(
                ["update", "claude"], run, lock=tmp_path / "lock", log=tmp_path / "log"
            )
            == 1
        )
    assert run.calls == []


def test_allowlist_is_immutable():
    with pytest.raises(TypeError):
        ALLOWLIST["gemini"] = AiTool((), "")
