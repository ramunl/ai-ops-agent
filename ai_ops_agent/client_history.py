"""Bounded daily samples, counter deltas and rates; never invent missing traffic."""

from __future__ import annotations

import time

COUNTERS = ("rx_bytes", "tx_bytes", "traffic_bytes")


def update(report: dict, previous: dict, now: float) -> dict:
    """Attach observed UTC-day usage, rates and peaks to each service."""
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    old_services = {item["id"]: item for item in previous.get("services", [])}
    for item in report["services"]:
        old = old_services.get(item["id"], {})
        sample = old.get("sample", {})
        same_day = sample.get("day") == day
        daily = dict(sample.get("daily", {})) if same_day else {}
        peak = sample.get("peak", 0) if same_day else 0
        start = sample.get("since", now) if same_day else now
        elapsed = now - sample.get("at", now)
        reset = bool(
            old.get("epoch") and item.get("epoch") and old["epoch"] != item["epoch"]
        )
        baselines = dict(sample.get("counters", {}))
        times = dict(sample.get("counter_times", {}))
        for key in COUNTERS:
            value = item.get(key)
            if not isinstance(value, (int, float)):
                continue
            last = baselines.get(key)
            delta = value - last if last is not None and not reset else None
            counter_elapsed = now - times.get(key, now)
            if delta is not None and delta < 0:
                reset = True
                delta = None  # cannot recover traffic lost across a restart
            if delta is not None and elapsed > 0 and same_day:
                daily[key] = daily.get(key, 0) + delta
                if 0 < counter_elapsed <= 120:
                    item[key.replace("_bytes", "_bps")] = round(
                        delta / counter_elapsed, 1
                    )
            baselines[key] = value
            times[key] = now
        if item.get("available") and isinstance(item.get("active"), (int, float)):
            peak = max(peak, item["active"])
        item["sample"] = {
            "day": day,
            "since": start,
            "at": now,
            "counters": baselines,
            "counter_times": times,
            "daily": daily,
            "peak": peak,
            "counter_reset": reset,
            "incomplete": (sample.get("incomplete", False) if same_day else False)
            or reset
            or elapsed > 120
            or not item.get("available"),
        }
    return report
