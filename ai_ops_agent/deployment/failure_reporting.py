"""Crash-safe handoff from locked transactions to independently locked outboxes."""

from __future__ import annotations

from .failure_diagnostics import parse_diagnostics
from .failure_events import FailureEvents, new_failure
from .model import DeploymentError, Store, Target
from .pm_delivery import PMDelivery

REPORTING_ERROR = "Failure reporting unavailable; retry pending"


class FailureReporting:
    """Journal intent under the transaction lock; replay outside that lock."""

    def __init__(self, store: Store) -> None:
        """Share only the manager's private state root."""
        self.store = store
        self.events = FailureEvents(store)
        self.delivery = PMDelivery(store)

    def capture(
        self,
        state: dict,
        job: dict,
        target: Target,
        stage: str,
        error: Exception,
        evidence: tuple[str, str] = ("", ""),
        outcome: str = "pending",
        *,
        prior_evidence: tuple[str, str] = ("", ""),
    ) -> None:
        """Commit sanitized intent before recovery can alter service evidence."""
        record = state["targets"].setdefault(target.name, {})
        try:
            event = new_failure(
                operation=job["operation"],
                target=target,
                stage=stage,
                requested_ref=job.get("requested_ref"),
                requested_commit=job.get("commits", {}).get(target.name),
                previous_revision=(record.get("current") or {}).get("commit"),
                exit_code=getattr(error, "exit_code", None),
                rollback_outcome=outcome,
                status=evidence[0],
                journal=evidence[1],
            )
            event["diagnostics"] = sorted(
                set(event["diagnostics"]) | set(parse_diagnostics(*prior_evidence))
            )
            state.setdefault("failure_intents", {}).setdefault(event["identity"], event)
            if record.get("operation") != job["operation"] or not record.get(
                "failure_identity"
            ):
                record.update(
                    operation=job["operation"],
                    failure_identity=event["identity"],
                    failure_stage=stage,
                    failure_exit_code=event["exit_code"],
                    original_error=str(error),
                    rollback_outcome=outcome,
                    recovery_error=None,
                )
            self.store.write(state)
        except (DeploymentError, OSError, ValueError):
            record["reporting_error"] = REPORTING_ERROR

    def finish(self, state: dict, job: dict, name: str, outcome: str) -> None:
        """Record recovery separately before immutable PM text is constructed."""
        state["targets"][name]["rollback_outcome"] = outcome
        for event in state.get("failure_intents", {}).values():
            if (
                event["operation"] == job["operation"]
                and event["target"]["name"] == name
            ):
                event["rollback_outcome"] = outcome

    def flush(self) -> bool:
        """Reconcile interrupted saves/enqueues; never hold two Store locks."""
        try:
            with self.store.lock():
                pending = self.store.read().get("failure_intents", {})
            for identity, event in pending.items():
                saved = self.events.save(event)
                if event["rollback_outcome"] == "pending":
                    continue
                self.delivery.enqueue(saved)
                with self.store.lock():
                    state = self.store.read()
                    # A concurrent recovery may have advanced the same intent.
                    active = state.get("active") or {}
                    recovering = active.get("operation") == event[
                        "operation"
                    ] and active.get("recovery")
                    if (
                        not recovering
                        and state.get("failure_intents", {}).get(identity) == event
                    ):
                        del state["failure_intents"][identity]
                        self.store.write(state)
            return True
        except (DeploymentError, OSError, ValueError):
            return False

    def result(self, record: dict) -> None:
        """Add current delivery state without exposing bridge diagnostics."""
        record.setdefault("ticket_status", None)
        record.setdefault("confirmed_ticket_id", None)
        if not record.get("failure_identity"):
            return
        record["ticket_status"] = "pending"
        try:
            delivery = self.delivery.load(record["failure_identity"])
            if delivery:
                record.update(
                    ticket_status=delivery["status"],
                    confirmed_ticket_id=delivery["confirmed_ticket_id"],
                )
            else:
                record["reporting_error"] = REPORTING_ERROR
            state = self.store.read()
            intent = state.get("failure_intents", {}).get(record["failure_identity"])
            active = state.get("active") or {}
            if intent and not (
                active.get("operation") == intent["operation"]
                and active.get("recovery")
            ):
                record["reporting_error"] = REPORTING_ERROR
        except (DeploymentError, OSError, ValueError):
            record["reporting_error"] = REPORTING_ERROR

    def retry(self) -> dict:
        """Flush durable intents and attempt one bounded batch of due tickets."""
        ok = self.flush()
        attempted = 0
        try:
            for identity in self.delivery.due():
                self.delivery.attempt_due(identity)
                attempted += 1
        except (DeploymentError, OSError, ValueError):
            ok = False
        return {
            "ok": ok,
            "attempted": attempted,
            "reporting_error": None if ok else REPORTING_ERROR,
        }
