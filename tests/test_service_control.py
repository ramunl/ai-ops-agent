"""ai-service: the whitelisted service commands the dashboard and bot use."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_ops_agent import service_control
from ai_ops_agent.services_list import (
    MANAGED_SERVICES,
    SERVICE_PORTS,
    ServicePort,
    validate_service_ports,
)

REPO = Path(__file__).resolve().parent.parent
SHOW_STATE = ["systemctl", "show", "--property=ActiveState", "--value"]
LISTEN_CHECK = ["ss", "-H", "-l", "-n", "-t"]
DOCKER_CHECK = ["docker", "ps", "--format", "{{.Ports}}"]


class FakeRun:
    def __init__(self, code=0, out=""):
        self.calls, self.code, self.out = [], code, out

    def __call__(self, args, timeout):
        self.calls.append(args)
        return self.code, self.out


class FakeHost:
    """Fake systemctl, ufw, ss and docker; records every argument list."""

    def __init__(self, state="inactive", start_state="active", stop_state="inactive"):
        self.calls, self.state = [], state
        self.start_state, self.stop_state = start_state, stop_state
        self.listening = False  # something still listens on port 8080
        self.publisher = ""  # container name still publishing port 8080
        self.failures = {}  # "systemctl start" -> (code, output) to return

    def __call__(self, args, timeout):
        self.calls.append(args)
        key = " ".join(args[:2])
        if key in self.failures:
            return self.failures[key]
        if key == "systemctl start":
            self.state = self.start_state
        elif key == "systemctl stop":
            self.state = self.stop_state
        elif key == "systemctl show":
            return 0, self.state
        elif key == "ss -H" and self.listening:
            return 0, "LISTEN 0 4096 0.0.0.0:8080 0.0.0.0:*"
        elif key == "docker ps":
            return 0, self.publisher
        return 0, ""


@pytest.fixture(autouse=True)
def no_real_host(tmp_path):
    """Fail any test that would run real systemd, firewall or Docker commands."""
    real_run = subprocess.run
    host_tools = {"systemctl", "systemd-run", "ufw", "ss", "docker"}

    def guarded(args, *call_args, **kwargs):
        if args and args[0] in host_tools:
            raise AssertionError(f"test tried to run a real command: {args}")
        return real_run(args, *call_args, **kwargs)

    with (
        patch.object(service_control.subprocess, "run", guarded),
        patch.object(service_control, "LOG", str(tmp_path / "ai-service.log")),
    ):
        yield


def iptv_config():
    """Patch in a managed service with a firewall port, as the owner would set."""
    return patch.multiple(
        service_control,
        MANAGED_SERVICES=[*MANAGED_SERVICES, "ai-iptv"],
        SERVICE_PORTS={"ai-iptv": ServicePort(8080)},
    )


def test_whitelist_covers_every_agent_and_the_dashboard():
    assert MANAGED_SERVICES == [
        "ai-coding-agent",
        "ai-pm-agent",
        "ai-ops-agent",
        "ai-dashboard",
        "iptv-proxy",
    ]


def test_restart_is_queued_without_blocking():
    run = FakeRun()
    assert service_control.restart("ai-pm-agent", run) == {
        "ok": True,
        "service": "ai-pm-agent",
        "queued": True,
    }
    assert run.calls == [["systemctl", "--no-block", "restart", "ai-pm-agent"]]


def test_unknown_or_crafted_names_never_reach_systemctl():
    run = FakeRun()
    for name in ("sshd", "--force", "ai-pm-agent --now", "../ai-pm-agent", ""):
        assert service_control.restart(name, run)["ok"] is False
    assert run.calls == []


def test_systemctl_failure_is_reported():
    result = service_control.restart("ai-dashboard", FakeRun(1, "Unit not found"))
    assert result == {"ok": False, "error": "Unit not found"}


def test_cli(capsys, tmp_path):
    assert service_control.main(["list"]) == 0
    assert json.loads(capsys.readouterr().out)["services"] == MANAGED_SERVICES
    assert service_control.main(["delete", "ai-pm-agent"]) == 2
    assert "usage" in json.loads(capsys.readouterr().out)["error"]
    with patch.object(service_control.os, "geteuid", return_value=1000):
        assert service_control.main(["restart", "ai-pm-agent"]) == 1
    assert "root" in json.loads(capsys.readouterr().out)["error"]
    with (
        patch.object(service_control.os, "geteuid", return_value=0),
        patch.object(service_control, "LOG", str(tmp_path / "svc.log")),
    ):
        assert service_control.main(["restart", "ai-pm-agent"], FakeRun()) == 0
    assert "restart ai-pm-agent: queued" in (tmp_path / "svc.log").read_text()


def test_wrapper_runs_without_bot_credentials():
    # The dashboard runs it with no OPS_TELEGRAM_BOT_TOKEN in its environment.
    done = subprocess.run(
        ["sh", str(REPO / "deploy" / "ai-service"), "list"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "AI_OPS_AGENT_DIR": str(REPO)},
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(done.stdout)["services"] == MANAGED_SERVICES


def test_reboot_is_queued_without_blocking(tmp_path):
    run = FakeRun()
    assert service_control.reboot(run, {tmp_path / "absent": "x"}) == {
        "ok": True,
        "queued": True,
    }
    assert run.calls == [
        [
            "systemd-run",
            "--quiet",
            "--collect",
            "--unit=ai-server-reboot",
            "--on-active=5s",
            "/usr/bin/systemctl",
            "reboot",
        ]
    ]


def test_reboot_is_refused_while_protected_work_holds_its_lock(tmp_path):
    import fcntl

    lock = tmp_path / "ai-packages.lock"
    run = FakeRun()
    with open(lock, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        result = service_control.reboot(run, {lock: "a package upgrade"})
    assert result == {
        "ok": False,
        "busy": True,
        "error": "a package upgrade is running",
    }
    assert run.calls == []
    # A lock file left behind by a finished run does not block.
    assert service_control.reboot(run, {lock: "a package upgrade"})["ok"] is True


def test_reboot_watches_every_long_running_ops_command():
    from ai_ops_agent import cleanup, package_updates, tool_updates

    watched = set(service_control.BUSY_LOCKS)
    assert {package_updates.LOCK, tool_updates.LOCK, cleanup.Paths().lock} <= watched


def test_reboot_cli_takes_no_arguments_and_needs_root(capsys, tmp_path):
    run = FakeRun()
    for argv in (["reboot", "--force"], ["reboot", "now"], ["poweroff"]):
        assert service_control.main(argv, run) == 2
    with patch.object(service_control.os, "geteuid", return_value=1000):
        assert service_control.main(["reboot"], run) == 1
    assert run.calls == []
    capsys.readouterr()
    with (
        patch.object(service_control.os, "geteuid", return_value=0),
        patch.object(service_control, "LOG", str(tmp_path / "svc.log")),
    ):
        assert service_control.main(["reboot"], run, {}) == 0
    assert json.loads(capsys.readouterr().out)["queued"] is True
    assert "reboot: queued" in (tmp_path / "svc.log").read_text()


def test_reboot_refuses_detached_upgrade_without_a_caller_lock(tmp_path):
    run = FakeRun(0, "active")
    with patch.object(service_control, "BUSY_LOCKS", {tmp_path / "absent": "upgrade"}):
        result = service_control.reboot(run)
    assert result["busy"] is True
    assert "package upgrade" in result["error"]
    assert not any(call[0] == "systemd-run" for call in run.calls)


def test_start_opens_the_port_only_after_the_unit_is_active():
    host = FakeHost()
    with iptv_config():
        result = service_control.start("ai-iptv", host)
    assert result == {
        "ok": True,
        "service": "ai-iptv",
        "unit": "active",
        "port": 8080,
        "port_open": True,
    }
    assert host.calls == [
        ["systemctl", "start", "ai-iptv"],
        [*SHOW_STATE, "ai-iptv"],
        ["ufw", "allow", "8080/tcp"],
    ]
    assert all(isinstance(arg, str) for call in host.calls for arg in call)


def test_start_without_a_port_changes_only_the_unit():
    host = FakeHost()
    result = service_control.start("ai-pm-agent", host)
    assert result == {
        "ok": True,
        "service": "ai-pm-agent",
        "unit": "active",
        "port": None,
        "port_open": None,
    }
    assert [call[0] for call in host.calls] == ["systemctl", "systemctl"]


def test_start_failure_never_reaches_the_firewall():
    host = FakeHost()
    host.failures["systemctl start"] = (1, "Job failed")
    with iptv_config():
        result = service_control.start("ai-iptv", host)
    assert result["ok"] is False
    assert result["port_open"] is None
    assert not any(call[0] == "ufw" for call in host.calls)


def test_start_timeout_is_a_failure_and_skips_the_firewall():
    host = FakeHost()
    host.failures["systemctl start"] = (124, "systemctl: timed out after 60 s")
    with iptv_config():
        result = service_control.start("ai-iptv", host)
    assert result["ok"] is False
    assert "timed out" in result["error"]
    assert not any(call[0] == "ufw" for call in host.calls)


@pytest.mark.parametrize("start_state", ["activating", "failed"])
def test_start_never_opens_the_port_unless_the_unit_is_active(start_state):
    host = FakeHost(start_state=start_state)
    with iptv_config():
        result = service_control.start("ai-iptv", host)
    assert result["ok"] is False
    assert result["unit"] == start_state
    assert "port not changed" in result["error"]
    assert not any(call[0] == "ufw" for call in host.calls)


def test_start_reports_a_firewall_failure_with_the_unit_still_active():
    host = FakeHost()
    host.failures["ufw allow"] = (1, "ERROR: could not open port")
    with iptv_config():
        result = service_control.start("ai-iptv", host)
    assert result["ok"] is False
    assert result["unit"] == "active"
    assert result["port_open"] is None
    assert "is active but ufw allow 8080/tcp failed" in result["error"]


def test_stop_closes_the_port_only_after_the_unit_is_inactive():
    host = FakeHost(state="active")
    with iptv_config():
        result = service_control.stop("ai-iptv", host)
    assert result == {
        "ok": True,
        "service": "ai-iptv",
        "unit": "inactive",
        "port": 8080,
        "port_open": False,
    }
    assert host.calls == [
        ["systemctl", "stop", "ai-iptv"],
        [*SHOW_STATE, "ai-iptv"],
        ["ufw", "deny", "8080/tcp"],
        LISTEN_CHECK,
        DOCKER_CHECK,
    ]


def test_stop_without_a_port_changes_only_the_unit():
    host = FakeHost(state="active")
    result = service_control.stop("ai-pm-agent", host)
    assert result["ok"] is True
    assert result["port_open"] is None
    assert [call[0] for call in host.calls] == ["systemctl", "systemctl"]


@pytest.mark.parametrize("stop_state", ["active", "deactivating"])
def test_stop_never_closes_the_port_unless_the_unit_is_inactive(stop_state):
    host = FakeHost(state="active", stop_state=stop_state)
    with iptv_config():
        result = service_control.stop("ai-iptv", host)
    assert result["ok"] is False
    assert result["unit"] == stop_state
    assert result["port_open"] is None
    assert not any(call[0] == "ufw" for call in host.calls)


def test_stop_timeout_is_a_failure_and_skips_the_firewall():
    host = FakeHost(state="active")
    host.failures["systemctl stop"] = (124, "systemctl: timed out after 60 s")
    with iptv_config():
        result = service_control.stop("ai-iptv", host)
    assert result["ok"] is False
    assert "timed out" in result["error"]
    assert not any(call[0] == "ufw" for call in host.calls)


def test_stop_reports_partial_state_when_the_port_cannot_be_closed():
    host = FakeHost(state="active")
    host.failures["ufw deny"] = (1, "ERROR: rule failed")
    with iptv_config():
        result = service_control.stop("ai-iptv", host)
    assert result["ok"] is False
    assert result["unit"] == "inactive"
    assert result["port_open"] is None
    assert "stopped but ufw deny 8080/tcp failed" in result["error"]
    assert "may still be open" in result["error"]
    # Nothing is checked after the failed deny.
    assert LISTEN_CHECK not in host.calls


def test_stop_reports_a_port_that_is_still_listening():
    host = FakeHost(state="active")
    host.listening = True
    with iptv_config():
        result = service_control.stop("ai-iptv", host)
    assert result["ok"] is False
    assert result["unit"] == "inactive"
    assert result["port_open"] is True
    assert "still open" in result["error"]
    assert ["ufw", "deny", "8080/tcp"] in host.calls


def test_stop_reports_a_docker_container_that_still_publishes_the_port():
    host = FakeHost(state="active")
    host.publisher = "0.0.0.0:8080->8080/tcp"
    with iptv_config():
        result = service_control.stop("ai-iptv", host)
    assert result["ok"] is False
    assert result["port_open"] is True
    assert "Docker container still publishes" in result["error"]


def test_stop_treats_missing_docker_as_publishing_nothing():
    host = FakeHost(state="active")
    host.failures["docker ps"] = (127, "docker: not installed")
    with iptv_config():
        result = service_control.stop("ai-iptv", host)
    assert result["ok"] is True
    assert result["port_open"] is False


def test_stop_says_when_the_port_cannot_be_verified():
    host = FakeHost(state="active")
    host.failures["ss -H"] = (1, "ss: unavailable")
    with iptv_config():
        result = service_control.stop("ai-iptv", host)
    assert result["ok"] is False
    assert result["port_open"] is None
    assert "could not list listening sockets" in result["error"]


def test_start_and_stop_reject_names_outside_the_whitelist():
    host = FakeHost()
    for name in ("sshd", "ai-iptv", "--now", ""):
        assert service_control.start(name, host)["ok"] is False
        assert service_control.stop(name, host)["ok"] is False
    assert host.calls == []


def test_start_and_stop_cli_runs_the_steps_and_logs_them(capsys, tmp_path):
    host = FakeHost(state="active")
    with (
        iptv_config(),
        patch.object(service_control.os, "geteuid", return_value=0),
        patch.object(service_control, "LOG", str(tmp_path / "svc.log")),
    ):
        assert service_control.main(["stop", "ai-iptv"], host) == 0
        assert json.loads(capsys.readouterr().out)["port_open"] is False
        assert service_control.main(["start", "ai-iptv"], host) == 0
        assert json.loads(capsys.readouterr().out)["port_open"] is True
    assert host.calls[-1] == ["ufw", "allow", "8080/tcp"]
    log = (tmp_path / "svc.log").read_text()
    assert "stop ai-iptv: done" in log
    assert "start ai-iptv: done" in log


def test_start_and_stop_cli_need_one_service_and_root(capsys):
    host = FakeHost()
    for argv in (["stop"], ["start", "a", "b"]):
        assert service_control.main(argv, host) == 2
    capsys.readouterr()
    with patch.object(service_control.os, "geteuid", return_value=1000):
        assert service_control.main(["stop", "ai-pm-agent"], host) == 1
    assert "root" in json.loads(capsys.readouterr().out)["error"]
    assert host.calls == []


def test_port_settings_must_be_valid_integers_and_unique():
    services = ["ai-a", "ai-b"]
    validate_service_ports(services, {"ai-a": ServicePort(8080)})
    validate_service_ports(services, {"ai-a": ServicePort(1, "udp")})
    bad = [
        {"ai-a": ServicePort(0)},
        {"ai-a": ServicePort(65536)},
        {"ai-a": ServicePort("8080")},
        {"ai-a": ServicePort(True)},
        {"ai-a": ServicePort(8080, "icmp")},
        {"ai-c": ServicePort(8080)},
        {"ai-a": ServicePort(8080), "ai-b": ServicePort(8080)},
    ]
    for ports in bad:
        with pytest.raises(ValueError):
            validate_service_ports(services, ports)


def test_shipped_port_settings_are_valid():
    validate_service_ports(MANAGED_SERVICES, SERVICE_PORTS)


class FakeDocker:
    def __init__(
        self,
        state="running",
        listener=False,
        publisher="",
        failure=None,
        published=True,
    ):
        self.calls = []
        self.state = state
        self.listener = listener
        self.publisher = publisher
        self.failure = failure
        self.published = published

    def __call__(self, args, timeout):
        self.calls.append(args)
        if args[:2] == ["docker", "inspect"]:
            if args[3] == "{{.State.Status}}":
                return 0, self.state
            return 0, json.dumps(
                {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]}
                if self.published
                else {}
            )
        if args[:2] in (["docker", "start"], ["docker", "stop"], ["docker", "restart"]):
            return self.failure or (0, "iptv-proxy")
        if args[0] == "ss":
            return 0, "LISTEN 0 4096 0.0.0.0:8080 0.0.0.0:*" if self.listener else ""
        if args[:2] == ["docker", "ps"]:
            return 0, self.publisher
        raise AssertionError(args)


def test_iptv_start_checks_exact_container_and_published_port():
    host = FakeDocker()
    result = service_control.start("iptv-proxy", host)
    assert result["ok"] and result["port_open"]
    assert host.calls[0] == ["docker", "start", "iptv-proxy"]
    assert all(call[0] not in {"systemctl", "ufw"} for call in host.calls)


@pytest.mark.parametrize("state,published", [("exited", True), ("running", False)])
def test_iptv_start_rejects_wrong_state_or_missing_publication(state, published):
    result = service_control.start(
        "iptv-proxy", FakeDocker(state=state, published=published)
    )
    assert not result["ok"]
    assert result["port_open"] is None


def test_iptv_stop_verifies_no_listener_or_docker_publisher():
    host = FakeDocker(state="exited")
    result = service_control.stop("iptv-proxy", host)
    assert result["ok"] and result["port_open"] is False
    assert host.calls[0] == ["docker", "stop", "iptv-proxy"]
    assert host.calls[-1] == DOCKER_CHECK
    assert all(call[0] not in {"systemctl", "ufw"} for call in host.calls)


@pytest.mark.parametrize(
    "listener,publisher", [(True, ""), (False, "[::]:8080->80/tcp")]
)
def test_iptv_stop_reports_partial_state_when_port_remains(listener, publisher):
    result = service_control.stop(
        "iptv-proxy", FakeDocker(state="exited", listener=listener, publisher=publisher)
    )
    assert not result["ok"]
    assert result["unit"] == "inactive"
    assert result["port_open"] is True


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
@pytest.mark.parametrize("code", [1, 124])
def test_iptv_command_failure_or_timeout_skips_later_steps(action, code):
    host = FakeDocker(failure=(code, "failed"))
    assert not getattr(service_control, action)("iptv-proxy", host)["ok"]
    assert len(host.calls) == 1


def test_iptv_restart_and_backend_queries_use_docker():
    host = FakeDocker()
    assert service_control.restart("iptv-proxy", host)["ok"]
    assert host.calls[0] == ["docker", "restart", "iptv-proxy"]
    assert service_control.status_args("iptv-proxy") == [
        "docker",
        "inspect",
        "--format",
        "{{.State.Status}}",
        "iptv-proxy",
    ]
    assert service_control.log_args("iptv-proxy") == [
        "docker",
        "logs",
        "--tail",
        "30",
        "iptv-proxy",
    ]
    with pytest.raises(ValueError):
        service_control.status_args("--all")
