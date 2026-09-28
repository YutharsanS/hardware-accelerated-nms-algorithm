"""The detector workload: YOLO inference, as a real edge pipeline runs it.

Two ways to use it (plan.md Phase E, "Pi 5 under load"):

* ``pipeline``, the main condition -- :class:`Detector` in the benchmark's own process.
  Each timed NMS call follows one inference, as it does per frame in a real pipeline, so
  the call meets whatever the network left in the caches.
* ``concurrent`` -- this module run as a separate process with ``--loop``, inferring
  continuously on the cores the benchmark is not pinned to. That measures contention for
  the shared cache and memory.

Needs the ``bench-load`` extra (``ultralytics``, AGPL-3.0; plan.md E.6). Images default to
the two that ship with Ultralytics; pass ``--images`` for a directory of JPEGs.

Usage, as the concurrent worker::

    python -m benchmarks.workloads.detector --loop [--weights yolov8n.pt] [--images DIR]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

DEFAULT_WEIGHTS = "yolov8n.pt"
READY = "ready"
"""Printed once the first inference is done, so the parent starts timing under load."""


class Detector:
    """A YOLO model and a cycle of images, one inference per :meth:`step`."""

    def __init__(
        self, weights: str = DEFAULT_WEIGHTS, images: Path | None = None
    ) -> None:
        """Load the model and the images, and run one warm-up inference.

        Args:
            weights: Ultralytics weights, downloaded on first use.
            images: Directory of JPEGs; the Ultralytics sample images when omitted.

        Raises:
            RuntimeError: If ultralytics is not installed or no images are found.
        """
        try:
            import cv2
            from ultralytics import YOLO
            from ultralytics.utils import ASSETS
        except ImportError as exc:
            msg = "the detector load needs ultralytics: uv run --extra bench-load"
            raise RuntimeError(msg) from exc
        folder = images or ASSETS
        files = sorted(Path(folder).glob("*.jpg"))
        if not files:
            msg = f"no .jpg images in {folder}"
            raise RuntimeError(msg)
        self.frames = [cv2.imread(str(f)) for f in files]
        self.model = YOLO(weights)
        self.weights = weights
        self.k = 0
        self.step()

    def step(self) -> None:
        """Run inference on the next image."""
        frame = self.frames[self.k % len(self.frames)]
        self.k += 1
        self.model.predict(frame, verbose=False)


def main() -> None:
    """Run inference forever, as the ``concurrent`` background load."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--loop", action="store_true", required=True)
    ap.add_argument("--weights", default=DEFAULT_WEIGHTS)
    ap.add_argument("--images", type=Path)
    ap.add_argument("--cpus", help="comma-separated CPUs to run on")
    args = ap.parse_args()
    if args.cpus:
        os.sched_setaffinity(0, {int(c) for c in args.cpus.split(",")})
    detector = Detector(args.weights, args.images)
    sys.stdout.write(READY + "\n")
    sys.stdout.flush()
    while True:
        detector.step()


if __name__ == "__main__":
    main()
