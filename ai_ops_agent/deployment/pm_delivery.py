"""Durable PM outbox; no worker scheduling, sleeping or deployment actions."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from .model import DeploymentError, Store, atomic_json
from .pm_bridge import BridgeError, PMBridge, build_ticket, task_id, validate_ticket

MAX_ATTEMPTS = 8
BASE_DELAY = 60
MAX_DELAY = 3600
LEASE_SECONDS = 300  # exceeds six bridge calls at 30 seconds each
MAX_RECORDS = 1024
MAX_BATCH = 20
_FIELDS = {
    "version",
    "identity",
    "ticket",
    "status",
    "attempts",
    "next_attempt_utc",
    "confirmed_ticket_id",
}


class DeliveryError(DeploymentError):
    """Delivery state cannot be safely read or changed."""


def _utc(value: datetime) -> datetime:
    """Require an aware UTC clock for durable scheduling."""
    if value.utcoffset() != timedelta(0):
        raise ValueError("Expected UTC time")
    return value


class PMDelivery:
    """Own short Store locks; persist immutable ticket text before bridge I/O.

    Contention raises DeploymentError, as with FailureEvents. Call outside an
    existing Store lock. Expiring claims permit restart replay; attempt numbers
    fence stale completions and PM identity/text make overlapping adds safe.
    """

    def __init__(self, store: Store, bridge: PMBridge | None = None) -> None:
        """Use a separate private outbox under the deployment store."""
        self.store = store
        self.bridge = bridge if bridge is not None else PMBridge()
        self.directory = store.root / "pm-delivery"

    def _load(self, identity: str) -> dict | None:
        """Read and validate bounded state while holding the store lock."""
        expected_id = task_id(identity)
        try:
            with (self.directory / f"{identity}.json").open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != _FIELDS:
                raise ValueError
            validate_ticket(value["ticket"])
            if (
                type(value["version"]) is not int
                or value["version"] != 1
                or value["identity"] != identity
                or value["ticket"]["id"] != expected_id
                or value["status"] not in {"pending", "delivered", "exhausted"}
                or type(value["attempts"]) is not int
                or not 0 <= value["attempts"] <= MAX_ATTEMPTS
            ):
                raise ValueError
            if value["status"] == "delivered":
                if (
                    value["confirmed_ticket_id"] != expected_id
                    or value["next_attempt_utc"] is not None
                ):
                    raise ValueError
            else:
                if value["confirmed_ticket_id"] is not None:
                    raise ValueError
                if value["status"] == "pending":
                    _utc(datetime.fromisoformat(value["next_attempt_utc"]))
                    if value["attempts"] >= MAX_ATTEMPTS:
                        raise ValueError
                elif (
                    value["attempts"] != MAX_ATTEMPTS
                    or value["next_attempt_utc"] is not None
                ):
                    raise ValueError
            return value
        except FileNotFoundError:
            return None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DeliveryError("Corrupt PM delivery record") from None

    def _save(self, value: dict) -> None:
        """Atomically fsync private delivery state under the caller's lock."""
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.directory.chmod(0o700)
        atomic_json(self.directory / f"{value['identity']}.json", value)

    def enqueue(self, event: dict, *, now: datetime | None = None) -> dict:
        """Persist the first ticket unchanged; return existing state on replay."""
        ticket = build_ticket(event)
        when = _utc(now if now is not None else datetime.now(UTC))
        with self.store.lock():
            existing = self._load(event["identity"])
            if existing is not None:
                return existing
            if sum(1 for _ in self.directory.glob("*.json")) >= MAX_RECORDS:
                raise DeliveryError("PM delivery capacity reached")
            value = {
                "version": 1,
                "identity": event["identity"],
                "ticket": ticket,
                "status": "pending",
                "attempts": 0,
                "next_attempt_utc": when.isoformat(),
                "confirmed_ticket_id": None,
            }
            self._save(value)
            return value

    def load(self, identity: str) -> dict | None:
        """Return delivery status separately from failure and rollback state."""
        with self.store.lock():
            return self._load(identity)

    def due(self, *, now: datetime | None = None, limit: int = MAX_BATCH) -> list[str]:
        """List at most twenty due identities without claiming or doing I/O to PM."""
        if type(limit) is not int or not 1 <= limit <= MAX_BATCH:
            raise ValueError("Invalid delivery batch limit")
        when = _utc(now if now is not None else datetime.now(UTC))
        with self.store.lock():
            result = []
            for path in sorted(self.directory.glob("*.json")):
                value = self._load(path.stem)
                if (
                    value is not None
                    and value["status"] == "pending"
                    and datetime.fromisoformat(value["next_attempt_utc"]) <= when
                ):
                    result.append(path.stem)
                    if len(result) == limit:
                        break
            return result

    def attempt_due(self, identity: str, *, now: datetime | None = None) -> dict | None:
        """Claim at most one due attempt, call PM unlocked, then persist outcome.

        A crash consumes an attempt. The final claim is provisionally exhausted
        so crashes cannot exceed the retry budget. Storage errors propagate.
        """
        when = _utc(now if now is not None else datetime.now(UTC))
        with self.store.lock():
            value = self._load(identity)
            if (
                value is None
                or value["status"] != "pending"
                or datetime.fromisoformat(value["next_attempt_utc"]) > when
            ):
                return value
            value["attempts"] += 1
            attempt = value["attempts"]
            if attempt == MAX_ATTEMPTS:
                value.update(status="exhausted", next_attempt_utc=None)
            else:
                value["next_attempt_utc"] = (
                    when + timedelta(seconds=LEASE_SECONDS)
                ).isoformat()
            self._save(value)
        confirmed = None
        try:
            confirmed = self.bridge.add(dict(value["ticket"]))
        except BridgeError:
            pass  # No raw bridge errors enter state or logs.
        with self.store.lock():
            current = self._load(identity)
            if current is None or current["attempts"] != attempt:
                return current
            if confirmed == value["ticket"]["id"]:
                current.update(
                    status="delivered",
                    confirmed_ticket_id=confirmed,
                    next_attempt_utc=None,
                )
            elif attempt < MAX_ATTEMPTS:
                delay = min(MAX_DELAY, BASE_DELAY * 2 ** (attempt - 1))
                current["next_attempt_utc"] = (
                    when + timedelta(seconds=delay)
                ).isoformat()
            self._save(current)
            return current
