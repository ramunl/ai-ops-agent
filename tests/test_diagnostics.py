"""Read-only diagnostics never accept paths, shell commands or raw secrets."""

import json
from unittest.mock import patch

import pytest

from ai_ops_agent import diagnostics


def test_fixed_commands_and_redaction(tmp_path):
    checks = tmp_path / "checks.json"
    checks.write_text(
        json.dumps({"packages": {"report": {"ok": False, "error": "402 token=hidden"}}})
    )
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {"targets": {"ai-ops-agent": {"status": "healthy", "private": "NEVER"}}}
        )
    )
    calls = []

    def run(args, timeout):
        calls.append(args)
        return 0, "configured-secret Bearer super-secret\nHTTP 402 Payment Required"

    with (
        patch.object(diagnostics, "CHECKS", checks),
        patch.object(diagnostics, "DEPLOYMENTS", state),
        patch.object(diagnostics, "secret_values", return_value=["configured-secret"]),
    ):
        report = diagnostics.collect("ai-ops-agent", run)
    text = json.dumps(report)
    for secret in ["configured-secret", "super-secret", "hidden", "NEVER"]:
        assert secret not in text
    assert "402" in text
    assert report["read_only"] is True
    assert [call[0] for call in calls] == ["systemctl", "journalctl"]
    assert calls[0][1] == "show"
    assert calls[1][2] == "ai-ops-agent"


def test_rejects_unknown_targets_before_reading_or_running():
    with patch.object(diagnostics, "secret_values") as secrets:
        for value in ["/etc/shadow", "ai-ops-agent;reboot", "--all"]:
            with pytest.raises(ValueError):
                diagnostics.collect(value)
        secrets.assert_not_called()
    assert diagnostics.main(["ai-ops-agent", "restart"]) == 2


def test_logs_are_bounded_and_missing_evidence_is_explicit(tmp_path):
    assert len(diagnostics.sanitize("x" * 10000, [])) <= 600
    assert len(diagnostics.sanitize("line\n" * 1000, []).splitlines()) <= 40
    assert "unavailable" in diagnostics.read_state(tmp_path / "missing")
    assert "[redacted]" in diagnostics.sanitize("https://user:pass@example.test", [])
