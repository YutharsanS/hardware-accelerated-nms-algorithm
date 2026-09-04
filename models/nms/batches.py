"""Adversarial batches for verifying the NMS model and the RTL against it.

The notebook's own test set is necessary but far from sufficient: it contains **no
duplicate scores and no pairs on the IoU threshold**, so it exercises neither tie-breaking
nor the boundary predicate -- the two subtlest parts of the design. Every generator below
targets something the anchor cannot reach.

Shared by the model's property tests and by the vector generator that writes the files the
VHDL testbenches read, so both exercise identical data.
"""

from __future__ import annotations

import random

from models.nms import params as p
from models.nms.model import Box

# The notebook's 32-box set, coordinates verbatim and confidences quantised with
# quantise_score. `test_model.py` re-executes the notebook and asserts this matches, so the
# constant stays honest without the tests depending on notebook execution.
NOTEBOOK_32: tuple[Box, ...] = (
    Box(10, 10, 50, 50, 62258),
    Box(12, 11, 52, 51, 58982),
    Box(9, 13, 48, 53, 55705),
    Box(14, 9, 54, 49, 45874),
    Box(11, 14, 51, 54, 42598),
    Box(15, 12, 55, 52, 36044),
    Box(8, 8, 46, 46, 26214),
    Box(13, 15, 53, 55, 19660),
    Box(100, 100, 150, 150, 60292),
    Box(102, 101, 152, 151, 57671),
    Box(98, 103, 148, 153, 52428),
    Box(104, 99, 154, 149, 47185),
    Box(101, 105, 151, 155, 39321),
    Box(103, 98, 153, 148, 32768),
    Box(97, 102, 147, 152, 27525),
    Box(105, 104, 155, 154, 18350),
    Box(200, 50, 260, 100, 60948),
    Box(202, 52, 262, 102, 57015),
    Box(198, 48, 258, 98, 51117),
    Box(204, 53, 264, 103, 44564),
    Box(201, 47, 261, 97, 38010),
    Box(199, 55, 259, 105, 31457),
    Box(203, 49, 263, 99, 24903),
    Box(197, 51, 257, 101, 16384),
    Box(50, 200, 100, 280, 59637),
    Box(52, 202, 102, 282, 53739),
    Box(48, 198, 98, 278, 47841),
    Box(54, 203, 104, 283, 40632),
    Box(47, 199, 97, 279, 29491),
    Box(300, 300, 340, 340, 49151),
    Box(0, 280, 30, 310, 22937),
    Box(280, 0, 320, 40, 13107),
)

NOTEBOOK_KEEP_MASK = 0xE1010101
"""The regression anchor: survivors at slots 0, 8, 16, 24, 29, 30, 31."""


def boundary_pair(d: int, height: int, offset: int = 0) -> tuple[Box, Box]:
    """Build a pair of boxes sitting exactly on, or just off, the IoU threshold.

    For two equal boxes offset horizontally by ``d`` with width ``W``::

        I = (W - d)·H        U = H·(W + d)

    so ``2I == U`` exactly when ``W == 3d``. Setting ``W = 3d + offset`` therefore lands
    just above the threshold for ``offset > 0`` and just below for ``offset < 0``, which is
    the only way to test a `>=` boundary without relying on floating point rounding.

    Args:
        d: Horizontal offset between the two boxes.
        height: Shared box height.
        offset: Width adjustment; 0 gives an exact ``2I == U``.

    Returns:
        The two boxes.
    """
    width = 3 * d + offset
    return (
        Box(0, 0, width, height, p.SCORE_MAX),
        Box(d, 0, d + width, height, p.SCORE_MAX // 2),
    )


def _pad(boxes: list[Box], n: int = p.N) -> list[Box]:
    """Pad a list to ``n`` boxes with far-apart non-overlapping filler.

    Filler is placed on a coarse grid well away from the interesting boxes so it neither
    suppresses nor is suppressed, keeping each case's intent isolated.

    Args:
        boxes: Boxes to keep.
        n: Target length.

    Returns:
        A list of exactly ``n`` boxes.
    """
    out = list(boxes[:n])
    while len(out) < n:
        i = len(out)
        x = 1000 + (i % 8) * 300
        y = 1000 + (i // 8) * 300
        out.append(Box(x, y, x + 20, y + 20, 1 + i))
    return out


def case_ties(rng: random.Random) -> list[Box]:
    """Boxes with heavily duplicated scores, in overlapping clusters.

    The sort key makes ties impossible by construction; this is what proves it. With equal
    scores the winner must be decided by slot index, so a network that is merely
    *unstable* rather than strictly ordered gives a different survivor set.
    """
    scores = [1000, 1000, 1000, 2000, 2000, 500, 500, 500]
    boxes = []
    for cluster in range(4):
        cx, cy = 100 + cluster * 400, 100 + cluster * 200
        for k in range(8):
            jitter = rng.randrange(0, 6)
            boxes.append(
                Box(
                    cx + jitter,
                    cy + jitter,
                    cx + 60 + jitter,
                    cy + 60 + jitter,
                    scores[k],
                ),
            )
    return boxes[: p.N]


def case_all_equal() -> list[Box]:
    """Every box identical in score and heavily overlapping.

    The most hostile input for tie-breaking: the outcome is decided purely by slot order.
    """
    return [Box(100 + i, 100 + i, 200 + i, 200 + i, 30000) for i in range(p.N)]


def case_degenerate() -> list[Box]:
    """Zero-area, inverted and single-pixel boxes.

    Exercises the clamps. An inverted box would give a negative area in Python and a huge
    unsigned value in VHDL, so without clamping the two would disagree on identical input.
    """
    boxes = [
        Box(100, 100, 100, 100, 60000),  # zero area, both dimensions
        Box(200, 200, 200, 260, 59000),  # zero width
        Box(300, 300, 360, 300, 58000),  # zero height
        Box(400, 400, 350, 350, 57000),  # fully inverted, a < x and b < y
        Box(500, 500, 450, 560, 56000),  # inverted in x only
        Box(600, 600, 660, 550, 55000),  # inverted in y only
        Box(700, 700, 701, 701, 54000),  # single pixel
        Box(100, 100, 160, 160, 53000),  # normal box overlapping the first
        Box(100, 100, 100, 100, 52000),  # duplicate zero-area, same place
    ]
    return _pad(boxes)


def case_boundary() -> list[Box]:
    """Pairs exactly on and immediately either side of the ``2I == U`` threshold."""
    boxes: list[Box] = []
    for i, offset in enumerate((0, 0, 1, -1)):
        first, second = boundary_pair(d=10 + i, height=30)
        shift = i * 500
        boxes.append(
            Box(first.x + shift, first.y, first.a + shift, first.b, first.score - i)
        )
        width = 3 * (10 + i) + offset
        d = 10 + i
        boxes.append(Box(d + shift, 0, d + width + shift, 30, second.score - i))
    return _pad(boxes)


def case_disjoint() -> list[Box]:
    """Boxes that never overlap, so every one survives and no row ever fires."""
    return [
        Box(i * 120, i * 120, i * 120 + 50, i * 120 + 50, p.SCORE_MAX - i)
        for i in range(p.N)
    ]


def case_all_survive() -> list[Box]:
    """Boxes that overlap but stay *below* the threshold, so every one survives.

    Distinct from :func:`case_disjoint`, which has no overlap at all. Here the lanes
    compute a non-zero intersection for many pairs and every suppression row still comes
    back empty, exercising the "row computed, nothing fires" path rather than the trivial
    "no overlap" one. Spacing 70 against size 90 gives IoU 0.125, comfortably under 0.5.
    """
    boxes = []
    for i in range(p.N):
        cx, cy = 50 + (i % 6) * 70, 50 + (i // 6) * 70
        boxes.append(Box(cx, cy, cx + 90, cy + 90, p.SCORE_MAX - i))
    return boxes


def case_low_res_scores(rng: random.Random) -> list[Box]:
    """Tightly overlapping boxes with only 8 bits of score resolution.

    At 8-bit resolution, 32 draws from 256 values collide with probability about 86%, so
    ties are the common case rather than an edge case. This is what a real detector with a
    coarse confidence output would produce.

    The spacing must be tight enough that suppression actually happens: at spacing 70 with
    size 90 the IoU is 0.125 and nothing fires, so the case would exercise no tie-breaking
    at all despite being full of ties. Spacing 25 gives adjacent IoU well above 0.5 while
    boxes two apart stay below it, so the outcome genuinely depends on tie order.
    """
    boxes = []
    for i in range(p.N):
        cx, cy = 50 + (i % 6) * 25, 50 + (i // 6) * 25
        coarse = rng.randrange(0, 256) * 257  # 8 significant bits, spread over 16
        boxes.append(Box(cx, cy, cx + 90, cy + 90, coarse))
    return boxes


def case_random(rng: random.Random, *, overlap: bool = True) -> list[Box]:
    """A random batch, optionally clustered so suppression actually happens.

    Args:
        rng: Seeded generator.
        overlap: When true, cluster the boxes so many pairs overlap; when false, scatter
            them across the whole coordinate range.

    Returns:
        A batch of ``N`` boxes.
    """
    boxes = []
    for _ in range(p.N):
        if overlap:
            x = rng.randrange(0, 400)
            y = rng.randrange(0, 400)
            w = rng.randrange(40, 200)
            h = rng.randrange(40, 200)
        else:
            x = rng.randrange(0, p.COORD_MAX - 100)
            y = rng.randrange(0, p.COORD_MAX - 100)
            w = rng.randrange(1, 100)
            h = rng.randrange(1, 100)
        boxes.append(
            Box(
                x,
                y,
                min(x + w, p.COORD_MAX),
                min(y + h, p.COORD_MAX),
                rng.randrange(0, p.SCORE_MAX + 1),
            ),
        )
    return boxes


def case_random_hostile(rng: random.Random) -> list[Box]:
    """A random batch mixing every hazard: ties, degenerate boxes and heavy overlap."""
    boxes = []
    for _ in range(p.N):
        mode = rng.randrange(0, 10)
        x = rng.randrange(0, 300)
        y = rng.randrange(0, 300)
        if mode == 0:  # degenerate
            a, b = x, y
        elif mode == 1:  # inverted
            a, b = max(0, x - rng.randrange(1, 50)), max(0, y - rng.randrange(1, 50))
        else:
            a = min(x + rng.randrange(1, 150), p.COORD_MAX)
            b = min(y + rng.randrange(1, 150), p.COORD_MAX)
        score = rng.choice(
            [0, 1, 30000, 30000, 30000, p.SCORE_MAX, rng.randrange(0, 256) * 257]
        )
        boxes.append(Box(x, y, a, b, score))
    return boxes


def named_cases() -> dict[str, list[Box]]:
    """Return the deterministic, file-backed cases keyed by name.

    Returns:
        Mapping of case name to batch. Every batch has exactly ``N`` boxes.
    """
    rng = random.Random(0)
    cases = {
        "notebook32": list(NOTEBOOK_32),
        "ties": case_ties(random.Random(1)),
        "all_equal": case_all_equal(),
        "degenerate": case_degenerate(),
        "boundary": case_boundary(),
        "disjoint": case_disjoint(),
        "all_survive": case_all_survive(),
        "low_res_scores": case_low_res_scores(random.Random(2)),
    }
    for seed in range(10):
        cases[f"rand_seed{seed}"] = case_random(random.Random(100 + seed))
        rng.random()
    for name, boxes in cases.items():
        if len(boxes) != p.N:
            msg = f"case {name!r} has {len(boxes)} boxes, expected {p.N}"
            raise ValueError(msg)
    return cases


def hostile_stream(count: int, *, seed: int = 0) -> list[list[Box]]:
    """Return many hostile random batches, for the model-versus-model agreement sweep.

    Args:
        count: How many batches to generate.
        seed: Base seed.

    Returns:
        A list of batches.
    """
    rng = random.Random(seed)
    generators = (
        lambda: case_random(rng),
        lambda: case_random(rng, overlap=False),
        lambda: case_random_hostile(rng),
        lambda: case_low_res_scores(rng),
        case_all_equal,
    )
    return [generators[i % len(generators)]() for i in range(count)]
