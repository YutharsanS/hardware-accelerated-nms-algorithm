"""Sole source of constants for the NMS accelerator's integer datapath.

Every width and field offset here mirrors `docs/plan.md` Part 2 (the frozen spec) and
Part 1 (the answered width questions). `src/components/nms_pkg.vhd` mirrors this file
on the VHDL side, and `models/test_params_agree.py` (Phase B) parses the VHDL and
asserts the two never drift apart.
"""

from __future__ import annotations

N = 32
"""Boxes per batch."""

COORD_W = 12
"""Width of each of x, y, a, b (Q7/Q12)."""

SCORE_W = 16
"""Width of the quantised confidence score (A1's 16-vs-8-bit resolution)."""

AREA_W = 24
"""Width of `area1`/`area2`/`I` (Q9)."""

UNION_W = 25
"""Width of `U = area1 + area2 - I` (Q9)."""

LHS_W = 32
"""Width of `LHS = I << K_SHIFT` (Q9)."""

RHS_W = 33
"""Width of `RHS = T_INT * U` (Q9)."""

INDEX_W = 5
"""Width of a box's arrival-order slot index, `ceil(log2(N))`."""

KEY_W = SCORE_W + INDEX_W
"""Width of the sort key `K = score * N + (N - 1 - index)` (Q18)."""

K_SHIFT = 8
"""Fixed-point shift applied to `I` before the threshold compare (Q11); not a stored field."""

T_INT = 128
"""Fixed IoU-threshold generic, `T_INT / 2**K_SHIFT = 0.5` (Q11)."""

RECORD_BYTES = 8
"""Wire size of one packed box record (Part 2)."""

# Field bit offsets within the 64-bit record, LSB-relative. Transmitted MSB-first.
SCORE_OFFSET = 0
B_OFFSET = 16
A_OFFSET = 28
Y_OFFSET = 40
X_OFFSET = 52
