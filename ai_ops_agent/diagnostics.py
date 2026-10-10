"""Bounded, read-only server evidence for coding runs; never execute repairs."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from ai_ops_agent.service_control import Runner, run_command
from ai_ops_agent.services_list import MANAGED_SERVICES

CHECKS = Path("/var/lib/ai-dashboard/ops-checks.json")
DEPLOYMENTS = Path("/var/lib/ai-deploy/state.json")
ENV_FILES = (
    "/etc/ai-coding-agent/ai-coding-agent.env",
    "/etc/ai-pm-agent.env",
    "/etc/ai-ops-agent/ai-ops-agent.env",
    "/etc/ai-ops-agent.env",
    "/etc/ai-dashboard/ai-dashboard.env",
)


def secret_values() -> list[str]:
    """Read configured secret values only for removal, never for publication."""
    values = []
    for name in ENV_FILES:
        try:
            lines = Path(name).read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            key, sep, value = line.partition("=")
            if sep and re.search(r"TOKEN|SECRET|PASSWORD|API_KEY", key, re.I):
                value = value.strip().strip("\"'")
                if value:
                    values.append(value)
    return values


def sanitize(text: str, secrets: list[str]) -> str:
    """Redact before bounding output; preserve useful operational errors."""
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    text = re.sub(
        r"\b(?:\d{5,}:[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{16,}|"
        r"gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,})",
        "[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)(bearer\s+|(?:token|password|secret|api[_-]?key)\s*[=:]\s*)[^\s,;]+",
        r"\1[redacted]",
        text,
    )
    text = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", text)
    return "\n".join(line[:600] for line in text.splitlines()[-40:])[:12000]


def redact_tree(value: object, secrets: list[str]) -> object:
    """Remove credentials from nested report fields without exposing raw values."""
    if isinstance(value, str):
        return sanitize(value, secrets)
    if isinstance(value, dict):
        return {key: redact_tree(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_tree(item, secrets) for item in value]
    return value


def read_state(path: Path) -> dict:
    """Read bounded JSON or explicitly mark unavailable evidence."""
    try:
        if path.stat().st_size > 2_000_000:
            return {"unavailable": "state file exceeds size limit"}
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {"unavailable": "invalid state"}
    except (OSError, ValueError):
        return {"unavailable": "state file missing or unreadable"}


def collect(target: str, run: Runner = run_command) -> dict:
    """Collect fixed service, journal and cached checks for one managed target."""
    if target not in MANAGED_SERVICES:
        raise ValueError("Unknown managed service")
    secrets = secret_values()
    service_code, service = run(
        ["systemctl", "show", target, "--property=ActiveState,SubState,Result"], 10
    )
    journal_code, journal = run(
        [
            "journalctl",
            "-u",
            target,
            "--since",
            "24 hours ago",
            "-n",
            "40",
            "--no-pager",
            "-q",
            "-o",
            "short-iso",
        ],
        10,
    )
    checks = read_state(CHECKS)
    state = read_state(DEPLOYMENTS)
    deployment = state.get("targets", {}).get(target, {})
    evidence = {
        "service": {"exit_code": service_code, "output": service},
        "journal": {"exit_code": journal_code, "output": journal},
        "deployment": {
            key: deployment.get(key) for key in ("status", "error", "current")
        },
        "packages": checks.get("packages", {}).get("report", {}),
        "ai_tools": checks.get("ai_tools", {}).get("report", {}),
        "state_warning": checks.get("unavailable") or state.get("unavailable"),
    }
    # Redact text before encoding so multiline journal entries retain their bounds.
    return {
        "target": target,
        "collected_at": time.time(),
        "read_only": True,
        "evidence": {
            key: json.dumps(redact_tree(value, secrets))[:6000]
            for key, value in evidence.items()
        },
    }


def main(argv: list[str] | None = None) -> int:
    """Print one sanitized report; accept only a fixed managed service name."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in MANAGED_SERVICES:
        print(
            json.dumps(
                {"ok": False, "error": "usage: ai-diagnostics <managed-service>"}
            )
        )
        return 2
    print(json.dumps({"ok": True, **collect(args[0])}))
    return 0
