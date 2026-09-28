"""The batches every benchmark runs, and the references they are checked against.

plan.md Phase E, E.3:

* the four ``bench.benchmark_suite`` cases, each timed on its own;
* ``hostile``: cycling through ``batches.hostile_stream(1000)``, so that a repeated batch
  can't flatter the branch predictor.

Our own variants must match ``model.nms_sequential`` exactly. The libraries suppress on
IoU **>** threshold where the spec says **>=** (architecture.md §5), so they are checked
against :func:`nms_strict`, the same algorithm with ``>``. Where a library still disagrees,
:func:`explain_mismatch` says whether a known cause -- tied scores, inverted boxes, or a
pair exactly on the threshold, where float rounding decides -- can account for it.
"""

from __future__ import annotations

from dataclasses import dataclass

from models.nms import batches, model
from models.nms import params as p

SUITE_CASES = ("notebook32", "all_survive", "all_equal", "rand_seed0")
HOSTILE = "hostile"
HOSTILE_COUNT = 1000
ALL_PRESENT = (1 << p.N) - 1


@dataclass(frozen=True)
class Batch:
    """One NMS input with its expected answers.

    Attributes:
        boxes: The 32 boxes, indexed by slot.
        present_mask: Which slots hold real detections.
        keep_mask: The golden model's answer, ``>=``.
        keep_strict: The same algorithm with ``>``, what the libraries compute.
    """

    boxes: list[model.Box]
    present_mask: int
    keep_mask: int
    keep_strict: int


def nms_strict(boxes: list[model.Box], present_mask: int = ALL_PRESENT) -> int:
    """Run ``model.nms_sequential`` with ``>`` in place of ``>=``.

    This is what torchvision and OpenCV compute: suppress when IoU > threshold. The order
    is the golden model's, descending score with ties to the lower index.

    Args:
        boxes: The batch.
        present_mask: Which slots are real.

    Returns:
        ``keep_mask``.
    """
    valid = present_mask
    keep = 0
    order = model.sort_order(boxes)
    area = [model.box_area(b) for b in boxes]
    for rank, slot in enumerate(order):
        if not (valid >> slot) & 1:
            continue
        keep |= 1 << slot
        valid &= ~(1 << slot)
        for other in order[rank + 1 :]:
            if not (valid >> other) & 1:
                continue
            inter = model.intersection_area(boxes[slot], boxes[other])
            union = area[slot] + area[other] - inter
            if (inter << p.K_SHIFT) > p.T_INT * union:
                valid &= ~(1 << other)
    return keep


def make_batch(boxes: list[model.Box], present_mask: int = ALL_PRESENT) -> Batch:
    """Wrap a batch with both reference answers.

    Args:
        boxes: The batch.
        present_mask: Which slots are real.

    Returns:
        The batch and its expected ``keep_mask`` under both predicates.
    """
    return Batch(
        boxes=list(boxes),
        present_mask=present_mask,
        keep_mask=model.nms_sequential(boxes, present_mask),
        keep_strict=nms_strict(boxes, present_mask),
    )


def suite(*, hostile_count: int = HOSTILE_COUNT) -> dict[str, list[Batch]]:
    """Return every input group, keyed by the case name results are reported under.

    Args:
        hostile_count: Size of the cycling hostile stream.

    Returns:
        One single-batch group per suite case, plus the ``hostile`` stream.
    """
    cases = batches.named_cases()
    groups = {name: [make_batch(cases[name])] for name in SUITE_CASES}
    groups[HOSTILE] = [make_batch(b) for b in batches.hostile_stream(hostile_count)]
    return groups


def _present(batch: Batch) -> list[int]:
    return [i for i in range(len(batch.boxes)) if (batch.present_mask >> i) & 1]


def explain_mismatch(batch: Batch) -> str:
    """Name the known cause that could make a float library disagree with ``nms_strict``.

    Args:
        batch: A batch on which a library's answer differed from ``keep_strict``.

    Returns:
        ``"ties"`` when present boxes share a score, so the library's tie order decides;
        ``"inverted"`` when a box has ``a < x`` or ``b < y``, which the libraries don't
        clamp; ``"on-threshold"`` when a pair has IoU exactly 0.5, where float rounding
        decides; otherwise ``"unexplained"``, which is a failure.
    """
    slots = _present(batch)
    scores = [batch.boxes[i].score for i in slots]
    if len(set(scores)) < len(scores):
        return "ties"
    if any(
        batch.boxes[i].a < batch.boxes[i].x or batch.boxes[i].b < batch.boxes[i].y
        for i in slots
    ):
        return "inverted"
    for j, first in enumerate(slots):
        for second in slots[j + 1 :]:
            a, b = batch.boxes[first], batch.boxes[second]
            inter = model.intersection_area(a, b)
            union = model.box_area(a) + model.box_area(b) - inter
            if (inter << p.K_SHIFT) == p.T_INT * union:
                return "on-threshold"
    return "unexplained"
