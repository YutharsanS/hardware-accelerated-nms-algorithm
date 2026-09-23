"""Write synthetic 3DGS ``.ply`` scenes with known ground truth.

A real scene is ~250 MB and needs downloading; these scenes are small, deterministic and
have properties chosen so downstream stages can be checked against hand-computed answers.
Every field uses the same storage convention as the reference implementation (opacity as a
logit, scales as logs, quaternion in ``w, x, y, z`` order), so the loader and projector see
exactly the format they will see in the wild.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

# Property order matching the reference 3DGS writer, SH degree 0 (no f_rest).
_PROPS = (
    "x",
    "y",
    "z",
    "nx",
    "ny",
    "nz",
    "f_dc_0",
    "f_dc_1",
    "f_dc_2",
    "opacity",
    "scale_0",
    "scale_1",
    "scale_2",
    "rot_0",
    "rot_1",
    "rot_2",
    "rot_3",
)


@dataclass(frozen=True)
class SyntheticScene:
    """Ground truth for a generated scene, for asserting against loader output.

    Attributes:
        means: ``(N, 3)`` world-space centres as written.
        opacity: ``(N,)`` alpha values in ``[0, 1]`` (pre-logit, i.e. what a loader
            should return after its sigmoid).
        scales: ``(N, 3)`` positive axis scales (pre-log, i.e. post-exp values).
        quats: ``(N, 4)`` unit quaternions in ``(w, x, y, z)`` order.
        colours: ``(N, 3)`` base RGB in ``[0, 1]``.
    """

    means: np.ndarray
    opacity: np.ndarray
    scales: np.ndarray
    quats: np.ndarray
    colours: np.ndarray

    def __len__(self) -> int:
        """Return the number of Gaussians."""
        return int(self.means.shape[0])


def _logit(alpha: np.ndarray) -> np.ndarray:
    """Invert the sigmoid so the written value round-trips through a loader.

    Args:
        alpha: Opacities strictly inside ``(0, 1)``.

    Returns:
        The corresponding logits.
    """
    clipped = np.clip(alpha, 1e-6, 1.0 - 1e-6)
    return np.log(clipped / (1.0 - clipped))


def write_ply(path: str | Path, scene: SyntheticScene) -> Path:
    """Write a scene as a binary little-endian 3DGS ``.ply``.

    Args:
        path: Destination file path.
        scene: The scene to serialise.

    Returns:
        The path written.
    """
    n = len(scene)
    header = f"ply\nformat binary_little_endian 1.0\nelement vertex {n}\n"
    header += "".join(f"property float {p}\n" for p in _PROPS)
    header += "end_header\n"

    table = np.zeros((n, len(_PROPS)), dtype="<f4")
    table[:, 0:3] = scene.means
    table[:, 3:6] = 0.0  # normals, unused
    table[:, 6:9] = (scene.colours - 0.5) / 0.28209479177387814  # invert SH DC decode
    table[:, 9] = _logit(scene.opacity)
    table[:, 10:13] = np.log(np.maximum(scene.scales, 1e-12))
    table[:, 13:17] = scene.quats

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as handle:
        handle.write(header.encode("ascii"))
        handle.write(table.tobytes(order="C"))
    return out


def planes_scene(
    depths: tuple[float, ...] = (2.0, 4.0, 8.0),
    per_plane: int = 64,
    extent: float = 1.0,
    alpha: float = 0.5,
    scale: float = 0.02,
    seed: int = 0,
) -> SyntheticScene:
    """Build fronto-parallel planes of Gaussians at known camera-space depths.

    Chosen so tile occupancy and depth ordering are both predictable: every Gaussian on a
    plane shares a depth, so a correct sort must group them by plane.

    Args:
        depths: Z distances of each plane from the origin, looking down +Z.
        per_plane: Gaussians per plane, laid out on a jittered square grid.
        extent: Half-width of each plane in world units.
        alpha: Opacity given to every Gaussian.
        scale: Isotropic scale given to every Gaussian.
        seed: Seed for the grid jitter.

    Returns:
        The generated scene.
    """
    rng = np.random.default_rng(seed)
    side = int(np.ceil(np.sqrt(per_plane)))
    lin = np.linspace(-extent, extent, side)
    grid_x, grid_y = np.meshgrid(lin, lin)
    base = np.stack([grid_x.ravel(), grid_y.ravel()], axis=1)[:per_plane]

    means = []
    for z in depths:
        jitter = rng.uniform(-extent / side, extent / side, size=base.shape)
        xy = base + jitter
        means.append(np.concatenate([xy, np.full((len(xy), 1), z)], axis=1))
    stacked = np.concatenate(means, axis=0).astype(np.float32)

    n = len(stacked)
    quats = np.zeros((n, 4), dtype=np.float32)
    quats[:, 0] = 1.0  # identity rotation
    return SyntheticScene(
        means=stacked,
        opacity=np.full(n, alpha, dtype=np.float32),
        scales=np.full((n, 3), scale, dtype=np.float32),
        quats=quats,
        colours=np.tile(np.array([0.8, 0.3, 0.2], dtype=np.float32), (n, 1)),
    )


def clustered_scene(
    n_clusters: int = 40,
    per_cluster: int = 500,
    z_range: tuple[float, float] = (2.0, 20.0),
    spread: float = 0.15,
    seed: int = 0,
) -> SyntheticScene:
    """Build clustered Gaussians with a wide spread of scales and opacities.

    This deliberately produces uneven screen coverage, so the per-tile histogram has real
    variance to measure rather than the uniform occupancy a grid would give.

    Args:
        n_clusters: Number of clusters.
        per_cluster: Gaussians per cluster.
        z_range: Range of cluster centre depths.
        spread: Standard deviation of Gaussian positions within a cluster.
        seed: Seed for all randomness.

    Returns:
        The generated scene.
    """
    rng = np.random.default_rng(seed)
    centres = np.column_stack(
        [
            rng.uniform(-3.0, 3.0, n_clusters),
            rng.uniform(-2.0, 2.0, n_clusters),
            rng.uniform(*z_range, n_clusters),
        ],
    )
    means = np.concatenate(
        [c + rng.normal(0.0, spread, size=(per_cluster, 3)) for c in centres],
        axis=0,
    ).astype(np.float32)

    n = len(means)
    # log-uniform scales spanning two decades, which is what drives tile-count variance
    scales = np.exp(rng.uniform(np.log(0.005), np.log(0.25), size=(n, 3))).astype(
        np.float32
    )
    quats = rng.normal(size=(n, 4)).astype(np.float32)
    quats /= np.linalg.norm(quats, axis=1, keepdims=True)
    return SyntheticScene(
        means=means,
        opacity=rng.uniform(0.05, 0.99, n).astype(np.float32),
        scales=scales,
        quats=quats,
        colours=rng.uniform(0.0, 1.0, size=(n, 3)).astype(np.float32),
    )
