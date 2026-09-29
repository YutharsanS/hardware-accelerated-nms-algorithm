"""What every result row must name: machine, clock, governor, library versions.

docs/results/benchmarks.md §3: every row carries the commit, CPU, frequency, governor, temperature and
library versions. On a Raspberry Pi it also carries ``vcgencmd`` readings taken before and
after the run, and a run with the throttled flag set at either point is invalid.
"""

from __future__ import annotations

import importlib.metadata
import os
import platform
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from models.nms import bench

LIBRARIES = ("numpy", "torch", "torchvision", "opencv-python-headless", "ultralytics")
DEVICE_TREE_MODEL = Path("/proc/device-tree/model")
REPO = Path(__file__).resolve().parents[1]


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, check=False, cwd=REPO
        ).stdout.strip()
    except OSError:
        return ""


def board_model() -> str:
    """Return the board name from the device tree, e.g. ``Raspberry Pi 5 Model B``.

    Returns:
        The model string, or an empty string on a PC.
    """
    if DEVICE_TREE_MODEL.exists():
        return DEVICE_TREE_MODEL.read_text().strip("\x00").strip()
    return ""


def host_label() -> str:
    """Return the short machine name a results table shows, board first.

    Returns:
        The board model on a Pi, otherwise the CPU model.
    """
    return board_model() or bench.cpu_name().replace("13th Gen Intel(R) Core(TM) ", "")


def pi_readings() -> dict[str, str]:
    """Return ``vcgencmd`` temperature, ARM clock and throttle flags.

    Returns:
        Empty strings on a machine without ``vcgencmd``.
    """
    return {
        "vc_temp": _run(["vcgencmd", "measure_temp"]),
        "vc_clock": _run(["vcgencmd", "measure_clock", "arm"]),
        "vc_throttled": _run(["vcgencmd", "get_throttled"]),
    }


def throttled(readings: dict[str, str]) -> bool:
    """Return whether a ``get_throttled`` reading has any flag set.

    Args:
        readings: Output of :func:`pi_readings`.

    Returns:
        True if the Pi reported throttling or under-voltage; False off a Pi.
    """
    text = readings.get("vc_throttled", "")
    if "=" not in text:
        return False
    return int(text.split("=", 1)[1], 16) != 0


def library_versions() -> dict[str, str]:
    """Return the installed version of every library a benchmark may time.

    Returns:
        Version strings, empty for a library that is not installed.
    """
    out = {}
    for name in LIBRARIES:
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out[name] = ""
    return out


def machine_metadata() -> dict:
    """Return the machine description written next to every results CSV.

    Returns:
        A flat dictionary.
    """
    cpus = os.sched_getaffinity(0)
    first = min(cpus)
    meta: dict = {
        "host": socket.gethostname(),
        "label": host_label(),
        "date": datetime.now(tz=UTC).date().isoformat(),
        "cpu": bench.cpu_name(),
        "board": board_model(),
        "machine": platform.machine(),
        "affinity": sorted(cpus),
        "commit": _run(["git", "rev-parse", "--short", "HEAD"]),
        "dirty": bool(_run(["git", "status", "--porcelain", "--untracked-files=no"])),
        "python": platform.python_version(),
        **library_versions(),
    }
    for key, name in (
        ("governor", "scaling_governor"),
        ("freq_khz", "scaling_cur_freq"),
        ("max_freq_khz", "scaling_max_freq"),
    ):
        f = Path(f"/sys/devices/system/cpu/cpu{first}/cpufreq/{name}")
        meta[key] = f.read_text().strip() if f.exists() else ""
    meta.update(pi_readings())
    return meta
