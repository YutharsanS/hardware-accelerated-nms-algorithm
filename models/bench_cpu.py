"""Reproducible CPU baseline benchmark for the NMS accelerator report (`docs/plan.md` Part 1e).

Times three implementations over 2,000 iterations on the notebook's 32-box dataset:

- **notebook-float**: the original notebook algorithm, as written -- float IoU division.
- **planned-integer**: `nms_model.nms_sequential`, the same integer predicate the RTL uses.
- **numpy-all-pairs**: a vectorised float implementation -- the fair Python upper bound,
  same all-pairs structure as the proposed hardware.

Also runs the thread-pool overhead probe that shows multicore cannot help at N=32: a
bare zero-work round trip through the pool costs more than the entire numpy run.

Run: `uv run python models/bench_cpu.py` (add `--check` to verify all three agree on
`keep_mask` for the benchmark dataset before timing, so this baseline cannot silently
diverge from the golden model).
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from os import cpu_count

import numpy as np
from gen_vectors import NOTEBOOK_RAW, case_notebook31
from nms_model import nms_sequential

ACCELERATOR_TIME_US = 0.72
"""P=16, all-pairs restructure, from Part 1d/1e -- the number every ratio is against."""

ITERATIONS = 2000


def _timeit(fn: Callable[[], object], iterations: int = ITERATIONS) -> float:
    """Average wall time per call, in microseconds, over `iterations` back-to-back calls."""
    start = time.perf_counter()
    for _ in range(iterations):
        fn()
    end = time.perf_counter()
    return (end - start) / iterations * 1e6


def _calculate_iou_float(bbox1: tuple, bbox2: tuple) -> float:
    """The notebook's `calculate_iou`, verbatim."""
    x1, y1, a1, b1, _ = bbox1
    x2, y2, a2, b2, _ = bbox2
    area1 = (a1 - x1) * (b1 - y1)
    area2 = (a2 - x2) * (b2 - y2)
    xx, yy = max(x1, x2), max(y1, y2)
    aa, bb = min(a1, a2), min(b1, b2)
    w, h = max(0, aa - xx), max(0, bb - yy)
    intersection_area = w * h
    union_area = area1 + area2 - intersection_area
    return intersection_area / union_area


def nms_float(boxes: list[tuple], iou_threshold: float = 0.5) -> list[tuple]:
    """The notebook's `nms`, verbatim -- float division, `>=` threshold."""
    sorted_boxes = sorted(boxes, key=lambda box: box[4], reverse=True)
    valid = [True] * len(sorted_boxes)
    keep = []
    for i in range(len(sorted_boxes)):
        if valid[i]:
            keep.append(sorted_boxes[i])
            valid[i] = False
            for j in range(i + 1, len(sorted_boxes)):
                if (
                    valid[j]
                    and _calculate_iou_float(sorted_boxes[i], sorted_boxes[j])
                    >= iou_threshold
                ):
                    valid[j] = False
    return keep


def nms_numpy_allpairs(boxes: list[tuple], iou_threshold: float = 0.5) -> np.ndarray:
    """Vectorised all-pairs NMS: the IoU matrix is computed once via broadcasting.

    Only the rank-order resolve stays a serial Python loop, since each keeper
    decision depends on every earlier one -- the same structural dependency the
    hardware's resolve stage has (Part 1e).

    Args:
        boxes: `(x, y, a, b, score)` tuples.
        iou_threshold: Suppression threshold.

    Returns:
        Boolean array, True where the box at that original index survives.
    """
    arr = np.asarray(boxes, dtype=np.float64)
    x, y, a, b, scores = arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4]
    areas = (a - x) * (b - y)
    order = np.argsort(-scores, kind="stable")

    xx = np.maximum(x[:, None], x[None, :])
    yy = np.maximum(y[:, None], y[None, :])
    aa = np.minimum(a[:, None], a[None, :])
    bb = np.minimum(b[:, None], b[None, :])
    w = np.maximum(0.0, aa - xx)
    h = np.maximum(0.0, bb - yy)
    inter = w * h
    union = areas[:, None] + areas[None, :] - inter
    iou = np.divide(inter, union, out=np.zeros_like(inter), where=union > 0)
    suppress_matrix = iou >= iou_threshold

    valid = np.ones(len(boxes), dtype=bool)
    keep = np.zeros(len(boxes), dtype=bool)
    for i in order:
        if valid[i]:
            keep[i] = True
            valid[i] = False
            valid &= ~suppress_matrix[i]
    return keep


def thread_pool_overhead(iterations: int = ITERATIONS) -> float:
    """Average round-trip time, in microseconds, to dispatch and collect zero work.

    Demonstrates that the thread pool itself costs more than the entire numpy NMS run
    at N=32 -- the workload is too small to parallelise in software (Part 1e).
    """
    workers = cpu_count() or 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        start = time.perf_counter()
        for _ in range(iterations):
            list(pool.map(lambda _: None, range(workers)))
        end = time.perf_counter()
    return (end - start) / iterations * 1e6


def _quantised_boxes() -> list[tuple]:
    boxes, present_mask = case_notebook31()
    assert present_mask == (1 << len(boxes)) - 1
    return [(box.x, box.y, box.a, box.b, box.score) for box in boxes]


def check_all_agree() -> None:
    """Assert the three implementations agree on `keep_mask` for the benchmark dataset."""
    float_keep = {NOTEBOOK_RAW.index(b) for b in nms_float(NOTEBOOK_RAW)}
    quantised = _quantised_boxes()
    numpy_keep = {i for i, kept in enumerate(nms_numpy_allpairs(quantised)) if kept}

    boxes, present_mask = case_notebook31()
    integer_mask = nms_sequential(boxes, present_mask)
    integer_keep = {i for i in range(len(boxes)) if (integer_mask >> i) & 1}

    assert float_keep == numpy_keep == integer_keep, (
        f"baselines disagree: float={sorted(float_keep)} numpy={sorted(numpy_keep)} "
        f"integer={sorted(integer_keep)}"
    )
    print(f"PASS --check: all three baselines agree, {len(float_keep)} survivors")


def format_report(rows: list[tuple[str, float]], thread_overhead_us: float) -> str:
    """Render the Part 1e comparison table.

    Args:
        rows: `(label, microseconds)` pairs, one per implementation.
        thread_overhead_us: The thread-pool round-trip overhead measurement.

    Returns:
        A multi-line report suitable for printing or pasting into the report.
    """
    lines = [
        f"{'implementation':<28} {'measured':>12} {'vs accelerator (0.72us)':>26}",
    ]
    for label, us in rows:
        lines.append(f"{label:<28} {us:>9.2f} us {us / ACCELERATOR_TIME_US:>22.0f}x")
    lines += [
        "",
        (
            f"thread-pool round trip, zero work: {thread_overhead_us:.2f} us "
            f"({cpu_count() or 1} workers)"
        ),
        "-> multicore cannot help at N=32: dispatch overhead alone exceeds the numpy run.",
    ]
    return "\n".join(lines)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Assert all three implementations agree on keep_mask before timing.",
    )
    parser.add_argument("--iterations", type=int, default=ITERATIONS)
    args = parser.parse_args()

    if args.check:
        check_all_agree()

    quantised = _quantised_boxes()
    boxes, present_mask = case_notebook31()

    rows = [
        (
            "notebook-float, as written",
            _timeit(lambda: nms_float(NOTEBOOK_RAW), args.iterations),
        ),
        (
            "planned-integer (RTL predicate)",
            _timeit(lambda: nms_sequential(boxes, present_mask), args.iterations),
        ),
        (
            "numpy all-pairs, vectorised",
            _timeit(lambda: nms_numpy_allpairs(quantised), args.iterations),
        ),
    ]
    thread_overhead = thread_pool_overhead(args.iterations)
    print(format_report(rows, thread_overhead))


if __name__ == "__main__":
    main()
