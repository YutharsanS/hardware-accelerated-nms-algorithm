"""Measure what sorting one 3DGS frame actually costs, and derive the hardware spec.

Three numbers matter, and only the first two can be measured locally:

* what a CPU takes (this machine, numpy) -- establishes whether a bottleneck exists at all;
* what the workload *is* -- instances per frame, which fixes the throughput requirement;
* what a GPU takes (from the literature) -- establishes the bar an accelerator must clear.

The derived spec is the output: sorted elements per clock cycle, and the working-set size
per tile and per frame. Those two decide whether a given FPGA can host the design.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from .project import Camera, Instances, sort_key

# Published figure for a CUB device radix sort over 64-bit composite keys at 4K.
GPU_RADIX_MS_4K = (2.5, 4.2)
KEY_BYTES = 8  # 32-bit tile id + 32-bit depth, as the reference packs it


@dataclass(frozen=True)
class SortCost:
    """Measured sort cost and the hardware requirement it implies.

    Attributes:
        n_instances: Instances in the frame.
        n_splats: Surviving splats in the frame.
        n_tiles: Tiles covering the image.
        global_key_ms: Wall time to sort the packed 64-bit keys.
        per_tile_ms: Wall time to sort each tile's depths independently.
        median_tile: Median instances in an occupied tile.
        max_tile: Largest instance count in any tile.
        target_fps: Frame rate the spec is derived for.
        clock_hz: Clock the elements-per-cycle figure is derived for.
    """

    n_instances: int
    n_splats: int
    n_tiles: int
    global_key_ms: float
    per_tile_ms: float
    median_tile: float
    max_tile: int
    target_fps: float
    clock_hz: float

    @property
    def instances_per_second(self) -> float:
        """Return the sorted-element rate needed to sustain ``target_fps``."""
        return self.n_instances * self.target_fps

    @property
    def elements_per_cycle(self) -> float:
        """Return the sorted elements per clock cycle needed at ``clock_hz``."""
        return self.instances_per_second / self.clock_hz

    @property
    def frame_working_set_mb(self) -> float:
        """Return the memory needed to hold one frame's keys, in megabytes."""
        return self.n_instances * KEY_BYTES / 1e6

    @property
    def tile_working_set_kb(self) -> float:
        """Return the memory needed to hold the largest tile's keys, in kilobytes."""
        return self.max_tile * KEY_BYTES / 1e3

    def network_undersize(self, n: int) -> float:
        """Return how many times too small a fixed size-``n`` network is.

        Args:
            n: Capacity of a fixed sorting network.

        Returns:
            Ratio of the median tile occupancy to ``n``.
        """
        return self.median_tile / n


def _bench(fn: object, repeats: int = 3) -> float:
    """Time a callable, discarding one warm-up run.

    Args:
        fn: Zero-argument callable to time.
        repeats: Timed repetitions.

    Returns:
        Mean wall time in milliseconds.
    """
    fn()  # type: ignore[operator]
    start = time.perf_counter()
    for _ in range(repeats):
        fn()  # type: ignore[operator]
    return (time.perf_counter() - start) / repeats * 1e3


def measure(
    instances: Instances,
    camera: Camera,
    n_splats: int,
    *,
    target_fps: float = 30.0,
    clock_hz: float = 100e6,
) -> SortCost:
    """Measure CPU sort cost for one frame and derive the hardware requirement.

    Args:
        instances: The frame's instance list.
        camera: The view, providing the tile count.
        n_splats: Surviving splats in that frame.
        target_fps: Frame rate the derived spec should sustain.
        clock_hz: Clock frequency for the elements-per-cycle figure.

    Returns:
        The measurement and derived spec.
    """
    keys = sort_key(instances)
    counts = np.bincount(instances.tile_id, minlength=camera.n_tiles)
    occupied = counts[counts > 0]

    order = np.argsort(instances.tile_id, kind="stable")
    depths = instances.depth[order]
    bounds = np.concatenate([[0], np.cumsum(counts)])

    def per_tile() -> None:
        for i in range(camera.n_tiles):
            lo, hi = bounds[i], bounds[i + 1]
            if hi - lo > 1:
                np.sort(depths[lo:hi])

    return SortCost(
        n_instances=len(instances),
        n_splats=n_splats,
        n_tiles=camera.n_tiles,
        global_key_ms=_bench(lambda: np.sort(keys, kind="stable")),
        per_tile_ms=_bench(per_tile, repeats=1),
        median_tile=float(np.median(occupied)) if occupied.size else 0.0,
        max_tile=int(occupied.max()) if occupied.size else 0,
        target_fps=target_fps,
        clock_hz=clock_hz,
    )


def format_report(cost: SortCost, *, frame_budget_ms: float = 1000.0 / 30.0) -> str:
    """Render the measurement and derived spec as plain text.

    Args:
        cost: A completed measurement.
        frame_budget_ms: Frame budget to compare CPU cost against.

    Returns:
        A multi-line report.
    """
    lines = [
        (
            f"workload: {cost.n_instances:,} instances from {cost.n_splats:,} splats "
            f"({cost.n_instances / max(cost.n_splats, 1):.1f} per splat) over {cost.n_tiles:,} tiles"
        ),
        "",
        f"CPU cost for one frame (budget {frame_budget_ms:.1f} ms at {cost.target_fps:g} fps):",
        (
            f"  global 64-bit key sort : {cost.global_key_ms:9.1f} ms "
            f"({cost.global_key_ms / frame_budget_ms:5.1f}x over budget)"
        ),
        (
            f"  per-tile depth sorts   : {cost.per_tile_ms:9.1f} ms "
            f"({cost.per_tile_ms / frame_budget_ms:5.1f}x over budget)"
        ),
        f"  GPU CUB radix @4K (lit): {GPU_RADIX_MS_4K[0]:5.1f}-{GPU_RADIX_MS_4K[1]:.1f} ms  -- real-time",
        "",
        "derived hardware requirement:",
        f"  sorted elements/second : {cost.instances_per_second / 1e6:9.0f} M/s",
        f"  at {cost.clock_hz / 1e6:.0f} MHz              : {cost.elements_per_cycle:9.2f} elements per cycle",
        f"  a 1 elem/cycle sorter delivers {100.0 / max(cost.elements_per_cycle, 1e-9):.0f}% of requirement",
        "",
        "memory, which decides whether a device can host it:",
        f"  whole frame            : {cost.frame_working_set_mb:9.1f} MB",
        f"  largest single tile    : {cost.tile_working_set_kb:9.1f} KB",
        f"  Basys 3 total BRAM     : {1800 / 8:9.1f} KB  (no external DRAM on the board)",
        "",
        (
            "fixed-network sizing against the median tile "
            f"({cost.median_tile:,.0f} elements):"
        ),
    ]
    lines += [
        f"  N={n:<5} is {cost.network_undersize(n):6.1f}x too small"
        for n in (32, 64, 128, 256, 512)
    ]
    return "\n".join(lines)
