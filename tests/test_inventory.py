"""Verify configured target selection and metadata parsing."""

import json

import pytest

from ai_ops_agent import config, inventory


@pytest.mark.parametrize("alias", ["coding", "CODER", "ai-agent", "ai-coding-agent"])
def test_agent_aliases_resolve_to_the_configured_coding_agent(alias):
    assert inventory.resolve_ai_agents([alias])[0]["name"] == "ai-coding-agent"


def test_unknown_targets_are_rejected_and_default_selection_is_all():
    assert inventory.resolve_ai_agents(["unknown"]) is None
    assert inventory.resolve_ai_systems(["unknown"]) is None
    assert inventory.resolve_ai_agents([]) == config.AI_AGENT_INSTALLS
    assert inventory.resolve_ai_systems(["all"]) == config.AI_SYSTEM_INSTALLS
    assert inventory.resolve_ai_systems(["@openai/codex"])[0]["name"] == "codex"


def test_agent_version_prefers_version_file_over_project_metadata(tmp_path):
    (tmp_path / "VERSION").write_text("1.0\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "2.0"\n')
    (tmp_path / "package.json").write_text(json.dumps({"version": "3.0"}))
    assert inventory.read_agent_version(tmp_path) == "1.0"
    (tmp_path / "VERSION").unlink()
    assert inventory.read_agent_version(tmp_path) == "2.0"
    (tmp_path / "pyproject.toml").unlink()
    assert inventory.read_agent_version(tmp_path) == "3.0"


def test_invalid_or_missing_metadata_reports_unknown(tmp_path):
    (tmp_path / "package.json").write_text("invalid json")
    assert inventory.read_agent_version(tmp_path) == "unknown"
    assert inventory.read_agent_model(str(tmp_path / "missing.env")) == "unknown"


def test_model_reading_ignores_credentials_and_deduplicates_sources(
    tmp_path, monkeypatch
):
    env_file = tmp_path / "agent.env"
    env_file.write_text(
        "# configuration\nCODEX_MODEL='example-model'\nTELEGRAM_BOT_TOKEN=secret-value\n"
    )
    monkeypatch.setenv("CODEX_MODEL", "example-model")
    assert inventory.read_agent_model(str(env_file)) == "example-model"
    assert (
        inventory.read_tool_model(
            {"env_file": str(env_file), "model_env_keys": ["CODEX_MODEL"]}
        )
        == "example-model"
    )
