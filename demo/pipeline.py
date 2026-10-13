"""One frame through the detector, the board and the checks, producing a FrameRecord.

No Qt here: the window and the headless runner both drive :meth:`Pipeline.step`, and the
tests run it without a display. Per frame::

    grab -> YOLOv8n head, before its NMS (8,400 anchors) -> candidates above conf
         -> one batch of <= 32 per class -> board over the UART -> keep_mask per batch
         -> golden model on each batch (the check) -> OpenCV and torchvision on each batch

Every stage is stamped ``(start, end)`` on ``time.perf_counter()``.

The board is never silently replaced. :class:`BoardLink` is either connected or offline;
when offline, the frame's batches carry no reply and the window shows no result. The golden
model answers only when ``--no-board`` chose it at start, and the records say so.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

import numpy as np

from benchmarks.inputs import make_batch
from benchmarks.targets import cpu
from demo import batching
from demo.records import (
    BOARD_MODEL,
    CONNECTED,
    OFFLINE,
    BatchRecord,
    FrameRecord,
    Settings,
)
from demo.sources import Grab
from models.nms import host, model, wire
from models.nms import params as p

RETRY_S = 2.0
"""How often an offline board is retried."""


class GoldenBoard:
    """The golden model in place of the board, chosen with ``--no-board``."""

    state = BOARD_MODEL
    error = ""

    def transact_many(
        self, batches: Sequence[tuple[list[model.Box], int]]
    ) -> list[wire.Reply] | None:
        """Answer each batch as the board would."""
        return [
            wire.Reply(p.STATUS_OK, 0, model.nms_sequential(boxes, mask))
            for boxes, mask in batches
        ]

    def close(self) -> None:
        """Nothing to close."""


class BoardLink:
    """The board on a serial port, with an explicit connected/offline state.

    Any failure (port gone, timeout, malformed reply) closes the port and marks the link
    offline; it is reopened at most every :data:`RETRY_S` seconds. While offline,
    :meth:`transact_many` returns None: there is no result, and nothing stands in for one.
    """

    def __init__(
        self,
        port: str,
        open_board: Callable[[str], host.Board] = host.Board,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Try to open the board.

        Args:
            port: Serial device.
            open_board: Opens the port; replaced in tests.
            clock: Time source for the retry interval; replaced in tests.
        """
        self.port = port
        self._open_board = open_board
        self._clock = clock
        self._board: host.Board | None = None
        self._last_try = -RETRY_S
        self.error = ""
        self._try_open()

    @property
    def state(self) -> str:
        """``connected`` or ``offline``."""
        return CONNECTED if self._board is not None else OFFLINE

    def _try_open(self) -> None:
        self._last_try = self._clock()
        try:
            host.set_latency_timer(self.port)
            self._board = self._open_board(self.port)
            self.error = ""
        except OSError as exc:
            self._board = None
            self.error = str(exc)

    def transact_many(
        self, batches: Sequence[tuple[list[model.Box], int]]
    ) -> list[wire.Reply] | None:
        """Send the batches and return the replies, or None while offline."""
        if self._board is None and self._clock() - self._last_try >= RETRY_S:
            self._try_open()
        if self._board is None:
            return None
        try:
            return self._board.transact_many(batches)
        except (OSError, wire.ReplyError) as exc:
            self.error = str(exc)
            self.close()
            return None

    def close(self) -> None:
        """Close the port, if open."""
        if self._board is not None:
            self._board.__exit__(None, None, None)
            self._board = None


class SoftwareNMS:
    """OpenCV and torchvision NMS, timed on the same batches the board gets.

    These are ``make bench``'s own implementations and timing
    (``benchmarks/targets/cpu.py``), so the numbers compare with benchmarks.md.
    """

    def __init__(self) -> None:
        """Build the implementations, leaving the process's thread counts as they were.

        ``opencv_impl()`` and ``torchvision_impl()`` pin OpenCV and torch to one thread for
        the whole process, which would slow the detector. Both are put back to what they
        were. The NMS calls stay single-threaded either way: neither library parallelises
        one small NMS.
        """
        import cv2
        import torch

        cv_threads, torch_threads = cv2.getNumThreads(), torch.get_num_threads()
        self.impls = [i for i in (cpu.opencv_impl(), cpu.torchvision_impl()) if i]
        cv2.setNumThreads(cv_threads)
        torch.set_num_threads(torch_threads)

    def time(self, batch: object) -> dict[str, float]:
        """Return microseconds per implementation on one ``benchmarks.inputs.Batch``."""
        out = {}
        for impl in self.impls:
            arg = impl.prepare(batch)
            _, us = impl.timed(arg)
            out[impl.name] = us
        return out


@dataclass(frozen=True)
class FrameResult:
    """A processed frame: its record, plus what the window draws but never records.

    Attributes:
        record: The frame's data.
        image: The frame.
        cands: Its candidates.
        batches: The batches sent, with slot-to-anchor maps for drawing.
        names: Class id to name.
    """

    record: FrameRecord
    image: np.ndarray
    cands: batching.Candidates
    batches: list[batching.ClassBatch]
    names: dict[int, str]


class Detector:
    """YOLOv8n, its raw head output before Ultralytics' own NMS."""

    def __init__(self, weights: str) -> None:
        """Load the weights (downloaded by Ultralytics on first use)."""
        from ultralytics import YOLO

        self.model = YOLO(weights)
        self.names: dict[int, str] = self.model.names

    def __call__(self, image: np.ndarray, imgsz: int) -> tuple[np.ndarray, np.ndarray]:
        """Return ``(xyxy in frame pixels, class scores)`` for every anchor."""
        from ultralytics.utils import ops

        from benchmarks.feasibility.count_boxes import raw_predictions

        xyxy, scores = raw_predictions(self.model, image, imgsz=imgsz)
        xyxy = ops.scale_boxes((imgsz, imgsz), xyxy.copy(), image.shape[:2])
        return xyxy, scores


class Pipeline:
    """Detector, board and checks, one frame at a time."""

    def __init__(
        self,
        detect: Callable[[np.ndarray, int], tuple[np.ndarray, np.ndarray]],
        names: dict[int, str],
        board: BoardLink | GoldenBoard,
        software: SoftwareNMS | None,
    ) -> None:
        """Wire the stages together.

        Args:
            detect: Image and input size to raw ``(xyxy, class scores)``.
            names: Class id to name.
            board: The board link, or the golden model under ``--no-board``.
            software: The software comparison, or None to skip it.
        """
        self.detect = detect
        self.names = names
        self.board = board
        self.software = software

    def step(
        self,
        grab: Grab,
        read: tuple[float, float],
        settings: Settings,
        index: int,
        source_fps: float | None,
    ) -> FrameResult:
        """Process one frame.

        Args:
            grab: The frame.
            read: ``(start, end)`` of the source read that produced it.
            settings: The settings in force for this frame.
            index: Frame number.
            source_fps: The source's frame rate, if any.

        Returns:
            The record and what to draw.
        """
        t = {"read": read}
        clock = time.perf_counter

        t0 = clock()
        xyxy, scores = self.detect(grab.image, settings.imgsz)
        t["detect"] = (t0, clock())

        t0 = clock()
        cands = batching.candidates(xyxy, scores, settings.conf)
        batches = batching.make_batches(cands)
        t["batch"] = (t0, clock())

        t0 = clock()
        replies = self.board.transact_many([(b.boxes, b.present_mask) for b in batches])
        t["board"] = (t0, clock())

        records = []
        no_wire = self.board.state == BOARD_MODEL
        t0 = clock()
        checked = []
        for i, b in enumerate(batches):
            reference = make_batch(b.boxes, b.present_mask)
            checked.append(reference)
            reply = None if replies is None else replies[i]
            records.append(
                BatchRecord(
                    cls=b.cls,
                    boxes=[list(box) for box in b.boxes[: b.count]],
                    present_mask=b.present_mask,
                    status=None if reply is None else reply.status,
                    # The golden model has no wire, so no sequence number to show.
                    seq=None if reply is None or no_wire else reply.seq,
                    keep_mask=None if reply is None else reply.keep_mask,
                    golden_mask=reference.keep_mask,
                ),
            )
        t["check"] = (t0, clock())

        if self.software is not None:
            t0 = clock()
            for rec, reference in zip(records, checked, strict=True):
                rec.software_us = self.software.time(reference)
            t["software"] = (t0, clock())

        record = FrameRecord(
            index=index,
            settings=settings,
            source_name=grab.name,
            t_capture=grab.t_capture,
            t=t,
            dropped=grab.dropped,
            source_fps=source_fps,
            candidates=len(cands.anchors),
            over_cap=sum(len(b.over_cap) for b in batches),
            board_state=self.board.state,
            batches=records,
        )
        return FrameResult(record, grab.image, cands, batches, self.names)


def with_paint(record: FrameRecord, start: float, end: float) -> FrameRecord:
    """Return the record with its paint stage, once the window has drawn it."""
    return replace(record, t={**record.t, "paint": (start, end)})
