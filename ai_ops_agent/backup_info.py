"""Describe backup source results without opening files or inspecting secrets."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ai_ops_agent.backup_containers import is_container_path

SOURCE_LABELS = {
    "/etc/ai-coding-agent/ai-coding-agent.env": (
        "Coding agent credentials and configuration (service environment)"
    ),
    "/etc/ai-pm-agent.env": (
        "PM agent credentials and configuration (service environment)"
    ),
    "/etc/ai-ops-agent.env": (
        "Ops agent credentials and configuration (service environment)"
    ),
    "/etc/ai-dashboard/ai-dashboard.env": (
        "Dashboard credentials and configuration (service environment)"
    ),
    "/etc/ai-agent-projects.json": "Configuration: agent project registry",
    "/var/lib/ai-coding-agent/state.json": "State: coding agent",
    "/var/lib/ai-deploy/state.json": "State: deployment manager",
    "/opt/ai-todos-active.txt": "State: active todos",
    "/etc/caddy/Caddyfile": "Configuration: Caddy web server",
    "/etc/ai-backup/key": "Backup encryption key (explicit extra path)",
}


def source_label(source: str) -> str:
    """Label a known source by purpose; never infer unknown extra contents."""
    if is_container_path(source):
        return "Docker: archived directory plus container metadata (inspect.json)"
    return SOURCE_LABELS.get(source, "Extra path (file or directory; contents unknown)")


def format_backup_info(result: Mapping[str, Any]) -> str:
    """Format actual included/skipped sources as plain text, without reading them.

    These are source-level results, not a verified inventory of credentials or
    recursive directory contents. The caller must escape and bound delivery.
    """
    if not result.get("ok"):
        return f"⚠️ Backup failed: {result.get('error', 'unknown error')}"
    lines = ["Backup contents (source paths):"]
    for field, title in (("included", "Included"), ("skipped", "Skipped (missing)")):
        sources = result.get(field, [])
        lines.append(f"\n{title}: {len(sources)} sources")
        lines.extend(f"• {source_label(source)}: {source}" for source in sources)
        if not sources:
            lines.append("None")
    lines.extend(
        [
            "\nCredential labels describe source purposes only; values are not read "
            "or displayed. Individual credentials are not verified; this does not "
            "guarantee coverage of all API or SSH keys.",
            "\nEncryption: OpenSSL AES-256-CBC with PBKDF2 uses "
            "/etc/ai-backup/key. The encryption key is excluded by default. "
            "Extra paths can include it directly or through a directory; "
            "the source list above reflects this backup, not individual files "
            "within directories. Save the key separately in a safe place: "
            "you need it to decrypt, even if a copy is inside the archive.",
        ]
    )
    return "\n".join(lines)
