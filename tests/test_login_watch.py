"""SSH login alerts: learn quietly, then tell about new addresses and keys."""

import json

from ai_ops_agent import login_watch

NOW = 1_800_000_000.0


def line(at: float, user: str, ip: str, key: str = "SHA256:aaaaaaaaKEYONE11") -> str:
    return (
        f"{at:.6f} vps sshd[1]: Accepted publickey for {user} from {ip} "
        f"port 5000 ssh2: ED25519 {key}"
    )


class Journal:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.asked: list[list[str]] = []

    def __call__(self, argv: list[str]) -> str:
        self.asked.append(argv)
        since = float(argv[argv.index("--since") + 1].lstrip("@"))
        return "\n".join(x for x in self.lines if float(x.split()[0]) >= since)


def test_the_first_run_learns_without_alerting(tmp_path):
    journal = Journal()
    journal.lines.append(line(NOW - 10 * 86400, "roman", "192.0.2.10"))
    state = tmp_path / "state.json"
    assert login_watch.check(journal, state, NOW) == []
    assert "@" + str(int(NOW - 30 * 86400)) in journal.asked[0]
    saved = json.loads(state.read_text())
    assert "ip:192.0.2.10" in saved["known"] and saved["learned"]


def test_new_address_or_key_warns_and_known_ones_stay_quiet(tmp_path):
    journal = Journal()
    journal.lines.append(line(NOW - 10, "roman", "192.0.2.10"))
    state = tmp_path / "state.json"
    login_watch.check(journal, state, NOW)
    journal.lines += [
        line(NOW + 20, "roman", "192.0.2.10"),  # known: quiet
        line(NOW + 30, "roman", "203.0.113.9"),  # new address
        line(NOW + 40, "roman", "192.0.2.10", "SHA256:bbbbbbbbKEYTWO22"),  # new key
    ]
    alerts = login_watch.check(journal, state, NOW + 60)
    assert len(alerts) == 2
    assert alerts[0].startswith("⚠️ New SSH login: roman from 203.0.113.9")
    assert "First time for this address." in alerts[0]
    assert "First time for this key." in alerts[1]
    assert login_watch.check(journal, state, NOW + 120) == []  # not twice


def test_all_mode_reports_every_login_and_off_reports_none(tmp_path):
    journal = Journal()
    state = tmp_path / "state.json"
    login_watch.check(journal, state, NOW, mode="all")
    journal.lines.append(line(NOW + 10, "roman", "192.0.2.10"))
    journal.lines.append(line(NOW + 11, "roman", "192.0.2.10"))
    alerts = login_watch.check(journal, state, NOW + 60, mode="all")
    assert alerts[0].startswith("⚠️ New SSH login")
    assert alerts[1].startswith("🔐 SSH login: roman from 192.0.2.10")
    assert login_watch.check(journal, state, NOW + 120, mode="off") == []


def test_an_unreadable_journal_keeps_the_cursor(tmp_path):
    state = tmp_path / "state.json"
    login_watch.check(Journal(), state, NOW)
    before = state.read_text()
    assert login_watch.check(lambda argv: None, state, NOW + 60) == []
    assert state.read_text() == before


def test_a_flood_is_summarized(tmp_path):
    journal = Journal()
    state = tmp_path / "state.json"
    login_watch.check(journal, state, NOW)
    journal.lines += [line(NOW + i, "x", f"198.51.100.{i}") for i in range(1, 15)]
    alerts = login_watch.check(journal, state, NOW + 60)
    assert len(alerts) == login_watch.MAX_ALERTS + 1
    assert alerts[-1].startswith("… and 4 more SSH logins")
