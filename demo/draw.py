"""Draw the demo: detector output on the left, what the board kept on the right, a HUD below.

Pure OpenCV drawing on numpy images; nothing here talks to the board or the detector.
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass

import cv2
import numpy as np

from demo.batching import Candidates, ClassBatch
from models.nms import model

FONT = cv2.FONT_HERSHEY_SIMPLEX
PANEL_MAX_W = 800
HUD_LINE_H = 24
WHITE = (255, 255, 255)
GREY = (150, 150, 150)
RED = (60, 60, 230)
BLACK = (0, 0, 0)


@dataclass(frozen=True)
class Frame:
    """Everything one processed video frame shows.

    Attributes:
        image: The BGR video frame.
        cands: Every candidate above the confidence threshold.
        batches: The batches sent, one per class.
        keep_masks: The board's ``keep_mask`` for each batch.
        names: Class id to name.
    """

    image: np.ndarray
    cands: Candidates
    batches: list[ClassBatch]
    keep_masks: list[int]
    names: dict[int, str]

    @property
    def kept(self) -> int:
        """Number of boxes the board kept."""
        return sum(m.bit_count() for m in self.keep_masks)


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
    """Return a stable, distinct BGR colour for a class id, never red (red = removed)."""
    hue = 0.12 + 0.76 * ((cls * 0.618034) % 1.0)
    r, g, b = colorsys.hsv_to_rgb(hue, 0.75, 1.0)
    return int(b * 255), int(g * 255), int(r * 255)


def _pt(
    box: model.Box | np.ndarray, s: float
) -> tuple[tuple[int, int], tuple[int, int]]:
    x, y, a, b = (round(float(v) * s) for v in box[:4])
    return (x, y), (a, b)


def _dashed(img: np.ndarray, p0: tuple[int, int], p1: tuple[int, int]) -> None:
    (x0, y0), (x1, y1) = p0, p1
    dash = 6
    for x in range(x0, x1, 2 * dash):
        xe = min(x + dash, x1)
        cv2.line(img, (x, y0), (xe, y0), GREY, 1)
        cv2.line(img, (x, y1), (xe, y1), GREY, 1)
    for y in range(y0, y1, 2 * dash):
        ye = min(y + dash, y1)
        cv2.line(img, (x0, y), (x0, ye), GREY, 1)
        cv2.line(img, (x1, y), (x1, ye), GREY, 1)


def _label(img: np.ndarray, text: str, org: tuple[int, int], colour: tuple) -> None:
    (w, h), _ = cv2.getTextSize(text, FONT, 0.5, 1)
    x, y = min(org[0], img.shape[1] - w - 4), max(org[1], h + 4)
    cv2.rectangle(img, (x, y - h - 4), (x + w + 4, y), colour, -1)
    cv2.putText(img, text, (x + 2, y - 3), FONT, 0.5, BLACK, 1, cv2.LINE_AA)


def _title(img: np.ndarray, text: str, legend: str = "") -> None:
    height = 48 if legend else 30
    cv2.rectangle(img, (0, 0), (img.shape[1], height), BLACK, -1)
    cv2.putText(img, text, (8, 21), FONT, 0.6, WHITE, 1, cv2.LINE_AA)
    if legend:
        cv2.putText(img, legend, (8, 40), FONT, 0.45, GREY, 1, cv2.LINE_AA)


def left_panel(frame: Frame, s: float) -> np.ndarray:
    """Every candidate the detector produced, before NMS."""
    img = cv2.resize(frame.image, None, fx=s, fy=s)
    for corners, cls in zip(frame.cands.corners, frame.cands.classes, strict=True):
        cv2.rectangle(img, *_pt(corners, s), class_colour(int(cls)), 1)
    _title(img, f"Detector output, before NMS: {len(frame.cands.anchors)} boxes")
    return img


def right_panel(frame: Frame, s: float, *, simulated: bool) -> np.ndarray:
    """The boxes the board kept, the ones it removed, and the ones it never saw."""
    img = cv2.resize(frame.image, None, fx=s, fy=s)
    faint = img.copy()
    over = 0
    for b, keep in zip(frame.batches, frame.keep_masks, strict=True):
        for slot in range(b.count):
            if not (keep >> slot) & 1:
                cv2.rectangle(faint, *_pt(b.boxes[slot], s), RED, 1)
        anchors = np.searchsorted(frame.cands.anchors, b.over_cap)
        for i in anchors:
            _dashed(img, *_pt(frame.cands.corners[i], s))
        over += len(b.over_cap)
    cv2.addWeighted(faint, 0.75, img, 0.25, 0, img)
    for b, keep in zip(frame.batches, frame.keep_masks, strict=True):
        colour = class_colour(b.cls)
        for slot in range(b.count):
            if (keep >> slot) & 1:
                box = b.boxes[slot]
                p0, p1 = _pt(box, s)
                cv2.rectangle(img, p0, p1, colour, 3)
                name = frame.names.get(b.cls, str(b.cls))
                _label(img, f"{name} {box.score / 65535:.2f}", p0, colour)
    who = "golden model (SIMULATED)" if simulated else "the FPGA"
    legend = "red = removed by NMS"
    if over:
        legend += f"  |  dashed grey = over 32 per class, not checked ({over})"
    _title(img, f"After NMS on {who}: {frame.kept} kept", legend)
    return img


def step_panel(
    frame: Frame, step: Step, total: int, index: int, s: float
) -> np.ndarray:
    """One batch mid-resolve: keepers so far, the current box, what it suppresses."""
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
    _title(
        img,
        f"Step {index + 1}/{total}  {name}, rank {st.rank}: {verdict}",
        "highest score first  |  white = this box, thick = kept so far,"
        " red = removed now, grey = undecided",
    )
    return img


def hud(width: int, lines: list[str]) -> np.ndarray:
    """A dark strip with one line of text per entry."""
    img = np.zeros((HUD_LINE_H * len(lines) + 8, width, 3), np.uint8)
    for i, line in enumerate(lines):
        y = HUD_LINE_H * (i + 1)
        cv2.putText(img, line, (10, y), FONT, 0.55, WHITE, 1, cv2.LINE_AA)
    return img


def compose(
    frame: Frame,
    hud_lines: list[str],
    *,
    simulated: bool,
    step: tuple[Step, int, int] | None = None,
) -> np.ndarray:
    """Lay out both panels side by side over the HUD.

    Args:
        frame: The processed frame.
        hud_lines: Text for the HUD strip.
        simulated: Whether the golden model stands in for the board.
        step: ``(step, total, index)`` to show the step-through view on the right.

    Returns:
        The BGR canvas to show or save.
    """
    s = min(1.0, PANEL_MAX_W / frame.image.shape[1])
    left = left_panel(frame, s)
    right = (
        step_panel(frame, step[0], step[1], step[2], s)
        if step
        else right_panel(frame, s, simulated=simulated)
    )
    top = np.hstack([left, np.full((left.shape[0], 4, 3), 40, np.uint8), right])
    return np.vstack([top, hud(top.shape[1], hud_lines)])
