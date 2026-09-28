"""Resolve configured agents and tools and read their metadata."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from ai_ops_agent import config
from ai_ops_agent.settings import MODEL_ENV_KEYS

logger = logging.getLogger(__name__)


def _agent_match_keys(agent: dict) -> set[str]:
    name = agent["name"]
    keys = {
        name,
        name.removeprefix("ai-").removesuffix("-agent"),
    }
    service = agent.get("service")
    if service:
        keys.add(service)
    keys.update(agent.get("aliases", ()))
    return {key.lower() for key in keys}


def resolve_ai_agents(args: list[str] | None) -> list[dict] | None:
    """Return all configured AI agents, or one matched by name/alias."""
    if not args:
        return list(config.AI_AGENT_INSTALLS)

    requested = args[0].lower()
    for agent in config.AI_AGENT_INSTALLS:
        if requested in _agent_match_keys(agent):
            return [agent]
    return None


def _ai_system_match_keys(system: dict) -> set[str]:
    keys = {system["name"]}
    keys.update(system.get("aliases", ()))
    package = system.get("package")
    if package:
        keys.add(package)
        keys.add(package.rsplit("/", 1)[-1])
    return {key.lower() for key in keys}


def resolve_ai_systems(args: list[str] | None) -> list[dict] | None:
    """Return all configured AI systems, or one matched by name/alias/package."""
    if not args or args[0].lower() == "all":
        return list(config.AI_SYSTEM_INSTALLS)

    requested = args[0].lower()
    for system in config.AI_SYSTEM_INSTALLS:
        if requested in _ai_system_match_keys(system):
            return [system]
    return None


def agent_names() -> str:
    """List the configured agent names for command usage messages."""
    return ", ".join(agent["name"] for agent in config.AI_AGENT_INSTALLS)


def tool_names() -> str:
    """List the configured CLI tool names for command usage messages."""
    return ", ".join(system["name"] for system in config.AI_SYSTEM_INSTALLS)


def self_service() -> str:
    """Return the configured service name of this ops agent."""
    for agent in config.AI_AGENT_INSTALLS:
        if agent["name"] == "ai-ops-agent":
            return agent.get("service", "")
    return ""


def _read_text_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as error:
        logger.debug("Could not read metadata from %s (%s)", path, type(error).__name__)
        return None


def _parse_env_file(path: Path) -> dict[str, str]:
    raw = _read_text_file(path)
    if raw is None:
        return {}

    values = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            values[key] = value
    return values


def read_agent_version(path: Path) -> str:
    """Read a version from VERSION, Python metadata, or package.json."""
    version = _read_text_file(path / "VERSION")
    if version:
        return version

    pyproject = _read_text_file(path / "pyproject.toml")
    if pyproject:
        match = re.search(r'(?m)^version\s*=\s*["\']([^"\']+)["\']', pyproject)
        if match:
            return match.group(1)

    package_json = _read_text_file(path / "package.json")
    if package_json:
        try:
            version = json.loads(package_json).get("version")
        except json.JSONDecodeError:
            logger.warning("Invalid JSON in %s", path / "package.json")
            version = None
        if version:
            return str(version)

    return "unknown"


def read_agent_model(env_file: str | None) -> str:
    """Read configured model names from an agent environment file."""
    if not env_file:
        return "unknown"
    env = _parse_env_file(Path(env_file))
    models = [env[key] for key in MODEL_ENV_KEYS if env.get(key)]
    return ", ".join(models) if models else "unknown"


def read_tool_model(system: dict) -> str:
    """Combine configured tool model names without duplicates."""
    values = []
    keys = system.get("model_env_keys") or MODEL_ENV_KEYS
    env_file = system.get("env_file")
    if env_file:
        env = _parse_env_file(Path(env_file))
        values.extend(env[key] for key in keys if env.get(key))
    values.extend(os.environ[key] for key in keys if os.environ.get(key))
    seen = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return ", ".join(seen) if seen else "unknown"
