"""The services the ops tools may control: the single whitelist.

Kept free of bot settings so the ai-service command (dashboard) can import it
without credentials. The ops bot's /restart, /logs and Start/Stop buttons use
the same list.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

MANAGED_SERVICES = [
    "ai-coding-agent",
    "ai-pm-agent",
    "ai-ops-agent",
    "ai-dashboard",
    "iptv-proxy",
]


@dataclass(frozen=True)
class ServicePort:
    """The port a service serves: ufw opens it on start and closes it on stop."""

    port: int
    proto: str = "tcp"


# Optional firewall port per managed service. A service without an entry is
# started and stopped without touching the firewall. The owner adds an entry
# (and the unit to MANAGED_SERVICES) once the unit name and port are confirmed,
# e.g. ``"iptv-stream": ServicePort(<port>)``.
SERVICE_PORTS: dict[str, ServicePort] = {"iptv-proxy": ServicePort(8080)}

# Exact container names only; never accept arbitrary Docker targets from chat.
DOCKER_SERVICES = {"iptv-proxy": "iptv-proxy"}


def validate_service_ports(
    services: list[str], ports: Mapping[str, ServicePort]
) -> None:
    """Raise ValueError unless every port belongs to a managed service.

    Each port must be an integer 1-65535, the protocol tcp or udp, and no
    port number may be used by two services.
    """
    owners: dict[int, str] = {}
    for service, spec in ports.items():
        if service not in services:
            raise ValueError(f"SERVICE_PORTS '{service}' is not in MANAGED_SERVICES")
        port = spec.port
        is_int = isinstance(port, int) and not isinstance(port, bool)
        if not is_int or not 1 <= port <= 65535:
            raise ValueError(f"port for '{service}' must be an integer 1-65535")
        if spec.proto not in ("tcp", "udp"):
            raise ValueError(f"protocol for '{service}' must be tcp or udp")
        if port in owners:
            raise ValueError(
                f"port {port} is set for both '{owners[port]}' and '{service}'"
            )
        owners[port] = service
