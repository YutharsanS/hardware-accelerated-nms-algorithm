"""Alpha-blend the sorted per-tile lists into an image.

This exists to *validate* the projection. The unit checks in Phase 0 confirm depths,
radii, screen centres and key ordering against hand-computed values, but they cannot
catch an error that is self-consistent yet wrong -- a transposed covariance, a flipped
axis, a bad quaternion convention. Rendering the scene and looking at it can.

It is also the measuring instrument for key-width analysis: quantising the depth key
perturbs the blend order, and the honest way to quantify that is to render both ways and
compare, rather than counting order inversions that may be visually irrelevant.

Blending follows ``renderCUDA`` in the reference: front-to-back, alpha = 2D Gaussian
evaluated at the pixel times splat opacity clamped to 0.99, contributions under 1/255
skipped, and a pixel stops once transmittance would fall below 1e-4.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .project import TILE, Camera, Instances, Splats, conic_from_cov2d

MIN_ALPHA = 1.0 / 255.0
MAX_ALPHA = 0.99
T_CUTOFF = 1e-4


def render(
    splats: Splats,
    instances: Instances,
    camera: Camera,
    *,
    background: tuple[float, float, float] = (0.0, 0.0, 0.0),
    depth_key: np.ndarray | None = None,
) -> np.ndarray:
    """Render one frame by blending each tile's depth-sorted splats.

    Args:
        splats: Projected splats with centres, covariances, opacities and colours.
        instances: The frame's instance list.
        camera: The view being rendered.
        background: RGB colour composited behind the splats.
        depth_key: Optional alternative per-instance sort key, used to render with a
            quantised depth ordering. Defaults to the exact float depths.

    Returns:
        ``(height, width, 3)`` float32 image with values in ``[0, 1]``.

    Raises:
        ValueError: If the scene carried no colours, so nothing can be shaded.
    """
    if splats.colour is None:
        msg = "scene has no colours; load with with_colour=True to render"
        raise ValueError(msg)

    grid_x, _ = camera.grid
    conic = conic_from_cov2d(splats.cov2d)
    image = np.tile(
        np.asarray(background, dtype=np.float32), (camera.height, camera.width, 1)
    )

    keys = instances.depth if depth_key is None else depth_key
    order = np.argsort(instances.tile_id, kind="stable")
    tiles = instances.tile_id[order]
    splat_of = instances.splat[order]
    keys = keys[order]

    starts = np.searchsorted(tiles, np.arange(camera.n_tiles), side="left")
    ends = np.searchsorted(tiles, np.arange(camera.n_tiles), side="right")

    offs = np.arange(TILE, dtype=np.float64) + 0.5
    for tile in range(camera.n_tiles):
        lo, hi = starts[tile], ends[tile]
        if hi <= lo:
            continue
        idx = splat_of[lo:hi][np.argsort(keys[lo:hi], kind="stable")]

        tx, ty = tile % grid_x, tile // grid_x
        x0, y0 = tx * TILE, ty * TILE
        x1, y1 = min(x0 + TILE, camera.width), min(y0 + TILE, camera.height)
        if x1 <= x0 or y1 <= y0:
            continue

        gx, gy = np.meshgrid(x0 + offs[: x1 - x0], y0 + offs[: y1 - y0])
        pix = np.stack([gx.ravel(), gy.ravel()], axis=1)

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

        # transmittance before each contribution, and the cutoff mask
        trans_before = np.concatenate(
            [np.ones((1, alpha.shape[1])), np.cumprod(1.0 - alpha, axis=0)[:-1]],
            axis=0,
        )
        alive = trans_before >= T_CUTOFF
        weight = alpha * trans_before * alive

        colours = splats.colour[idx]  # (n, 3)
        acc = weight.T @ colours  # (pixels, 3)
        tail = (trans_before[-1] * (1.0 - alpha[-1]) * alive[-1])[:, None]
        acc += tail * np.asarray(background, dtype=np.float64)

        image[y0:y1, x0:x1] = acc.reshape(y1 - y0, x1 - x0, 3).astype(np.float32)

    return np.clip(image, 0.0, 1.0)


def quantise_depth(
    depth: np.ndarray, bits: int, *, reciprocal: bool = False
) -> np.ndarray:
    """Quantise camera-space depths to a fixed number of bits.

    Args:
        depth: ``(K,)`` positive camera-space depths.
        bits: Number of bits in the quantised key.
        reciprocal: Quantise ``1/z`` instead of ``z``, which allocates precision to the
            near field the way a hardware depth buffer does.

    Returns:
        ``(K,)`` uint32 quantised keys preserving depth order.
    """
    values = 1.0 / np.maximum(depth, 1e-9) if reciprocal else depth.astype(np.float64)
    lo, hi = float(values.min()), float(values.max())
    span = max(hi - lo, 1e-12)
    levels = (1 << bits) - 1
    q = np.rint((values - lo) / span * levels).astype(np.uint32)
    # reciprocal reverses the ordering, so flip it back
    return (levels - q).astype(np.uint32) if reciprocal else q


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    """Compute peak signal-to-noise ratio between two images in ``[0, 1]``.

    Args:
        a: Reference image.
        b: Comparison image.

    Returns:
        PSNR in decibels, or ``inf`` when the images are identical.
    """
    mse = float(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))
    return float("inf") if mse == 0.0 else float(10.0 * np.log10(1.0 / mse))


def save_png(image: np.ndarray, path: str | Path) -> Path:
    """Write a float image to a PNG.

    Args:
        image: ``(H, W, 3)`` float image with values in ``[0, 1]``.
        path: Destination path.

    Returns:
        The path written.
    """
    from PIL import Image  # optional dependency, only needed to save

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray((np.clip(image, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)).save(out)
    return out
