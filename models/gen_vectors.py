"""Generate the RTL verification vectors specified in `docs/plan.md` A4.

Each case is `N` boxes plus a `present_mask`. The expected `keep_mask` is not
hand-derived -- it is computed by running the golden model (`nms_model.py`) itself,
so these files are exactly what `test/tb_iou_lane.vhd` and friends (Phase B/C1) will
be checked bit-exact against. Every case also asserts `nms_sequential ==
nms_allpairs` at generation time, so a broken case can never be committed.

Run: `uv run python models/gen_vectors.py` from the repository root.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

from nms_model import Box, nms_allpairs, nms_sequential, pack_record, quantise_score
from nms_params import N

ALL_PRESENT = (1 << N) - 1

# Mirrors models/golden-model.ipynb's `test_boxes` cell. Keep in sync if that cell
# changes -- this is the one dataset shared between the notebook narrative and the
# RTL vectors. (x, y, a, b, confidence)
NOTEBOOK_RAW: list[tuple[int, int, int, int, float]] = [
    # Cluster A -- cat detection
    (10, 10, 50, 50, 0.95),
    (12, 11, 52, 51, 0.90),
    (9, 13, 48, 53, 0.85),
    (14, 9, 54, 49, 0.70),
    (11, 14, 51, 54, 0.65),
    (15, 12, 55, 52, 0.55),
    (8, 8, 46, 46, 0.40),
    (13, 15, 53, 55, 0.30),
    # Cluster B -- dog detection
    (100, 100, 150, 150, 0.92),
    (102, 101, 152, 151, 0.88),
    (98, 103, 148, 153, 0.80),
    (104, 99, 154, 149, 0.72),
    (101, 105, 151, 155, 0.60),
    (103, 98, 153, 148, 0.50),
    (97, 102, 147, 152, 0.42),
    (105, 104, 155, 154, 0.28),
    # Cluster C -- car detection
    (200, 50, 260, 100, 0.93),
    (202, 52, 262, 102, 0.87),
    (198, 48, 258, 98, 0.78),
    (204, 53, 264, 103, 0.68),
    (201, 47, 261, 97, 0.58),
    (199, 55, 259, 105, 0.48),
    (203, 49, 263, 99, 0.38),
    (197, 51, 257, 101, 0.25),
    # Cluster D -- person detection
    (50, 200, 100, 280, 0.91),
    (52, 202, 102, 282, 0.82),
    (48, 198, 98, 278, 0.73),
    (54, 203, 104, 283, 0.62),
    (47, 199, 97, 279, 0.45),
    # Isolated boxes -- should all survive
    (300, 300, 340, 340, 0.75),
    (0, 280, 30, 310, 0.35),
    (280, 0, 320, 40, 0.20),
]


def case_notebook31() -> tuple[list[Box], int]:
    """The notebook's own dataset, all 32 slots present.

    Named `notebook31` per `docs/plan.md` A4; the notebook narrative undercounts this
    set by one (it has always had 32 entries -- see the isolated-box cluster), so all
    32 are marked present here. That is what reproduces the notebook's stated 7
    survivors, which is the acceptance test A3 is checked against.
    """
    boxes = [Box(x, y, a, b, quantise_score(c)) for x, y, a, b, c in NOTEBOOK_RAW]
    return boxes, ALL_PRESENT


def case_ties() -> tuple[list[Box], int]:
    """Same geometry as `notebook31`, but every score identical -- all-equal ties.

    With every score equal, ranking is decided entirely by `sort_key`'s index
    tie-break (Q18): the lowest-index box in each overlapping cluster survives, and
    the isolated boxes survive regardless.
    """
    boxes = [Box(x, y, a, b, quantise_score(0.5)) for x, y, a, b, _ in NOTEBOOK_RAW]
    return boxes, ALL_PRESENT


def case_degenerate() -> tuple[list[Box], int]:
    """Zero-area, inverted, normal-cluster and isolated boxes, mixed (Q15).

    Exercises: degenerate-vs-degenerate mutual suppression (`I=0, U=0` -> suppress,
    Q15), degenerate boxes never suppressing a normal candidate, and a normal keeper
    still suppressing normal candidates correctly amid the degenerate noise.
    """
    boxes: list[Box] = []
    # 8 zero-area boxes (a == x): distinct positions, descending score.
    for i in range(8):
        x = 100 + i * 5
        boxes.append(Box(x, 500, x, 550, quantise_score(0.9 - i * 0.05)))
    # 8 inverted boxes (a < x or b < y): distinct positions, descending score.
    for i in range(8):
        x = 300 + i * 5
        if i % 2 == 0:
            boxes.append(Box(x, 500, x - 10, 550, quantise_score(0.5 - i * 0.02)))
        else:
            boxes.append(Box(x, 500, x + 40, 480, quantise_score(0.5 - i * 0.02)))
    # 8 normal, mutually overlapping boxes -- one real cluster amid the noise.
    for i in range(8):
        off = i * 2
        boxes.append(
            Box(
                1000 + off,
                1000 + off,
                1060 + off,
                1060 + off,
                quantise_score(0.9 - i * 0.05),
            ),
        )
    # 8 isolated normal boxes, always present, well separated.
    for i in range(8):
        x = 2000 + i * 200
        boxes.append(Box(x, 2000, x + 80, 2080, quantise_score(0.6)))
    return boxes, ALL_PRESENT


def case_boundary() -> tuple[list[Box], int]:
    """Two boxes with intersection exactly `2*I == U` -- the `>=` predicate's edge.

    `box0 = (0,0,100,100)` area 10,000; `box1 = (25,0,125,80)` area 8,000; their
    intersection is exactly 6,000, giving `U = 12,000 = 2*I`, i.e. `LHS == RHS`
    exactly at `T_INT=128`. Q19 requires `>=`, so this pair must suppress.
    """
    boxes = [
        Box(0, 0, 100, 100, quantise_score(0.9)),
        Box(25, 0, 125, 80, quantise_score(0.8)),
    ]
    for i in range(N - 2):
        x = 1000 + (i % 16) * 120
        y = 1000 + (i // 16) * 120
        boxes.append(Box(x, y, x + 80, y + 80, quantise_score(0.5)))
    return boxes, ALL_PRESENT


def case_disjoint() -> tuple[list[Box], int]:
    """32 boxes on a grid, spaced so no pair ever overlaps -- everyone survives."""
    boxes = []
    for i in range(N):
        col, row = i % 8, i // 8
        x, y = col * 120, row * 120
        boxes.append(Box(x, y, x + 80, y + 80, quantise_score(0.3 + 0.02 * i)))
    return boxes, ALL_PRESENT


def case_all_survive() -> tuple[list[Box], int]:
    """32 boxes overlapping only their immediate neighbour, always below the 50% IoU threshold."""
    boxes = []
    for i in range(N):
        x = i * 85
        boxes.append(Box(x, 0, x + 100, 100, quantise_score(0.9 - 0.01 * i)))
    return boxes, ALL_PRESENT


def case_random(seed: int) -> tuple[list[Box], int]:
    """A reproducible random batch: coordinates, scores and `present_mask` all random."""
    rng = random.Random(f"rand_seed{seed}")
    boxes = []
    for _ in range(N):
        x = rng.randrange(0, 3500)
        y = rng.randrange(0, 3500)
        a = min(x + rng.randrange(1, 800), 4095)
        b = min(y + rng.randrange(1, 800), 4095)
        boxes.append(Box(x, y, a, b, rng.randrange(0, 1 << 16)))
    present_mask = 0
    for i in range(N):
        if rng.random() < 0.9:
            present_mask |= 1 << i
    return boxes, present_mask


def _write_case(out_dir: Path, name: str, boxes: list[Box], present_mask: int) -> None:
    keep_seq = nms_sequential(boxes, present_mask)
    keep_all = nms_allpairs(boxes, present_mask)
    if keep_seq != keep_all:
        msg = (
            f"case {name!r}: nms_sequential (0x{keep_seq:08x}) != "
            f"nms_allpairs (0x{keep_all:08x})"
        )
        raise AssertionError(msg)

    hex_lines = [pack_record(box).hex() for box in boxes]
    hex_lines.append(f"{present_mask:08x}")
    (out_dir / f"{name}.hex").write_text("\n".join(hex_lines) + "\n")
    (out_dir / f"{name}.mask").write_text(f"{keep_all:08x}\n")
    print(f"PASS {name}: {keep_all.bit_count()} survivors, sequential == allpairs")


def generate(out_dir: Path) -> None:
    """Generate every mandatory A4 case into `out_dir`, asserting each is self-consistent."""
    cases = {
        "notebook31": case_notebook31(),
        "ties": case_ties(),
        "degenerate": case_degenerate(),
        "boundary": case_boundary(),
        "disjoint": case_disjoint(),
        "all_survive": case_all_survive(),
    }
    for seed in range(10):
        cases[f"rand_seed{seed}"] = case_random(seed)

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, (boxes, present_mask) in cases.items():
        _write_case(out_dir, name, boxes, present_mask)

    notebook_survivors = nms_allpairs(*case_notebook31()).bit_count()
    if notebook_survivors != 7:
        msg = f"notebook31 must reproduce 7 survivors, got {notebook_survivors}"
        raise AssertionError(msg)

    disjoint_boxes, disjoint_mask = case_disjoint()
    if nms_allpairs(disjoint_boxes, disjoint_mask) != disjoint_mask:
        msg = "disjoint case must survive every box"
        raise AssertionError(msg)

    survive_boxes, survive_mask = case_all_survive()
    if nms_allpairs(survive_boxes, survive_mask) != survive_mask:
        msg = "all_survive case must survive every box"
        raise AssertionError(msg)

    boundary_boxes, boundary_mask = case_boundary()
    boundary_keep = nms_allpairs(boundary_boxes, boundary_mask)
    if boundary_keep & 0b11 != 0b01:
        msg = f"boundary case must suppress slot 1 exactly, keep_mask=0x{boundary_keep:08x}"
        raise AssertionError(msg)

    print("ALL CASES CONSISTENT")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).parent / "data",
        help="Directory to write <case>.hex / <case>.mask into (default: models/data).",
    )
    args = parser.parse_args()
    generate(args.out_dir)


if __name__ == "__main__":
    main()
