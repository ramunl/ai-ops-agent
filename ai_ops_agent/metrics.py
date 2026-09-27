"""Parse and format server resource measurements."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def usage_bar(pct: float, width: int = 10) -> str:
    """Render a Unicode progress bar for a 0-100 percentage."""
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100 * width))
    return "█" * filled + "░" * (width - filled)


def format_bytes(n: float) -> str:
    """Human-readable size, e.g. 4.0G."""
    for unit in ("B", "K", "M", "G", "T", "P"):
        if n < 1024 or unit == "P":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}P"


def parse_memory_usage(free_b: str, label: str = "Mem:") -> tuple[int, int] | None:
    """(used_bytes, total_bytes) for a `free -b` row ("Mem:"/"Swap:"), or None."""
    for line in free_b.splitlines():
        if line.startswith(label):
            parts = line.split()
            try:
                total = int(parts[1])
                if label == "Mem:" and len(parts) > 6:
                    used = total - int(parts[6])  # total - available
                else:
                    used = int(parts[2])
                return used, total
            except (ValueError, IndexError):
                logger.warning("Could not parse %s memory measurement", label)
                return None
    return None


def parse_disk_usage(df_hp: str) -> tuple[str, str, float] | None:
    """(used, size, percent) parsed from `df -hP /`, or None on failure."""
    lines = df_hp.splitlines()
    if len(lines) < 2:
        return None
    parts = lines[1].split()
    try:
        return parts[2], parts[1], float(parts[4].rstrip("%"))
    except (ValueError, IndexError):
        logger.warning("Could not parse root disk measurement")
        return None


def parse_disk_rows(df_hp: str) -> list[tuple[str, str, str, float]]:
    """[(mount, used, size, percent), ...] for real block devices from `df -hP`."""
    rows = []
    for line in df_hp.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6 or not parts[0].startswith("/dev/"):
            continue
        try:
            rows.append((parts[5], parts[2], parts[1], float(parts[4].rstrip("%"))))
        except (ValueError, IndexError):
            logger.warning("Skipping invalid block-device measurement")
            continue
    return rows


def filter_disk_rows(raw: str) -> str:
    """Keep block-device lines from df output, dropping overlay/tmpfs noise."""
    lines = raw.splitlines()
    if not lines:
        return raw
    header = lines[0]
    real = [line for line in lines[1:] if line.startswith("/dev/")]
    return "\n".join([header] + real) if real else raw
