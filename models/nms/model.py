"""Integer golden model for the NMS accelerator.

Two implementations of the same algorithm live here on purpose:

* :func:`nms_sequential` -- the textbook loop. This is the *authority* on what NMS means.
* :func:`nms_allpairs` -- the structure the RTL implements: every pair evaluated first,
  then resolved in rank order.

Keeping both, and asserting they agree, is what stops a shared misconception passing
silently. If the model implemented only the all-pairs form, the RTL would be compared
against a model that made the same restructuring, and an error in the restructuring itself
would be invisible.

Everything is integer. No division is performed anywhere -- the IoU threshold test is
cross-multiplied to ``I * 2**k >= T_INT * U``, which is what the hardware evaluates, so
agreement is bit-exact by construction rather than by tolerance. That matters because the
disagreements a tolerance band would hide are exactly the pairs sitting on the threshold.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

from models.nms import params as p


class Box(NamedTuple):
    """One bounding box in the frozen record format.

    Attributes:
        x: Lower-left x, ``0..4095``.
        y: Lower-left y, ``0..4095``.
        a: Upper-right x, ``0..4095``.
        b: Upper-right y, ``0..4095``.
        score: Quantised confidence, ``0..65535``.
    """

    x: int
    y: int
    a: int
    b: int
    score: int


@dataclass(frozen=True)
class ResolveStep:
    """State captured after one rank has been resolved.

    The RTL's control FSM is checked against a list of these, step by step. A whole-batch
    comparison would hide an off-by-one in the pipeline offset between the row fill and the
    resolve, which is the most likely control bug in that module.

    Attributes:
        rank: Position in the sorted order, 0 is the highest-scoring box.
        slot: Which input slot this rank refers to.
        kept: Whether this box became a keeper.
        suppress_row: Bitmask of slots this box suppresses, as the lanes computed it.
        valid_mask: Candidate mask *after* this step.
        keep_mask: Survivor mask *after* this step.
    """

    rank: int
    slot: int
    kept: bool
    suppress_row: int
    valid_mask: int
    keep_mask: int


# --- record packing ----------------------------------------------------------------


def pack_record(box: Box) -> int:
    """Pack a box into its 64-bit wire record.

    Args:
        box: The box to pack. Fields must already be in range.

    Returns:
        The 64-bit record, laid out MSB-first as x, y, a, b, score.

    Raises:
        ValueError: If any field is out of range for its width.
    """
    for name, value, limit in (
        ("x", box.x, p.COORD_MAX),
        ("y", box.y, p.COORD_MAX),
        ("a", box.a, p.COORD_MAX),
        ("b", box.b, p.COORD_MAX),
        ("score", box.score, p.SCORE_MAX),
    ):
        if not 0 <= value <= limit:
            msg = f"{name}={value} out of range 0..{limit}"
            raise ValueError(msg)
    return (
        (box.x << p.X_SHIFT)
        | (box.y << p.Y_SHIFT)
        | (box.a << p.A_SHIFT)
        | (box.b << p.B_SHIFT)
        | (box.score << p.SCORE_SHIFT)
    )


def unpack_record(record: int) -> Box:
    """Unpack a 64-bit wire record into a box.

    Args:
        record: The 64-bit record.

    Returns:
        The decoded box.
    """
    coord = (1 << p.COORD_W) - 1
    return Box(
        x=(record >> p.X_SHIFT) & coord,
        y=(record >> p.Y_SHIFT) & coord,
        a=(record >> p.A_SHIFT) & coord,
        b=(record >> p.B_SHIFT) & coord,
        score=(record >> p.SCORE_SHIFT) & p.SCORE_MAX,
    )


# --- geometry ----------------------------------------------------------------------


def box_area(box: Box) -> int:
    """Return a box's area, clamped so a degenerate box has area zero.

    The clamp is what keeps an inverted box (``a <= x``) from wrapping to a huge value in
    the hardware's unsigned arithmetic. Python would give a negative area instead, so
    without the clamp the model and the RTL would disagree on the same input.

    Args:
        box: The box.

    Returns:
        Area in ``0..16769025``.
    """
    width = box.a - box.x if box.a > box.x else 0
    height = box.b - box.y if box.b > box.y else 0
    return width * height


def intersection_area(first: Box, second: Box) -> int:
    """Return the overlap area of two boxes, clamped to zero when they miss.

    Args:
        first: One box.
        second: The other box.

    Returns:
        Intersection area in ``0..16769025``.
    """
    # max/max/min/min is the nature of an intersection; the RTL does the same four
    # comparisons in stage 1 of iou_lane.
    xx = max(first.x, second.x)
    yy = max(first.y, second.y)
    aa = min(first.a, second.a)
    bb = min(first.b, second.b)
    width = aa - xx if aa > xx else 0
    height = bb - yy if bb > yy else 0
    return width * height


def suppresses_at(keeper: Box, candidate: Box, t_int: int) -> bool:
    """Return the suppression verdict at an arbitrary threshold.

    ``T_INT`` is a synthesis-time generic on ``iou_lane``, not a constant, so the model has
    to be able to evaluate any of its 256 values. It matters for more than completeness:
    with the shipped ``T_INT = 128`` the largest possible RHS is 4,292,870,400, which is
    *under* 2**32 -- so the top bit of the 33-bit RHS is only reachable at ``T_INT > 128``,
    and without this the width frozen in ``docs/architecture.md`` would go unexercised.

    Args:
        keeper: The surviving box.
        candidate: The box being tested.
        t_int: Threshold in Q0.``K_SHIFT``, ``0..2**T_INT_W - 1``.

    Returns:
        True when the candidate should be suppressed.

    Raises:
        AssertionError: If the union underflows, which the clamps make impossible.
    """
    inter = intersection_area(keeper, candidate)
    union = box_area(keeper) + box_area(candidate) - inter
    assert union >= 0, f"union underflow: I={inter} U={union}"
    return (inter << p.K_SHIFT) >= t_int * union


def suppresses(keeper: Box, candidate: Box) -> bool:
    """Return whether ``keeper`` suppresses ``candidate`` under the frozen predicate.

    Evaluates exactly what the RTL evaluates::

        (I << K_SHIFT) >= T_INT * U

    No division, so no divide-by-zero and no floating point. Two fully degenerate boxes
    give ``I = 0`` and ``U = 0``, hence ``0 >= 0`` and suppression; that is specified
    behaviour rather than an accident.

    Args:
        keeper: The surviving box.
        candidate: The box being tested.

    Returns:
        True when the candidate should be suppressed.

    Raises:
        AssertionError: If the union underflows, which the clamps make impossible. Left in
            as a live check of the proof in ``docs/architecture.md`` section 7.
    """
    return suppresses_at(keeper, candidate, p.T_INT)


# --- ordering ----------------------------------------------------------------------


def sort_key(score: int, index: int) -> int:
    """Return the 21-bit sort key for a box.

    ``K = score * N + (N - 1 - index)``, i.e. the score with the bitwise complement of the
    index in the low bits. Because indices are unique this is a *strict total order*, so
    ties are impossible and the bitonic network's instability can never be observed.

    Args:
        score: Quantised confidence.
        index: Slot index, ``0..N-1``.

    Returns:
        The key, ``0..2097151``.
    """
    return (score << p.INDEX_W) | (p.N - 1 - index)


def sort_order(boxes: list[Box]) -> list[int]:
    """Return slot indices in descending key order.

    Args:
        boxes: The batch, indexed by slot.

    Returns:
        Slot indices, highest key first. Position in this list is the box's rank.
    """
    return sorted(range(len(boxes)), key=lambda i: -sort_key(boxes[i].score, i))


# --- the algorithm, twice ----------------------------------------------------------


def nms_sequential(boxes: list[Box], present_mask: int | None = None) -> int:
    """Run NMS as the textbook loop.

    This is the authority on what the algorithm means: walk boxes in descending score, keep
    each surviving box, and suppress every *later* box that overlaps it too much.

    Args:
        boxes: The batch, indexed by slot.
        present_mask: Bit *i* set means slot *i* holds a real detection. Defaults to all
            slots present.

    Returns:
        ``keep_mask``, indexed by input slot.
    """
    n = len(boxes)
    valid = (1 << n) - 1 if present_mask is None else present_mask
    keep = 0

    order = sort_order(boxes)
    for rank, slot in enumerate(order):
        if not (valid >> slot) & 1:
            continue
        keep |= 1 << slot
        valid &= ~(1 << slot)
        for other in order[rank + 1 :]:
            if (valid >> other) & 1 and suppresses(boxes[slot], boxes[other]):
                valid &= ~(1 << other)
    return keep


def nms_allpairs(
    boxes: list[Box],
    present_mask: int | None = None,
    *,
    trace: bool = False,
) -> int | tuple[int, list[ResolveStep]]:
    """Run NMS as the hardware does: all pairs first, then resolve in rank order.

    Every suppression row is computed before any suppression decision is made, which is
    what lets the hardware evaluate P pairs per cycle and pay the pipeline drain once
    instead of once per keeper. It is equivalent to :func:`nms_sequential` because a
    suppressed box can never revive, so applying a keeper's row to boxes of *earlier* rank
    is a no-op.

    Args:
        boxes: The batch, indexed by slot.
        present_mask: Bit *i* set means slot *i* holds a real detection. Defaults to all
            slots present.
        trace: When true, also return the per-rank resolve trace that the RTL control
            testbench is checked against.

    Returns:
        ``keep_mask``, or ``(keep_mask, trace)`` when ``trace`` is set.
    """
    n = len(boxes)
    valid = (1 << n) - 1 if present_mask is None else present_mask
    keep = 0
    order = sort_order(boxes)

    # Rows in rank order, exactly as the fill stage produces them.
    rows = [
        sum(1 << j for j in range(n) if suppresses(boxes[slot], boxes[j]))
        for slot in order
    ]

    steps: list[ResolveStep] = []
    for rank, slot in enumerate(order):
        kept = bool((valid >> slot) & 1)
        if kept:
            keep |= 1 << slot
            valid &= ~rows[rank]
        valid &= ~(1 << slot)
        if trace:
            steps.append(
                ResolveStep(
                    rank=rank,
                    slot=slot,
                    kept=kept,
                    suppress_row=rows[rank],
                    valid_mask=valid,
                    keep_mask=keep,
                ),
            )

    return (keep, steps) if trace else keep


def suppression_matrix(boxes: list[Box]) -> list[int]:
    """Return the full suppression matrix, one row per rank.

    Args:
        boxes: The batch, indexed by slot.

    Returns:
        Row *r* is a bitmask of the slots suppressed by the rank-*r* box.
    """
    n = len(boxes)
    return [
        sum(1 << j for j in range(n) if suppresses(boxes[slot], boxes[j]))
        for slot in sort_order(boxes)
    ]
