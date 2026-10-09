"""The demo's work loop, off the UI thread: read a frame, run the pipeline, hand it over.

One frame is in flight at a time. The worker waits until the window has painted the last
frame before reading the next, so frames never queue up behind a slow window and the
capture-to-screen time stays the time of one pass. Settings from the window arrive through
a queue and apply from the next frame; each record carries the settings it ran with.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable

from PySide6.QtCore import QThread, Signal

from demo.pipeline import Pipeline
from demo.records import Settings
from demo.sources import Source

IDLE_S = 0.05


class Worker(QThread):
    """Runs :meth:`Pipeline.step` in a loop and emits each result."""

    frame_ready = Signal(object)
    """A ``pipeline.FrameResult``."""
    message = Signal(str)
    """A source problem, for the status line: cannot open, no frames."""

    def __init__(
        self,
        pipeline: Pipeline,
        open_source: Callable[[str], Source],
        settings: Settings,
    ) -> None:
        """Prepare the loop; it starts with :meth:`start`.

        Args:
            pipeline: Detector, board and checks.
            open_source: Opens a ``Settings.source`` spec.
            settings: The starting settings.
        """
        super().__init__()
        self.pipeline = pipeline
        self.open_source = open_source
        self.settings = settings
        self.painted = threading.Event()
        self.painted.set()
        self._commands: queue.Queue[tuple[str, object]] = queue.Queue()
        self._running = True
        self._paused = False
        self._source: Source | None = None
        self._index = 0

    # --- called from the UI thread ---------------------------------------------------

    def set_settings(self, settings: Settings) -> None:
        """Apply new settings from the next frame."""
        self._commands.put(("settings", settings))

    def set_paused(self, paused: bool) -> None:
        """Stop or resume producing frames."""
        self._commands.put(("paused", paused))

    def step_source(self, delta: int) -> None:
        """Go to the next (+1) or previous (-1) file or image."""
        self._commands.put(("step", delta))

    def stop(self) -> None:
        """End the loop and release the source."""
        self._running = False
        self.painted.set()
        self.wait()

    # --- the worker thread -----------------------------------------------------------

    def _open(self, spec: str) -> None:
        if self._source is not None:
            self._source.close()
            self._source = None
        self.message.emit(f"opening {spec} ...")
        try:
            self._source = self.open_source(spec)
            self.message.emit("")
        except OSError as exc:
            self.message.emit(f"cannot open {spec}: {exc}")

    def _drain_commands(self) -> None:
        while True:
            try:
                kind, value = self._commands.get_nowait()
            except queue.Empty:
                return
            if kind == "settings":
                old, self.settings = self.settings, value
                if value.source != old.source or self._source is None:
                    self._open(value.source)
            elif kind == "paused":
                self._paused = bool(value)
            elif kind == "step" and self._source is not None:
                (self._source.next if value > 0 else self._source.prev)()

    def run(self) -> None:
        """Produce frames until stopped."""
        self._open(self.settings.source)
        while self._running:
            self._drain_commands()
            if self._paused or self._source is None:
                time.sleep(IDLE_S)
                continue
            if not self.painted.wait(IDLE_S):
                continue
            r0 = time.perf_counter()
            grab = self._source.read()
            r1 = time.perf_counter()
            if grab is None:
                self.message.emit("the source delivered no frame")
                time.sleep(IDLE_S)
                continue
            result = self.pipeline.step(
                grab, (r0, r1), self.settings, self._index, self._source.fps
            )
            self._index += 1
            self.painted.clear()
            self.frame_ready.emit(result)
        if self._source is not None:
            self._source.close()
        self.pipeline.board.close()
