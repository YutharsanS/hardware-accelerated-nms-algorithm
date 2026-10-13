"""Turn one frame's raw detector output into the board's N = 32 batches.

This is the host sanitisation contract (docs/project/plan.md, Part 2) applied to a real
detector: confidence threshold, one batch per class, cap at 32, clamp and quantise. Pure
numpy, so it is tested without Ultralytics or a board.

Two choices keep the demo honest about what the hardware does:

* **The cap is a selection, not a sort.** ``np.argpartition`` picks a class's 32
  highest-keyed candidates in Θ(n). If the host sorted them, the on-chip sorter would
  have nothing left to do.
* **Slots are filled in anchor order**, which is spatial, not score order, so the sorter
  really sorts.

Candidates over the cap are returned rather than dropped silently, so the demo can draw
them as "not checked".

Coordinates are image pixels with y pointing down. The record format is y-up, but IoU
is invariant under a global y-flip and ``b > y`` still holds (architecture.md §2).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from models.nms import model
from models.nms import params as p

CONF_THRESHOLD = 0.25
"""Ultralytics' own default, and the figure benchmarks.md §8 counted batches at."""

PAD_BOX = model.Box(0, 0, 0, 0, 0)
"""Filler for absent slots; ``present_mask`` marks them, so their content is ignored."""


@dataclass(frozen=True)
class ClassBatch:
    """One class's candidates in one frame: the batch sent and what was left out.

    Attributes:
        cls: Class id.
        boxes: Exactly ``N`` boxes, indexed by slot, ready for ``wire.encode_frame``.
        present_mask: Bit *i* set means slot *i* holds a real detection.
        anchors: Anchor index of each present slot, in slot order.
        over_cap: Anchor indices of candidates beyond the cap, never sent.
    """

    cls: int
    boxes: list[model.Box]
    present_mask: int
    anchors: np.ndarray
    over_cap: np.ndarray

    @property
    def count(self) -> int:
        """Number of present slots."""
        return len(self.anchors)


@dataclass(frozen=True)
class Candidates:
    """Every candidate of one frame above the confidence threshold.

    Attributes:
        anchors: Anchor indices, ascending.
        corners: ``(k, 4)`` int corners ``x, y, a, b``, clamped to ``0..COORD_MAX``.
        scores: ``(k,)`` quantised scores, ``0..SCORE_MAX``.
        classes: ``(k,)`` class ids.
    """

    anchors: np.ndarray
    corners: np.ndarray
    scores: np.ndarray
    classes: np.ndarray


def candidates(
    xyxy: np.ndarray,
    class_scores: np.ndarray,
    conf_threshold: float = CONF_THRESHOLD,
) -> Candidates:
    """Threshold, label, clamp and quantise one frame's anchors.

    Single-label, as ``YOLO.predict`` is by default: each anchor keeps its best class. Boxes
    with no area after clamping are dropped, as the contract's step 3 allows. The hardware
    would clamp them anyway, but they would only clutter the picture.

    Args:
        xyxy: ``(A, 4)`` float corners in frame pixels.
        class_scores: ``(A, C)`` per-class confidences in ``[0, 1]``.
        conf_threshold: Keep anchors whose best confidence exceeds this.

    Returns:
        The surviving candidates, in anchor order.
    """
    conf = class_scores.max(1)
    cls = class_scores.argmax(1)
    corners = np.clip(np.rint(xyxy), 0, p.COORD_MAX).astype(np.int64)
    has_area = (corners[:, 2] > corners[:, 0]) & (corners[:, 3] > corners[:, 1])
    anchors = np.flatnonzero((conf > conf_threshold) & has_area)
    return Candidates(
        anchors=anchors,
        corners=corners[anchors],
        scores=np.rint(conf[anchors] * p.SCORE_MAX).astype(np.int64),
        classes=cls[anchors],
    )


def select_top(scores: np.ndarray, anchors: np.ndarray, n: int) -> np.ndarray:
    """Pick the ``n`` highest-keyed members without sorting them.

    The key is the spec's own sort key, widened to the anchor index:
    ``score · 2^w + (2^w − 1 − anchor)``. It is a strict total order, so equal scores at the
    cut are broken the same way the sorter breaks them (lower index first).

    Args:
        scores: Quantised scores of the members.
        anchors: Their anchor indices, ascending and unique.
        n: How many to keep.

    Returns:
        A boolean mask over the members, true for the ``n`` chosen, so the chosen keep
        their anchor order.
    """
    chosen = np.ones(len(scores), dtype=bool)
    if len(scores) <= n:
        return chosen
    width = max(1, int(anchors.max()).bit_length())
    key = scores.astype(np.int64) * (1 << width) + ((1 << width) - 1 - anchors)
    chosen[:] = False
    chosen[np.argpartition(-key, n - 1)[:n]] = True
    return chosen


def make_batches(cands: Candidates, n: int = p.N) -> list[ClassBatch]:
    """Split a frame's candidates into one batch per class, capped at ``n``.

    Args:
        cands: The frame's candidates, from :func:`candidates`.
        n: Boxes per batch.

    Returns:
        One batch per class present, in class order.
    """
    batches = []
    for k in np.unique(cands.classes):
        members = np.flatnonzero(cands.classes == k)
        chosen = select_top(cands.scores[members], cands.anchors[members], n)
        sent = members[chosen]
        boxes = [
            model.Box(*(int(v) for v in cands.corners[i]), int(cands.scores[i]))
            for i in sent
        ]
        batches.append(
            ClassBatch(
                cls=int(k),
                boxes=boxes + [PAD_BOX] * (n - len(boxes)),
                present_mask=(1 << len(boxes)) - 1,
                anchors=cands.anchors[sent],
                over_cap=cands.anchors[members[~chosen]],
            ),
        )
    return batches
