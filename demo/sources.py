"""Where frames come from: a webcam, video files, or a set of still images.

All three answer :meth:`Source.read` with a :class:`Grab`: the image, when it was
captured, and how many frames went unseen since the last read.

A webcam keeps producing frames while the detector works, and its driver buffers them. If
the demo read them in order it would fall further and further behind the scene. So the
webcam is drained by a capture thread that keeps only the newest frame and counts the ones
it overwrote: the demo always shows the present, and the count says what it skipped.
Files are read on demand, so they never drop anything.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

READ_TIMEOUT_S = 2.0
"""How long :meth:`Webcam.read` waits for a new frame before reporting none."""


@dataclass(frozen=True)
class Grab:
    """One frame as delivered.

    Attributes:
        image: BGR pixels.
        t_capture: ``time.perf_counter()`` when the frame arrived from the device or file.
        dropped: Frames delivered by the device and overwritten unseen since the last read.
        name: What this frame is, for the screen: ``webcam 0``, a file name.
    """

    image: np.ndarray
    t_capture: float
    dropped: int
    name: str


class Source(Protocol):
    """What the pipeline reads frames from."""

    fps: float | None
    """The source's own frame rate, or None if it has none (still images)."""

    def read(self) -> Grab | None:
        """Return the next frame, or None if there is none."""
        ...

    def next(self) -> None:
        """Move to the next item (file or image); a webcam ignores it."""
        ...

    def prev(self) -> None:
        """Move to the previous item; a webcam ignores it."""
        ...

    def close(self) -> None:
        """Release the device or file."""
        ...


class Capture(Protocol):
    """The part of ``cv2.VideoCapture`` a :class:`Webcam` uses, so tests can fake it."""

    def read(self) -> tuple[bool, np.ndarray | None]:  # noqa: D102
        ...

    def get(self, prop: int) -> float:  # noqa: D102
        ...

    def release(self) -> None:  # noqa: D102
        ...


class Webcam:
    """A camera, drained by a thread that keeps only the newest frame."""

    def __init__(
        self,
        index: int,
        open_capture: Callable[[int], Capture] = cv2.VideoCapture,
    ) -> None:
        """Open the camera and start draining it.

        Args:
            index: Camera index, as for ``cv2.VideoCapture``.
            open_capture: Opens the device; replaced in tests.

        Raises:
            OSError: If the camera cannot be opened.
        """
        self.index = index
        self._cap = open_capture(index)
        ok, first = self._cap.read()
        if not ok or first is None:
            self._cap.release()
            msg = f"cannot open webcam {index}"
            raise OSError(msg)
        fps = self._cap.get(cv2.CAP_PROP_FPS)
        self.fps = fps if fps and fps > 0 else None
        self._cond = threading.Condition()
        self._latest: tuple[np.ndarray, float] = (first, time.perf_counter())
        self._delivered = 1  # frames the device has delivered
        self._taken = 0  # value of _delivered at the last read
        self._running = True
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        while self._running:
            ok, image = self._cap.read()
            t = time.perf_counter()
            if not ok or image is None:
                time.sleep(0.01)
                continue
            with self._cond:
                self._latest = (image, t)
                self._delivered += 1
                self._cond.notify_all()

    def read(self) -> Grab | None:
        """Return the newest frame not yet returned, waiting for one if needed.

        Returns:
            The frame and how many newer-than-last frames were overwritten before it,
            or None if the camera delivered nothing for :data:`READ_TIMEOUT_S`.
        """
        with self._cond:
            if not self._cond.wait_for(
                lambda: self._delivered > self._taken, READ_TIMEOUT_S
            ):
                return None
            image, t = self._latest
            dropped = self._delivered - self._taken - 1
            self._taken = self._delivered
        return Grab(image, t, dropped, f"webcam {self.index}")

    def next(self) -> None:
        """A webcam has one item."""

    def prev(self) -> None:
        """A webcam has one item."""

    def close(self) -> None:
        """Stop the thread and release the camera."""
        self._running = False
        self._thread.join(timeout=1.0)
        self._cap.release()


class VideoFiles:
    """One or more video files, each looping, read frame by frame on demand."""

    def __init__(self, paths: Sequence[Path]) -> None:
        """Open the first file.

        Args:
            paths: The files, in the order next/prev walk them.

        Raises:
            OSError: If there are no paths or the first cannot be opened.
        """
        if not paths:
            msg = "no video files given"
            raise OSError(msg)
        self.paths = list(paths)
        self._i = 0
        self._cap: cv2.VideoCapture | None = None
        self.fps: float | None = None
        self._open()

    def _open(self) -> None:
        if self._cap is not None:
            self._cap.release()
        path = self.paths[self._i]
        self._cap = cv2.VideoCapture(str(path))
        if not self._cap.isOpened():
            msg = f"cannot open video {path}"
            raise OSError(msg)
        fps = self._cap.get(cv2.CAP_PROP_FPS)
        self.fps = fps if fps and fps > 0 else None

    def read(self) -> Grab | None:
        """Return the next frame of the current file, rewinding at its end."""
        ok, image = self._cap.read()
        if not ok:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, image = self._cap.read()
            if not ok:
                return None
        return Grab(image, time.perf_counter(), 0, self.paths[self._i].name)

    def next(self) -> None:
        """Go to the next file."""
        self._i = (self._i + 1) % len(self.paths)
        self._open()

    def prev(self) -> None:
        """Go to the previous file."""
        self._i = (self._i - 1) % len(self.paths)
        self._open()

    def close(self) -> None:
        """Release the current file."""
        if self._cap is not None:
            self._cap.release()


class ImageSet:
    """Still images, each shown for a few seconds and re-processed every frame meanwhile.

    A still is run through the detector and the board again on every frame, so the
    dashboard keeps measuring: each frame is a real, separate pass.
    """

    def __init__(
        self,
        paths: Sequence[Path],
        seconds: float = 5.0,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        """Load the images.

        Args:
            paths: Image files, in order.
            seconds: How long each is shown before the next; 0 holds until next().
            clock: Time source; replaced in tests.

        Raises:
            OSError: If none of the paths is a readable image.
        """
        self.fps = None
        self.seconds = seconds
        self._clock = clock
        self._images: list[tuple[np.ndarray, str]] = []
        for path in paths:
            image = cv2.imread(str(path))
            if image is not None:
                self._images.append((image, path.name))
        if not self._images:
            msg = "no readable images"
            raise OSError(msg)
        self._i = 0
        self._since = clock()

    def read(self) -> Grab | None:
        """Return the current image, advancing first if its time is up."""
        now = self._clock()
        if self.seconds > 0 and now - self._since >= self.seconds:
            self.next()
        image, name = self._images[self._i]
        return Grab(image, self._clock(), 0, f"{name} ({self._i + 1}/{len(self)})")

    def __len__(self) -> int:
        """Number of images."""
        return len(self._images)

    def next(self) -> None:
        """Go to the next image."""
        self._i = (self._i + 1) % len(self._images)
        self._since = self._clock()

    def prev(self) -> None:
        """Go to the previous image."""
        self._i = (self._i - 1) % len(self._images)
        self._since = self._clock()

    def close(self) -> None:
        """Nothing to release."""
