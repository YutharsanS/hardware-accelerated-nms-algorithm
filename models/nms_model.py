"""Integer golden model for the NMS accelerator (`docs/plan.md` Parts 1 and 2).

Every function here evaluates the exact integer expression the RTL will implement --
no floats, no division. `nms_sequential` is the textbook winner-takes-all loop and is
the authority on what NMS *means*; `nms_allpairs` is the rank-ordered
matrix-then-resolve structure the accelerator implements (Part 1e). The two must
agree on every input -- that agreement is the equivalence proof the hardware speedup
rests on (Q20), and `models/test_model.py` checks it over thousands of batches.
"""

from __future__ import annotations

from dataclasses import dataclass

from nms_params import (
    A_OFFSET,
    B_OFFSET,
    COORD_W,
    K_SHIFT,
    RECORD_BYTES,
    SCORE_OFFSET,
    SCORE_W,
    T_INT,
    X_OFFSET,
    Y_OFFSET,
    N,
)


@dataclass(frozen=True)
class Box:
    """One quantised detection, exactly as it sits in `box_store`.

    Attributes:
        x: Lower-left x, u12.
        y: Lower-left y, u12.
        a: Upper-right x, u12.
        b: Upper-right y, u12.
        score: Quantised confidence, u16.
    """

    x: int
    y: int
    a: int
    b: int
    score: int


def quantise_score(score_f: float) -> int:
    """Quantise a float confidence into the u16 fixed-point score field (Q16).

    Args:
        score_f: Confidence, nominally in `[0, 1]`.

    Returns:
        `round(score_f * 65535)`, clamped to `[0, 65535]`.
    """
    score_max = (1 << SCORE_W) - 1
    return min(max(round(score_f * score_max), 0), score_max)


def box_area(box: Box) -> int:
    """Clamped box area (Q15): `bw * bh` with `bw = max(0, a - x)`, `bh = max(0, b - y)`.

    Args:
        box: The box to measure.

    Returns:
        The area; 0 for a zero-area or inverted box, never negative.
    """
    bw = box.a - box.x if box.a > box.x else 0
    bh = box.b - box.y if box.b > box.y else 0
    return bw * bh


def intersection_area(first: Box, second: Box) -> int:
    """Clamped intersection area between two boxes (Q15), same clamp as `box_area`.

    Args:
        first: One box.
        second: The other box.

    Returns:
        The intersection area; 0 when the boxes do not overlap.
    """
    xx = max(first.x, second.x)
    yy = max(first.y, second.y)
    aa = min(first.a, second.a)
    bb = min(first.b, second.b)
    w = aa - xx if aa > xx else 0
    h = bb - yy if bb > yy else 0
    return w * h


def suppresses(keeper: Box, candidate: Box) -> bool:
    """The exact integer predicate the RTL compares: `(I << K_SHIFT) >= T_INT * U` (Q19).

    Args:
        keeper: The higher-ranked box (the current winner).
        candidate: The box being tested against it.

    Returns:
        True when `candidate` is suppressed by `keeper`. Never divides and never
        raises: `U` cannot underflow for any input, including zero-area and inverted
        boxes (Q15's proof), so the same expression is safe everywhere.
    """
    intersection = intersection_area(keeper, candidate)
    union = box_area(keeper) + box_area(candidate) - intersection
    lhs = intersection << K_SHIFT
    rhs = T_INT * union
    return lhs >= rhs


def sort_key(score: int, index: int) -> int:
    """The strict-total-order sort key `K = score * N + (N - 1 - index)` (Q18).

    Indices are unique, so `K` is a strict total order over the batch: ties are
    structurally impossible, and descending `K` is descending score with ties broken
    by the lower index -- equivalent to `sorted(key=lambda i: (-score[i], i))`.

    Args:
        score: The box's u16 score.
        index: The box's arrival-order slot, `0..N-1`.

    Returns:
        The `KEY_W`-bit sort key.
    """
    return score * N + (N - 1 - index)


def pack_record(box: Box) -> bytes:
    """Pack a box into its 8-byte wire record, MSB-first (Part 2).

    Args:
        box: The box to pack. Fields must already fit their widths.

    Returns:
        `RECORD_BYTES` bytes: `x(12) | y(12) | a(12) | b(12) | score(16)`.
    """
    value = (
        (box.x << X_OFFSET)
        | (box.y << Y_OFFSET)
        | (box.a << A_OFFSET)
        | (box.b << B_OFFSET)
        | (box.score << SCORE_OFFSET)
    )
    return value.to_bytes(RECORD_BYTES, "big")


def unpack_record(data: bytes) -> Box:
    """Unpack an 8-byte wire record into a `Box` (Part 2).

    Args:
        data: `RECORD_BYTES` bytes, MSB-first.

    Returns:
        The decoded box.
    """
    value = int.from_bytes(data, "big")
    coord_mask = (1 << COORD_W) - 1
    score_mask = (1 << SCORE_W) - 1
    return Box(
        x=(value >> X_OFFSET) & coord_mask,
        y=(value >> Y_OFFSET) & coord_mask,
        a=(value >> A_OFFSET) & coord_mask,
        b=(value >> B_OFFSET) & coord_mask,
        score=(value >> SCORE_OFFSET) & score_mask,
    )


def _rank_order(boxes: list[Box]) -> list[int]:
    """Slot indices in descending sort-key order -- `index_table` (Part 2)."""
    return sorted(
        range(len(boxes)),
        key=lambda i: sort_key(boxes[i].score, i),
        reverse=True,
    )


def nms_sequential(boxes: list[Box], present_mask: int) -> int:
    """Textbook winner-takes-all NMS -- the authority on what the algorithm means.

    Walks boxes in rank order; each still-valid box becomes a keeper, is removed from
    consideration, and immediately suppresses every other still-valid box it beats
    (Q19's `>=` predicate). Absent slots (`present_mask` bit clear) are never selected
    as keepers and never suppress anything (Part 2's `present_mask` contract).

    Args:
        boxes: Exactly `N` boxes, indexed by arrival-order slot.
        present_mask: Bit `i` set means slot `i` holds a real detection.

    Returns:
        `keep_mask`: bit `i` set means slot `i` survived.
    """
    order = _rank_order(boxes)
    valid = present_mask
    keep = 0
    for i in order:
        if not (valid >> i) & 1:
            continue
        keep |= 1 << i
        valid &= ~(1 << i)
        for j in order:
            if (valid >> j) & 1 and suppresses(boxes[i], boxes[j]):
                valid &= ~(1 << j)
    return keep


def nms_allpairs(boxes: list[Box], present_mask: int) -> int:
    """Rank-ordered matrix-then-resolve NMS -- the structure the RTL implements (Part 1e/2).

    Every suppression row is evaluated independent of `valid_mask` state, exactly as
    the `iou_lane`s do ahead of any keeper decision, then resolved one rank at a time,
    trailing the (here notional) fill by the lane latency. Verified equivalent to
    `nms_sequential` over adversarial batches (Q20) -- that equivalence is what lets
    the RTL skip the per-keeper pipeline drain.

    Args:
        boxes: Exactly `N` boxes, indexed by arrival-order slot.
        present_mask: Bit `i` set means slot `i` holds a real detection.

    Returns:
        `keep_mask`: bit `i` set means slot `i` survived.
    """
    order = _rank_order(boxes)
    valid = present_mask
    keep = 0
    for idx_r in order:
        row_mask = 0
        for column, candidate in enumerate(boxes):
            if suppresses(boxes[idx_r], candidate):
                row_mask |= 1 << column
        if (valid >> idx_r) & 1:
            keep |= 1 << idx_r
            valid &= ~row_mask
        valid &= ~(1 << idx_r)
    return keep
