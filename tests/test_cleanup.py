"""Disk cleanup: measuring, cleaning, and the guards around them."""

import json
import tempfile
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

from ai_ops_agent import cleanup
from ai_ops_agent.cleanup import Paths

APT_SIM = """NOTE: This is only a simulation!
Reading package lists...
The following packages will be REMOVED:
  linux-image-6.8.0-40-generic* linux-modules-6.8.0-40-generic*
Purg linux-image-6.8.0-40-generic [6.8.0-40.40]
Purg linux-modules-6.8.0-40-generic [6.8.0-40.40]
"""
SNAP_LIST = """Name    Version   Rev    Tracking       Publisher   Notes
core20  20240416  2318   latest/stable  canonical**  base,disabled
core20  20240705  2379   latest/stable  canonical**  base
lxd     5.21.1    29351  5.21/stable    canonical**  disabled
lxd     5.21.2    29619  5.21/stable    canonical**  -
"""


def _file(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


class FakeRun:
    """Answers commands by prefix and records every call."""

    def __init__(self, answers=None) -> None:
        self.calls = []
        self.answers = answers or {}

    def __call__(self, args, timeout):
        self.calls.append(args)
        for prefix, answer in self.answers.items():
            if " ".join(args).startswith(prefix):
                return answer
        return 0, ""


def _paths(tmp: Path) -> Paths:
    return Paths(
        root=tmp,
        apt_archives=tmp / "apt",
        journal=tmp / "journal",
        snaps=tmp / "snaps",
        caches=(tmp / "pip", tmp / "npm"),
        lock=tmp / "lock",
        log=tmp / "cleanup.log",
    )


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


# ---------------------------------------------------------------- parsing


def test_unused_packages_reads_purge_and_remove_lines():
    run = FakeRun({"apt-get -s": (0, APT_SIM + "Remv old-lib [1.0]\n")})
    assert cleanup.unused_packages(run) == [
        "linux-image-6.8.0-40-generic",
        "linux-modules-6.8.0-40-generic",
        "old-lib",
    ]
    assert cleanup.unused_packages(FakeRun({"apt-get": (100, "E: locked")})) == []


def test_installed_bytes_sums_dpkg_kib():
    run = FakeRun({"dpkg-query": (0, "1024\n2048\n")})
    assert cleanup.installed_bytes(run, ["a", "b"]) == 3072 * 1024
    assert cleanup.installed_bytes(run, []) == 0


def test_disabled_snaps():
    run = FakeRun({"snap list": (0, SNAP_LIST)})
    assert cleanup.disabled_snaps(run) == [("core20", "2318"), ("lxd", "29351")]
    assert cleanup.disabled_snaps(FakeRun({"snap": (127, "not installed")})) == []


def test_largest_directories_lists_culprits_not_their_parents():
    du = "\n".join(
        [
            "3000000\t/var",
            "2000000\t/var/log",
            "900000\t/var/cache",
            "2500000\t/root",
            "2400000\t/root/.codex",
            "500000\t/etc",
            "9000000\t/",
        ]
    )
    largest = cleanup.largest_directories(FakeRun({"du": (1, du)}), Path("/"))
    assert [entry["path"] for entry in largest] == [
        "/root/.codex",
        "/var/log",
        "/var/cache",
        "/etc",
    ]


# ---------------------------------------------------------------- measuring


def test_measure_sizes_each_category():
    tmp = _tmp()
    paths = _paths(tmp)
    _file(paths.apt_archives / "a.deb", 64 * 1024)
    _file(paths.journal / "system.journal", 300 * 1024**2)
    _file(paths.snaps / "core20_2318.snap", 128 * 1024)
    _file(paths.caches[0] / "wheel", 256 * 1024)
    run = FakeRun(
        {
            "apt-get -s": (0, APT_SIM),
            "dpkg-query": (0, "100\n"),
            "snap list": (0, SNAP_LIST),
        }
    )
    by_id = {category.id: category for category in cleanup.measure(run, paths)}

    assert by_id["packages"].bytes == 100 * 1024 + 64 * 1024
    assert "2 unused packages" in by_id["packages"].detail
    # Journal keeps up to 200 MB, so only the excess is reclaimable.
    assert by_id["journal"].bytes == 100 * 1024**2
    assert by_id["snaps"].bytes == 128 * 1024
    assert by_id["snaps"].items == ["core20 rev 2318", "lxd rev 29351"]
    assert by_id["caches"].bytes == 256 * 1024


def test_report_is_json_with_total():
    tmp = _tmp()
    result = cleanup.report(FakeRun(), _paths(tmp))
    json.dumps(result)
    assert result["ok"] is True
    assert result["reclaimable_bytes"] == sum(c["bytes"] for c in result["categories"])
    assert [c["id"] for c in result["categories"]] == [
        "packages",
        "journal",
        "snaps",
        "caches",
    ]


# ---------------------------------------------------------------- cleaning


Usage = namedtuple("Usage", "total used free")


def test_clean_runs_fixed_steps_in_order_and_measures_freed_space():
    tmp = _tmp()
    paths = _paths(tmp)
    _file(paths.caches[0] / "wheel", 4096)
    run = FakeRun({"snap list": (0, SNAP_LIST)})
    used = iter(
        [10_000, 10_000, 9_000, 9_000, 8_500, 8_500, 8_500, 8_500, 8_000, 8_000]
    )
    with patch.object(
        cleanup.shutil, "disk_usage", side_effect=lambda _p: Usage(0, next(used), 0)
    ):
        result = cleanup.clean(run, paths)

    assert [args[:2] for args in run.calls] == [
        ["apt-get", "autoremove"],
        ["apt-get", "clean"],
        ["journalctl", "--vacuum-time=7d"],
        ["snap", "list"],
        ["snap", "remove"],
        ["snap", "remove"],
    ]
    assert run.calls[4] == ["snap", "remove", "core20", "--revision=2318"]
    assert [step["freed_bytes"] for step in result["steps"]] == [1000, 500, 0, 500]
    assert result["freed_bytes"] == 2000
    assert result["ok"] is True
    assert not paths.caches[0].exists()


def test_a_failing_step_does_not_stop_the_others():
    tmp = _tmp()
    run = FakeRun({"journalctl": (1, "Failed to vacuum")})
    result = cleanup.clean(run, _paths(tmp))
    assert result["ok"] is False
    assert [step["ok"] for step in result["steps"]] == [True, False, True, True]
    assert "Failed to vacuum" in result["steps"][1]["message"]


# ---------------------------------------------------------------- guards


def test_main_rejects_unknown_mode(capsys):
    assert cleanup.main(["purge-everything"]) == 2
    assert "usage" in json.loads(capsys.readouterr().out)["error"]


def test_main_requires_root(capsys):
    with patch.object(cleanup.os, "geteuid", return_value=1000):
        assert cleanup.main(["run"], FakeRun(), _paths(_tmp())) == 1
    assert "root" in json.loads(capsys.readouterr().out)["error"]


def test_only_one_cleanup_at_a_time(capsys):
    paths = _paths(_tmp())
    with (
        patch.object(cleanup.os, "geteuid", return_value=0),
        cleanup.exclusive(paths.lock),
    ):
        assert cleanup.main(["run"], FakeRun(), paths) == 1
    assert "already running" in json.loads(capsys.readouterr().out)["error"]


def test_run_logs_and_prints_result(capsys):
    paths = _paths(_tmp())
    with patch.object(cleanup.os, "geteuid", return_value=0):
        assert cleanup.main(["run"], FakeRun(), paths) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert "cleanup freed" in paths.log.read_text()


def test_cache_failure_is_reported_and_other_caches_are_attempted():
    paths = _paths(_tmp())
    for cache in paths.caches:
        _file(cache / "entry", 4096)
    with patch.object(
        cleanup.shutil, "rmtree", side_effect=[PermissionError("denied"), None]
    ) as remove:
        code, message = cleanup._remove_caches(paths)
    assert code == 1
    assert "denied" in message
    assert remove.call_count == 2
