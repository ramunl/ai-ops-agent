"""ai-backup: secrets and state, encrypted, kept off the server via the bot."""

import asyncio
import html
import io
import json
import subprocess
import tarfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ai_ops_agent import backup
from ai_ops_agent.bot import backup as bot_backup

REPO = Path(__file__).resolve().parent.parent


def plain_copy(data: bytes, key: Path, target: Path) -> None:
    """Stand-in for openssl: write the tar.gz as is, so tests can read it."""
    target.write_bytes(data)


@pytest.fixture
def paths(tmp_path):
    secret = tmp_path / "etc" / "agent.env"
    secret.parent.mkdir()
    secret.write_text("TELEGRAM_BOT_TOKEN=123:abc\n")
    state = tmp_path / "var" / "state.json"
    state.parent.mkdir()
    state.write_text('{"tasks": []}')
    key = tmp_path / "key"
    key.write_text("passphrase\n")
    return backup.Paths(
        key=key,
        extra=tmp_path / "extra-paths",
        archives=tmp_path / "archives",
        status=tmp_path / "status" / "status.json",
        lock=tmp_path / "lock",
        files=(str(secret), str(state), str(tmp_path / "missing.json")),
    )


def test_run_packs_the_files_and_reports_what_was_missing(paths):
    result = backup.run(paths, plain_copy)
    assert result["ok"] is True
    assert result["skipped"] == [paths.files[2]]
    with tarfile.open(fileobj=io.BytesIO(Path(result["file"]).read_bytes())) as archive:
        names = sorted(archive.getnames())
        secret = archive.extractfile(paths.files[0].lstrip("/")).read()
    assert names == sorted(name.lstrip("/") for name in paths.files[:2])
    assert secret == b"TELEGRAM_BOT_TOKEN=123:abc\n"


def test_extra_paths_are_added_but_comments_and_relative_paths_ignored(paths, tmp_path):
    proxy = tmp_path / "proxy.conf"
    proxy.write_text("secret=deadbeef")
    paths.extra.write_text(f"# proxy\n{proxy}\nrelative/path\n\n")
    assert backup.wanted_paths(paths)[-1] == str(proxy)
    assert "relative/path" not in backup.wanted_paths(paths)
    assert str(proxy) in backup.run(paths, plain_copy)["included"]


def test_without_a_key_nothing_is_written(paths):
    paths.key.unlink()
    result = backup.run(paths, plain_copy)
    assert result["ok"] is False
    assert "install-ai-backup" in result["error"]
    assert not paths.archives.exists()


def test_only_the_newest_archives_are_kept(paths):
    paths.archives.mkdir()
    for day in range(10):
        (paths.archives / f"ai-backup-202610{day:02d}-040000.tar.gz.enc").write_text(
            "x"
        )
    backup.prune(paths.archives)
    kept = sorted(path.name for path in paths.archives.iterdir())
    assert kept == [
        f"ai-backup-202610{day:02d}-040000.tar.gz.enc" for day in range(3, 10)
    ]


def test_archives_are_readable_by_root_only(paths):
    result = backup.run(paths, plain_copy)
    assert Path(result["file"]).stat().st_mode & 0o077 == 0


def test_real_openssl_round_trip(paths, tmp_path):
    result = backup.run(paths)  # the system openssl
    encrypted = Path(result["file"]).read_bytes()
    assert b"TELEGRAM_BOT_TOKEN" not in encrypted
    decrypted = subprocess.run(
        ["openssl", "enc", "-d", *backup.CIPHER[:-1], "-pass", f"file:{paths.key}"],
        input=encrypted,
        capture_output=True,
        check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(decrypted)) as archive:
        assert (
            archive.extractfile(paths.files[0].lstrip("/"))
            .read()
            .startswith(b"TELEGRAM")
        )


def test_cli_status_root_and_lock(paths, capsys):
    assert backup.main(["status"], paths) == 0
    assert json.loads(capsys.readouterr().out)["last_ok"] is None
    assert backup.main(["restore"], paths) == 2
    capsys.readouterr()
    with patch.object(backup.os, "geteuid", return_value=1000):
        assert backup.main(["run"], paths, plain_copy) == 1
    capsys.readouterr()
    with patch.object(backup.os, "geteuid", return_value=0):
        assert backup.main(["run"], paths, plain_copy) == 0
        ok = json.loads(capsys.readouterr().out)
        paths.key.unlink()
        assert backup.main(["run"], paths, plain_copy) == 1
        capsys.readouterr()
        with backup.exclusive(paths.lock) as held:
            assert held
            assert backup.main(["run"], paths, plain_copy) == 1
    saved = backup.status(paths)
    assert saved["last"]["ok"] is False  # the run without a key
    assert saved["last_ok"]["file"] == ok["file"]  # the last good one is kept


def test_wrapper_runs_without_the_bot_environment():
    done = subprocess.run(
        [str(REPO / "deploy" / "ai-backup"), "bogus"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "AI_OPS_AGENT_DIR": str(REPO)},
        check=False,
    )
    assert done.returncode == 2
    assert "usage" in json.loads(done.stdout)["error"]


# ---------------------------------------------------------------- the bot side


def test_next_run_is_tonight_or_tomorrow():
    assert bot_backup.seconds_until(4, datetime(2026, 10, 12, 3, 30)) == 1800
    assert bot_backup.seconds_until(4, datetime(2026, 10, 12, 4, 0)) == 86400
    assert bot_backup.seconds_until(4, datetime(2026, 10, 12, 23, 0)) == 5 * 3600


def test_caption_is_concise_and_counts_sources():
    text = bot_backup.caption(
        {"bytes": 4096, "included": ["/a", "/b"], "skipped": ["/etc/caddy/Caddyfile"]}
    )
    assert text.startswith("🗄 Backup · 2 sources · 4 KB")
    assert "Skipped: 1 sources" in text
    assert "/etc/caddy/Caddyfile" not in text
    assert len(bot_backup.caption({"skipped": ["/x" * 900]})) <= 1024


def test_send_backup_sends_the_file_or_the_reason(tmp_path):
    archive = tmp_path / "ai-backup-1.tar.gz.enc"
    archive.write_bytes(b"encrypted")
    bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())
    ok = {
        "ok": True,
        "file": str(archive),
        "bytes": 9,
        "included": ["/a"],
        "skipped": [],
    }
    asyncio.run(bot_backup.send_backup(bot, bot_backup.config.AUTHORIZED_CHAT_ID, ok))
    call = bot.send_document.await_args
    assert (call.args[0], call.kwargs["filename"]) == (
        bot_backup.config.AUTHORIZED_CHAT_ID,
        archive.name,
    )
    asyncio.run(
        bot_backup.send_backup(
            bot, bot_backup.config.AUTHORIZED_CHAT_ID, {"ok": False, "error": "no key"}
        )
    )
    assert "Restore: openssl enc -d" in bot.send_message.await_args_list[0].args[1]
    assert bot.send_message.await_args.args[1] == "⚠️ Backup failed: no key"
    assert bot.send_document.await_count == 1


def test_make_backup_reads_failures_too():
    failed = '[exit 1] {"ok": false, "error": "a backup is already running"}'
    with patch.object(bot_backup, "run_command", AsyncMock(return_value=failed)):
        result = asyncio.run(bot_backup.make_backup())
    assert result == {"ok": False, "error": "a backup is already running"}
    with patch.object(
        bot_backup, "run_command", AsyncMock(return_value="sh: not found")
    ):
        assert asyncio.run(bot_backup.make_backup())["error"] == "sh: not found"


def test_container_config_is_copied_fresh_into_each_backup(paths):
    paths.extra.write_text("docker:amnezia-awg2:/opt/amnezia\n")
    with patch("ai_ops_agent.backup_containers.subprocess.run") as run:
        run.side_effect = [
            SimpleNamespace(returncode=0, stdout=b"fresh container tar"),
            SimpleNamespace(returncode=0, stdout=b'[{"Name":"amnezia-awg2"}]'),
        ]
        result = backup.run(paths, plain_copy)
    assert "docker:amnezia-awg2:/opt/amnezia" in result["included"]
    with tarfile.open(result["file"]) as archive:
        assert (
            archive.extractfile("containers/amnezia-awg2/opt_amnezia.tar").read()
            == b"fresh container tar"
        )
    assert run.call_args_list[0].args[0] == [
        "docker",
        "cp",
        "amnezia-awg2:/opt/amnezia",
        "-",
    ]


def test_missing_container_fails_backup_instead_of_silently_skipping(paths):
    paths.extra.write_text("docker:amnezia-awg2:/opt/amnezia\n")
    with patch("ai_ops_agent.backup_containers.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=1, stdout=b"")
        with pytest.raises(OSError, match="Container backup failed"):
            backup.run(paths, plain_copy)
    assert not paths.archives.exists()


def test_long_details_are_complete_bounded_and_escaped(tmp_path):
    archive = tmp_path / "backup.enc"
    archive.write_bytes(b"encrypted")
    result = {
        "ok": True,
        "file": str(archive),
        "included": [f"/extra/{n}/<&>😀" for n in range(200)] + ["/<&" * 3000],
        "skipped": [f"/missing/{n}" for n in range(200)],
    }
    bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())
    asyncio.run(
        bot_backup.send_backup(bot, bot_backup.config.AUTHORIZED_CHAT_ID, result)
    )
    messages = bot.send_message.await_args_list
    assert len(messages) > 2
    for call in messages:
        assert call.args[0] == bot_backup.config.AUTHORIZED_CHAT_ID
        assert call.kwargs["parse_mode"] == "HTML"
        assert len(call.args[1].encode("utf-16-le")) // 2 <= 4000
        assert "<" not in call.args[1]
    restored = "".join(html.unescape(call.args[1]) for call in messages)
    assert (
        restored
        == bot_backup.format_backup_info(result) + "\n\nRestore: " + bot_backup.RESTORE
    )


def test_failed_backup_is_escaped_without_document_or_inventory():
    bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())
    result = {"ok": False, "error": "<failure>&" * 1000, "included": ["/stale"]}
    asyncio.run(
        bot_backup.send_backup(bot, bot_backup.config.AUTHORIZED_CHAT_ID, result)
    )
    bot.send_document.assert_not_awaited()
    text = "".join(html.unescape(c.args[1]) for c in bot.send_message.await_args_list)
    assert text == "⚠️ Backup failed: " + result["error"]
    assert all("<" not in c.args[1] for c in bot.send_message.await_args_list)


def test_non_owner_cannot_receive_backup_or_trigger_command():
    bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())
    asyncio.run(bot_backup.send_backup(bot, -999, {"ok": True, "file": "/not-opened"}))
    bot.send_document.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    with (
        patch.object(bot_backup, "is_authorized", return_value=False),
        patch.object(bot_backup, "make_backup", AsyncMock()) as make,
    ):
        asyncio.run(bot_backup.backup_cmd(SimpleNamespace(), SimpleNamespace()))
    make.assert_not_awaited()


def test_manual_and_nightly_use_same_owner_delivery(chat):
    result = {"ok": True, "included": ["/example"], "skipped": []}
    chat.effective_chat = SimpleNamespace(id=bot_backup.config.AUTHORIZED_CHAT_ID)
    application = SimpleNamespace(bot=object())
    with (
        patch.object(bot_backup, "is_authorized", return_value=True),
        patch.object(bot_backup, "make_backup", AsyncMock(return_value=result)),
        patch.object(bot_backup, "send_backup", AsyncMock()) as send,
    ):
        asyncio.run(bot_backup.backup_cmd(chat, application))
        send.assert_awaited_once_with(application.bot, chat.effective_chat.id, result)
        send.reset_mock()
        with patch.object(
            bot_backup.asyncio,
            "sleep",
            AsyncMock(side_effect=[None, asyncio.CancelledError]),
        ):
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(bot_backup.backup_forever(application))
        send.assert_awaited_once_with(application.bot, chat.effective_chat.id, result)


@pytest.mark.parametrize(
    "output", ['[exit 1] {"ok": true}', "Command timed out", "(exit 1)"]
)
def test_command_failure_never_becomes_success(output):
    with patch.object(bot_backup, "run_command", AsyncMock(return_value=output)) as run:
        assert asyncio.run(bot_backup.make_backup())["ok"] is False
    run.assert_awaited_once_with([bot_backup.BACKUP_COMMAND, "run"], timeout=300)
