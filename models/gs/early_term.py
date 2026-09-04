"""Measure how much of each tile's depth-sorted list alpha blending actually consumes.

3DGS blends front-to-back and stops a pixel once its accumulated transmittance falls
below ``1e-4``. If tiles saturate after a small prefix of their list, then a *full* sort is
wasted work and a top-K selector is the right primitive -- which would change the hardware
substantially. This module measures that prefix.

The measurement is per-pixel and then reduced with a max, because a tile may only stop
once *every* pixel it covers has saturated. Taking the mean instead would flatter the
result and understate the sort length required.

Blending follows ``renderCUDA`` in the reference: alpha is the 2D Gaussian evaluated at the
pixel times the splat opacity, clamped to 0.99, contributions below 1/255 are skipped, and
a pixel is done once transmittance would drop below 1e-4.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .project import TILE, Camera, Instances, Splats, conic_from_cov2d

MIN_ALPHA = 1.0 / 255.0
MAX_ALPHA = 0.99
T_CUTOFF = 1e-4


@dataclass(frozen=True)
class EarlyTermStats:
    """How far into each tile's sorted list blending had to go.

    Attributes:
        tile_ids: ``(M,)`` tiles that were sampled.
        occupancy: ``(M,)`` instances present in each sampled tile.
        consumed: ``(M,)`` prefix length needed so every sampled pixel saturated or ran out.
        saturated: ``(M,)`` bool, whether every sampled pixel reached the cutoff at all.
    """

    tile_ids: np.ndarray
    occupancy: np.ndarray
    consumed: np.ndarray
    saturated: np.ndarray

    @property
    def fraction(self) -> np.ndarray:
        """Return the consumed fraction of each sampled tile's list."""
        return self.consumed / np.maximum(self.occupancy, 1)


def measure(
    splats: Splats,
    instances: Instances,
    camera: Camera,
    *,
    n_tiles_sampled: int = 200,
    pixel_stride: int = 4,
    seed: int = 0,
) -> EarlyTermStats:
    """Measure the consumed prefix of the sorted list for a sample of tiles.

    Args:
        splats: Projected splats, providing centres, conics and opacities.
        instances: The frame's instance list.
        camera: The view, providing the tile grid.
        n_tiles_sampled: How many occupied tiles to sample.
        pixel_stride: Sample every Nth pixel in each axis of the 16x16 tile.
        seed: Seed for tile sampling.

    Returns:
        Per-sampled-tile statistics.
    """
    rng = np.random.default_rng(seed)
    grid_x, _ = camera.grid
    conic = conic_from_cov2d(splats.cov2d)

    counts = np.bincount(instances.tile_id, minlength=camera.n_tiles)
    occupied = np.flatnonzero(counts > 0)
    chosen = rng.choice(
        occupied, size=min(n_tiles_sampled, occupied.size), replace=False
    )

    # group instances by tile once, then slice
    order = np.argsort(instances.tile_id, kind="stable")
    tile_sorted = instances.tile_id[order]
    splat_sorted = instances.splat[order]
    depth_sorted = instances.depth[order]
    starts = np.searchsorted(tile_sorted, chosen, side="left")
    ends = np.searchsorted(tile_sorted, chosen, side="right")

    offsets = np.arange(0, TILE, pixel_stride, dtype=np.float64) + 0.5

    consumed = np.zeros(chosen.size, dtype=np.int64)
    saturated = np.zeros(chosen.size, dtype=bool)
    occupancy = (ends - starts).astype(np.int64)

    for i, tile in enumerate(chosen):
        lo, hi = starts[i], ends[i]
        idx = splat_sorted[lo:hi]
        # front-to-back within this tile
        local = np.argsort(depth_sorted[lo:hi], kind="stable")
        idx = idx[local]

        tx, ty = int(tile % grid_x), int(tile // grid_x)
        px = tx * TILE + offsets
        py = ty * TILE + offsets
        gx, gy = np.meshgrid(px, py)
        pix = np.stack([gx.ravel(), gy.ravel()], axis=1)  # (P, 2)

        dx = splats.centre[idx, 0][:, None] - pix[None, :, 0]
        dy = splats.centre[idx, 1][:, None] - pix[None, :, 1]
        con = conic[idx]
        power = (
            -0.5 * (con[:, 0:1] * dx * dx + con[:, 2:3] * dy * dy)
            - con[:, 1:2] * dx * dy
        )
        alpha = np.minimum(
            MAX_ALPHA, splats.opacity[idx][:, None] * np.exp(np.minimum(power, 0.0))
        )
        alpha[alpha < MIN_ALPHA] = 0.0

        # transmittance after each contribution, per pixel
        trans = np.cumprod(1.0 - alpha, axis=0)
        done = trans < T_CUTOFF
        any_done = done.any(axis=0)
        # first index at which each pixel saturates; unsaturated pixels need the whole list
        first = np.where(any_done, done.argmax(axis=0) + 1, alpha.shape[0])
        consumed[i] = int(first.max())
        saturated[i] = bool(any_done.all())

    return EarlyTermStats(
        tile_ids=chosen,
        occupancy=occupancy,
        consumed=consumed,
        saturated=saturated,
    )


def format_report(stats: EarlyTermStats) -> str:
    """Render the early-termination measurement as plain text.

    Args:
        stats: A completed measurement.

    Returns:
        A multi-line report.
    """
    frac = stats.fraction
    lines = [
        (
            f"sampled {stats.tile_ids.size} occupied tiles; "
            f"{100.0 * stats.saturated.mean():.1f}% had every sampled pixel saturate"
        ),
        "",
        f"  {'':>14} {'occupancy':>10} {'consumed':>10} {'fraction':>9}",
        (
            f"  {'median':>14} {np.median(stats.occupancy):10,.0f} {np.median(stats.consumed):10,.0f} "
            f"{np.median(frac):8.1%}"
        ),
        f"  {'mean':>14} {stats.occupancy.mean():10,.0f} {stats.consumed.mean():10,.0f} {frac.mean():8.1%}",
        (
            f"  {'90th pct':>14} {np.percentile(stats.occupancy, 90):10,.0f} "
            f"{np.percentile(stats.consumed, 90):10,.0f} {np.percentile(frac, 90):8.1%}"
        ),
        f"  {'max':>14} {stats.occupancy.max():10,.0f} {stats.consumed.max():10,.0f} {frac.max():8.1%}",
        "",
        "implication: a top-K selector suffices only if the consumed fraction is small AND",
        "the K needed is far below the tile occupancy; otherwise a full sort is required.",
    ]
    return "\n".join(lines)
