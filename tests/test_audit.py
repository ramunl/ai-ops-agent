"""ai-audit: security stats and findings from fixed, read-only commands."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ai_ops_agent import audit, security_stats
from ai_ops_agent.bot import security

JOURNAL = """\
1792600001.000001 vps sshd[10]: Failed password for root from 203.0.113.5 port 4022 ssh2
1792600002.000001 vps sshd[10]: Failed password for invalid user admin from 203.0.113.5 port 4023 ssh2
1792600003.000001 vps sshd[11]: Invalid user oracle from 198.51.100.7 port 5000
1792600004.000001 vps sshd[10]: Failed password for root from 203.0.113.5 port 4024 ssh2
1792600005.000001 vps sshd[12]: Accepted publickey for roman from 192.0.2.10 port 51000 ssh2: ED25519 SHA256:abcdefghijklmnop
1792600009.000001 vps sshd[13]: Accepted publickey for roman from 192.0.2.10 port 51002 ssh2: ED25519 SHA256:abcdefghijklmnop
Accepted password for deploy from 192.0.2.11 port 51001 ssh2
"""
F2B = "Status\n|- Number of jail:\t1\n`- Jail list:\tsshd\n"
F2B_SSHD = (
    "Status for the jail: sshd\n|- Filter\n|  |- Currently failed:\t3\n"
    "|  |- Total failed:\t812\n`- Actions\n   |- Currently banned:\t7\n"
    "   |- Total banned:\t145\n"
)
WHO = "roman    pts/0        2026-10-16 10:01 (192.0.2.10)\n"
SS = (
    'tcp LISTEN 0 4096 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=1,fd=3))\n'
    'tcp LISTEN 0 4096 127.0.0.1:8080 0.0.0.0:* users:(("python3",pid=2,fd=3))\n'
    'tcp LISTEN 0 4096 *:443 *:* users:(("caddy",pid=3,fd=3))\n'
    'udp UNCONN 0 0 0.0.0.0:51820 0.0.0.0:* users:(("wg",pid=4,fd=3))\n'
)
SSHD = "port 22\npermitrootlogin yes\npasswordauthentication yes\nmaxauthtries 6\n"
APT = (
    "Inst openssl [3.0.2] (3.0.2-0ubuntu1.18 Ubuntu:22.04/jammy-security [amd64])\n"
    "Inst vim [2:8.2] (2:8.2.3995 Ubuntu:22.04/jammy-updates [amd64])\n"
)
OUTPUTS = {
    "journalctl": JOURNAL,
    "fail2ban-client status": F2B,
    "fail2ban-client status sshd": F2B_SSHD,
    "who": WHO,
    "ss": SS,
    "sshd": SSHD,
    "ufw": "Status: inactive\n",
    "apt-get": APT,
}


def fake_run(argv: list[str]) -> str | None:
    key = " ".join(argv) if argv[0] == "fail2ban-client" else argv[0]
    return OUTPUTS.get(key)


@pytest.fixture
def data(tmp_path):
    return audit.collect(
        fake_run,
        auto_updates_file=tmp_path / "missing",
        reboot_flag=tmp_path / "no-reboot",
    )


def test_ssh_attempts_are_counted_by_ip_and_user(data):
    ssh = data["ssh"]
    assert (ssh["failed"], ssh["failed_ips"], ssh["accepted"]) == (4, 2, 3)
    assert ssh["top_attackers"][0] == {"ip": "203.0.113.5", "count": 3}
    assert ssh["top_users"][0] == {"user": "root", "count": 2}
    assert ssh["password_logins"] == 1
    assert ssh["login_groups"][0] == {
        "user": "roman",
        "from": "192.0.2.10",
        "method": "publickey",
        "key": "ED25519 …ijklmnop",
        "count": 2,
        "last": 1792600009.000001,
    }
    assert ssh["login_groups"][1]["user"] == "deploy"
    assert ssh["login_groups"][1]["last"] is None  # a line without a time


def test_bans_sessions_and_public_ports(data):
    assert data["fail2ban"]["jails"] == [
        {"name": "sshd", "banned_now": 7, "banned_total": 145, "failed_total": 812}
    ]
    assert data["sessions"][0]["user"] == "roman"
    assert data["sessions"][0]["from"] == "192.0.2.10"
    ports = [(item["port"], item["process"]) for item in data["ports"]]
    assert ports == [(22, "sshd"), (443, "caddy"), (51820, "wg")]  # not 127.0.0.1
    assert data["security_updates"] == 1
    assert data["firewall"] == "inactive"


def test_findings_say_what_to_fix_most_serious_first(data):
    found = audit.findings(data)
    titles = [item["title"] for item in found]
    assert titles[:2] == [
        "SSH accepts passwords",
        "root can log in over SSH with a password",
    ]
    assert "firewall is off" in titles
    assert "1 security update(s) waiting" in titles
    assert "automatic security updates are off" in titles
    assert "port 51820/udp is public (wg)" in titles
    assert not any(
        "443" in title or "/tcp is public (sshd" in title for title in titles
    )
    assert [item["level"] for item in found] == sorted(
        (item["level"] for item in found), key=audit.LEVELS.index
    )
    assert "4 failed SSH logins" in found[0]["detail"]


def test_missing_tools_are_reported_not_crashed(tmp_path):
    data = audit.collect(
        lambda argv: None,
        auto_updates_file=tmp_path / "x",
        reboot_flag=tmp_path / "y",
    )
    assert data["ssh"] == {"available": False}
    assert data["fail2ban"] == {"installed": False, "jails": []}
    assert data["firewall"] == "missing"
    assert data["security_updates"] is None
    titles = [item["title"] for item in audit.findings(data)]
    assert "no fail2ban" in titles and "no firewall" in titles


def test_loose_secret_files_are_found(tmp_path):
    secret = tmp_path / "agent.env"
    secret.write_text("TOKEN=x")
    secret.chmod(0o644)
    tight = tmp_path / "key"
    tight.write_text("k")
    tight.chmod(0o600)
    loose = security_stats.loose_secrets((str(secret), str(tight), "/nope"))
    assert loose == [f"{secret} (0o644)"]


def test_cli_prints_json_and_refuses_other_arguments(capsys):
    assert audit.main(["fix"]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False
    with patch.object(audit, "check", return_value={"ok": True, "findings": []}):
        assert audit.main(["check"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "findings": []}


def test_report_message_has_stats_then_fixes(data):
    report = {"ok": True, "stats": data, "findings": audit.findings(data)}
    text = security.render(report)
    assert text.startswith("🛡 Security · ")
    assert (
        "SSH, 24 h: 4 failed logins from 2 IPs · 3 successful (1 by password)" in text
    )
    assert "✓ roman from 192.0.2.10 ×2 (publickey, key ED25519 …ijklmnop)" in text
    assert "Top attackers: 203.0.113.5 ×3, 198.51.100.7 ×1" in text
    assert "fail2ban: sshd 7 banned now, 145 in total" in text
    assert "Logged in now: 1 — roman from 192.0.2.10" in text
    assert "🔴 SSH accepts passwords" in text
    assert "Fix: Set PasswordAuthentication no" in text
    assert len(text) <= security.MESSAGE_LIMIT
    assert security.render({"ok": False, "error": "boom"}).startswith("⚠️")


def test_security_command_runs_the_audit(chat):
    report = {"ok": True, "stats": {}, "findings": []}
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat.message.chat_id),
        effective_user=SimpleNamespace(id=chat.message.chat_id),
        message=chat.message,
    )
    with (
        patch.object(security, "is_authorized", return_value=True),
        patch.object(security, "run_audit", AsyncMock(return_value=report)),
        patch.object(security, "reply", AsyncMock()) as reply,
    ):
        asyncio.run(security.security_cmd(update, None))
    assert "nothing to fix" in reply.await_args_list[-1].args[1]
