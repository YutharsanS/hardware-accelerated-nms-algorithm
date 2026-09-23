"""Reimplement the 3DGS rasterizer preprocess stage: project, cull, assign to tiles.

This mirrors ``preprocessCUDA`` and ``duplicateWithKeys`` in the reference implementation
(``diff-gaussian-rasterization/cuda_rasterizer/forward.cu``), vectorised over all
Gaussians with numpy so it runs without a GPU. The output is the set of
``(tile_id, depth, gaussian_index)`` instances that the reference sorts with one radix
pass over a 64-bit ``tile << 32 | depth`` key.

Conventions kept identical to the reference so the statistics are comparable:

* 16x16 pixel tiles.
* Near-plane cull at ``z < 0.2`` in camera space.
* View-space ``x/z``, ``y/z`` clamped to ``+-1.3 * tan(fov/2)`` before the Jacobian.
* A ``+0.3`` low-pass term added to the 2D covariance diagonal (antialiasing).
* Screen radius ``ceil(3 * sqrt(max_eigenvalue))`` of the 2D covariance.
* Depth key taken from camera-space ``z``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .load_ply import Gaussians

TILE = 16
NEAR_PLANE = 0.2
LOW_PASS = 0.3
CLAMP_FACTOR = 1.3


@dataclass(frozen=True)
class Camera:
    """A pinhole camera with a world-to-camera transform.

    Attributes:
        rotation: ``(3, 3)`` world-to-camera rotation.
        translation: ``(3,)`` world-to-camera translation, applied after rotation.
        width: Image width in pixels.
        height: Image height in pixels.
        fx: Focal length in pixels, horizontal.
        fy: Focal length in pixels, vertical.
    """

    rotation: np.ndarray
    translation: np.ndarray
    width: int
    height: int
    fx: float
    fy: float

    @property
    def tan_fovx(self) -> float:
        """Return the tangent of the horizontal half field of view."""
        return 0.5 * self.width / self.fx

    @property
    def tan_fovy(self) -> float:
        """Return the tangent of the vertical half field of view."""
        return 0.5 * self.height / self.fy

    @property
    def grid(self) -> tuple[int, int]:
        """Return the tile grid dimensions ``(tiles_x, tiles_y)``."""
        return ((self.width + TILE - 1) // TILE, (self.height + TILE - 1) // TILE)

    @property
    def n_tiles(self) -> int:
        """Return the total number of tiles covering the image."""
        gx, gy = self.grid
        return gx * gy

    @classmethod
    def look_at(
        cls,
        eye: np.ndarray,
        target: np.ndarray,
        width: int = 1920,
        height: int = 1080,
        fov_x_deg: float = 60.0,
        up: np.ndarray | None = None,
    ) -> Camera:
        """Build a camera positioned at ``eye`` looking towards ``target``.

        Args:
            eye: ``(3,)`` camera position in world space.
            target: ``(3,)`` point to look at.
            width: Image width in pixels.
            height: Image height in pixels.
            fov_x_deg: Horizontal field of view in degrees.
            up: World up direction; defaults to ``+Y``.

        Returns:
            A camera whose ``+Z`` axis points from ``eye`` towards ``target``.
        """
        eye = np.asarray(eye, dtype=np.float64).reshape(3)
        target = np.asarray(target, dtype=np.float64).reshape(3)
        up_vec = (
            np.array([0.0, 1.0, 0.0])
            if up is None
            else np.asarray(up, dtype=np.float64)
        )

        forward = target - eye
        forward /= max(np.linalg.norm(forward), 1e-12)
        if abs(float(np.dot(forward, up_vec))) > 0.999:  # degenerate up
            up_vec = np.array([0.0, 0.0, 1.0])
        right = np.cross(up_vec, forward)
        right /= max(np.linalg.norm(right), 1e-12)
        true_up = np.cross(forward, right)

        rotation = np.stack([right, true_up, forward], axis=0)  # rows = camera axes
        fx = 0.5 * width / np.tan(np.deg2rad(fov_x_deg) * 0.5)
        return cls(
            rotation=rotation,
            translation=-rotation @ eye,
            width=width,
            height=height,
            fx=float(fx),
            fy=float(fx),
        )


@dataclass(frozen=True)
class Splats:
    """Per-Gaussian screen-space quantities for the Gaussians that survived culling.

    Attributes:
        index: ``(M,)`` indices back into the original scene.
        centre: ``(M, 2)`` screen-space centres in pixels.
        depth: ``(M,)`` camera-space z, the value used as the depth key.
        cov2d: ``(M, 3)`` packed 2D covariance ``(a, b, c)`` for ``[[a, b], [b, c]]``.
        radius: ``(M,)`` integer screen radius, ``ceil(3 sigma)``.
        opacity: ``(M,)`` alpha in ``[0, 1]``.
        colour: ``(M, 3)`` base RGB, or None when the scene carried no colour.
        n_tiles: ``(M,)`` number of tiles each splat touches.
    """

    index: np.ndarray
    centre: np.ndarray
    depth: np.ndarray
    cov2d: np.ndarray
    radius: np.ndarray
    opacity: np.ndarray
    colour: np.ndarray | None
    n_tiles: np.ndarray

    def __len__(self) -> int:
        """Return the number of surviving splats."""
        return int(self.index.shape[0])


@dataclass(frozen=True)
class Instances:
    """The splat-tile instances that the reference renderer sorts.

    One entry per (splat, overlapped tile) pair, which is why the count exceeds the
    number of Gaussians. Sorting these by ``(tile_id, depth)`` is the operation this
    project is investigating.

    Attributes:
        tile_id: ``(K,)`` int32 flat tile index, ``ty * grid_x + tx``.
        depth: ``(K,)`` float32 camera-space depth.
        splat: ``(K,)`` int32 index into the owning :class:`Splats`.
    """

    tile_id: np.ndarray
    depth: np.ndarray
    splat: np.ndarray

    def __len__(self) -> int:
        """Return the number of instances."""
        return int(self.tile_id.shape[0])


def quats_to_rotations(quats: np.ndarray) -> np.ndarray:
    """Convert ``(w, x, y, z)`` quaternions to rotation matrices.

    Matches ``build_rotation`` in the reference implementation.

    Args:
        quats: ``(N, 4)`` unit quaternions ordered ``(w, x, y, z)``.

    Returns:
        ``(N, 3, 3)`` rotation matrices.
    """
    w, x, y, z = quats[:, 0], quats[:, 1], quats[:, 2], quats[:, 3]
    out = np.empty((quats.shape[0], 3, 3), dtype=np.float64)
    out[:, 0, 0] = 1 - 2 * (y * y + z * z)
    out[:, 0, 1] = 2 * (x * y - w * z)
    out[:, 0, 2] = 2 * (x * z + w * y)
    out[:, 1, 0] = 2 * (x * y + w * z)
    out[:, 1, 1] = 1 - 2 * (x * x + z * z)
    out[:, 1, 2] = 2 * (y * z - w * x)
    out[:, 2, 0] = 2 * (x * z - w * y)
    out[:, 2, 1] = 2 * (y * z + w * x)
    out[:, 2, 2] = 1 - 2 * (x * x + y * y)
    return out


def project(scene: Gaussians, camera: Camera, *, scale_modifier: float = 1.0) -> Splats:
    """Project Gaussians to screen space and compute their 2D covariances.

    Performs the near-plane and zero-area culls the reference performs, so the returned
    splats are exactly those the renderer would rasterise.

    Args:
        scene: The loaded scene.
        camera: The view to project into.
        scale_modifier: Global multiplier on Gaussian scales, as in the reference.

    Returns:
        Screen-space quantities for the surviving splats.
    """
    means = scene.means.astype(np.float64)
    p_view = means @ camera.rotation.T + camera.translation

    keep = p_view[:, 2] > NEAR_PLANE
    idx = np.flatnonzero(keep)
    p_view = p_view[keep]
    z = p_view[:, 2]

    # screen-space centre from the perspective divide
    cx = camera.fx * p_view[:, 0] / z + 0.5 * camera.width
    cy = camera.fy * p_view[:, 1] / z + 0.5 * camera.height

    # 3D covariance: Sigma = (R S)(R S)^T
    rot = quats_to_rotations(scene.quats[keep])
    s = (scene.scales[keep].astype(np.float64) * scale_modifier)[:, None, :]
    m = rot * s  # scale columns of R
    cov3d = m @ np.transpose(m, (0, 2, 1))

    # EWA Jacobian, with the reference's clamp on x/z and y/z
    limx = CLAMP_FACTOR * camera.tan_fovx
    limy = CLAMP_FACTOR * camera.tan_fovy
    tx = np.clip(p_view[:, 0] / z, -limx, limx) * z
    ty = np.clip(p_view[:, 1] / z, -limy, limy) * z

    jac = np.zeros((len(z), 2, 3), dtype=np.float64)
    jac[:, 0, 0] = camera.fx / z
    jac[:, 0, 2] = -camera.fx * tx / (z * z)
    jac[:, 1, 1] = camera.fy / z
    jac[:, 1, 2] = -camera.fy * ty / (z * z)

    w_rot = camera.rotation
    cov_cam = w_rot @ cov3d @ w_rot.T
    cov2d = jac @ cov_cam @ np.transpose(jac, (0, 2, 1))

    a = cov2d[:, 0, 0] + LOW_PASS
    b = cov2d[:, 0, 1]
    c = cov2d[:, 1, 1] + LOW_PASS

    # radius from the larger eigenvalue, exactly as the reference computes it
    det = a * c - b * b
    mid = 0.5 * (a + c)
    disc = np.sqrt(np.maximum(0.1, mid * mid - det))
    lam = np.maximum(mid + disc, mid - disc)
    radius = np.ceil(3.0 * np.sqrt(np.maximum(lam, 0.0))).astype(np.int32)

    valid = det != 0.0
    idx, cx, cy, z = idx[valid], cx[valid], cy[valid], z[valid]
    a, b, c, radius = a[valid], b[valid], c[valid], radius[valid]

    n_tiles = tiles_touched(cx, cy, radius, camera)
    hit = n_tiles > 0

    colour = None if scene.colours is None else scene.colours[idx[hit]]
    return Splats(
        index=idx[hit].astype(np.int32),
        centre=np.stack([cx[hit], cy[hit]], axis=1).astype(np.float32),
        depth=z[hit].astype(np.float32),
        cov2d=np.stack([a[hit], b[hit], c[hit]], axis=1).astype(np.float32),
        radius=radius[hit],
        opacity=scene.opacity[idx[hit]],
        colour=colour,
        n_tiles=n_tiles[hit].astype(np.int32),
    )


def tile_rect(
    cx: np.ndarray,
    cy: np.ndarray,
    radius: np.ndarray,
    camera: Camera,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Compute each splat's axis-aligned tile rectangle.

    Mirrors ``getRect`` in the reference: the bounding box is clamped to the grid, and
    the maximum is rounded up so a partially covered tile still counts.

    Args:
        cx: Screen-space centre x, in pixels.
        cy: Screen-space centre y, in pixels.
        radius: Screen radius in pixels.
        camera: The view, providing the tile grid.

    Returns:
        ``(x_min, y_min, x_max, y_max)`` in tile units, half-open on the maxima.
    """
    gx, gy = camera.grid
    x_min = np.clip((cx - radius) // TILE, 0, gx).astype(np.int32)
    y_min = np.clip((cy - radius) // TILE, 0, gy).astype(np.int32)
    x_max = np.clip((cx + radius + TILE - 1) // TILE, 0, gx).astype(np.int32)
    y_max = np.clip((cy + radius + TILE - 1) // TILE, 0, gy).astype(np.int32)
    return x_min, y_min, x_max, y_max


def tiles_touched(
    cx: np.ndarray,
    cy: np.ndarray,
    radius: np.ndarray,
    camera: Camera,
) -> np.ndarray:
    """Count how many tiles each splat overlaps.

    Args:
        cx: Screen-space centre x, in pixels.
        cy: Screen-space centre y, in pixels.
        radius: Screen radius in pixels.
        camera: The view, providing the tile grid.

    Returns:
        ``(N,)`` int64 counts, zero for splats entirely off screen.
    """
    x_min, y_min, x_max, y_max = tile_rect(cx, cy, radius, camera)
    return np.maximum(x_max - x_min, 0).astype(np.int64) * np.maximum(
        y_max - y_min, 0
    ).astype(np.int64)


def expand_instances(splats: Splats, camera: Camera) -> Instances:
    """Expand each splat into one instance per overlapped tile.

    This is ``duplicateWithKeys`` in the reference. It is done fully vectorised: the
    per-splat rectangles are flattened with a repeat-and-offset trick rather than a
    Python loop, so a multi-million-instance frame takes a few seconds.

    Args:
        splats: Projected splats with their tile counts.
        camera: The view, providing the tile grid.

    Returns:
        The complete instance list, unsorted.
    """
    gx, _ = camera.grid
    x_min, y_min, x_max, y_max = tile_rect(
        splats.centre[:, 0], splats.centre[:, 1], splats.radius, camera
    )
    span_x = np.maximum(x_max - x_min, 0).astype(np.int64)
    span_y = np.maximum(y_max - y_min, 0).astype(np.int64)
    counts = span_x * span_y

    total = int(counts.sum())
    splat_of = np.repeat(np.arange(len(splats), dtype=np.int64), counts)
    # index of each instance within its own splat's rectangle
    starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
    local = np.arange(total, dtype=np.int64) - np.repeat(starts, counts)

    span_x_rep = np.repeat(span_x, counts)
    tx = np.repeat(x_min.astype(np.int64), counts) + local % span_x_rep
    ty = np.repeat(y_min.astype(np.int64), counts) + local // span_x_rep

    return Instances(
        tile_id=(ty * gx + tx).astype(np.int32),
        depth=splats.depth[splat_of],
        splat=splat_of.astype(np.int32),
    )


def sort_key(instances: Instances) -> np.ndarray:
    """Build the reference's 64-bit composite sort key.

    The reference packs ``tile_id`` into the high 32 bits and the raw float32 depth bit
    pattern into the low 32 bits, which orders correctly for positive depths because
    IEEE-754 positive floats compare identically to their unsigned bit patterns.

    Args:
        instances: The instance list.

    Returns:
        ``(K,)`` uint64 keys.
    """
    depth_bits = instances.depth.astype(np.float32).view(np.uint32).astype(np.uint64)
    return (instances.tile_id.astype(np.uint64) << np.uint64(32)) | depth_bits


def preprocess(scene: Gaussians, camera: Camera) -> tuple[Splats, Instances]:
    """Run the full preprocess stage for one view.

    Args:
        scene: The loaded scene.
        camera: The view to render.

    Returns:
        The surviving splats and their expanded tile instances.
    """
    splats = project(scene, camera)
    return splats, expand_instances(splats, camera)


def conic_from_cov2d(cov2d: np.ndarray) -> np.ndarray:
    """Invert packed 2D covariances into the conic form used when evaluating splats.

    The reference stores ``con_o = (conic.x, conic.y, conic.z, opacity)`` and evaluates
    ``power = -0.5 * (conic.x * dx^2 + conic.z * dy^2) - conic.y * dx * dy``.

    Args:
        cov2d: ``(M, 3)`` packed ``(a, b, c)`` covariances for ``[[a, b], [b, c]]``.

    Returns:
        ``(M, 3)`` packed conics ``(c/det, -b/det, a/det)``.
    """
    a, b, c = cov2d[:, 0], cov2d[:, 1], cov2d[:, 2]
    det = np.maximum(a * c - b * b, 1e-12)
    return np.stack([c / det, -b / det, a / det], axis=1)
