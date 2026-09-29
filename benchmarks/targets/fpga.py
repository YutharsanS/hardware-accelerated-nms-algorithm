"""The Basys 3 over the UART, end to end, timed from this machine.

This measures the **full system**: USB, the FTDI latency timer, 264 + 6 bytes at 1 Mbaud,
and the core's 0.80 µs, which is about 0.02% of it. Its rows go in their own "end to end"
column and are never set against a compute time. The core figure it
sits beside comes from :func:`core_row`, not from this measurement.

Every reply is checked against the golden model; the board implements ``>=``, so it must
match ``model.nms_sequential`` exactly, like our own software variants.
"""

from __future__ import annotations

from benchmarks.inputs import Batch
from models.nms import host
from models.nms import params as p

SETTLE_CYCLES = 1
"""box_store computes each area one edge after its record lands; tb_nms_core pins it."""
CORE_NOTE = (
    "T = 80 captured on silicon by the ILA (16 of 16 windows); load and settle pinned in "
    "tb_nms_core, in simulation"
)


def full_latency_cycles() -> int:
    """Return the block's latency from the first record in to ``done``.

    One record per cycle through ``we``, the one-cycle area settle, then T. This, not T
    alone, is what a whole software NMS call is compared with (docs/results/benchmarks.md §2).

    Returns:
        ``N + SETTLE + T``: 113 at the shipped generics.
    """
    return p.N + SETTLE_CYCLES + p.latency_cycles()


def core_row() -> dict[str, float | str]:
    """Return the core's own latency, for the column beside the system figure.

    Returns:
        T, and the full latency with load and settle, in cycles and microseconds.
    """
    t = p.latency_cycles()
    full = full_latency_cycles()
    return {
        "impl": "core",
        "cycles": t,
        "t_us": t / p.CLOCK_HZ * 1e6,
        "full_cycles": full,
        "full_us": full / p.CLOCK_HZ * 1e6,
        "note": CORE_NOTE,
    }


def measure(board: host.Board, group: list[Batch], count: int) -> list[float]:
    """Time ``count`` round trips cycling through ``group``, checking every answer.

    Args:
        board: The connected board.
        group: Batches to send, in order.
        count: Round trips.

    Returns:
        Round-trip times in microseconds.

    Raises:
        host.LatencyError: On a timeout, a rejected frame, or a wrong ``keep_mask``.
    """
    work = [(b.boxes, b.present_mask, b.keep_mask) for b in group]
    return [ms * 1e3 for ms in host.measure_latency(board, count, work)]
