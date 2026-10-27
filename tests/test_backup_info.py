"""Source-level backup descriptions must never inspect credential values."""

from pathlib import Path
from unittest.mock import patch

from ai_ops_agent import backup
from ai_ops_agent.backup_info import format_backup_info, source_label


def test_known_sources_have_purposes_and_actual_dispositions():
    result = {
        "ok": True,
        "included": list(backup.FILES[:6]),
        "skipped": list(backup.FILES[6:]),
    }
    text = format_backup_info(result)
    included, skipped = text.split("Skipped (missing):")
    for source in result["included"]:
        assert source in included
    for source in result["skipped"]:
        assert source not in included
        assert source in skipped
    for source in backup.FILES:
        assert "unknown" not in source_label(source)
    assert "Coding agent credentials" in text
    assert "PM agent credentials" in text
    assert "Ops agent credentials" in text
    assert "Dashboard credentials" in text
    assert "Individual credentials are not verified" in text


def test_docker_and_unknown_extras_are_sources_not_files():
    text = format_backup_info(
        {
            "ok": True,
            "included": ["docker:vpn:/opt/vpn", "/custom/key.env"],
            "skipped": [],
        }
    )
    assert "Included: 2 sources" in text
    assert (
        "Docker: archived directory plus container metadata (inspect.json): docker:vpn:/opt/vpn"
        in text
    )
    assert "Extra path (file or directory; contents unknown): /custom/key.env" in text
    assert "Skipped (missing): 0 sources\nNone" in text


def test_key_exclusion_is_default_not_absolute():
    for sources in ([], ["/etc/ai-backup/key"], ["/etc"]):
        text = format_backup_info({"ok": True, "included": sources})
        assert "excluded by default" in text
        assert "Extra paths can include it directly or through a directory" in text
        assert "Save the key separately" in text
        if "/etc/ai-backup/key" in sources:
            assert (
                "Backup encryption key (explicit extra path): /etc/ai-backup/key"
                in text
            )


def test_actual_archive_results_do_not_disclose_secrets(tmp_path):
    secret = tmp_path / "agent.env"
    secret.write_text("API_KEY=never-display-this")
    key = tmp_path / "key"
    key.write_text("never-display-passphrase")
    extra = tmp_path / "extras"
    extra.write_text(str(key))
    paths = backup.Paths(
        key=key,
        extra=extra,
        archives=tmp_path / "archives",
        files=(str(secret), str(tmp_path / "missing")),
    )
    result = backup.run(paths, lambda data, key, target: target.write_bytes(data))
    with patch.object(Path, "open", side_effect=AssertionError("must not read files")):
        text = format_backup_info(result)
    assert str(secret) in text
    assert str(key) in text
    assert str(tmp_path / "missing") in text
    assert "never-display" not in text
    assert "API_KEY=" not in text


def test_failed_result_does_not_claim_archive_contents():
    assert (
        format_backup_info({"ok": False, "error": "failed", "included": ["/stale"]})
        == "⚠️ Backup failed: failed"
    )
