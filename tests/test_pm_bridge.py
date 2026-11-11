"""PM bridge contract, resource bounds and diagnostic privacy."""

import json
import sys

import pytest

from ai_ops_agent.deployment.failure_events import new_failure
from ai_ops_agent.deployment.model import Target
from ai_ops_agent.deployment.pm_bridge import (
    BRIDGE,
    MAX_OUTPUT,
    MAX_REQUEST,
    TIMEOUT,
    BridgeError,
    PMBridge,
    build_ticket,
)
from ai_ops_agent.shell import run_json


@pytest.fixture
def event(tmp_path):
    return new_failure(
        operation="job-1",
        target=Target("agent", tmp_path / "repo", tmp_path / "venv"),
        stage="verify",
        requested_ref="main",
        requested_commit="a" * 40,
        previous_revision="b" * 40,
        exit_code=217,
        status="status=217/USER Environment=SECRET --token=SECRET",
        journal="Traceback (most recent call last):\nValueError: SECRET\n"
        + "SECRET\n" * 20000,
    )


class Contract:
    """Mirror PM read/add limits, revision checks and matching-ID/text replay."""

    def __init__(self):
        self.items = []
        self.revision = 0
        self.calls = []
        self.conflicts = 0
        self.offline = False
        self.lose_response = False
        self.published = True

    def __call__(self, argv, raw, timeout, max_output):
        assert argv == [BRIDGE]
        assert timeout == TIMEOUT and max_output == MAX_OUTPUT
        assert len(raw) <= MAX_REQUEST
        request = json.loads(raw)
        self.calls.append(request)
        if self.offline:
            return "Error: SECRET"
        if request["action"] == "add":
            text = request["text"]
            assert 0 < len(text) <= 10000
            assert "\n" not in text and "\r" not in text and "<!-- pm-task:" not in text
            existing = next((x for x in self.items if x["id"] == request["id"]), None)
            if existing:
                if existing["text"] != text.strip():
                    return json.dumps(
                        {"ok": False, "conflict": True, "error": "SECRET"}
                    )
            elif self.conflicts or request["revision"] != f"{self.revision:064x}":
                self.conflicts = max(0, self.conflicts - 1)
                self.revision += 1
                return json.dumps({"ok": False, "conflict": True, "error": "SECRET"})
            else:
                self.items.append({"id": request["id"], "text": text.strip()})
                self.revision += 1
            if self.lose_response:
                self.lose_response = False
                return "Command timed out SECRET"
        return json.dumps(
            {
                "ok": True,
                "published": self.published,
                "warning": "SECRET",
                "workspace": {
                    "project": request["project"],
                    "revision": f"{self.revision:064x}",
                    "items": self.items,
                },
            }
        )


def test_sanitized_payload_and_prestart(event, caplog):
    ticket = build_ticket(event)
    remote = Contract()
    assert PMBridge(remote).add(ticket) == ticket["id"]
    serialized = json.dumps(remote.calls)
    assert "SECRET" not in serialized + caplog.text
    assert "Python traceback (source omitted)" in ticket["text"]
    assert "diagnostic input truncated" in ticket["text"]
    assert "systemd: 217/USER" in ticket["text"]
    assert "Environment" not in serialized
    assert len(serialized) < 32768


def test_conflicts_and_unpublished_saved_id(event):
    remote = Contract()
    remote.conflicts = 2
    remote.published = False
    ticket = build_ticket(event)
    assert PMBridge(remote).add(ticket) == ticket["id"]
    assert len(remote.calls) == 6
    assert PMBridge(remote).add(ticket) == ticket["id"]
    assert len(remote.items) == 1
    remote.items.clear()
    remote.conflicts = 10
    with pytest.raises(BridgeError, match="revision conflict"):
        PMBridge(remote).add(ticket)


@pytest.mark.parametrize(
    "raw",
    [
        "SECRET",
        "[]",
        "null",
        '{"ok":1}',
        '{"ok":true}',
        '{"ok":false,"error":"SECRET"}',
        "[exit 1] SECRET",
        "Command timed out SECRET",
        "x" * (MAX_OUTPUT + 1),
    ],
)
def test_bad_responses_are_safe(event, raw, caplog):
    with pytest.raises(BridgeError) as error:
        PMBridge(lambda *args: raw).add(build_ticket(event))
    assert "SECRET" not in str(error.value) + caplog.text


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(published="false"),
        lambda r: r["workspace"].update(project="other"),
        lambda r: r["workspace"].update(revision="bad"),
        lambda r: r["workspace"].update(items=[]),
        lambda r: r["workspace"]["items"][0].update(text="SECRET"),
    ],
)
def test_invalid_confirmations(event, change):
    remote = Contract()

    def run(*args):
        response = json.loads(remote(*args))
        if json.loads(args[1])["action"] == "add":
            change(response)
        return json.dumps(response)

    with pytest.raises(BridgeError):
        PMBridge(run).add(build_ticket(event))


def test_real_runner_bounds_and_no_stderr_leak(caplog):
    for script, timeout in [
        ("import sys; print('SECRET'); sys.exit(1)", 1),
        ("import time; time.sleep(5)", 0.05),
        ("print('SECRET' * 10000)", 1),
        ("import sys; sys.stdout.buffer.write(b'\\xff')", 1),
    ]:
        result = run_json([sys.executable, "-c", script], "{}", timeout, 100)
        assert result.startswith("Error:") and "SECRET" not in result
    assert (
        run_json(
            [
                sys.executable,
                "-c",
                "import sys; print(sys.stdin.read()); print('SECRET', file=sys.stderr)",
            ],
            "{}",
            1,
            100,
        )
        == "{}\n"
    )
    assert "SECRET" not in caplog.text


def test_request_limit_and_text_constraints(event):
    ticket = build_ticket(event)
    remote = Contract()
    # Valid PM text can still exceed the JSON CLI character budget after escaping.
    with pytest.raises(BridgeError, match="too large"):
        PMBridge(remote).add({**ticket, "text": "\u2603" * 10000})
    assert [call["action"] for call in remote.calls] == ["read"]
    for text in ("x" * 10001, "line\nbreak", "line\rbreak", "<!-- pm-task:{} -->"):
        with pytest.raises(ValueError, match="Invalid PM ticket"):
            PMBridge(remote).add({**ticket, "text": text})


def test_unsafe_diagnostics_rejected(event):
    from ai_ops_agent.deployment.failure_events import FailureRecordError

    with pytest.raises(FailureRecordError):
        build_ticket({**event, "diagnostics": ["ValueError: SECRET"]})


@pytest.mark.parametrize(
    "status, journal, expected",
    [
        ("status=203/EXEC --token=SECRET", "", "systemd: 203/EXEC"),
        ("Failed at step USER SECRET", "", "systemd: 217/USER"),
        (
            "",
            "ai-coding-runner: not found SECRET",
            "missing executable: ai-coding-runner",
        ),
    ],
)
def test_prestart_diagnostics(event, status, journal, expected):
    from ai_ops_agent.deployment.failure_diagnostics import parse_diagnostics

    event["diagnostics"] = list(parse_diagnostics(status, journal))
    ticket = build_ticket(event)
    assert expected in ticket["text"] and "SECRET" not in ticket["text"]


def test_runner_exception_does_not_expose_command_or_output(event):
    import subprocess

    def timeout(*args):
        raise subprocess.TimeoutExpired("SECRET", 30, output="SECRET")

    with pytest.raises(BridgeError) as error:
        PMBridge(timeout).add(build_ticket(event))
    assert "SECRET" not in str(error.value)
    assert error.value.__suppress_context__
