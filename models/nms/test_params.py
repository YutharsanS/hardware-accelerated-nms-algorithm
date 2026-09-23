"""Checks that the frozen constants agree with docs/architecture.md."""

from __future__ import annotations

import re
from pathlib import Path

from models.nms import params as p

ARCH_MD = Path(__file__).resolve().parents[2] / "docs" / "architecture.md"


def test_internally_consistent() -> None:
    problems = p.validate()
    assert problems == [], (
        "constants disagree with their own derivations:\n  " + "\n  ".join(problems)
    )


def test_record_is_exactly_64_bits_with_no_spare() -> None:
    assert p.RECORD_BITS == 64
    assert 4 * p.COORD_W + p.SCORE_W == p.RECORD_BITS
    # every field must be reachable and non-overlapping
    shifts = [p.SCORE_SHIFT, p.B_SHIFT, p.A_SHIFT, p.Y_SHIFT, p.X_SHIFT]
    widths = [p.SCORE_W, p.COORD_W, p.COORD_W, p.COORD_W, p.COORD_W]
    covered = 0
    for shift, width in zip(shifts, widths, strict=True):
        mask = ((1 << width) - 1) << shift
        assert covered & mask == 0, "record fields overlap"
        covered |= mask
    assert covered == (1 << p.RECORD_BITS) - 1, "record has spare bits"


def test_threshold_value_and_range_are_distinct() -> None:
    # T_INT is the value; the field range is what 8 bits can hold. Conflating the two is
    # what produced the "0 to 126" error in the original width table.
    assert p.T_INT == 128
    assert p.T_INT / (1 << p.K_SHIFT) == 0.5
    assert (1 << p.T_INT_W) - 1 == 255
    assert p.T_INT <= (1 << p.T_INT_W) - 1


def test_union_cannot_underflow_for_degenerate_boxes() -> None:
    # area == 0 implies I == 0, so U = area1 + area2 - I >= 0 always. Proof by the clamp:
    # if a <= x then min(a1,a2) <= x <= max(x1,x2), so t_w <= 0 and I clamps to 0.
    inverted = (100, 100, 50, 50)  # a < x and b < y
    other = (0, 0, 200, 200)
    xx, yy = max(inverted[0], other[0]), max(inverted[1], other[1])
    aa, bb = min(inverted[2], other[2]), min(inverted[3], other[3])
    w, h = max(0, aa - xx), max(0, bb - yy)
    assert w * h == 0


def test_latency_is_data_independent_and_scales_as_expected() -> None:
    assert p.latency_cycles(16) == 72
    # halving the lanes must double only the N^2/P term, not the fixed overhead
    fixed = p.LANE_LATENCY + p.PIPE_CUTS + 2
    assert p.latency_cycles(8) - fixed == 2 * (p.latency_cycles(16) - fixed)
    for lanes in (1, 2, 4, 8, 16, 32):
        assert p.N % lanes == 0
        assert p.latency_cycles(lanes) == p.N * p.N // lanes + fixed


def test_quantise_score_is_monotonic_and_saturating() -> None:
    assert p.quantise_score(0.0) == 0
    assert p.quantise_score(1.0) == p.SCORE_MAX
    assert p.quantise_score(-5.0) == 0
    assert p.quantise_score(5.0) == p.SCORE_MAX
    steps = [p.quantise_score(i / 100) for i in range(101)]
    assert steps == sorted(steps)
    # two-decimal confidences must stay distinct, or ties appear where none existed
    assert len(set(steps)) == len(steps)


def test_sort_key_is_a_strict_total_order() -> None:
    # K = score*N + (N-1-index): unique indices make ties impossible by construction.
    keys = [s * p.N + (p.N - 1 - i) for s in (0, 1, p.SCORE_MAX) for i in range(p.N)]
    assert len(set(keys)) == len(keys)
    assert max(keys).bit_length() == p.KEY_W


def test_matches_architecture_md() -> None:
    # The document is normative, so scrape the figures it states and compare. This is what
    # catches the two drifting apart later.
    text = ARCH_MD.read_text()
    for literal in (
        "16,769,025",  # max area
        "33,538,050",  # max union
        "4,292,870,400",  # max LHS
        "8,552,202,750",  # max RHS
        "72 cycles",  # latency at P=16
        "264 bytes in, 6 out",  # frame sizes
    ):
        assert literal in text, f"architecture.md no longer states {literal!r}"

    # widths table entries, as "| u | 24 |" style rows
    assert re.search(r"`area1, area2`.*\|\s*24\s*\|", text)
    assert re.search(r"`U = area1 \+ area2 − I`.*\|\s*25\s*\|", text)
    assert re.search(r"`LHS = I << 8`.*\|\s*32\s*\|", text)
    assert re.search(r"`RHS = T_INT · U`.*\|\s*33\s*\|", text)
