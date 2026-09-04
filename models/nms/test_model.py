"""B1.2 gate: the integer model reproduces the anchor and both forms agree everywhere."""

from __future__ import annotations

import ast
import itertools
import json
import random
from pathlib import Path

import pytest

from models.nms import batches, model
from models.nms import params as p

REPO = Path(__file__).resolve().parents[2]
AGREEMENT_BATCHES = 20_000


# --- the anchor --------------------------------------------------------------------


def test_anchor_keep_mask() -> None:
    boxes = list(batches.NOTEBOOK_32)
    assert len(boxes) == 32
    assert model.nms_sequential(boxes) == batches.NOTEBOOK_KEEP_MASK
    assert model.nms_allpairs(boxes) == batches.NOTEBOOK_KEEP_MASK
    # survivors at slots 0, 8, 16, 24, 29, 30, 31
    kept = [i for i in range(32) if batches.NOTEBOOK_KEEP_MASK >> i & 1]
    assert kept == [0, 8, 16, 24, 29, 30, 31]


def test_embedded_notebook_set_matches_the_notebook() -> None:
    # The constant is embedded so tests do not depend on executing the notebook, but it
    # must not drift from it. Re-execute and compare.
    nb = json.loads((REPO / "models" / "golden-model.ipynb").read_text())
    code = "\n".join(
        "".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"
    )
    scope: dict[str, object] = {}
    # B1.5 rewired the notebook onto models/nms/model.py, and it now executes cleanly, so
    # this deliberately does NOT suppress exceptions -- a notebook that raises is a failure.
    # Before that it ended in bare expressions that raised outside Jupyter.
    exec(code, scope)  # noqa: S102 - the notebook is repository content
    raw = scope.get("test_boxes")
    assert raw is not None, "notebook no longer defines test_boxes"
    assert len(raw) == len(batches.NOTEBOOK_32)
    for embedded, source in zip(batches.NOTEBOOK_32, raw, strict=True):
        assert (embedded.x, embedded.y, embedded.a, embedded.b) == tuple(source[:4])
        assert embedded.score == p.quantise_score(source[4])


def test_anchor_exercises_neither_ties_nor_the_boundary() -> None:
    # Recorded as a test so the claim cannot quietly stop being true: passing the anchor
    # alone proves very little, which is why the synthetic cases exist.
    boxes = list(batches.NOTEBOOK_32)
    scores = [b.score for b in boxes]
    assert len(set(scores)) == len(scores), "anchor gained a duplicate score"

    on_boundary = 0
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            inter = model.intersection_area(boxes[i], boxes[j])
            union = model.box_area(boxes[i]) + model.box_area(boxes[j]) - inter
            if union and 2 * inter == union:
                on_boundary += 1
    assert on_boundary == 0, "anchor gained a boundary pair"


# --- the two forms agree -----------------------------------------------------------


@pytest.mark.parametrize("name", sorted(batches.named_cases()))
def test_both_forms_agree_on_named_cases(name: str) -> None:
    boxes = batches.named_cases()[name]
    assert model.nms_sequential(boxes) == model.nms_allpairs(boxes)


def test_both_forms_agree_over_many_hostile_batches() -> None:
    mismatches = []
    for i, boxes in enumerate(batches.hostile_stream(AGREEMENT_BATCHES, seed=7)):
        if model.nms_sequential(boxes) != model.nms_allpairs(boxes):
            mismatches.append(i)
    assert mismatches == [], (
        f"{len(mismatches)} mismatches, first at batch {mismatches[:5]}"
    )


def test_both_forms_agree_with_partial_present_masks() -> None:
    rng = random.Random(11)
    for boxes in batches.hostile_stream(500, seed=13):
        mask = rng.getrandbits(p.N)
        assert model.nms_sequential(boxes, mask) == model.nms_allpairs(boxes, mask)


def test_absent_slots_never_survive() -> None:
    rng = random.Random(17)
    for boxes in batches.hostile_stream(200, seed=19):
        mask = rng.getrandbits(p.N)
        keep = model.nms_allpairs(boxes, mask)
        assert keep & ~mask == 0, "a slot marked absent appeared in keep_mask"
    assert model.nms_allpairs(list(batches.NOTEBOOK_32), 0) == 0


# --- no division, and the union lemma holds ----------------------------------------


def test_model_performs_no_division() -> None:
    # Division is what a float formulation would use, and it is what makes a boundary pair
    # ambiguous. Assert structurally rather than by inspection.
    tree = ast.parse((Path(model.__file__)).read_text())
    banned = (ast.Div, ast.FloorDiv, ast.Mod)
    offenders = [
        f"line {node.lineno}: {type(node.op).__name__}"
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp) and isinstance(node.op, banned)
    ]
    offenders += [
        f"line {node.lineno}: augmented {type(node.op).__name__}"
        for node in ast.walk(tree)
        if isinstance(node, ast.AugAssign) and isinstance(node.op, banned)
    ]
    assert offenders == [], "model.py contains division: " + "; ".join(offenders)


def test_union_never_underflows_across_every_case() -> None:
    # suppresses() asserts U >= 0 internally, so this passing means the lemma in
    # architecture.md section 7 held for every pair evaluated below.
    for boxes in batches.named_cases().values():
        model.suppression_matrix(boxes)
    for boxes in batches.hostile_stream(2_000, seed=23):
        model.suppression_matrix(boxes)


def test_degenerate_boxes_have_zero_area_and_zero_intersection() -> None:
    inverted = model.Box(400, 400, 350, 350, 1000)
    zero = model.Box(100, 100, 100, 100, 1000)
    normal = model.Box(0, 0, 500, 500, 1000)
    assert model.box_area(inverted) == 0
    assert model.box_area(zero) == 0
    # area == 0 implies I == 0, which is what keeps U >= 0
    for degenerate in (inverted, zero):
        assert model.intersection_area(degenerate, normal) == 0
        assert model.intersection_area(normal, degenerate) == 0
    # two fully degenerate boxes give 0 >= 0, i.e. suppression, by specification
    assert model.suppresses(zero, zero) is True


# --- the boundary predicate --------------------------------------------------------


def test_boundary_predicate_is_inclusive() -> None:
    # 2I == U exactly: `>=` must suppress. This is the case a float IoU would decide by
    # rounding, which is why the predicate is cross-multiplied.
    exact_a, exact_b = batches.boundary_pair(d=10, height=30)
    inter = model.intersection_area(exact_a, exact_b)
    union = model.box_area(exact_a) + model.box_area(exact_b) - inter
    assert 2 * inter == union, f"pair is not on the boundary: 2*{inter} vs {union}"
    assert model.suppresses(exact_a, exact_b) is True

    above_a, above_b = batches.boundary_pair(d=10, height=30, offset=1)
    assert model.suppresses(above_a, above_b) is True

    below_a, below_b = batches.boundary_pair(d=10, height=30, offset=-1)
    i2 = model.intersection_area(below_a, below_b)
    u2 = model.box_area(below_a) + model.box_area(below_b) - i2
    assert 2 * i2 < u2
    assert model.suppresses(below_a, below_b) is False


def test_predicate_is_symmetric() -> None:
    # IoU is symmetric, so the suppression matrix must be too; the hardware relies on this
    # when it computes all N^2 ordered pairs instead of the N(N-1)/2 unordered ones.
    for boxes in batches.hostile_stream(200, seed=29):
        for i in range(0, p.N, 3):
            for j in range(i, p.N, 5):
                assert model.suppresses(boxes[i], boxes[j]) == model.suppresses(
                    boxes[j], boxes[i]
                )


# --- ordering ----------------------------------------------------------------------


def test_sort_key_matches_the_python_equivalent() -> None:
    rng = random.Random(31)
    for _ in range(500):
        boxes = batches.case_low_res_scores(rng)
        by_key = model.sort_order(boxes)
        by_tuple = sorted(range(len(boxes)), key=lambda i: (-boxes[i].score, i))
        assert by_key == by_tuple


def test_sort_key_is_injective_and_fits_its_width() -> None:
    keys = [
        model.sort_key(s, i) for s in (0, 1, 32767, p.SCORE_MAX) for i in range(p.N)
    ]
    assert len(set(keys)) == len(keys)
    assert max(keys).bit_length() == p.KEY_W


# --- record packing ----------------------------------------------------------------


def test_record_round_trips() -> None:
    rng = random.Random(37)
    for boxes in batches.hostile_stream(100, seed=41):
        for box in boxes:
            assert model.unpack_record(model.pack_record(box)) == box
    for _ in range(2_000):
        box = model.Box(
            rng.randrange(0, p.COORD_MAX + 1),
            rng.randrange(0, p.COORD_MAX + 1),
            rng.randrange(0, p.COORD_MAX + 1),
            rng.randrange(0, p.COORD_MAX + 1),
            rng.randrange(0, p.SCORE_MAX + 1),
        )
        record = model.pack_record(box)
        assert 0 <= record < (1 << p.RECORD_BITS)
        assert model.unpack_record(record) == box


def test_pack_rejects_out_of_range_fields() -> None:
    with pytest.raises(ValueError, match="out of range"):
        model.pack_record(model.Box(p.COORD_MAX + 1, 0, 0, 0, 0))
    with pytest.raises(ValueError, match="out of range"):
        model.pack_record(model.Box(0, 0, 0, 0, p.SCORE_MAX + 1))


# --- the trace the RTL control testbench needs -------------------------------------


def test_trace_is_consistent_and_ends_at_the_final_masks() -> None:
    boxes = list(batches.NOTEBOOK_32)
    keep, steps = model.nms_allpairs(boxes, trace=True)
    assert keep == batches.NOTEBOOK_KEEP_MASK
    assert len(steps) == p.N
    assert [s.rank for s in steps] == list(range(p.N))
    assert [s.slot for s in steps] == model.sort_order(boxes)
    assert steps[-1].keep_mask == keep
    assert steps[-1].valid_mask == 0, "every slot must be resolved by the last rank"
    # keep_mask only ever grows; valid_mask only ever shrinks
    for earlier, later in itertools.pairwise(steps):
        assert later.keep_mask & earlier.keep_mask == earlier.keep_mask
        assert later.valid_mask & earlier.valid_mask == later.valid_mask


def test_trace_shows_rows_applied_to_earlier_ranks_are_no_ops() -> None:
    # This is the invariant that makes the all-pairs restructure equivalent to the
    # sequential loop: when rank r is a keeper, every bit of its row that lands on an
    # earlier rank is already clear, so applying the whole row is harmless.
    for boxes in batches.hostile_stream(300, seed=43):
        _, steps = model.nms_allpairs(boxes, trace=True)
        order = model.sort_order(boxes)
        rank_of = {slot: r for r, slot in enumerate(order)}
        valid_before = (1 << p.N) - 1
        for step in steps:
            if step.kept:
                for slot in range(p.N):
                    if step.suppress_row >> slot & 1 and rank_of[slot] < step.rank:
                        assert valid_before >> slot & 1 == 0, (
                            f"row of rank {step.rank} hit still-valid earlier rank "
                            f"{rank_of[slot]}"
                        )
            valid_before = step.valid_mask


def test_union_is_bounded_by_the_image_not_by_twice_a_box() -> None:
    # docs/architecture.md gives max U as 2*COORD_MAX**2 = 33,538,050, needing 25 bits.
    # That is a correct *safe* bound but not a tight one: U = |A| + |B| - |A and B| is the
    # area of the geometric union, and both boxes live inside the same 4096x4096 space, so
    # U can never exceed COORD_MAX**2 = 16,769,025, which fits 24 bits.
    #
    # Recorded as a test for two reasons. UNION_W = 25 is still *required*, because the
    # k_area + c_area intermediate genuinely reaches 33,538,050 -- so anyone narrowing it
    # to 24 on the strength of the U bound would break the adder. And the 25th bit of U,
    # hence the 33rd bit of RHS, is unreachable by construction, so chasing 100% bit
    # coverage there is wasted effort rather than a missing test.
    full = model.Box(0, 0, p.COORD_MAX, p.COORD_MAX, 0)
    assert model.box_area(full) == p.COORD_MAX**2

    # the k_area + c_area intermediate genuinely needs all 25 bits
    area_sum = model.box_area(full) + model.box_area(full)
    assert area_sum == 2 * p.COORD_MAX**2
    assert area_sum.bit_length() == p.UNION_W

    # the union itself cannot exceed the image area, whatever the two boxes are
    rng = random.Random(103)

    def well_formed() -> model.Box:
        """Return a random box with a > x and b > y over the full coordinate range."""
        x, a = sorted((rng.randint(0, p.COORD_MAX), rng.randint(0, p.COORD_MAX)))
        y, b = sorted((rng.randint(0, p.COORD_MAX), rng.randint(0, p.COORD_MAX)))
        return model.Box(x, y, a, b, 0)

    worst = 0
    candidates = [
        (full, full),
        (full, model.Box(0, 0, p.COORD_MAX, p.COORD_MAX // 2, 0)),
        # two disjoint half-planes -- the arrangement that looks like it should double U
        (
            model.Box(0, 0, p.COORD_MAX, 2047, 0),
            model.Box(0, 2048, p.COORD_MAX, p.COORD_MAX, 0),
        ),
        *((well_formed(), well_formed()) for _ in range(20_000)),
    ]
    for first, second in candidates:
        inter = model.intersection_area(first, second)
        union = model.box_area(first) + model.box_area(second) - inter
        assert 0 <= union <= p.COORD_MAX**2, f"union {union} exceeds the image area"
        worst = max(worst, union)
    assert worst == p.COORD_MAX**2, f"never reached the true maximum, only {worst}"
    assert worst.bit_length() == p.UNION_W - 1

    # and therefore the top bit of the 33-bit RHS is unreachable
    max_rhs = (2**p.T_INT_W - 1) * p.COORD_MAX**2
    assert max_rhs.bit_length() == p.RHS_W - 1
    assert max_rhs < 2 ** (p.RHS_W - 1)


def test_suppresses_at_covers_the_whole_threshold_field() -> None:
    # T_INT is a generic, not a constant, so every value it can hold must work. 0 means
    # "suppress everything" (0 >= 0) and 255 is the widest RHS the datapath can see.
    boxes = batches.named_cases()["boundary"]
    first, second = boxes[0], boxes[1]
    assert model.suppresses_at(first, second, 0) is True
    assert model.suppresses(first, second) == model.suppresses_at(
        first, second, p.T_INT
    )

    # Monotone in the threshold: raising T_INT can only ever suppress less. This is the
    # property the RTL's T_INT generic has to preserve, and it holds for all 256 values.
    verdicts = [model.suppresses_at(first, second, t) for t in range(2**p.T_INT_W)]
    assert verdicts[0] is True, "T_INT = 0 must suppress everything (0 >= 0)"
    for lower, higher in itertools.pairwise(verdicts):
        assert not (higher and not lower), "raising T_INT started suppressing more"

    # monotone in the threshold: raising it can only ever suppress less
    verdicts = [model.suppresses_at(first, second, t) for t in range(2**p.T_INT_W)]
    assert verdicts[0] is True
    for lower, higher in itertools.pairwise(verdicts):
        assert not (higher and not lower), "raising T_INT started suppressing more"
