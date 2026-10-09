"""Draw the two video panels: detector output on the left, the board's result on the right.

Pure OpenCV drawing on numpy images; nothing here talks to the board or the detector, and
the window (``demo/app.py``) lays the panels out.
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import cv2
import numpy as np

from demo.batching import Candidates, ClassBatch
from demo.records import BOARD_MODEL, OFFLINE
from models.nms import model
from models.nms import params as p

if TYPE_CHECKING:
    from demo.pipeline import FrameResult

FONT = cv2.FONT_HERSHEY_SIMPLEX
PANEL_W = 640
"""Default panel width; the window passes the size it will actually show."""
WHITE = (255, 255, 255)
GREY = (150, 150, 150)
RED = (60, 60, 230)
BLACK = (0, 0, 0)


@dataclass(frozen=True)
class Panel:
    """A drawn panel: the image, and its title and legend for the window to set as text.

    Titles are not drawn into the image: a narrow (portrait) panel would cut them off.

    Attributes:
        image: BGR pixels.
        title: One line saying what the panel shows.
        legend: What the colours mean, or empty.
    """

    image: np.ndarray
    title: str
    legend: str = ""


@dataclass(frozen=True)
class Frame:
    """Everything one processed video frame shows.

    Attributes:
        image: The BGR video frame.
        cands: Every candidate above the confidence threshold.
        batches: The batches sent, one per class.
        keep_masks: Each batch's ``keep_mask``; None where no OK reply arrived.
        names: Class id to name.
        board_state: ``connected``, ``offline`` or ``model`` (``--no-board``).
        box: ``(width, height)`` the panel is drawn to fit, so its text is drawn at the
            size it is shown, never scaled afterwards.
    """

    image: np.ndarray
    cands: Candidates
    batches: list[ClassBatch]
    keep_masks: list[int | None]
    names: dict[int, str]
    board_state: str
    box: tuple[int, int] = (PANEL_W, PANEL_W)

    @property
    def kept(self) -> int:
        """Number of boxes kept, over the batches that have a result."""
        return sum(m.bit_count() for m in self.keep_masks if m is not None)

    @property
    def scale(self) -> float:
        """Panel scale: the frame fitted into :attr:`box`."""
        h, w = self.image.shape[:2]
        return min(self.box[0] / w, self.box[1] / h)


def frame_of(result: FrameResult, box: tuple[int, int] = (PANEL_W, PANEL_W)) -> Frame:
    """Return what to draw for a processed frame, from its record.

    A batch's mask is drawn only when its reply arrived with status OK.

    Args:
        result: The processed frame.
        box: ``(width, height)`` to fit the panel into.
    """
    rec = result.record
    masks = [b.keep_mask if b.status == p.STATUS_OK else None for b in rec.batches]
    return Frame(
        result.image,
        result.cands,
        result.batches,
        masks,
        result.names,
        rec.board_state,
        box,
    )


@dataclass(frozen=True)
class Step:
    """One rank of one batch, for the step-through view.

    Attributes:
        batch: Index into ``Frame.batches``.
        step: The resolve step at that rank, from ``model.nms_allpairs(trace=True)``.
        removed: Slots this step suppressed (were valid before it, are not after it).
    """

    batch: int
    step: model.ResolveStep
    removed: int


def steps_of(frame: Frame) -> list[Step]:
    """List every present box of every batch, in the order NMS resolves them.

    Args:
        frame: The processed frame.

    Returns:
        One step per present slot, batch by batch, in descending key order.
    """
    out = []
    for i, b in enumerate(frame.batches):
        _, trace = model.nms_allpairs(b.boxes, b.present_mask, trace=True)
        valid = b.present_mask
        for s in trace:
            if (b.present_mask >> s.slot) & 1:
                removed = valid & ~s.valid_mask & ~(1 << s.slot)
                out.append(Step(batch=i, step=s, removed=removed))
            valid = s.valid_mask
    return out


def class_colour(cls: int) -> tuple[int, int, int]:
    """Return a stable, distinct BGR colour for a class id; never red, the step view's mark."""
    hue = 0.12 + 0.76 * ((cls * 0.618034) % 1.0)
    r, g, b = colorsys.hsv_to_rgb(hue, 0.75, 1.0)
    return int(b * 255), int(g * 255), int(r * 255)


def _pt(
    box: model.Box | np.ndarray, s: float
) -> tuple[tuple[int, int], tuple[int, int]]:
    x, y, a, b = (round(float(v) * s) for v in box[:4])
    return (x, y), (a, b)


def _label(img: np.ndarray, text: str, org: tuple[int, int], colour: tuple) -> None:
    (w, h), _ = cv2.getTextSize(text, FONT, 0.5, 1)
    x, y = min(org[0], img.shape[1] - w - 4), max(org[1], h + 4)
    cv2.rectangle(img, (x, y - h - 4), (x + w + 4, y), colour, -1)
    cv2.putText(img, text, (x + 2, y - 3), FONT, 0.5, BLACK, 1, cv2.LINE_AA)


def left_panel(frame: Frame) -> Panel:
    """Every candidate the detector produced, before NMS."""
    s = frame.scale
    img = cv2.resize(frame.image, None, fx=s, fy=s)
    for corners, cls in zip(frame.cands.corners, frame.cands.classes, strict=True):
        cv2.rectangle(img, *_pt(corners, s), class_colour(int(cls)), 1)
    return Panel(img, f"YOLOv8n output, before NMS: {len(frame.cands.anchors)} boxes")


def right_panel(frame: Frame) -> Panel:
    """The boxes the board kept; nothing at all when the board gave no result."""
    s = frame.scale
    img = cv2.resize(frame.image, None, fx=s, fy=s)
    if frame.board_state == OFFLINE:
        return Panel(img, "Board offline: no NMS result for this frame")
    for b, keep in zip(frame.batches, frame.keep_masks, strict=True):
        if keep is None:
            continue
        colour = class_colour(b.cls)
        for slot in range(b.count):
            if (keep >> slot) & 1:
                box = b.boxes[slot]
                p0, p1 = _pt(box, s)
                cv2.rectangle(img, p0, p1, colour, 3)
                name = frame.names.get(b.cls, str(b.cls))
                _label(img, f"{name} {box.score / p.SCORE_MAX:.2f}", p0, colour)
    who = (
        "the golden model (no board)"
        if frame.board_state == BOARD_MODEL
        else "the FPGA"
    )
    failed = sum(m is None for m in frame.keep_masks)
    legend = f"{failed} batch(es) got an error reply: not drawn" if failed else ""
    return Panel(img, f"After NMS on {who}: {frame.kept} kept", legend)


def step_panel(frame: Frame, step: Step, total: int, index: int) -> Panel:
    """One batch mid-resolve, replayed in the golden model.

    The board returns only the final ``keep_mask``: it resolves a whole batch in 1.13 µs and
    reports no intermediate steps. This view replays the same batch in software, step by
    step, and says so; its final step equals the board's result.
    """
    s = frame.scale
    img = cv2.resize(frame.image, None, fx=s, fy=s)
    b = frame.batches[step.batch]
    st = step.step
    colour = class_colour(b.cls)
    # The current box as a translucent white fill, so the duplicates it removes, which
    # usually sit almost exactly on top of it, stay visible as red outlines over it.
    p0, p1 = _pt(b.boxes[st.slot], s)
    tint = img.copy()
    cv2.rectangle(tint, p0, p1, WHITE, -1)
    cv2.addWeighted(tint, 0.3, img, 0.7, 0, img)
    cv2.rectangle(img, p0, p1, WHITE, 2)
    for slot in range(b.count):
        box = b.boxes[slot]
        if slot == st.slot:
            continue
        if (st.keep_mask >> slot) & 1:
            cv2.rectangle(img, *_pt(box, s), colour, 3)
        elif (step.removed >> slot) & 1:
            cv2.rectangle(img, *_pt(box, s), RED, 2)
        elif (st.valid_mask >> slot) & 1:
            cv2.rectangle(img, *_pt(box, s), GREY, 1)
    verdict = (
        f"KEEP, suppresses {step.removed.bit_count()} (red)"
        if st.kept
        else "already suppressed, skip"
    )
    name = frame.names.get(b.cls, str(b.cls))
    _label(img, f"rank {st.rank}", p0, WHITE)
    return Panel(
        img,
        f"Software replay (golden model), step {index + 1}/{total}: {name},"
        f" rank {st.rank}: {verdict}",
        "highest score first  |  white = this box, thick = kept so far,"
        " red = removed now, grey = undecided",
    )
