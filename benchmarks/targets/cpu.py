"""Software NMS on this machine: our own variants and the libraries edge pipelines call.

Each implementation is an :class:`Impl`: ``prepare`` converts a batch into the
implementation's native input *outside* the timed region (tensors for torchvision, lists
for OpenCV), and ``timed`` runs one call and returns ``(keep_mask, microseconds)``.

* ``ours`` -- ``integer_sequential``, ``integer_allpairs`` and ``numpy_allpairs`` from the
  golden model, and ``c_scalar`` from ``benchmarks/c/nms_bench.c``. Each must match
  ``model.nms_sequential`` bit for bit.
* ``library`` -- ``torchvision.ops.nms`` (Ultralytics) and ``cv2.dnn.NMSBoxes`` (OpenCV
  DNN). They are checked against ``inputs.nms_strict``, since they suppress on ``>``.

A library that is not installed is skipped with a note, so the harness runs with the core
dependencies alone. ``uv run --extra bench`` adds torchvision and OpenCV.
"""

from __future__ import annotations

import ctypes
import platform
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from benchmarks.inputs import Batch
from models.nms import bench, model
from models.nms import params as p

IOU = p.T_INT / (1 << p.K_SHIFT)
"""0.5: the threshold ``T_INT = 128`` encodes, handed to the float libraries."""
C_SOURCE = Path(__file__).resolve().parent.parent / "c" / "nms_bench.c"
C_BUILD_DIR = Path(__file__).resolve().parents[2] / "build" / "bench"
FIELDS = 5


@dataclass(frozen=True)
class Impl:
    """One timed NMS implementation.

    Attributes:
        name: Label in the results.
        kind: ``ours`` (must equal the golden model) or ``library`` (checked against ``>``).
        prepare: Converts a batch to native input, untimed.
        timed: Runs one call on prepared input; returns ``(keep_mask, microseconds)``.
        notes: Anything the results must say about how this one was built or called.
    """

    name: str
    kind: str
    prepare: Callable[[Batch], Any]
    timed: Callable[[Any], tuple[int, float]]
    notes: dict[str, str] = field(default_factory=dict)


def _wall(fn: Callable[[], int]) -> tuple[int, float]:
    t0 = time.perf_counter_ns()
    keep = fn()
    return keep, (time.perf_counter_ns() - t0) / 1e3


def _mask(indices: list[int], slots: list[int]) -> int:
    keep = 0
    for i in indices:
        keep |= 1 << slots[int(i)]
    return keep


def _slots(batch: Batch) -> list[int]:
    return [i for i in range(len(batch.boxes)) if (batch.present_mask >> i) & 1]


# --- ours -----------------------------------------------------------------------------


def golden_impls() -> list[Impl]:
    """Return the golden model's forms, timed from Python.

    Returns:
        ``integer_sequential``, ``integer_allpairs`` and ``numpy_allpairs``.
    """

    def numpy_timed(b: Batch) -> tuple[int, float]:
        if b.present_mask != (1 << len(b.boxes)) - 1:
            msg = "numpy_allpairs has no present_mask; every slot must be present"
            raise ValueError(msg)
        return _wall(lambda: bench.numpy_allpairs(b.boxes))

    return [
        Impl(
            "integer_sequential",
            "ours",
            lambda b: b,
            lambda b: _wall(lambda: model.nms_sequential(b.boxes, b.present_mask)),
        ),
        Impl(
            "integer_allpairs",
            "ours",
            lambda b: b,
            lambda b: _wall(lambda: model.nms_allpairs(b.boxes, b.present_mask)),
        ),
        Impl("numpy_allpairs", "ours", lambda b: b, numpy_timed),
    ]


def c_flags() -> list[str]:
    """Return the compiler flags for this machine.

    ``-mcpu=native`` on a Pi 5 selects the Cortex-A76; ``-march=native`` on x86 lets the
    compiler use whatever vector units the host has. Neither is hand-written SIMD.

    Returns:
        Flags for a shared library at ``-O3``.
    """
    tune = (
        "-mcpu=native"
        if platform.machine() in ("aarch64", "arm64")
        else "-march=native"
    )
    return ["-O3", tune, "-shared", "-fPIC", "-std=c11", "-Wall", "-Wextra", "-Werror"]


def build_c_library(build_dir: Path = C_BUILD_DIR) -> Path:
    """Compile ``nms_bench.c`` into a shared library, if it is missing or stale.

    Args:
        build_dir: Where the library goes.

    Returns:
        Path to the library.

    Raises:
        RuntimeError: If no C compiler is found or compilation fails.
    """
    lib = build_dir / "libnmsbench.so"
    if lib.exists() and lib.stat().st_mtime >= C_SOURCE.stat().st_mtime:
        return lib
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        msg = "no C compiler on PATH (cc, gcc or clang)"
        raise RuntimeError(msg)
    build_dir.mkdir(parents=True, exist_ok=True)
    cmd = [cc, *c_flags(), "-o", str(lib), str(C_SOURCE)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        msg = f"C build failed: {' '.join(cmd)}\n{result.stderr}"
        raise RuntimeError(msg)
    return lib


def compiler_version() -> str:
    """Return the first line of ``cc --version``, for the results metadata.

    Returns:
        The version line, or an empty string.
    """
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        return ""
    out = subprocess.run([cc, "--version"], capture_output=True, text=True, check=False)
    return out.stdout.splitlines()[0] if out.stdout else ""


def load_c_library(path: Path | None = None) -> ctypes.CDLL:
    """Build if needed, load, and declare the C entry points.

    Args:
        path: A prebuilt library; built from source when omitted.

    Returns:
        The loaded library.
    """
    lib = ctypes.CDLL(str(path or build_c_library()))
    lib.nms_scalar.argtypes = [
        ctypes.POINTER(ctypes.c_int32),
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint64),
    ]
    lib.nms_scalar.restype = ctypes.c_uint32
    lib.timer_overhead_ns.argtypes = [ctypes.c_int]
    lib.timer_overhead_ns.restype = ctypes.c_uint64
    return lib


def c_impl(lib: ctypes.CDLL) -> Impl:
    """Return the C variant, timed inside C.

    Args:
        lib: From :func:`load_c_library`.

    Returns:
        ``c_scalar``.
    """
    ns = ctypes.c_uint64()

    def prepare(b: Batch) -> tuple[Any, int]:
        flat = [v for box in b.boxes for v in box]
        return (ctypes.c_int32 * (len(b.boxes) * FIELDS))(*flat), b.present_mask

    def timed(arg: tuple[Any, int]) -> tuple[int, float]:
        keep = lib.nms_scalar(arg[0], arg[1], ctypes.byref(ns))
        return int(keep), ns.value / 1e3

    return Impl(
        "c_scalar",
        "ours",
        prepare,
        timed,
        notes={
            "flags": " ".join(c_flags()[:2]),
            "compiler": compiler_version(),
            "timer_overhead_ns": str(lib.timer_overhead_ns(10_000)),
        },
    )


# --- libraries ------------------------------------------------------------------------


def torchvision_impl() -> Impl | None:
    """Return ``torchvision.ops.nms``, single-threaded, or None if not installed.

    Returns:
        The implementation, or None.
    """
    try:
        import torch
        import torchvision
    except ImportError:
        return None
    torch.set_num_threads(1)

    def prepare(b: Batch) -> tuple[Any, Any, list[int]]:
        slots = _slots(b)
        xyxy = torch.tensor([b.boxes[i][:4] for i in slots], dtype=torch.float32)
        score = torch.tensor(
            [b.boxes[i].score / p.SCORE_MAX for i in slots], dtype=torch.float32
        )
        return xyxy, score, slots

    def timed(arg: tuple[Any, Any, list[int]]) -> tuple[int, float]:
        t0 = time.perf_counter_ns()
        kept = torchvision.ops.nms(arg[0], arg[1], IOU)
        us = (time.perf_counter_ns() - t0) / 1e3
        return _mask(kept.tolist(), arg[2]), us

    return Impl("torchvision", "library", prepare, timed, notes={"threads": "1"})


def opencv_impl() -> Impl | None:
    """Return ``cv2.dnn.NMSBoxes``, single-threaded, or None if not installed.

    OpenCV drops scores at or below its threshold, which must be >= 0, so scores are
    shifted by one quantisation step: ``(q + 1) / 65536`` keeps a zero score and the order.

    Returns:
        The implementation, or None.
    """
    try:
        import cv2
    except ImportError:
        return None
    cv2.setNumThreads(1)

    def prepare(b: Batch) -> tuple[list, list, list[int]]:
        slots = _slots(b)
        xywh = [
            [
                b.boxes[i].x,
                b.boxes[i].y,
                b.boxes[i].a - b.boxes[i].x,
                b.boxes[i].b - b.boxes[i].y,
            ]
            for i in slots
        ]
        score = [(b.boxes[i].score + 1) / (p.SCORE_MAX + 1) for i in slots]
        return xywh, score, slots

    def timed(arg: tuple[list, list, list[int]]) -> tuple[int, float]:
        t0 = time.perf_counter_ns()
        kept = cv2.dnn.NMSBoxes(arg[0], arg[1], 0.0, IOU)
        us = (time.perf_counter_ns() - t0) / 1e3
        return _mask([int(i) for i in kept], arg[2]), us

    return Impl("opencv", "library", prepare, timed, notes={"threads": "1"})


def all_impls(names: list[str] | None = None) -> tuple[list[Impl], list[str]]:
    """Return every implementation available here, optionally filtered by name.

    Args:
        names: Only these; all when omitted.

    Returns:
        ``(implementations, skipped)``, the second listing what could not load and why.
    """
    impls = golden_impls()
    skipped = []
    try:
        impls.append(c_impl(load_c_library()))
    except (RuntimeError, OSError) as exc:
        skipped.append(f"c_scalar: {exc}")
    for name, factory in (("torchvision", torchvision_impl), ("opencv", opencv_impl)):
        impl = factory()
        if impl is None:
            skipped.append(f"{name}: not installed (uv run --extra bench)")
        else:
            impls.append(impl)
    if names:
        unknown = (
            set(names) - {i.name for i in impls} - {s.split(":")[0] for s in skipped}
        )
        if unknown:
            msg = f"unknown implementation(s): {', '.join(sorted(unknown))}"
            raise ValueError(msg)
        impls = [i for i in impls if i.name in names]
    return impls, skipped
