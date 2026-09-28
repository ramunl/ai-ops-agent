"""Verify resource parsing on normal and malformed system output."""

import pytest

from ai_ops_agent.metrics import parse_disk_usage, parse_memory_usage, usage_bar


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Mem: 100 40 10 0 50 60", (40, 100)),
        ("Mem: 100 40 10", (40, 100)),
        ("Mem: malformed", None),
        ("Error: free unavailable", None),
    ],
)
def test_memory_parser_uses_available_memory_when_present(text, expected):
    assert parse_memory_usage(text) == expected


def test_disk_parser_reads_root_usage():
    assert parse_disk_usage(
        "Filesystem Size Used Avail Use% Mounted on\n/dev/vda 100G 20G 80G 20% /"
    ) == ("20G", "100G", 20.0)


@pytest.mark.parametrize(
    "percent,expected", [(-5, "░" * 10), (50, "█" * 5 + "░" * 5), (110, "█" * 10)]
)
def test_usage_bar_clamps_invalid_percentages(percent, expected):
    assert usage_bar(percent) == expected
