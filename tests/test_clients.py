"""Client metrics count devices and inbound connections, not backend sockets."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from ai_ops_agent import client_sources as sources
from ai_ops_agent import clients
from ai_ops_agent.client_history import update


def test_sockets_exclude_backend_and_normalize_mapped_ipv6():
    text = "\n".join(
        [
            "0 0 10.0.0.1:9443 203.0.113.4:50000",
            "0 0 [::]:9443 [::ffff:203.0.113.4]:50001",
            "0 0 [::]:9443 [2001:db8::1]:50002",
            "0 0 10.0.0.1:50002 149.154.1.2:9443",  # outgoing
            "0 0 10.0.0.1:2398 203.0.113.5:1234",
        ]
    )
    result = sources.sockets(text, 9443)
    assert result["connections"] == 3
    assert result["source_addresses"] == 2
    assert sources.sockets("", 9443)["connections"] == 0
    assert not sources.sockets(None, 9443)["available"]


def test_vpn_uses_recent_handshakes_and_never_exposes_public_keys():
    def probe(argv):
        if argv[-1] == "latest-handshakes":
            return "awg0 PUBLICKEY1 900\nawg0 PUBLICKEY2 700\nawg0 PUBLICKEY3 0"
        if argv[-1] == "transfer":
            return "awg0 PUBLICKEY1 100 200\nawg0 PUBLICKEY2 30 40\nawg0 PUBLICKEY3 0 0"
        return json.dumps(
            [{"clientId": "PUBLICKEY1", "userData": {"clientName": "Phone"}}]
        )

    result = sources.vpn(probe, 1000)
    assert result["active"] == 1
    assert result["configured"] == 3
    assert result["rx_bytes"] == 130
    assert result["clients"][0]["name"] == "Phone"
    assert result["clients"][2]["last_seen_seconds"] is None
    assert "PUBLICKEY" not in json.dumps(result)
    assert not sources.vpn(lambda args: None, 1000)["available"]


def test_exporters_sum_labels_and_select_only_safe_fields():
    result = sources.exporter(
        """mtg_client_connections{ip_family="v4"} 2
mtg_client_connections{ip_family="v6"} 3
mtg_telegram_traffic{direction="from_client",dc="2"} 100
mtg_telegram_traffic{direction="to_client",dc="2"} 200
mtg_domain_fronting_traffic{direction="to_client"} 30
mtg_secret{secret="do-not-copy"} 99
mtg_bad 1e999
mtg_bad 1.2.3
""",
        "mtg",
    )
    assert result["protocol_connections"] == 5
    assert result["rx_bytes"] == 100
    assert result["tx_bytes"] == 230
    assert "do-not-copy" not in json.dumps(result)
    assert sources.exporter(None, "mtg") == {"metrics_available": False}
    assert not sources.exporter("garbage", "mtg")["metrics_available"]
    relay = sources.exporter("tproxy_sessions_live 4\ntproxy_streams_live 9", "tproxy")
    assert relay["active"] == 4
    assert relay["streams"] == 9
    assert "rx_bytes" not in relay


def sample(now, value, previous=None, available=True):
    item = {"id": "vpn", "active": 2, "available": available}
    if value is not None:
        item["rx_bytes"] = value
    return update({"services": [item]}, previous or {}, now)


def test_history_resets_midnight_and_never_spikes_after_counter_reset():
    first = sample(1000, 100)
    assert "rx_bps" not in first["services"][0]
    second = sample(1030, 400, first)
    item = second["services"][0]
    assert item["rx_bps"] == 10
    assert item["sample"]["daily"]["rx_bytes"] == 300
    reset = sample(1060, 5, second)["services"][0]
    assert "rx_bps" not in reset
    assert reset["sample"]["incomplete"]
    assert reset["sample"]["daily"]["rx_bytes"] == 300
    midnight = sample(86401, 500, second)["services"][0]
    assert midnight["sample"]["daily"] == {}
    assert "rx_bps" not in midnight


def test_missing_metrics_preserve_baseline_and_use_correct_elapsed_time():
    first = sample(1000, 100)
    missing = sample(1030, None, first, available=False)
    restored = sample(1060, 700, missing)["services"][0]
    assert restored["rx_bps"] == 10  # 600 bytes / 60s, not 30s
    stale = sample(1300, 900, {"services": [restored]})["services"][0]
    assert "rx_bps" not in stale
    assert stale["sample"]["incomplete"]


def test_atomic_history_survives_restart_without_real_probes():
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)

        def collect(*args):
            return [{"id": "vpn", "active": 0, "available": True, "rx_bytes": 100}]

        with patch.object(sources, "collect", collect):
            first = clients.check(directory)
            second = clients.check(directory)
        assert first["ok"] and second["ok"]
        assert json.loads((directory / "report.json").read_text())["ok"]
        assert not list(directory.glob(".report-*"))


def test_collect_isolates_missing_services_and_never_uses_a_shell():
    calls = []

    def probe(argv):
        calls.append(argv)
        return None

    result = sources.collect(probe, lambda url: None, 1000)
    assert len(result) == 6
    assert all(not item["available"] for item in result)
    assert all(isinstance(argv, list) for argv in calls)
    assert not any("dump" in argv for argv in calls)


def test_service_restart_does_not_turn_new_lifetime_into_traffic_spike():
    old = sample(1000, 100)
    old["services"][0]["epoch"] = "old-process"
    report = {
        "services": [
            {
                "id": "vpn",
                "available": True,
                "active": 1,
                "epoch": "new-process",
                "rx_bytes": 10000,
            }
        ]
    }
    item = update(report, old, 1030)["services"][0]
    assert "rx_bps" not in item
    assert item["sample"]["incomplete"]
    assert item["sample"]["daily"] == {}


def test_media_uses_container_namespace_and_combined_network_counters():
    calls = []

    def probe(argv):
        calls.append(argv)
        if argv[:2] == ["docker", "inspect"] and argv[-1] == "iptv-proxy":
            return "1234\n"
        if argv[:2] == ["nsenter", "-t"]:
            if argv[-1] == "/proc/net/dev":
                return "lo: 100 0 0 0 0 0 0 0 100 0\neth0: 200 0 0 0 0 0 0 0 300 0"
            return "0 0 172.17.0.2:8080 203.0.113.8:50000"
        return None

    result = next(
        item
        for item in sources.collect(probe, lambda url: None, 1000)
        if item["id"] == "media"
    )
    assert result["active"] == 1
    assert result["traffic_bytes"] == 500
    assert all(argv[2] == "1234" for argv in calls if argv[0] == "nsenter")


def test_accept_queue_includes_over_capacity_and_multiple_listeners():
    result = sources.listener_queue(
        "LISTEN 4097 4096 *:9443 *:*\nLISTEN 2 128 [::]:9443 [::]:*\nLISTEN 90 100 *:2398 *:*",
        9443,
    )
    assert result["accept_queue"] == 4099
    assert result["accept_queue_capacity"] == 4224
    assert result["accept_queue_full"] is True
    assert sources.listener_queue("LISTEN 0 4096 *:9443 *:*", 9443)["accept_queue"] == 0
    for text in (None, "", "LISTEN broken 4096 *:9443 *:*", "LISTEN 0 0 *:9443 *:*"):
        assert sources.listener_queue(text, 9443) == {"accept_queue_available": False}


def test_queue_peak_survives_recovery_but_resets_at_utc_day_boundary():
    def report(queue):
        return {
            "services": [
                {
                    "id": "mtg",
                    "available": True,
                    "active": 14,
                    "accept_queue_available": True,
                    "accept_queue": queue,
                }
            ]
        }

    first = update(report(4097), {}, 1000)
    recovered = update(report(0), first, 1030)
    assert recovered["services"][0]["sample"]["accept_queue_peak"] == 4097
    tomorrow = update(report(0), recovered, 86401)
    assert tomorrow["services"][0]["sample"]["accept_queue_peak"] == 0


def test_collect_reads_accept_queue_independently_of_native_metrics():
    def probe(args):
        if args == ["ss", "-Hltn"]:
            return "LISTEN 4097 4096 *:9443 *:*"
        if args == ["ss", "-Htn", "state", "close-wait"]:
            return "0 0 10.0.0.1:9443 203.0.113.1:1234"
        if args == ["ss", "-Htn", "state", "established"]:
            return ""
        return None

    item = next(
        x for x in sources.collect(probe, lambda _: None, 1000) if x["id"] == "mtg"
    )
    assert item["connections"] == 0
    assert item["accept_queue"] == 4097
    assert item["accept_queue_full"] is True
    assert item["close_wait_connections"] == 1
    assert item["metrics_available"] is False
