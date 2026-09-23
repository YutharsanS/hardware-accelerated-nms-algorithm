"""Measure the per-tile depth-sorting workload of a 3DGS frame.

This is the artifact Phase 0 exists to produce. The literature reports Gaussians-per-tile
varying by two orders of magnitude, which is precisely what breaks a *fixed-parallelism*
sorter -- and a bitonic network is exactly that. So the numbers that matter are:

* the distribution of instances per tile, not its mean;
* for a candidate network size ``N``, what fraction of tiles fit, and more importantly
  what fraction of the total *work* lives in the tiles that do not.

A sorter sized at the median can be useless if the tail holds most of the instances.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .project import Camera, Instances

CANDIDATE_SIZES = (32, 64, 128, 256, 512, 1024, 2048)


@dataclass(frozen=True)
class TileStats:
    """Distribution of sorting work across the tiles of one or more frames.

    Attributes:
        counts: ``(n_tiles,)`` instances per tile, including empty tiles.
        n_frames: How many camera poses were accumulated.
        n_instances: Total instances across those frames.
        n_splats: Total surviving splats across those frames.
    """

    counts: np.ndarray
    n_frames: int
    n_instances: int
    n_splats: int

    @property
    def occupied(self) -> np.ndarray:
        """Return the counts of non-empty tiles only."""
        return self.counts[self.counts > 0]

    def percentiles(
        self, qs: tuple[float, ...] = (50, 75, 90, 95, 99, 99.9)
    ) -> dict[str, float]:
        """Summarise the occupied-tile distribution.

        Args:
            qs: Percentiles to report.

        Returns:
            Mapping of label to value, plus ``max`` and the tail-to-median ratio.
        """
        occ = self.occupied
        if occ.size == 0:
            return {}
        out = {f"p{q:g}": float(np.percentile(occ, q)) for q in qs}
        out["max"] = float(occ.max())
        out["mean"] = float(occ.mean())
        out["max_over_median"] = float(occ.max() / max(np.median(occ), 1.0))
        return out

    def coverage(self, sizes: tuple[int, ...] = CANDIDATE_SIZES) -> list[CoverageRow]:
        """Compute how well each candidate sorter size covers the workload.

        Args:
            sizes: Candidate fixed sorter capacities.

        Returns:
            One row per candidate size.
        """
        occ = self.occupied
        total_inst = float(occ.sum())
        rows = []
        for n in sizes:
            fits = occ <= n
            # instances that a size-n sorter handles in one pass, plus what spills
            handled = float(occ[fits].sum())
            passes = np.ceil(occ / n)
            rows.append(
                CoverageRow(
                    size=n,
                    tile_fraction=float(fits.mean()),
                    instance_fraction=handled / total_inst if total_inst else 0.0,
                    padding_waste=float(
                        (n * fits.sum() - occ[fits].sum()) / max(n * fits.sum(), 1)
                    ),
                    mean_passes=float(passes.mean()),
                    max_passes=int(passes.max()),
                ),
            )
        return rows


@dataclass(frozen=True)
class CoverageRow:
    """How a single fixed sorter size copes with the measured distribution.

    Attributes:
        size: The sorter capacity ``N``.
        tile_fraction: Fraction of occupied tiles with at most ``N`` instances.
        instance_fraction: Fraction of all instances living in those tiles.
        padding_waste: Fraction of comparator work wasted padding short tiles up to ``N``.
        mean_passes: Mean number of size-``N`` passes needed per tile.
        max_passes: Worst-case passes for the busiest tile.
    """

    size: int
    tile_fraction: float
    instance_fraction: float
    padding_waste: float
    mean_passes: float
    max_passes: int


@dataclass
class StatsAccumulator:
    """Accumulate tile statistics over several camera poses.

    Attributes:
        n_tiles: Number of tiles per frame, fixed by the camera.
        counts: Running per-tile totals.
        n_frames: Frames accumulated so far.
        n_instances: Instances accumulated so far.
        n_splats: Splats accumulated so far.
    """

    n_tiles: int
    counts: np.ndarray = field(init=False)
    n_frames: int = 0
    n_instances: int = 0
    n_splats: int = 0

    def __post_init__(self) -> None:
        """Allocate the per-tile accumulator."""
        self.counts = np.zeros(self.n_tiles, dtype=np.int64)

    def add(self, instances: Instances, n_splats: int) -> None:
        """Fold one frame's instances into the accumulator.

        Args:
            instances: That frame's expanded instance list.
            n_splats: Number of surviving splats in that frame.
        """
        self.counts += np.bincount(instances.tile_id, minlength=self.n_tiles).astype(
            np.int64
        )
        self.n_frames += 1
        self.n_instances += len(instances)
        self.n_splats += n_splats

    def result(self) -> TileStats:
        """Return the accumulated statistics.

        Returns:
            A frozen snapshot; per-tile counts are averaged over frames.
        """
        return TileStats(
            counts=self.counts // max(self.n_frames, 1),
            n_frames=self.n_frames,
            n_instances=self.n_instances,
            n_splats=self.n_splats,
        )


def per_frame_stats(instances: Instances, camera: Camera) -> TileStats:
    """Compute tile statistics for a single frame.

    Args:
        instances: The frame's instance list.
        camera: The view, providing the tile count.

    Returns:
        Statistics for that frame alone.
    """
    counts = np.bincount(instances.tile_id, minlength=camera.n_tiles).astype(np.int64)
    n_splats = int(np.unique(instances.splat).size) if len(instances) else 0
    return TileStats(
        counts=counts, n_frames=1, n_instances=len(instances), n_splats=n_splats
    )


def histogram(stats: TileStats, n_bins: int = 24) -> tuple[np.ndarray, np.ndarray]:
    """Build a log-spaced histogram of occupied-tile counts.

    Log spacing because the distribution spans orders of magnitude; a linear histogram
    would put almost everything in the first bin and hide the tail that matters.

    Args:
        stats: Measured statistics.
        n_bins: Number of log-spaced bins.

    Returns:
        ``(counts, edges)`` as returned by :func:`numpy.histogram`.
    """
    occ = stats.occupied
    if occ.size == 0:
        return np.zeros(0, dtype=np.int64), np.zeros(0)
    edges = np.unique(np.geomspace(1, max(occ.max(), 2), n_bins).astype(np.int64))
    return np.histogram(occ, bins=edges)


def format_report(stats: TileStats, sizes: tuple[int, ...] = CANDIDATE_SIZES) -> str:
    """Render the statistics as a plain-text report.

    Args:
        stats: Measured statistics.
        sizes: Candidate sorter capacities to tabulate.

    Returns:
        A multi-line report suitable for printing or writing to a file.
    """
    occ = stats.occupied
    lines = [
        f"frames={stats.n_frames}  splats={stats.n_splats:,}  instances={stats.n_instances:,}",
        (
            f"tiles total={stats.counts.size:,}  occupied={occ.size:,} "
            f"({100.0 * occ.size / max(stats.counts.size, 1):.1f}%)"
        ),
        f"instances per splat (mean) = {stats.n_instances / max(stats.n_splats, 1):.2f}",
        "",
        "occupied-tile distribution:",
    ]
    pcts = stats.percentiles()
    lines += [f"  {k:>16} = {v:,.1f}" for k, v in pcts.items()]

    lines += ["", "log-spaced histogram of instances per occupied tile:"]
    hist, edges = histogram(stats)
    peak = max(hist.max(), 1) if hist.size else 1
    for i, count in enumerate(hist):
        bar = "#" * int(40 * count / peak)
        lines.append(f"  {edges[i]:>7,}-{edges[i + 1] - 1:<7,} {count:>7,} {bar}")

    lines += [
        "",
        "fixed-size sorter coverage:",
        f"  {'N':>6} {'tiles fit':>10} {'work in fit':>12} {'pad waste':>10} {'mean pass':>10} {'max pass':>9}",
    ]
    for row in stats.coverage(sizes):
        lines.append(
            f"  {row.size:>6} {row.tile_fraction:>9.1%} {row.instance_fraction:>11.1%} "
            f"{row.padding_waste:>9.1%} {row.mean_passes:>10.2f} {row.max_passes:>9}",
        )
    return "\n".join(lines)
