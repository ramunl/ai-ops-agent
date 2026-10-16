"""ai-audit: the server's security stats and findings, as one JSON document.

``ai-audit check`` reads SSH attempts, fail2ban bans, sessions, public ports,
sshd settings, the firewall, pending security updates and secret-file
permissions (security_stats), then turns them into findings, each with the
exact fix. It only reads: nothing is changed here, and nothing leaves the
server. The ops bot reports it daily and on /security; the dashboard shows it.

Standard library only; no bot credentials. Installed as /usr/local/sbin/ai-audit.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from ai_ops_agent import security_stats as stats
from ai_ops_agent.backup import FILES

SECRETS = tuple(name for name in FILES if name.endswith(".env")) + (
    "/etc/ai-backup/key",
)
# Ports that are expected to be public on this server.
EXPECTED_PORTS = {80, 443}
LEVELS = ("high", "medium", "low")


AUTO_UPDATES = Path("/etc/apt/apt.conf.d/20auto-upgrades")
REBOOT_FLAG = Path("/run/reboot-required")


def collect(
    run: stats.Runner = stats.run,
    auto_updates_file: Path = AUTO_UPDATES,
    reboot_flag: Path = REBOOT_FLAG,
) -> dict:
    """Every reading, in one dictionary."""
    return {
        "ssh": stats.ssh_activity(run),
        "fail2ban": stats.fail2ban(run),
        "sessions": stats.sessions(run),
        "ports": stats.public_ports(run),
        "sshd": stats.sshd_settings(run),
        "firewall": stats.firewall(run),
        "security_updates": stats.security_updates(run),
        "auto_updates": stats.auto_updates(auto_updates_file),
        "reboot_required": reboot_flag.exists(),
        "loose_secrets": stats.loose_secrets(SECRETS),
    }


def _finding(level: str, title: str, detail: str, fix: str) -> dict:
    return {"level": level, "title": title, "detail": detail, "fix": fix}


def findings(data: dict) -> list[dict]:
    """What to fix, most serious first; [] when everything checked is fine."""
    found = []
    sshd = data["sshd"]
    failed = data["ssh"].get("failed", 0)
    if sshd.get("passwordauthentication") == "yes":
        found.append(
            _finding(
                "high",
                "SSH accepts passwords",
                f"{failed} failed SSH logins in 24 h; a guessed password is a full "
                "login. Check that your key works first.",
                "Set PasswordAuthentication no and KbdInteractiveAuthentication no "
                "in /etc/ssh/sshd_config, then: systemctl reload ssh",
            )
        )
    if sshd.get("permitrootlogin") == "yes":
        found.append(
            _finding(
                "high",
                "root can log in over SSH with a password",
                "PermitRootLogin is yes.",
                "Set PermitRootLogin prohibit-password (or no) in "
                "/etc/ssh/sshd_config, then: systemctl reload ssh",
            )
        )
    if data["loose_secrets"]:
        found.append(
            _finding(
                "high",
                "secret files readable by other users",
                ", ".join(data["loose_secrets"]),
                "chmod 600 <file> for each file listed",
            )
        )
    if not data["fail2ban"]["installed"]:
        found.append(
            _finding(
                "medium",
                "no fail2ban",
                f"{failed} failed SSH logins in 24 h go unpunished.",
                "apt install fail2ban (its sshd jail is on by default)",
            )
        )
    if data["firewall"] != "active":
        found.append(
            _finding(
                "medium",
                "firewall is off" if data["firewall"] == "inactive" else "no firewall",
                "Every listening port is reachable.",
                "ufw allow OpenSSH && ufw allow 80,443/tcp && ufw enable "
                "(allow your SSH port first, or you lock yourself out)",
            )
        )
    updates = data["security_updates"]
    if updates:
        found.append(
            _finding(
                "medium",
                f"{updates} security update(s) waiting",
                "Known vulnerabilities stay open until installed.",
                "/upgrade in this bot, or: apt upgrade",
            )
        )
    if not data["auto_updates"]:
        found.append(
            _finding(
                "low",
                "automatic security updates are off",
                "Security fixes wait for you.",
                "apt install unattended-upgrades && "
                "dpkg-reconfigure -plow unattended-upgrades",
            )
        )
    if data["reboot_required"]:
        found.append(
            _finding(
                "low",
                "a reboot is needed",
                "An installed update (often the kernel) is not running yet.",
                "/reboot in this bot when convenient",
            )
        )
    ssh_port = int(sshd.get("port", "22") or 22)
    for item in data["ports"]:
        if item["port"] in EXPECTED_PORTS or item["port"] == ssh_port:
            continue
        found.append(
            _finding(
                "low",
                f"port {item['port']}/{item['proto']} is public ({item['process']})",
                "Fine if you meant it (a VPN or proxy); otherwise close it.",
                f"bind {item['process']} to 127.0.0.1, or: ufw deny {item['port']}",
            )
        )
    return sorted(found, key=lambda item: LEVELS.index(item["level"]))


def check(run: stats.Runner = stats.run) -> dict:
    """The full report: stats plus findings."""
    data = collect(run)
    return {"ok": True, "at": time.time(), "stats": data, "findings": findings(data)}


def main(argv: list[str]) -> int:
    """ai-audit check: print the report as JSON."""
    if argv != ["check"]:
        print(json.dumps({"ok": False, "error": "usage: ai-audit check"}))
        return 2
    print(json.dumps(check()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
