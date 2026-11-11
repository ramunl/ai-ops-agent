"""Sanitized, bounded client for the installed PM JSON read/add bridge."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable

from ai_ops_agent.command_output import command_failed
from ai_ops_agent.shell import run_json

from .failure_events import _validate

BRIDGE = "/usr/local/sbin/ai-pm-todos"
MAX_REQUEST = 32768
MAX_TEXT = 10000
MAX_OUTPUT = 262144
TIMEOUT = 30
CONFLICT_RETRIES = 2


class BridgeError(Exception):
    """A fixed-message bridge failure, containing no remote diagnostic data."""


def task_id(identity: str) -> str:
    """Derive a stable PM identity in a deployment-specific namespace."""
    if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity):
        raise ValueError("Invalid failure identity")
    return hashlib.sha256(f"ai-deploy:{identity}".encode()).hexdigest()[:32]


def build_ticket(event: dict) -> dict:
    """Build one-line bug text from validated metadata and diagnostic vocabulary.

    Configuration metadata is trusted; paths and arbitrary refs are omitted.
    No raw diagnostics, environment, arguments or exception source are accepted.
    """
    _validate(event)
    project = event["target"]["name"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", project):
        raise ValueError("Invalid PM project")
    stage = event["stage"]
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", stage):
        raise ValueError("Invalid failure stage")
    parts = [
        f"Deployment failure: {project}; stage={stage}",
        f"failure={event['identity']}",
        f"at={event['failed_at']}",
        f"rollback={event['rollback_outcome']}",
    ]
    for key in ("requested_commit", "previous_revision"):
        value = event[key]
        if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40,64}", value):
            parts.append(f"{key}={value}")
    code = event["exit_code"]
    if code is not None and -255 <= code <= 65535:
        parts.append(f"exit={code}")
    parts.extend(event["diagnostics"])
    ticket = {
        "project": project,
        "id": task_id(event["identity"]),
        "text": "; ".join(parts),
    }
    validate_ticket(ticket)
    return ticket


def validate_ticket(ticket: dict) -> None:
    """Reject payloads that violate the installed PM text and identity contract."""
    if (
        not isinstance(ticket, dict)
        or set(ticket) != {"project", "id", "text"}
        or not isinstance(ticket["project"], str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", ticket["project"])
        or not isinstance(ticket["id"], str)
        or not re.fullmatch(r"[a-f0-9]{32}", ticket["id"])
        or not isinstance(ticket["text"], str)
        or not 0 < len(ticket["text"]) <= MAX_TEXT
        or ticket["text"] != ticket["text"].strip()
        or not ticket["text"].isprintable()
        or "<!-- pm-task:" in ticket["text"]
    ):
        raise ValueError("Invalid PM ticket")


class PMBridge:
    """Read current revisions and retry conflicts a bounded number of times."""

    def __init__(self, run: Callable = run_json) -> None:
        """Accept an injectable bounded command runner for offline tests."""
        self.run = run

    def _request(self, payload: dict) -> dict:
        """Validate JSON and discard all raw bridge errors and warnings."""
        encoded = json.dumps(payload, ensure_ascii=True)
        if len(encoded) > MAX_REQUEST:
            raise BridgeError("PM request too large")
        try:
            raw = self.run([BRIDGE], encoded, TIMEOUT, MAX_OUTPUT)
            if not isinstance(raw, str) or len(raw.encode()) > MAX_OUTPUT:
                raise ValueError
            if command_failed(raw):
                raise ValueError
            result = json.loads(raw)
            if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                raise ValueError
        except (OSError, ValueError, RecursionError, subprocess.TimeoutExpired):
            raise BridgeError("PM bridge unavailable or invalid") from None
        if result["ok"] is False:
            if result.get("conflict") is True:
                return {"conflict": True}
            raise BridgeError("PM bridge rejected request")
        workspace = result.get("workspace")
        if (
            not isinstance(workspace, dict)
            or workspace.get("project") != payload["project"]
            or not isinstance(workspace.get("revision"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", workspace["revision"])
            or not isinstance(workspace.get("items"), list)
            or not all(isinstance(item, dict) for item in workspace["items"])
        ):
            raise BridgeError("Invalid PM workspace")
        return result

    def add(self, ticket: dict) -> str:
        """Confirm the saved ID even when publication to GitHub failed."""
        validate_ticket(ticket)
        for _ in range(CONFLICT_RETRIES + 1):
            read = self._request({"action": "read", "project": ticket["project"]})
            if read.get("conflict"):
                continue
            result = self._request(
                {
                    **ticket,
                    "action": "add",
                    "type": "bug",
                    "status": "open",
                    "priority": "high",
                    "revision": read["workspace"]["revision"],
                }
            )
            if result.get("conflict"):
                continue
            if type(result.get("published")) is not bool:
                raise BridgeError("Invalid PM confirmation")
            matches = [
                item
                for item in result["workspace"]["items"]
                if item.get("id") == ticket["id"]
            ]
            if len(matches) != 1 or matches[0].get("text") != ticket["text"]:
                raise BridgeError("Invalid PM confirmation")
            return ticket["id"]
        raise BridgeError("PM revision conflict")
