"""Load a pretrained 3D Gaussian Splatting scene from a ``.ply`` file.

The 3DGS reference implementation (Kerbl et al., SIGGRAPH 2023) stores each Gaussian
as a vertex with float32 properties::

    x, y, z              mean position (world space)
    nx, ny, nz           unused normals, written but ignored
    f_dc_0..2            spherical-harmonic DC term (base colour)
    f_rest_0..N          higher-order SH coefficients, 0/9/24/45 of them for degree 0..3
    opacity              *logit* of alpha, needs a sigmoid
    scale_0..2           *log* of the axis scales, needs an exp
    rot_0..3             rotation quaternion (w, x, y, z), needs normalising

Only geometry and opacity are needed for tile/depth analysis, so the SH coefficients are
parsed but the higher orders are discarded by default to keep memory down.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# SH degree-0 basis constant: colour = SH_C0 * f_dc + 0.5
SH_C0 = 0.28209479177387814

_PLY_TO_NUMPY = {
    "char": "i1",
    "int8": "i1",
    "uchar": "u1",
    "uint8": "u1",
    "short": "i2",
    "int16": "i2",
    "ushort": "u2",
    "uint16": "u2",
    "int": "i4",
    "int32": "i4",
    "uint": "u4",
    "uint32": "u4",
    "float": "f4",
    "float32": "f4",
    "double": "f8",
    "float64": "f8",
}


@dataclass(frozen=True)
class PlyHeader:
    """Parsed header of a binary or ASCII ``.ply`` file.

    Attributes:
        count: Number of vertices in the ``vertex`` element.
        properties: ``(name, ply_type)`` pairs in file order.
        byte_order: Numpy byte-order prefix, ``"<"``, ``">"`` or ``"="`` for ASCII.
        is_ascii: True when the body is ASCII rather than packed binary.
        data_offset: Byte offset of the first vertex, immediately past ``end_header``.
    """

    count: int
    properties: list[tuple[str, str]]
    byte_order: str
    is_ascii: bool
    data_offset: int

    def numpy_dtype(self) -> np.dtype:
        """Build the structured dtype describing one vertex.

        Returns:
            A numpy structured dtype matching the property list and byte order.

        Raises:
            ValueError: If a property uses a PLY type with no numpy equivalent.
        """
        fields = []
        for name, ply_type in self.properties:
            if ply_type not in _PLY_TO_NUMPY:
                msg = f"unsupported PLY property type {ply_type!r} for {name!r}"
                raise ValueError(msg)
            fields.append((name, self.byte_order + _PLY_TO_NUMPY[ply_type]))
        return np.dtype(fields)


@dataclass(frozen=True)
class Gaussians:
    """A 3DGS scene with activations already applied.

    Attributes:
        means: ``(N, 3)`` float32 world-space centres.
        opacity: ``(N,)`` float32 alpha in ``[0, 1]`` (sigmoid of the stored logit).
        scales: ``(N, 3)`` float32 positive axis scales (exp of the stored log).
        quats: ``(N, 4)`` float32 unit quaternions in ``(w, x, y, z)`` order.
        colours: ``(N, 3)`` float32 base RGB from the SH DC term, or None if skipped.
        sh_degree: Spherical-harmonic degree inferred from the ``f_rest_*`` count.
    """

    means: np.ndarray
    opacity: np.ndarray
    scales: np.ndarray
    quats: np.ndarray
    colours: np.ndarray | None
    sh_degree: int

    def __len__(self) -> int:
        """Return the number of Gaussians in the scene."""
        return int(self.means.shape[0])


def read_header(path: str | Path) -> PlyHeader:
    """Read and parse the header of a ``.ply`` file.

    Args:
        path: Path to the ``.ply`` file.

    Returns:
        The parsed header, including the byte offset where vertex data begins.

    Raises:
        ValueError: If the file is not a PLY, declares an unknown format, or has no
            ``vertex`` element.
    """
    props: list[tuple[str, str]] = []
    count: int | None = None
    byte_order = "<"
    is_ascii = False
    in_vertex = False

    with Path(path).open("rb") as handle:
        if handle.readline().strip() != b"ply":
            msg = f"{path} is not a PLY file"
            raise ValueError(msg)
        while True:
            raw = handle.readline()
            if not raw:
                msg = f"{path} ended before end_header"
                raise ValueError(msg)
            line = raw.decode("ascii", "replace").strip()
            if line == "end_header":
                offset = handle.tell()
                break
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "format":
                fmt = parts[1]
                if fmt == "binary_little_endian":
                    byte_order = "<"
                elif fmt == "binary_big_endian":
                    byte_order = ">"
                elif fmt == "ascii":
                    byte_order, is_ascii = "=", True
                else:
                    msg = f"unknown PLY format {fmt!r}"
                    raise ValueError(msg)
            elif parts[0] == "element":
                in_vertex = parts[1] == "vertex"
                if in_vertex:
                    count = int(parts[2])
            elif parts[0] == "property" and in_vertex:
                props.append((parts[2], parts[1]))

    if count is None:
        msg = f"{path} has no vertex element"
        raise ValueError(msg)
    return PlyHeader(count, props, byte_order, is_ascii, offset)


def _sh_degree(names: set[str]) -> int:
    """Infer the SH degree from the number of ``f_rest_*`` properties.

    Args:
        names: All vertex property names present in the file.

    Returns:
        SH degree 0-3, or 0 when no higher-order coefficients are stored.
    """
    n_rest = sum(1 for n in names if n.startswith("f_rest_"))
    # (degree+1)^2 - 1 coefficients per channel, 3 channels
    for degree in (3, 2, 1):
        if n_rest >= 3 * ((degree + 1) ** 2 - 1):
            return degree
    return 0


def load_gaussians(path: str | Path, *, with_colour: bool = True) -> Gaussians:
    """Load a 3DGS ``.ply`` and apply the stored activations.

    Opacity is sigmoid-activated, scales are exponentiated and quaternions are
    normalised, so the returned values are directly usable for projection.

    Args:
        path: Path to the ``.ply`` file.
        with_colour: Whether to decode the SH DC term into a base RGB colour.
            Set False to skip it when only geometry is needed.

    Returns:
        The scene with activations applied.

    Raises:
        ValueError: If required 3DGS properties are missing, or the file is truncated.
    """
    header = read_header(path)
    dtype = header.numpy_dtype()
    names = {name for name, _ in header.properties}

    required = {
        "x",
        "y",
        "z",
        "opacity",
        *(f"scale_{i}" for i in range(3)),
        *(f"rot_{i}" for i in range(4)),
    }
    missing = sorted(required - names)
    if missing:
        msg = f"{path} is not a 3DGS PLY, missing properties: {missing}"
        raise ValueError(msg)

    if header.is_ascii:
        raw = np.loadtxt(path, skiprows=0, dtype=dtype, max_rows=header.count)
    else:
        raw = np.fromfile(
            path, dtype=dtype, count=header.count, offset=header.data_offset
        )
    if raw.shape[0] != header.count:
        msg = f"{path} truncated: header says {header.count} vertices, read {raw.shape[0]}"
        raise ValueError(msg)

    means = np.stack([raw["x"], raw["y"], raw["z"]], axis=1).astype(np.float32)
    # stored opacity is a logit; sigmoid computed in a numerically stable form
    logit = raw["opacity"].astype(np.float32)
    opacity = np.where(
        logit >= 0, 1.0 / (1.0 + np.exp(-logit)), np.exp(logit) / (1.0 + np.exp(logit))
    )
    scales = np.exp(
        np.stack([raw[f"scale_{i}"] for i in range(3)], axis=1).astype(np.float32)
    )
    quats = np.stack([raw[f"rot_{i}"] for i in range(4)], axis=1).astype(np.float32)
    quats /= np.maximum(np.linalg.norm(quats, axis=1, keepdims=True), 1e-12)

    colours = None
    if with_colour and {"f_dc_0", "f_dc_1", "f_dc_2"} <= names:
        dc = np.stack([raw[f"f_dc_{i}"] for i in range(3)], axis=1).astype(np.float32)
        colours = np.clip(SH_C0 * dc + 0.5, 0.0, 1.0)

    return Gaussians(
        means=means,
        opacity=opacity.astype(np.float32),
        scales=scales,
        quats=quats,
        colours=colours,
        sh_degree=_sh_degree(names),
    )


def summarise(scene: Gaussians) -> dict[str, object]:
    """Collect scalar statistics describing a loaded scene.

    Useful as a sanity check that activations were applied correctly: opacity must lie
    in ``[0, 1]`` and scales must be strictly positive.

    Args:
        scene: A loaded scene.

    Returns:
        A mapping of statistic name to value, safe to log or serialise.
    """
    return {
        "n_gaussians": len(scene),
        "sh_degree": scene.sh_degree,
        "mean_extent": np.ptp(scene.means, axis=0).tolist(),
        "centroid": scene.means.mean(axis=0).tolist(),
        "opacity_min": float(scene.opacity.min()),
        "opacity_median": float(np.median(scene.opacity)),
        "opacity_max": float(scene.opacity.max()),
        "scale_min": float(scene.scales.min()),
        "scale_median": float(np.median(scene.scales)),
        "scale_max": float(scene.scales.max()),
        "quat_norm_max_error": float(
            np.abs(np.linalg.norm(scene.quats, axis=1) - 1.0).max()
        ),
        "has_colour": scene.colours is not None,
    }


_PROP_RE = re.compile(r"^(f_dc|f_rest|scale|rot)_(\d+)$")


def property_groups(header: PlyHeader) -> dict[str, int]:
    """Count the indexed property families present in a header.

    Args:
        header: A parsed PLY header.

    Returns:
        Mapping from family name (``f_dc``, ``f_rest``, ``scale``, ``rot``) to count.
    """
    groups: dict[str, int] = {}
    for name, _ in header.properties:
        match = _PROP_RE.match(name)
        if match:
            groups[match.group(1)] = groups.get(match.group(1), 0) + 1
    return groups
