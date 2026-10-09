"""The COCO crowd set: still images where NMS has the most to do.

Chosen from the committed counts of YOLOv8n's candidates on 500 COCO val2017 images
(``benchmarks/results/feasibility/counts_yolov8n.json``, field ``cls@0.25``: candidates per
class at confidence 0.25). The set is the images whose busiest class has 20 to 32
candidates, which fill a batch without passing the cap, plus a few over 32, where the cap
shows. Downloaded once from cocodataset.org into a gitignored cache.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COUNTS = REPO / "benchmarks" / "results" / "feasibility" / "counts_yolov8n.json"
CACHE = REPO / "demo" / "data" / "coco"
URL = "http://images.cocodataset.org/val2017/{name}"
FULL_RANGE = (20, 32)
"""Busiest-class candidate counts that fill a batch without exceeding it."""
OVER_CAP = 4
"""How many images over the cap to include."""
TIMEOUT_S = 10


def select(counts: Path = COUNTS, limit: int = 24) -> list[str]:
    """Pick the crowd set's file names.

    Args:
        counts: The committed per-image counts.
        limit: Images in the set, including the over-cap ones.

    Returns:
        File names, busiest first: those in :data:`FULL_RANGE`, then :data:`OVER_CAP` of
        the busiest over 32.
    """
    rows = json.loads(counts.read_text())
    busiest = [(max(r["cls@0.25"], default=0), r["image"]) for r in rows]
    lo, hi = FULL_RANGE
    full = sorted(
        ((n, name) for n, name in busiest if lo <= n <= hi),
        key=lambda x: (-x[0], x[1]),
    )
    over = sorted(
        ((n, name) for n, name in busiest if n > hi),
        key=lambda x: (-x[0], x[1]),
    )
    chosen = full[: limit - OVER_CAP] + over[:OVER_CAP]
    return [name for _, name in chosen]


def fetch(names: list[str], cache: Path = CACHE) -> tuple[list[Path], list[str]]:
    """Download the images not yet cached.

    Args:
        names: COCO val2017 file names.
        cache: Where they are kept.

    Returns:
        ``(paths available, error messages)``: whatever could be fetched, and why the rest
        could not, so an offline machine says so instead of failing silently.
    """
    cache.mkdir(parents=True, exist_ok=True)
    paths, errors = [], []
    for name in names:
        path = cache / name
        if not path.exists():
            try:
                with urllib.request.urlopen(
                    URL.format(name=name), timeout=TIMEOUT_S
                ) as resp:
                    path.write_bytes(resp.read())
            except (urllib.error.URLError, OSError) as exc:
                errors.append(f"{name}: {exc}")
                continue
        paths.append(path)
    return paths, errors
