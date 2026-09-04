"""Self-checking tests for the integer golden model (`docs/plan.md` Phase A acceptance).

No pytest dependency -- each check prints a `PASS` line and asserts, mirroring the
VHDL testbench `report`/`assert` convention (CLAUDE.md). Run directly:

    uv run python models/test_model.py

Covers the three named acceptance checks: (a) `nms(notebook31)` reproduces the
notebook's 7 survivors, (b) `sort_key` order matches `sorted(key=(-score, idx))`,
(c) `suppresses` never divides and never raises on degenerate input. Also checks the
`pack_record`/`unpack_record` round trip and cross-checks every vector file
`gen_vectors.py` wrote under `models/data/`.
"""

from __future__ import annotations

import random
from pathlib import Path

from gen_vectors import (
    case_all_survive,
    case_boundary,
    case_degenerate,
    case_disjoint,
    case_notebook31,
    case_random,
    case_ties,
)
from nms_model import (
    Box,
    nms_allpairs,
    nms_sequential,
    pack_record,
    sort_key,
    suppresses,
    unpack_record,
)
from nms_params import N

DATA_DIR = Path(__file__).parent / "data"


def check_notebook_survivors() -> None:
    """(a) `nms(notebook31)` reproduces the notebook's 7 survivors."""
    boxes, present_mask = case_notebook31()
    keep_seq = nms_sequential(boxes, present_mask)
    keep_all = nms_allpairs(boxes, present_mask)
    assert keep_seq == keep_all, "sequential and allpairs disagree on notebook31"
    assert keep_seq.bit_count() == 7, (
        f"expected 7 survivors, got {keep_seq.bit_count()}"
    )
    print("PASS (a) notebook31: 7 survivors, sequential == allpairs")


def check_sort_key_order() -> None:
    """(b) `sort_key` order matches `sorted(key=lambda i: (-score[i], i))`."""
    rng = random.Random("sort_key_order")
    for _ in range(10_000):
        scores = [rng.randrange(0, 1 << 16) for _ in range(N)]
        expected = sorted(range(N), key=lambda i: (-scores[i], i))
        got = sorted(range(N), key=lambda i: -sort_key(scores[i], i))
        assert expected == got, "sort_key order diverged from (-score, index)"
    print(
        "PASS (b) sort_key: matches sorted(key=(-score, idx)) over 10,000 random batches"
    )


def check_suppresses_never_raises() -> None:
    """(c) `suppresses` never divides and never raises on zero-area or inverted input."""
    rng = random.Random("suppresses_degenerate")
    for _ in range(10_000):
        boxes = []
        for _ in range(2):
            x = rng.randrange(0, 4096)
            y = rng.randrange(0, 4096)
            # Bias heavily toward degenerate/inverted corners, not just normal boxes.
            a = rng.randrange(max(0, x - 50), min(4096, x + 50))
            b = rng.randrange(max(0, y - 50), min(4096, y + 50))
            score = rng.randrange(0, 1 << 16)
            boxes.append(Box(x, y, a, b, score))
        result = suppresses(boxes[0], boxes[1])
        assert isinstance(result, bool)
    print("PASS (c) suppresses: no exceptions over 10,000 degenerate/inverted trials")


def check_record_round_trip() -> None:
    """`pack_record`/`unpack_record` recover the exact box for every field's full range."""
    rng = random.Random("record_round_trip")
    for _ in range(10_000):
        box = Box(
            x=rng.randrange(0, 4096),
            y=rng.randrange(0, 4096),
            a=rng.randrange(0, 4096),
            b=rng.randrange(0, 4096),
            score=rng.randrange(0, 1 << 16),
        )
        packed = pack_record(box)
        assert len(packed) == 8
        assert unpack_record(packed) == box
    print("PASS record round trip: pack_record/unpack_record over 10,000 random boxes")


def check_allpairs_matches_sequential(n_batches: int = 3000) -> None:
    """`nms_allpairs` agrees with `nms_sequential` over adversarial random batches (Q20)."""
    for seed in range(n_batches):
        boxes, present_mask = case_random(seed)
        keep_seq = nms_sequential(boxes, present_mask)
        keep_all = nms_allpairs(boxes, present_mask)
        assert keep_seq == keep_all, f"mismatch at rand seed {seed}"
    print(
        f"PASS nms_allpairs == nms_sequential over {n_batches} adversarial random batches"
    )


def check_named_cases_agree() -> None:
    """Every named A4 case (not just the random ones) also agrees, sequential vs allpairs."""
    cases = {
        "ties": case_ties(),
        "degenerate": case_degenerate(),
        "boundary": case_boundary(),
        "disjoint": case_disjoint(),
        "all_survive": case_all_survive(),
    }
    for name, (boxes, present_mask) in cases.items():
        keep_seq = nms_sequential(boxes, present_mask)
        keep_all = nms_allpairs(boxes, present_mask)
        assert keep_seq == keep_all, f"{name}: sequential and allpairs disagree"
    print(f"PASS named cases agree: {', '.join(cases)}")


def check_generated_vectors() -> None:
    """Every `models/data/*.hex`/`.mask` pair matches the golden model when replayed."""
    hex_files = sorted(DATA_DIR.glob("*.hex"))
    assert hex_files, (
        f"no vector files found under {DATA_DIR} -- run gen_vectors.py first"
    )
    for hex_path in hex_files:
        lines = hex_path.read_text().split()
        record_lines, mask_line = lines[:-1], lines[-1]
        assert len(record_lines) == N, f"{hex_path.name}: expected {N} record lines"
        boxes = [unpack_record(bytes.fromhex(line)) for line in record_lines]
        present_mask = int(mask_line, 16)
        expected = int((hex_path.with_suffix(".mask")).read_text().strip(), 16)
        assert nms_allpairs(boxes, present_mask) == expected, (
            f"{hex_path.name}: mask mismatch"
        )
        assert nms_sequential(boxes, present_mask) == expected, (
            f"{hex_path.name}: mask mismatch"
        )
    print(
        f"PASS generated vectors: {len(hex_files)} files replay bit-exact against nms_allpairs"
    )


def main() -> None:
    """Run every check; exits non-zero (via AssertionError) on the first failure."""
    check_notebook_survivors()
    check_sort_key_order()
    check_suppresses_never_raises()
    check_record_round_trip()
    check_allpairs_matches_sequential()
    check_named_cases_agree()
    check_generated_vectors()
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
