"""Versioned failure evidence, separate from deployment recovery state."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta

from .failure_diagnostics import (
    MAX_DIAGNOSTIC_CHARS,
    SAFE_DIAGNOSTICS,
    parse_diagnostics,
)
from .model import DeploymentError, Store, Target, atomic_json, timestamp

VERSION = 1
ROLLBACK_OUTCOMES = frozenset({"pending", "not_required", "succeeded", "failed"})
_FIELDS = frozenset(
    {
        "version",
        "identity",
        "operation",
        "target",
        "requested_ref",
        "requested_commit",
        "previous_revision",
        "failed_at",
        "stage",
        "exit_code",
        "rollback_outcome",
        "diagnostics",
    }
)


class FailureRecordError(DeploymentError):
    """Failure evidence is corrupt, unsupported or unsafe to persist."""


def _identity(operation: str, target: dict, stage: str) -> str:
    """Hash an unambiguous, stable identity payload."""
    payload = {"operation": operation, "target": target, "stage": stage}
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _target(target: Target) -> dict:
    """Capture the configured target, never resolve paths from journal input."""
    return {"name": target.name, "repo": str(target.repo), "service": target.service}


def failure_identity(operation: str, target: Target, stage: str) -> str:
    """Identify one job/target/stage independently of time or recovery results."""
    return _identity(operation, _target(target), stage)


def new_failure(
    *,
    operation: str,
    target: Target,
    stage: str,
    requested_ref: str | None,
    requested_commit: str | None,
    previous_revision: str | None,
    exit_code: int | None,
    rollback_outcome: str = "pending",
    status: str = "",
    journal: str = "",
) -> dict:
    """Build validated evidence using trusted job metadata and raw diagnostics."""
    value = {
        "version": VERSION,
        "identity": failure_identity(operation, target, stage),
        "operation": operation,
        "target": _target(target),
        "stage": stage,
        "requested_ref": requested_ref,
        "requested_commit": requested_commit,
        "previous_revision": previous_revision,
        "failed_at": timestamp(),
        "exit_code": exit_code,
        "rollback_outcome": rollback_outcome,
        "diagnostics": list(parse_diagnostics(status, journal)),
    }
    _validate(value)
    return value


def _text(value: object) -> bool:
    """Accept bounded, nonempty metadata without control characters."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= 4096
        and all(char.isprintable() for char in value)
    )


def _require(condition: bool) -> None:
    """Enforce schema checks even with Python optimization enabled."""
    if not condition:
        raise ValueError("Invalid failure field")


def _validate(value: object) -> None:
    """Reject malformed schemas and unsafe diagnostic content without echoing it."""
    if not isinstance(value, dict):
        raise FailureRecordError("Corrupt failure record")
    if type(value.get("version")) is not int or value["version"] != VERSION:
        raise FailureRecordError("Unsupported failure record version")
    try:
        _require(set(value) == _FIELDS)
        target = value["target"]
        _require(
            isinstance(target, dict) and set(target) == {"name", "repo", "service"}
        )
        _require(all(_text(item) for item in target.values()))
        _require(all(_text(value[key]) for key in ("operation", "stage", "failed_at")))
        for key in ("requested_ref", "requested_commit", "previous_revision"):
            _require(value[key] is None or _text(value[key]))
        _require(len(json.dumps(value).encode("utf-8")) <= 65536)
        when = datetime.fromisoformat(value["failed_at"])
        _require(when.utcoffset() == timedelta(0))
        _require(value["exit_code"] is None or type(value["exit_code"]) is int)
        _require(value["rollback_outcome"] in ROLLBACK_OUTCOMES)
        diagnostics = value["diagnostics"]
        _require(isinstance(diagnostics, list))
        _require(
            all(
                isinstance(item, str) and item in SAFE_DIAGNOSTICS
                for item in diagnostics
            )
        )
        _require(len(diagnostics) == len(set(diagnostics)))
        _require(sum(map(len, diagnostics)) <= MAX_DIAGNOSTIC_CHARS)
        _require(
            value["identity"] == _identity(value["operation"], target, value["stage"])
        )
    except (ValueError, TypeError, KeyError) as error:
        raise FailureRecordError("Corrupt failure record") from error


class FailureEvents:
    """Private event storage with internally acquired Store.lock().

    Public load/save own the lock for the entire read/modify/write cycle. Call
    them outside any existing Store.lock() scope; contention fails immediately.
    Neither method reads or writes state.json, nor triggers deployment actions.
    """

    def __init__(self, store: Store) -> None:
        """Use the manager's configured private root."""
        self.store = store

    def _load(self, identity: str) -> dict | None:
        """Read one validated event while the caller holds the store lock."""
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise FailureRecordError("Invalid failure identity")
        path = self.store.root / "failure-events" / f"{identity}.json"
        try:
            with path.open("rb") as stream:
                data = stream.read(65537)
            if len(data) > 65536:
                raise ValueError("Oversized record")
            value = json.loads(data)
        except FileNotFoundError:
            return None
        except (ValueError, UnicodeError, RecursionError) as error:
            raise FailureRecordError("Corrupt failure record") from error
        _validate(value)
        if value["identity"] != identity:
            raise FailureRecordError("Failure identity does not match filename")
        return value

    def load(self, identity: str) -> dict | None:
        """Return an event or None; report corrupt/unsupported evidence intact."""
        with self.store.lock():
            return self._load(identity)

    def save(self, value: dict) -> dict:
        """Persist first evidence; replay may update only a non-pending rollback.

        Original timestamp, revisions, exit status and diagnostics always win.
        A pending replay cannot erase a recorded recovery result. A later explicit
        outcome can replace it, e.g. failed recovery followed by successful retry.
        """
        _validate(value)
        with self.store.lock():
            existing = self._load(value["identity"])
            saved = dict(existing if existing is not None else value)
            if value["rollback_outcome"] != "pending":
                saved["rollback_outcome"] = value["rollback_outcome"]
            directory = self.store.root / "failure-events"
            directory.mkdir(mode=0o700, exist_ok=True)
            directory.chmod(0o700)
            atomic_json(directory / f"{saved['identity']}.json", saved)
            return saved
