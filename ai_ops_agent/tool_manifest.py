"""Load and validate the Coding installed-tool manifest, merged with the allowlist.

The manifest is an untrusted snapshot from the Coding registry. It may describe
which AI command-line tools exist, but it can never supply a version command, an
install command, or an install package: those come only from the repo-reviewed
allowlist in ``tools_list``. When the manifest is missing or invalid the merged
result falls back to the local inventory (the allowlist alone).

Standard library only; this module never imports bot or Telegram code.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

from ai_ops_agent.tools_list import AiTool

MANIFEST_PATH = Path(
    os.environ.get("AI_TOOLS_MANIFEST", "/var/lib/ai-coding-agent/tool-manifest.json")
)
SCHEMA_VERSION = 1

# The finalized Coding contract for a tool entry: identity only. Anything else,
# such as a command or package override, is rejected so it cannot be executed.
_ALLOWED_TOOL_KEYS = frozenset({"name", "package"})
_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_PACKAGE_RE = re.compile(r"@?[A-Za-z0-9][A-Za-z0-9._~-]*(?:/[A-Za-z0-9._~-]+)?\Z")


def _valid_package(value: str) -> bool:
    """Return True when the package is a single safe npm package name."""
    return (
        bool(value) and len(value) <= 200 and _PACKAGE_RE.fullmatch(value) is not None
    )


def validate_tool_entry(item: object) -> dict[str, str] | None:
    """Return a validated ``{name, package}`` entry, or None when it is not valid."""
    if not isinstance(item, dict):
        return None
    if set(item) - _ALLOWED_TOOL_KEYS:
        return None
    name = item.get("name")
    package = item.get("package")
    if not isinstance(name, str) or _TOOL_NAME_RE.fullmatch(name) is None:
        return None
    if not isinstance(package, str) or not _valid_package(package):
        return None
    return {"name": name, "package": package}


def validate_manifest(data: object) -> list[dict[str, str]] | None:
    """Validate the parsed manifest and return its tool entries, or None."""
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != SCHEMA_VERSION:
        return None
    tools = data.get("tools")
    if not isinstance(tools, list):
        return None

    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in tools:
        entry = validate_tool_entry(item)
        if entry is None:
            return None
        if entry["name"] in seen:
            return None
        seen.add(entry["name"])
        entries.append(entry)
    return entries


def load_manifest(path: str | Path | None = None) -> list[dict[str, str]] | None:
    """Return validated manifest tool entries, or None when missing or invalid."""
    manifest_path = Path(path) if path is not None else MANIFEST_PATH
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return validate_manifest(data)


def _allowlist_entry(name: str, spec: AiTool) -> dict:
    """Build an executable system entry from a reviewed allowlist spec."""
    return {
        "name": name,
        "aliases": spec.aliases,
        "version_cmd": spec.version_cmd,
        "package_manager": spec.package_manager,
        "package": spec.package,
        "model_env_keys": spec.model_env_keys,
        "env_file": spec.env_file,
        "executable": True,
    }


def _read_only_entry(entry: Mapping[str, str]) -> dict:
    """Build a read-only system entry from a manifest identity entry."""
    return {
        "name": entry["name"],
        "aliases": (),
        "package": entry["package"],
        "executable": False,
    }


def merged_tools(
    allowlist: Mapping[str, AiTool], entries: list[dict[str, str]] | None
) -> list[dict]:
    """Merge reviewed allowlist specs with validated manifest entries.

    Allowlisted tools keep their reviewed version and install specs, which win
    over any manifest data. Manifest tools without an allowlist entry appear
    read-only: they are listed so future Coding registry entries need no
    duplicate Ops tool list, but cannot be updated until reviewed here.
    """
    result = [_allowlist_entry(name, spec) for name, spec in allowlist.items()]
    if not entries:
        return result

    allowlisted = set(allowlist)
    for entry in entries:
        if entry["name"] in allowlisted:
            continue
        result.append(_read_only_entry(entry))
    return result


def load_merged_tools(
    allowlist: Mapping[str, AiTool] | None = None,
    path: str | Path | None = None,
) -> list[dict]:
    """Load and validate the manifest, then merge it with the allowlist.

    A missing or invalid manifest yields the local inventory (allowlist only).
    """
    if allowlist is None:
        from ai_ops_agent.tools_list import ALLOWLIST

        allowlist = ALLOWLIST
    return merged_tools(allowlist, load_manifest(path))
