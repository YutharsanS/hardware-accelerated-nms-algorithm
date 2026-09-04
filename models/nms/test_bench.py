"""B1.4 gate: every implementation agrees on keep_mask, including the float one."""

from __future__ import annotations

import random

import numpy as np
import pytest

from models.nms import batches, bench, model
from models.nms import params as p


@pytest.mark.parametrize("name", sorted(batches.named_cases()))
def test_integer_and_numpy_agree(name: str) -> None:
    boxes = batches.named_cases()[name]
    expected = model.nms_sequential(boxes)
    assert model.nms_allpairs(boxes) == expected
    assert bench.numpy_allpairs(boxes) == expected


@pytest.mark.parametrize("name", sorted(batches.named_cases()))
def test_float_agrees_where_it_is_defined(name: str) -> None:
    boxes = batches.named_cases()[name]
    if bench.has_zero_union_pair(boxes):
        pytest.skip("float reference divides by zero on this case; see the test below")
    assert bench.float_reference(boxes) == model.nms_sequential(boxes)


def test_all_implementations_agree_over_random_batches() -> None:
    compared = 0
    for boxes in batches.hostile_stream(400, seed=53):
        expected = model.nms_sequential(boxes)
        assert model.nms_allpairs(boxes) == expected
        assert bench.numpy_allpairs(boxes) == expected
        if not bench.has_zero_union_pair(boxes):
            assert bench.float_reference(boxes) == expected
            compared += 1
    assert compared > 100, f"only {compared} batches were float-comparable"


def test_float_reference_crashes_where_the_integer_form_does_not() -> None:
    # The finding this gate surfaced: cross-multiplying the predicate is not merely a
    # hardware convenience, it removes a crash. The notebook writes an unguarded
    # `iou = intersection_area / union_area`, and two zero-area boxes give I = 0, U = 0.
    # The degenerate case alone contains 49 such pairs.
    degenerate = batches.named_cases()["degenerate"]
    assert bench.has_zero_union_pair(degenerate)
    with pytest.raises(ZeroDivisionError):
        bench.float_reference(degenerate)
    # the integer forms return a defined answer on the same input
    expected = model.nms_sequential(degenerate)
    assert model.nms_allpairs(degenerate) == expected
    assert bench.numpy_allpairs(degenerate) == expected


def test_float_and_integer_predicates_cannot_disagree_at_these_widths() -> None:
    # Deciding I/U >= 0.5 differently from 2I >= U needs |2I - U| < 1 with both integers,
    # i.e. 2I == U -- and 0.5 is exactly representable, so even then they agree. float64
    # has 53 bits of mantissa against values below 2**25 here, so there is no room for the
    # rounding that would make a boundary pair ambiguous. Asserted rather than assumed,
    # because the conclusion would NOT hold for float32 or much larger coordinates.
    rng = random.Random(59)
    checked = 0
    for boxes in [
        *batches.named_cases().values(),
        *batches.hostile_stream(300, seed=61),
    ]:
        for _ in range(40):
            first = boxes[rng.randrange(p.N)]
            second = boxes[rng.randrange(p.N)]
            inter = model.intersection_area(first, second)
            union = model.box_area(first) + model.box_area(second) - inter
            integer = (inter << p.K_SHIFT) >= p.T_INT * union
            floating = (inter / union if union else 0.0) >= 0.5
            # union == 0 is the one genuine difference: the integer form gives 0 >= 0 and
            # suppresses, while a float division would be undefined and is short-circuited
            # to 0.0 above. Specified behaviour, not a rounding disagreement.
            if union != 0:
                assert integer == floating, (
                    f"predicates disagree: I={inter} U={union} "
                    f"integer={integer} float={floating}"
                )
                checked += 1
    assert checked > 5_000, f"only {checked} comparable pairs sampled"


def test_zero_union_is_the_only_predicate_difference() -> None:
    zero = model.Box(100, 100, 100, 100, 1000)
    assert model.suppresses(zero, zero) is True, "integer form suppresses on 0 >= 0"
    # the float form would divide by zero here, which is exactly why the predicate is
    # cross-multiplied rather than expressed as a ratio
    inter = model.intersection_area(zero, zero)
    union = model.box_area(zero) + model.box_area(zero) - inter
    assert (inter, union) == (0, 0)


def test_benchmark_runs_and_reports_the_anchor() -> None:
    timings = bench.benchmark()
    assert len(timings) == 4
    assert all(t.micros > 0 for t in timings)
    assert {t.keep_mask for t in timings} == {batches.NOTEBOOK_KEEP_MASK}, (
        "implementations disagree on the anchor"
    )
    # the anchor has no zero-union pair, so the float form is defined on it
    assert not bench.has_zero_union_pair(list(batches.NOTEBOOK_32))
    report = bench.format_report(timings)
    assert "accelerator core" in report
    assert "thread-pool dispatch" in report
    assert "name the processor class" in report


def test_thread_pool_overhead_is_measurable() -> None:
    # Small rep count: this test only checks the probe works, not the value.
    overhead = bench.thread_pool_overhead_us(reps=200)
    assert overhead > 0


def test_predicate_agrees_with_a_vectorised_form_over_a_million_pairs() -> None:
    """B4.1's Python half: 10**6 pairs, scalar model against a numpy reimplementation.

    Driving a million pairs through GHDL would mean either a ~50 MB vector file or an LFSR
    mirrored bit-exactly in VHDL and Python -- and that mirror would itself need a gate.
    Splitting it is what the plan chose: 15,120 pairs through the RTL from file, and this
    volume in Python, where the two implementations are genuinely independent (Python's
    unbounded ints and scalar branches against numpy's int64 arrays and where-free
    arithmetic).
    """
    rng = np.random.default_rng(97)
    count = 1_000_000

    # Fully uniform, so roughly half the boxes are inverted on each axis and the clamp is
    # exercised on most rows -- which is what makes this worth a million samples rather
    # than a curated set.
    keeper = rng.integers(0, p.COORD_MAX + 1, size=(4, count), dtype=np.int64)
    cand = rng.integers(0, p.COORD_MAX + 1, size=(4, count), dtype=np.int64)

    def area(box: np.ndarray) -> np.ndarray:
        return np.maximum(box[2] - box[0], 0) * np.maximum(box[3] - box[1], 0)

    inter = np.maximum(
        np.minimum(keeper[2], cand[2]) - np.maximum(keeper[0], cand[0]), 0
    ) * np.maximum(np.minimum(keeper[3], cand[3]) - np.maximum(keeper[1], cand[1]), 0)
    union = area(keeper) + area(cand) - inter
    vectorised = (inter << p.K_SHIFT) >= p.T_INT * union

    # int64 has room to spare: the largest RHS here is far below 2**63.
    assert union.min() >= 0, "union underflowed in the vectorised form"
    assert (p.T_INT * union).max() < 2**63

    # .tolist() once, rather than indexing the arrays a million times.
    scalar = [
        model.suppresses(model.Box(*k, 0), model.Box(*c, 0))
        for k, c in zip(
            zip(*keeper.tolist(), strict=True),
            zip(*cand.tolist(), strict=True),
            strict=True,
        )
    ]

    mismatches = np.flatnonzero(vectorised != np.array(scalar))
    assert mismatches.size == 0, (
        f"{mismatches.size} of {count} pairs disagree, first at index "
        f"{mismatches[0] if mismatches.size else -1}"
    )
    # Both verdicts well represented, or a million samples proved one branch.
    assert 0.2 < vectorised.mean() < 0.8, (
        f"suppress rate {vectorised.mean():.3f} -- the sample is one-sided"
    )
