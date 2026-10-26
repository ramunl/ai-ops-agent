"""Restart, start or stop a whitelisted service; the dashboard's buttons call this.

Run as ``ai-service list``, ``ai-service restart <service>``,
``ai-service start <service>``, ``ai-service stop <service>`` or
``ai-service reboot``; prints one JSON document. Only names in
services_list.MANAGED_SERVICES are accepted, so a caller can never reach
another unit or pass systemctl options.

Start and stop also change the firewall port configured for the service in
services_list.SERVICE_PORTS: start opens it once the unit is verified active,
stop closes it once the unit is verified inactive. Stopping drops any client
connected to the service, so the caller must confirm first.

A reboot is refused while an upgrade, cleanup, tool update or deployment
holds its lock: interrupting those can leave the server half-changed.

The restart is queued (``--no-block``) and the command returns at once: the
caller may be the very service being restarted (the dashboard restarting
itself), and must be able to answer before it goes down.

Standard library only, run with the system python3.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from ai_ops_agent.services_list import MANAGED_SERVICES, SERVICE_PORTS, ServicePort

LOG = "/var/log/ai-service.log"
DEPLOYMENT_STATE = Path("/var/lib/ai-deploy/state.json")
USAGE = "usage: ai-service list|restart|start|stop <service>|reboot"
UNIT_TIMEOUT = 60
FIREWALL_TIMEOUT = 30
QUERY_TIMEOUT = 10
# Work a reboot must not interrupt, by the lock each one holds while running.
BUSY_LOCKS = {
    Path("/run/ai-packages.lock"): "a package upgrade",
    Path("/run/lock/ai-cleanup.lock"): "a disk cleanup",
    Path("/run/ai-tools.lock"): "an AI tool update",
    Path("/var/lib/ai-deploy/lock"): "a deployment",
}
Runner = Callable[[list[str], int], tuple[int, str]]


def run_command(args: list[str], timeout: int) -> tuple[int, str]:
    """Run a command; return (exit code, combined output)."""
    try:
        done = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return 127, f"{args[0]}: not installed"
    except subprocess.TimeoutExpired:
        return 124, f"{args[0]}: timed out after {timeout} s"
    return done.returncode, (done.stdout + done.stderr).strip()


def restart(service: str, run: Runner = run_command) -> dict:
    """Queue a restart of a whitelisted service."""
    if service not in MANAGED_SERVICES:
        return {"ok": False, "error": f"unknown service '{service}'"}
    code, out = run(["systemctl", "--no-block", "restart", service], 30)
    if code != 0:
        return {"ok": False, "error": out or f"systemctl exited with {code}"}
    return {"ok": True, "service": service, "queued": True}


def _failed(
    service: str, error: str, unit: str | None, port: int | None, port_open: bool | None
) -> dict:
    """A failed start or stop, with what was verified about unit and port.

    ``unit`` and ``port_open`` are None when that part was not verified.
    """
    return {
        "ok": False,
        "service": service,
        "error": error,
        "unit": unit,
        "port": port,
        "port_open": port_open,
    }


def _done(service: str, unit: str, port: int | None, port_open: bool | None) -> dict:
    """A verified start or stop; ``port_open`` is None for a unit with no port."""
    return {
        "ok": True,
        "service": service,
        "unit": unit,
        "port": port,
        "port_open": port_open,
    }


def _unit_state(service: str, run: Runner) -> str | None:
    """systemd's ActiveState for a unit, or None when it cannot be read."""
    code, out = run(
        ["systemctl", "show", "--property=ActiveState", "--value", service],
        QUERY_TIMEOUT,
    )
    state = out.strip()
    return state if code == 0 and state else None


def _port_status(spec: ServicePort, run: Runner) -> tuple[bool | None, str]:
    """Whether anything still serves the port: True, False, or None if unknown.

    ufw does not block ports that Docker publishes (Docker writes its own
    iptables rules), so a running container publishing the port counts as open.
    """
    flag = "-t" if spec.proto == "tcp" else "-u"
    code, out = run(["ss", "-H", "-l", "-n", flag], QUERY_TIMEOUT)
    if code != 0:
        return None, f"could not list listening sockets: {out}"
    for line in out.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[3].rsplit(":", 1)[-1] == str(spec.port):
            return True, "a process still listens on the port"
    code, out = run(
        ["docker", "ps", "-q", "--filter", f"publish={spec.port}"], QUERY_TIMEOUT
    )
    if code == 127:  # Docker is not installed, so it publishes nothing.
        return False, ""
    if code != 0:
        return None, f"could not query Docker: {out}"
    if out:
        return True, "a Docker container still publishes the port"
    return False, ""


def start(service: str, run: Runner = run_command) -> dict:
    """Start a whitelisted unit, verify it is active, then open its port.

    The firewall is changed only after the unit is verified active.
    """
    if service not in MANAGED_SERVICES:
        return {"ok": False, "error": f"unknown service '{service}'"}
    spec: ServicePort | None = SERVICE_PORTS.get(service)
    port = spec.port if spec else None
    code, out = run(["systemctl", "start", service], UNIT_TIMEOUT)
    if code != 0:
        error = out or f"systemctl exited with {code}"
        return _failed(service, error, None, port, None)
    state = _unit_state(service, run)
    if state != "active":
        error = f"{service} is {state or 'unverified'}, not active; port not changed"
        return _failed(service, error, state, port, None)
    if spec is None:
        return _done(service, "active", None, None)
    rule = f"{spec.port}/{spec.proto}"
    code, out = run(["ufw", "allow", rule], FIREWALL_TIMEOUT)
    if code != 0:
        error = f"{service} is active but ufw allow {rule} failed: {out or code}"
        return _failed(service, error, "active", port, None)
    return _done(service, "active", port, True)


def stop(service: str, run: Runner = run_command) -> dict:
    """Stop a whitelisted unit, verify it is inactive, then close its port.

    The firewall is changed only after the unit is verified inactive. If the
    port cannot be closed, the result says so: the service is down and the port
    may still be open.
    """
    if service not in MANAGED_SERVICES:
        return {"ok": False, "error": f"unknown service '{service}'"}
    spec: ServicePort | None = SERVICE_PORTS.get(service)
    port = spec.port if spec else None
    code, out = run(["systemctl", "stop", service], UNIT_TIMEOUT)
    if code != 0:
        error = out or f"systemctl exited with {code}"
        return _failed(service, error, None, port, None)
    state = _unit_state(service, run)
    if state != "inactive":
        error = f"{service} is {state or 'unverified'} after stop; port not changed"
        return _failed(service, error, state, port, None)
    if spec is None:
        return _done(service, "inactive", None, None)
    rule = f"{spec.port}/{spec.proto}"
    code, out = run(["ufw", "deny", rule], FIREWALL_TIMEOUT)
    if code != 0:
        error = (
            f"{service} is stopped but ufw deny {rule} failed: {out or code}; "
            "the port may still be open"
        )
        return _failed(service, error, "inactive", port, None)
    listening, detail = _port_status(spec, run)
    if listening:
        error = f"{service} is stopped but port {rule} is still open: {detail}"
        return _failed(service, error, "inactive", port, True)
    if listening is None:
        error = f"{service} is stopped; port {rule} not verified closed: {detail}"
        return _failed(service, error, "inactive", port, None)
    return _done(service, "inactive", port, False)


def is_locked(path: Path) -> bool:
    """Whether another process holds the lock file (never creates it)."""
    try:
        handle = open(path, encoding="utf-8")  # noqa: SIM115 (closed below)
    except OSError:
        return False
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        handle.close()
    return False


def reboot(run: Runner = run_command, locks: dict[Path, str] | None = None) -> dict:
    """Queue a reboot unless protected work is running when checked.

    These checks do not prevent new work starting after the check.
    """
    locks = BUSY_LOCKS if locks is None else locks
    busy = [what for path, what in locks.items() if is_locked(path)]
    if busy:
        return {"ok": False, "busy": True, "error": f"{busy[0]} is running"}
    if locks == BUSY_LOCKS:
        try:
            state_path = DEPLOYMENT_STATE
            if state_path.exists() and json.loads(state_path.read_text()).get("active"):
                return {"ok": False, "busy": True, "error": "a deployment is running"}
        except (OSError, ValueError, AttributeError):
            return {"ok": False, "error": "Cannot verify deployment state"}
        unit_code, unit_state = run(
            ["systemctl", "is-active", "ai-packages-upgrade.service"], 10
        )
        if unit_code == 0 and unit_state.strip() in {"active", "activating"}:
            return {"ok": False, "busy": True, "error": "a package upgrade is running"}
    code, out = run(
        [
            "systemd-run",
            "--quiet",
            "--collect",
            "--unit=ai-server-reboot",
            "--on-active=5s",
            "/usr/bin/systemctl",
            "reboot",
        ],
        30,
    )
    if code != 0:
        return {"ok": False, "error": out or f"systemctl exited with {code}"}
    return {"ok": True, "queued": True}


def _log(line: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
    except OSError:
        pass


UNIT_ACTIONS = {"restart": restart, "start": start, "stop": stop}


def main(
    argv: list[str], run: Runner = run_command, locks: dict[Path, str] | None = None
) -> int:
    """CLI entry: ``list``, ``restart|start|stop <service>`` or ``reboot``."""
    if argv == ["list"]:
        print(json.dumps({"ok": True, "services": MANAGED_SERVICES}))
        return 0
    is_reboot = argv == ["reboot"]
    if not is_reboot and (len(argv) != 2 or argv[0] not in UNIT_ACTIONS):
        print(json.dumps({"ok": False, "error": USAGE}))
        return 2
    if os.geteuid() != 0:
        print(json.dumps({"ok": False, "error": "ai-service must run as root"}))
        return 1
    result = reboot(run, locks) if is_reboot else UNIT_ACTIONS[argv[0]](argv[1], run)
    outcome = result.get("error") or ("queued" if result.get("queued") else "done")
    _log(f"{' '.join(argv)}: {outcome}")
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
