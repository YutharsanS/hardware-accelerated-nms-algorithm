"""Measure what NMS costs on this CPU, so the accelerator has an honest comparison.

Four implementations are timed:

* ``float_reference`` -- the notebook's original algorithm, float IoU against a float
  threshold. Kept because it is what the project started from.
* ``integer_sequential`` / ``integer_allpairs`` -- the golden model's two forms.
* ``numpy_allpairs`` -- the fair upper bound for Python. At N=32 it is dominated by
  interpreter overhead rather than arithmetic, which the numbers show plainly.

Also probes thread-pool dispatch cost, because "use more cores" is the obvious objection
to any single-threaded baseline and at N=32 it does not survive contact with the numbers.

Every figure here belongs in the report next to a named processor. An unqualified "faster
than a CPU" claim is not supportable: see ``docs/build_log.md`` and the plan's Part 1e.
"""

from __future__ import annotations

import platform
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from models.nms import batches, model
from models.nms import params as p

FRAME_BUDGET_US = 1e6 / 30.0
"""A 30 fps frame budget, 33,333 us, for context on every timing below."""


@dataclass(frozen=True)
class Timing:
    """One implementation's measured cost.

    Attributes:
        name: Implementation label.
        micros: Mean wall time per batch, in microseconds.
        keep_mask: Result produced, so agreement can be checked.
    """

    name: str
    micros: float
    keep_mask: int

    @property
    def frame_fraction(self) -> float:
        """Return the share of a 30 fps frame budget this consumes."""
        return self.micros / FRAME_BUDGET_US


# --- the implementations -----------------------------------------------------------


def float_reference(boxes: list[model.Box], threshold: float = 0.5) -> int:
    """Run NMS the way the notebook did: float IoU against a float threshold.

    Kept **faithful to the original**, including its unguarded ``I / U``. That matters for
    two findings:

    * On non-degenerate input it is provably equivalent to the integer predicate. Deciding
      ``I/U >= 0.5`` differently from ``2I >= U`` needs ``|2I - U| < 1`` with both
      integers, i.e. ``2I == U`` -- and 0.5 is exactly representable, so even then they
      agree. float64 carries 53 mantissa bits against values below 2**25 here, leaving no
      room for the rounding that would make a boundary pair ambiguous. The conclusion
      would *not* hold for float32 or much larger coordinates.
    * On degenerate input it **raises**. Two zero-area boxes give ``I = 0, U = 0``, and the
      `degenerate` case alone contains 49 such pairs. The integer predicate returns a
      defined answer (``0 >= 0``, suppress), so cross-multiplying does not merely suit the
      hardware -- it removes a crash the original model would hit.

    Args:
        boxes: The batch.
        threshold: IoU threshold.

    Returns:
        ``keep_mask``.

    Raises:
        ZeroDivisionError: If any evaluated pair has zero union, exactly as the notebook
            would. Use the integer model for degenerate input.
    """
    order = sorted(range(len(boxes)), key=lambda i: (-boxes[i].score, i))
    valid = [True] * len(boxes)
    keep = 0
    for rank, slot in enumerate(order):
        if not valid[slot]:
            continue
        keep |= 1 << slot
        valid[slot] = False
        for other in order[rank + 1 :]:
            if not valid[other]:
                continue
            inter = model.intersection_area(boxes[slot], boxes[other])
            union = model.box_area(boxes[slot]) + model.box_area(boxes[other]) - inter
            iou = inter / union  # unguarded, as the notebook wrote it
            if iou >= threshold:
                valid[other] = False
    return keep


def has_zero_union_pair(boxes: list[model.Box]) -> bool:
    """Return whether any pair in the batch has zero union.

    Such a batch makes :func:`float_reference` raise, so callers comparing the float and
    integer forms must skip it.

    Args:
        boxes: The batch.

    Returns:
        True if some pair would divide by zero.
    """
    for i in range(len(boxes)):
        for j in range(len(boxes)):
            inter = model.intersection_area(boxes[i], boxes[j])
            if model.box_area(boxes[i]) + model.box_area(boxes[j]) - inter == 0:
                return True
    return False


def numpy_allpairs(boxes: list[model.Box]) -> int:
    """Run NMS with a vectorised all-pairs suppression matrix.

    The fair upper bound for Python: the same structure the hardware uses, with the
    pairwise work in numpy. The resolve loop stays in Python because it is inherently
    serial -- each keeper's decision depends on all previous ones.

    Args:
        boxes: The batch.

    Returns:
        ``keep_mask``.
    """
    arr = np.array(boxes, dtype=np.int64)
    x, y, a, b, score = arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4]
    order = np.lexsort((np.arange(len(boxes)), -score))

    xs, ys, as_, bs = x[order], y[order], a[order], b[order]
    area = np.maximum(0, as_ - xs) * np.maximum(0, bs - ys)
    iw = np.maximum(0, np.minimum(as_[:, None], as_) - np.maximum(xs[:, None], xs))
    ih = np.maximum(0, np.minimum(bs[:, None], bs) - np.maximum(ys[:, None], ys))
    inter = iw * ih
    union = area[:, None] + area - inter
    rows = (inter << p.K_SHIFT) >= p.T_INT * union

    valid = np.ones(len(boxes), dtype=bool)
    keep = 0
    for rank in range(len(boxes)):
        if valid[rank]:
            keep |= 1 << int(order[rank])
            valid &= ~rows[rank]
        valid[rank] = False
    return keep


def thread_pool_overhead_us(*, workers: int = 4, reps: int = 2_000) -> float:
    """Measure the round-trip cost of dispatching zero work to a warm thread pool.

    The point of comparison for "just use more cores": if dispatch alone costs a
    meaningful fraction of the whole computation, parallelising it cannot pay.

    Args:
        workers: Pool size.
        reps: Timed dispatches.

    Returns:
        Mean microseconds per submit-and-wait.
    """
    with ThreadPoolExecutor(workers) as pool:
        pool.submit(int).result()  # warm the pool
        start = time.perf_counter()
        for _ in range(reps):
            pool.submit(int).result()
        return (time.perf_counter() - start) / reps * 1e6


# --- timing ------------------------------------------------------------------------


def _time(fn: Callable[[], int], *, budget_s: float = 0.4) -> tuple[float, int]:
    """Time a callable repeatedly for roughly ``budget_s`` seconds.

    Args:
        fn: Zero-argument callable returning a ``keep_mask``.
        budget_s: Approximate wall time to spend.

    Returns:
        ``(mean_microseconds, result)``.
    """
    result = fn()
    start = time.perf_counter()
    runs = 0
    while time.perf_counter() - start < budget_s:
        fn()
        runs += 1
    elapsed = time.perf_counter() - start
    return (elapsed / max(runs, 1) * 1e6, result)


def cpu_name() -> str:
    """Return a human-readable processor name.

    ``platform.processor()`` returns just the architecture on Linux, which is useless for
    a report that must name the processor class alongside every speedup.

    Returns:
        The CPU model string, or the machine type if it cannot be read.
    """
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def benchmark(boxes: list[model.Box] | None = None) -> list[Timing]:
    """Time every implementation on one batch.

    Args:
        boxes: The batch; defaults to the notebook anchor.

    Returns:
        One :class:`Timing` per implementation.
    """
    batch = list(batches.NOTEBOOK_32) if boxes is None else boxes
    candidates: list[tuple[str, Callable[[], int]]] = [
        ("integer sequential", lambda: model.nms_sequential(batch)),
        ("integer all-pairs", lambda: model.nms_allpairs(batch)),
        ("numpy all-pairs", lambda: numpy_allpairs(batch)),
    ]
    # the float form raises on zero-union pairs, so only time it where it is defined
    if not has_zero_union_pair(batch):
        candidates.insert(0, ("notebook float", lambda: float_reference(batch)))

    results = []
    for name, fn in candidates:
        micros, keep = _time(fn)
        results.append(Timing(name=name, micros=micros, keep_mask=keep))
    return results


def benchmark_suite(names: tuple[str, ...] | None = None) -> dict[str, list[Timing]]:
    """Time every implementation across several batches.

    A single batch is misleading. The sequential form short-circuits once boxes are
    suppressed, so a batch with heavy suppression flatters it while one where everything
    survives does not -- the spread between them is wider than the spread between
    implementations. Quoting one number without naming the batch is how a benchmark
    becomes wrong.

    Args:
        names: Case names to run; defaults to a spread of suppression behaviour.

    Returns:
        Mapping of case name to its timings.
    """
    chosen = names or ("notebook32", "all_survive", "all_equal", "rand_seed0")
    cases = batches.named_cases()
    return {name: benchmark(cases[name]) for name in chosen}


def format_report(timings: list[Timing], *, accelerator_us: float | None = None) -> str:
    """Render the benchmark as the table that goes in the report.

    Args:
        timings: Results from :func:`benchmark`.
        accelerator_us: Core latency to compare against; defaults to the value implied by
            the frozen parameters.

    Returns:
        A multi-line report.
    """
    core_us = (
        p.latency_cycles() / p.CLOCK_HZ * 1e6
        if accelerator_us is None
        else accelerator_us
    )
    lines = [
        f"host: {cpu_name()}",
        (
            f"batch: N={p.N}, threshold {p.T_INT}/{1 << p.K_SHIFT} = "
            f"{p.T_INT / (1 << p.K_SHIFT)} (single batch -- see benchmark_suite for the spread)"
        ),
        "",
        f"  {'implementation':<20} {'us/batch':>10} {'% of frame':>11} {'vs core':>9} {'keep_mask':>12}",
    ]
    for t in timings:
        lines.append(
            f"  {t.name:<20} {t.micros:>10.1f} {t.frame_fraction:>10.2%} "
            f"{t.micros / core_us:>8.0f}x 0x{t.keep_mask:08X}",
        )

    overhead = thread_pool_overhead_us()
    fastest = min(t.micros for t in timings)
    lines += [
        "",
        (
            f"  accelerator core, {p.latency_cycles()} cycles at "
            f"{p.CLOCK_HZ / 1e6:.0f} MHz: {core_us:.2f} us"
        ),
        "",
        (
            f"  thread-pool dispatch, zero work: {overhead:.1f} us "
            f"({overhead / fastest:.0%} of the fastest implementation)"
        ),
        "  -> at N=32 the problem is far too small to parallelise in software; dispatch",
        "     alone costs a large fraction of the whole computation, and the resolve loop",
        "     is inherently serial because each keeper depends on all previous ones.",
        "",
        "  Always name the processor class alongside any speedup: this is a 13th-gen",
        "  laptop CPU. See docs/plan.md Part 1e -- against tuned C/AVX2 the accelerator",
        "  is roughly at parity, and end to end over the UART the CPU wins outright.",
    ]
    return "\n".join(lines)


def format_suite(suite: dict[str, list[Timing]]) -> str:
    """Render a multi-batch benchmark, which is the figure the report should quote.

    Args:
        suite: Results from :func:`benchmark_suite`.

    Returns:
        A multi-line report.
    """
    core_us = p.latency_cycles() / p.CLOCK_HZ * 1e6
    names: list[str] = []
    for timings in suite.values():
        names.extend(t.name for t in timings if t.name not in names)

    lines = [
        f"host: {cpu_name()}",
        (
            f"accelerator core: {p.latency_cycles()} cycles at "
            f"{p.CLOCK_HZ / 1e6:.0f} MHz = {core_us:.2f} us"
        ),
        "",
        "  us per batch, by case:",
        "",
        f"  {'implementation':<20}" + "".join(f"{n:>16}" for n in suite),
    ]
    for name in names:
        row = f"  {name:<20}"
        for timings in suite.values():
            match = next((t for t in timings if t.name == name), None)
            row += f"{match.micros:>15.1f} " if match else f"{'n/a':>16}"
        lines.append(row)

    lines += [
        "",
        f"  {'implementation':<20}{'best':>10}{'worst':>10}{'vs core (best)':>16}",
    ]
    for name in names:
        values = [
            t.micros for timings in suite.values() for t in timings if t.name == name
        ]
        if values:
            lines.append(
                f"  {name:<20}{min(values):>10.1f}{max(values):>10.1f}"
                f"{min(values) / core_us:>15.0f}x",
            )

    lines += [
        "",
        "  Note the all-pairs form is the SLOWEST in software while being the fastest in",
        "  hardware. It evaluates all N^2 = 1024 pairs unconditionally, where the",
        "  sequential loop short-circuits as boxes get suppressed. That redundant work is",
        "  what a CPU pays for and P parallel lanes get for free -- which is precisely the",
        "  argument for the restructure, quantified.",
        "",
        "  Always name the processor class alongside any speedup. See docs/plan.md Part 1e:",
        "  against tuned C/AVX2 the accelerator is roughly at parity, and end to end over",
        "  the UART the CPU wins outright.",
    ]
    return "\n".join(lines)
