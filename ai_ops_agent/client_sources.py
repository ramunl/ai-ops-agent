"""Fixed, read-only service probes; never read VPN keys or proxy credentials."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import subprocess
import urllib.request
from collections import Counter
from collections.abc import Callable

from ai_ops_agent import ssh_clients

Runner = Callable[[list[str]], str | None]
Fetcher = Callable[[str], str | None]
ACTIVE_SECONDS = 180


def run(argv: list[str]) -> str | None:
    """Run a fixed probe with a short deadline and no shell."""
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=5, check=False
        )
        return result.stdout if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def fetch(url: str) -> str | None:
    """Read only a fixed loopback exporter, ignoring environment HTTP proxies."""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=3) as response:
            return response.read(1_000_000).decode()
    except (OSError, UnicodeError):
        return None


def metrics(text: str) -> list[tuple[str, str, float]]:
    """Parse finite nonnegative Prometheus values, preserving metric labels."""
    found = []
    for line in text.splitlines():
        match = re.fullmatch(r"([a-zA-Z_:][\w:]*)(\{.*\})?\s+([\d.eE+\-]+)", line)
        if match:
            try:
                value = float(match[3])
            except ValueError:
                continue
            if math.isfinite(value) and value >= 0:
                found.append((match[1], match[2] or "", value))
    return found


def sockets(text: str | None, port: int | set[int]) -> dict:
    """Count only established inbound sockets on the service's listening port."""
    if text is None:
        return {"available": False, "error": "Connection probe unavailable"}
    peers: Counter[str] = Counter()
    wanted = {str(port)} if isinstance(port, int) else {str(p) for p in port}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        local, remote = fields[2:4]  # ss -Htn state established omits state
        if local.rpartition(":")[2] not in wanted:
            continue
        address = remote.rpartition(":")[0].strip("[]")
        try:
            ip = ipaddress.ip_address(address)
            address = str(ip.ipv4_mapped or ip) if ip.version == 6 else str(ip)
        except ValueError:
            continue
        peers[address] += 1
    return {
        "available": True,
        "connections": sum(peers.values()),
        "source_addresses": len(peers),
        "clients": [{"name": ip, "connections": n} for ip, n in peers.most_common(50)],
        "details_truncated": len(peers) > 50,
    }


def vpn(probe: Runner, now: float) -> dict:
    """Read peer handshakes and byte counters without requesting private keys."""
    base = ["docker", "exec", "amnezia-awg2"]
    handshakes = probe([*base, "awg", "show", "all", "latest-handshakes"])
    transfer = probe([*base, "awg", "show", "all", "transfer"])
    if handshakes is None or transfer is None:
        return {"available": False, "error": "VPN peer counters unavailable"}
    names = {}
    metadata = probe([*base, "cat", "/opt/amnezia/awg/clientsTable"])
    try:
        for item in json.loads(metadata or "[]"):
            names[item["clientId"]] = str(item["userData"].get("clientName", ""))[:80]
    except (ValueError, KeyError, TypeError, AttributeError):
        pass  # labels are optional; stable anonymous IDs still work
    traffic = {}
    for line in transfer.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
            traffic[(parts[0], parts[1])] = (int(parts[2]), int(parts[3]))
    clients = []
    for line in handshakes.splitlines():
        parts = line.split()
        if len(parts) != 3 or not parts[2].isdigit():
            continue
        interface, key, stamp = parts
        identity = hashlib.sha256(key.encode()).hexdigest()[:10]
        age = max(0, now - int(stamp)) if int(stamp) else None
        received, sent = traffic.get((interface, key), (None, None))
        clients.append(
            {
                "name": names.get(key) or f"Device {identity}",
                "id": identity,
                "active": age is not None and age <= ACTIVE_SECONDS,
                "last_seen_seconds": age,
                "rx_bytes": received,
                "tx_bytes": sent,
            }
        )
    active = sum(item["active"] for item in clients)
    complete = all(item["rx_bytes"] is not None for item in clients)
    return {
        "available": True,
        "active": active,
        "configured": len(clients),
        "unit": "recently active devices",
        "window_seconds": ACTIVE_SECONDS,
        "clients": clients[:50],
        "details_truncated": len(clients) > 50,
        "rx_bytes": sum(x[0] for x in traffic.values()) if complete else None,
        "tx_bytes": sum(x[1] for x in traffic.values()) if complete else None,
        "note": "Active = handshake within 3 minutes; idle devices may disappear. "
        "Traffic is from the server's perspective. Shared profiles count as one.",
    }


def exporter(text: str | None, prefix: str) -> dict:
    """Select only known metrics, never arbitrary exporter labels or secrets."""
    if text is None:
        return {"metrics_available": False}
    values = metrics(text)
    if not any(name.startswith(prefix + "_") for name, _, _ in values):
        return {"metrics_available": False}
    totals: Counter[str] = Counter()
    for name, _, value in values:
        totals[name] += value
    if prefix == "tproxy":
        wanted = {
            "active": "sessions_live",
            "streams": "streams_live",
            "rx_bytes": "bytes_up_total",
            "tx_bytes": "bytes_down_total",
            "sessions_total": "sessions_created_total",
            "streams_total": "streams_opened_total",
            "rejected_total": "streams_rejected_total",
            "dial_failures_total": "backend_dial_failures_total",
            "limit_hits_total": "limit_hits_total",
            "pending_bytes": "pending_bytes",
        }
        result = {
            key: int(totals[prefix + "_" + suffix])
            for key, suffix in wanted.items()
            if prefix + "_" + suffix in totals
        }
    else:
        result = {
            "protocol_connections": int(totals["mtg_client_connections"]),
            "fronting_connections": int(totals["mtg_domain_fronting_connections"]),
            "rejected_total": int(totals["mtg_concurrency_limited"]),
            "replay_total": int(totals["mtg_replay_attacks"]),
        }
        for key, direction in (("rx_bytes", "from_client"), ("tx_bytes", "to_client")):
            result[key] = int(
                sum(
                    value
                    for name, labels, value in values
                    if name in ("mtg_telegram_traffic", "mtg_domain_fronting_traffic")
                    and f'direction="{direction}"' in labels
                )
            )
    return {"metrics_available": True, **result}


def collect(probe: Runner, http: Fetcher, now: float) -> list[dict]:
    """Collect the known services, isolating missing tools per service."""
    host = probe(["ss", "-Htn", "state", "established"])
    listeners = probe(["ss", "-Hltn"])
    ports = {
        line.split()[3].rpartition(":")[2]
        for line in (listeners or "").splitlines()
        if len(line.split()) >= 5
    }
    services = [{"id": "vpn", "name": "Amnezia VPN", **vpn(probe, now)}]
    services[0]["epoch"] = probe(
        ["docker", "inspect", "--format", "{{.State.StartedAt}}", "amnezia-awg2"]
    )
    for identity, name, port in (
        ("mtproxy", "Telegram MTProxy", 2398),
        ("mtg", "Telegram mtg", 9443),
        ("relay", "HTTPS relay", 18080),
    ):
        item = {"id": identity, "name": name, **sockets(host, port)}
        item.update(
            unit="incoming connections",
            active=item.get("connections"),
            note="Connections and source addresses are not people. "
            "Backend connections are excluded; relays may hide addresses.",
        )
        if identity in ("mtg", "relay"):
            prefix, endpoint = (
                ("mtg", 19445) if identity == "mtg" else ("tproxy", 18081)
            )
            native = exporter(http(f"http://127.0.0.1:{endpoint}/metrics"), prefix)
            item.update(native)
            item["epoch"] = (
                probe(["docker", "inspect", "--format", "{{.State.StartedAt}}", "mtg"])
                if identity == "mtg"
                else probe(
                    [
                        "systemctl",
                        "show",
                        "tproxy-server",
                        "-p",
                        "InvocationID",
                        "--value",
                    ]
                )
            )
            if identity == "relay" and "active" in native:
                item.update(available=True, unit="live sessions")
        if listeners is not None and str(port) not in ports:
            item.update(available=False, error="Service is not listening")
        services.append(item)
    # Inspect only the fixed container's PID; query inside its network namespace.
    pid = probe(["docker", "inspect", "--format", "{{.State.Pid}}", "iptv-proxy"])
    media = None
    if pid and pid.strip().isdigit() and int(pid) > 0:
        media = probe(
            ["nsenter", "-t", pid.strip(), "-n", "ss", "-Htn", "state", "established"]
        )
    media_stats = sockets(media, 8080)
    if media is not None:
        net = probe(["nsenter", "-t", pid.strip(), "-n", "cat", "/proc/net/dev"])
        if net is not None:
            media_stats["traffic_bytes"] = sum(
                int(fields[0]) + int(fields[8])
                for line in net.splitlines()
                if ":" in line
                for interface, _, data in [line.partition(":")]
                if interface.strip() != "lo"
                for fields in [data.split()]
                if len(fields) >= 9 and fields[0].isdigit() and fields[8].isdigit()
            )
    services.append(
        {
            "id": "media",
            "name": "Media / IPTV proxy",
            "epoch": pid.strip() if pid else None,
            **media_stats,
            "active": media_stats.get("connections"),
            "unit": "incoming connections",
            "note": "HTTP connections, not viewers; HLS uses short requests. "
            "Viewer identities unavailable. Traffic includes upstream and downstream "
            "container traffic, so relayed content can be counted twice.",
        }
    )
    ssh = {"id": "ssh", "name": "SSH", **ssh_clients.collect(probe)}
    ssh_ports = set(ssh.pop("ports"))
    network = sockets(host, ssh_ports) if ssh_ports else {}
    if network.get("available"):
        ssh["connections"] = network["connections"]
        ssh["source_addresses"] = network["source_addresses"]
    services.append(ssh)
    return services
