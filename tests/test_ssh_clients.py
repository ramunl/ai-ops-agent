"""SSH totals must include non-terminal logins but exclude pre-auth processes."""

from ai_ops_agent.ssh_clients import collect


def test_authenticated_children_only_and_multiple_ports():
    processes = """1 0 sshd sshd: /usr/sbin/sshd -D [listener]
2 1 sshd sshd: root [priv]
3 2 sshd sshd: root@notty
4 1 sshd sshd: unknown [priv]
5 4 sshd sshd: unknown [net]
6 1 sshd-session sshd-session: roman@pts/0
7 1 other sshd: fake@notty
8 1 sshd sshd: [accepted]
"""

    def probe(argv):
        return processes if argv[0] == "ps" else "port 22\nport 2222\n"

    result = collect(probe)
    assert result["active"] == 2
    assert result["ports"] == [22, 2222]
    assert result["clients"][0]["name"] == "root · notty"
    assert result["clients"][1]["name"] == "roman · pts/0"


def test_unknown_is_distinct_from_no_authenticated_connections():
    assert not collect(lambda argv: None)["available"]
    result = collect(
        lambda argv: "1 0 sshd sshd: listener" if argv[0] == "ps" else "port 22"
    )
    assert result["available"]
    assert result["active"] == 0
