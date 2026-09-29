"""Time software NMS against the number of boxes N, on this machine.

Times ``torchvision.ops.nms``, ``cv2.dnn.NMSBoxes`` and the golden model's
``numpy_allpairs`` at N = 8, 16 and 32 (``--sizes`` for others, up to 8,400), on real
candidate sets: the top N anchors of each image exported by ``count_boxes.py``. Consecutive calls cycle through images, so a
repeated batch cannot flatter the branch predictor or the cache.

Inputs are prepared outside the timed call in each library's native format. Every call is
timed on its own and the samples are summarised as min / median / p99 / max, never a mean
(docs/results/benchmarks.md §3). Run it pinned to one core, e.g. ``taskset -c 2``.

Usage::

    taskset -c 2 python -m benchmarks.feasibility.time_vs_boxes --raw <dir>/raw_yolov8n.npz --out <dir>
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np
import torch
import torchvision

from benchmarks import meta as machine
from models.nms import bench, model

SIZES = (8, 16, 32)
"""The application's range: at most 32 boxes reach NMS (build_log.md, the feasibility decision)."""
IOU = 0.5
"""Matches the block's ``T_INT = 128``. The libraries suppress on IoU > 0.5, the block on >=."""
NUMPY_MAX_N = 2048
"""``numpy_allpairs`` builds several N x N int64 matrices: 8,400 would need ~3 GB."""
BUDGET_S = 0.5
MIN_SAMPLES, MAX_SAMPLES = 30, 3000


def prepare(sets: np.ndarray, n: int) -> dict[str, list]:
    """Convert each image's top-``n`` anchors into every implementation's input format.

    Args:
        sets: ``(images, 8400, 5)`` records, highest score first.
        n: Batch size.

    Returns:
        Per implementation, one prepared input per image.
    """
    top = sets[:, :n, :]
    prepared: dict[str, list] = {"torchvision": [], "opencv": [], "numpy_allpairs": []}
    for rec in top:
        xyxy = rec[:, :4].astype(np.float32)
        score = rec[:, 4].astype(np.float32) / 65535
        prepared["torchvision"].append(
            (torch.from_numpy(xyxy), torch.from_numpy(score))
        )
        xywh = np.concatenate([rec[:, :2], rec[:, 2:4] - rec[:, :2]], 1)
        # OpenCV drops scores <= its threshold, which must be >= 0. At N = 8,400 most
        # anchors quantise to 0, so shift by one step to keep them all, in the same order.
        cv_score = (rec[:, 4].astype(np.float32) + 1) / 65536
        prepared["opencv"].append((xywh.tolist(), cv_score.tolist()))
        if n <= NUMPY_MAX_N:
            prepared["numpy_allpairs"].append([model.Box(*map(int, r)) for r in rec])
    return prepared


IMPLS: dict[str, Callable] = {
    "torchvision": lambda a: torchvision.ops.nms(a[0], a[1], IOU),
    "opencv": lambda a: cv2.dnn.NMSBoxes(a[0], a[1], 0.0, IOU),
    "numpy_allpairs": lambda a: bench.numpy_allpairs(a),
}


def time_impl(fn: Callable, inputs: list) -> np.ndarray:
    """Time single calls, cycling through inputs, for about ``BUDGET_S`` seconds.

    Args:
        fn: The implementation.
        inputs: Prepared inputs, one per image.

    Returns:
        Per-call wall times in microseconds.
    """
    for a in inputs[:3]:
        fn(a)  # warm-up
    samples = []
    spent = 0.0
    k = 0
    while (spent < BUDGET_S or len(samples) < MIN_SAMPLES) and len(
        samples
    ) < MAX_SAMPLES:
        a = inputs[k % len(inputs)]
        t0 = time.perf_counter_ns()
        fn(a)
        dt = time.perf_counter_ns() - t0
        samples.append(dt / 1e3)
        spent += dt / 1e9
        k += 1
    return np.array(samples)


def agreement(prepared: dict[str, list]) -> int:
    """Count images where torchvision and OpenCV keep different boxes.

    Both suppress on IoU > threshold. They may still differ on tie order between equal
    scores, which the report states rather than hides.

    Args:
        prepared: Output of :func:`prepare`.

    Returns:
        Number of images with differing keep sets.
    """
    diff = 0
    for tv, cvin in zip(prepared["torchvision"], prepared["opencv"], strict=True):
        a = set(IMPLS["torchvision"](tv).tolist())
        b = set(np.asarray(IMPLS["opencv"](cvin)).reshape(-1).tolist())
        diff += a != b
    return diff


def main() -> None:
    """Parse arguments, sweep N, and write a CSV plus a metadata JSON."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--raw", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sizes", type=int, nargs="+", default=list(SIZES))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(1)
    cv2.setNumThreads(1)

    sets = np.load(args.raw)["raw"]
    meta = machine.machine_metadata()
    stem = f"time_{meta['host']}_{args.raw.stem}"
    rows = []
    for n in args.sizes:
        prepared = prepare(sets, n)
        mismatch = agreement(prepared)
        for name, fn in IMPLS.items():
            if not prepared[name]:
                continue
            us = time_impl(fn, prepared[name])
            row = {
                "impl": name,
                "n": n,
                "samples": len(us),
                "min_us": float(us.min()),
                "median_us": float(np.median(us)),
                "p99_us": float(np.percentile(us, 99)),
                "max_us": float(us.max()),
                "tv_cv_mismatch": mismatch,
                "images": len(prepared[name]),
            }
            rows.append(row)
            print(
                f"{name:15s} N={n:5d}  median {row['median_us']:10.1f} us"
                f"  p99 {row['p99_us']:10.1f}  mismatch {mismatch}/{len(sets)}",
                flush=True,
            )
    meta.update({f"{k}_after": v for k, v in machine.pi_readings().items()})
    with (args.out / f"{stem}.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (args.out / f"{stem}.json").write_text(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
