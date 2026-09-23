"""Derive plausible camera poses from a scene's own geometry.

The pretrained ``.ply`` files carry no camera parameters, so views have to be synthesised.
Naively framing the full bounding box is wrong: real scenes carry a small population of
far-field "floater" Gaussians (in the Tanks & Temples ``train`` scene, ~8% of Gaussians lie
outside a 40-unit radius while 77% sit within 5 units), which would push the camera so far
back that the subject occupies a handful of pixels.

So the core is located robustly first, then cameras are orbited around it at a distance
that fills the frame -- which is what the dataset's real capture rig does.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .project import Camera


@dataclass(frozen=True)
class SceneCore:
    """The dense subject of a scene, ignoring far-field outliers.

    Attributes:
        centre: ``(3,)`` centre of the dense region.
        radius: Radius enclosing the chosen fraction of Gaussians about that centre.
        fraction: Fraction of Gaussians actually inside ``radius``.
    """

    centre: np.ndarray
    radius: float
    fraction: float


def robust_core(
    means: np.ndarray, *, quantile: float = 0.7, bins: int = 32
) -> SceneCore:
    """Locate the dense core of a scene.

    A coarse 3D histogram finds the densest voxel, then the radius is taken as the
    ``quantile`` distance of all Gaussians from it. Using the mode rather than the mean
    keeps the far-field floaters from dragging the centre away from the subject.

    The default ``quantile`` is deliberately 0.7, not 0.9. On the ``train`` scene, 0.9
    yields a radius of 23 units and a camera 40 units back, which frames the floaters
    rather than the subject and drops the workload to 3.4 M instances at 3.4 per splat.
    At 0.7 the radius is 4 units, the camera sits 7 units back, and the result is 13.4 M
    instances at 18.1 per splat -- inside the 5-20 range reported for real captures. See
    ``docs/phase0_findings.md`` for the full framing sensitivity table.

    Args:
        means: ``(N, 3)`` Gaussian centres.
        quantile: Fraction of Gaussians the radius should enclose.
        bins: Histogram resolution per axis.

    Returns:
        The located core.
    """
    hist, edges = np.histogramdd(means, bins=bins)
    peak = np.unravel_index(np.argmax(hist), hist.shape)
    centre = np.array(
        [0.5 * (edges[d][peak[d]] + edges[d][peak[d] + 1]) for d in range(3)]
    )

    dist = np.linalg.norm(means - centre, axis=1)
    radius = float(np.quantile(dist, quantile))
    return SceneCore(
        centre=centre, radius=radius, fraction=float((dist <= radius).mean())
    )


def orbit(
    core: SceneCore,
    n_views: int = 8,
    *,
    width: int = 1920,
    height: int = 1080,
    fov_x_deg: float = 60.0,
    elevations_deg: tuple[float, ...] = (-10.0, 10.0),
    fill: float = 1.0,
) -> list[Camera]:
    """Generate cameras orbiting a scene core.

    Args:
        core: The located dense core.
        n_views: Number of azimuths to sample.
        width: Image width in pixels.
        height: Image height in pixels.
        fov_x_deg: Horizontal field of view in degrees.
        elevations_deg: Elevations to sample at each azimuth.
        fill: Fraction of the frame the core should span; 1.0 just fits it.

    Returns:
        ``n_views * len(elevations_deg)`` cameras, all looking at the core centre.
    """
    half_fov = np.deg2rad(fov_x_deg) * 0.5
    distance = core.radius / (np.tan(half_fov) * max(fill, 1e-3))

    cams = []
    for elev in elevations_deg:
        phi = np.deg2rad(elev)
        for azim in np.linspace(0.0, 2.0 * np.pi, n_views, endpoint=False):
            offset = distance * np.array(
                [np.cos(phi) * np.sin(azim), np.sin(phi), np.cos(phi) * np.cos(azim)],
            )
            cams.append(
                Camera.look_at(
                    core.centre + offset,
                    core.centre,
                    width=width,
                    height=height,
                    fov_x_deg=fov_x_deg,
                ),
            )
    return cams
