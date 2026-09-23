"""Frozen constants for the NMS accelerator.

Mirrors ``docs/architecture.md``, which is normative. ``src/components/nms_pkg.vhd`` mirrors
the same numbers on the VHDL side and ``test_params_agree.py`` fails the build if the three
ever diverge.

Every width here is *derived* rather than asserted where that is possible, so a change to
``COORD_W`` or ``SCORE_W`` propagates instead of silently contradicting the rest. The
:func:`validate` function then re-checks the derivations against the values the document
states, which is what catches a typo in either place.
"""

from __future__ import annotations

# --- batch -------------------------------------------------------------------------

N = 32
"""Boxes per batch. Fixed: the combinational sorter is Theta(N log^2 N) and N=64 does not
fit the XC7A35T (672 CAS ~ 20,160 LUT)."""

INDEX_W = 5
"""Bits to address a slot, ``log2(N)``."""

# --- record ------------------------------------------------------------------------

COORD_W = 12
"""Bits per coordinate. 11 is the minimum for 1080p; 12 rounds an (x, y) pair to 24 bits so
a whole record is byte-aligned at 64 bits."""

SCORE_W = 16
"""Bits of detector confidence. Normative -- the 8-bit figure in older revisions of the
architecture document referred to ``T_INT``, not to the score."""

RECORD_BITS = 4 * COORD_W + SCORE_W
RECORD_BYTES = RECORD_BITS // 8

# LSB position of each field within the 64-bit record, MSB-first as x, y, a, b, score.
SCORE_SHIFT = 0
B_SHIFT = SCORE_SHIFT + SCORE_W
A_SHIFT = B_SHIFT + COORD_W
Y_SHIFT = A_SHIFT + COORD_W
X_SHIFT = Y_SHIFT + COORD_W

COORD_MAX = (1 << COORD_W) - 1
SCORE_MAX = (1 << SCORE_W) - 1

# --- datapath ----------------------------------------------------------------------

AREA_W = 24
"""``COORD_MAX**2`` needs 24 bits."""

T_INTERMEDIATE_W = 13
"""Signed width of ``t_w``/``t_h`` before clamping, in ``[-COORD_MAX, COORD_MAX]``."""

UNION_W = 25
"""``2 * COORD_MAX**2`` needs 25 bits."""

K_SHIFT = 8
"""Q0.8 fixed point. Not a stored signal -- it is the shift in ``I << K_SHIFT``."""

T_INT_W = 8
T_INT = 128
"""Threshold *value* in Q0.8: ``128 / 2**8 = 0.5``. Distinct from the field range, which is
0..255 and covers thresholds up to 255/256."""

LHS_W = AREA_W + K_SHIFT
RHS_W = T_INT_W + UNION_W
COMPARE_W = max(LHS_W, RHS_W)

KEY_W = SCORE_W + INDEX_W
"""Sort key ``score & not(index)``, giving a strict total order so ties are impossible."""

# --- architecture ------------------------------------------------------------------

P_DEFAULT = 16
"""IoU lanes. Must divide N so each lane owns whole columns."""

LANE_LATENCY = 4
"""Registered stages in ``iou_lane``: min/max+clamp, multiply, union+RHS, compare."""

SORT_SUBSTAGES = INDEX_W * (INDEX_W + 1) // 2
"""Sub-stages in a Batcher bitonic network of N elements: ``k(k+1)/2`` for ``k = log2(N)``.
15 at N=32, which is also the number of CAS levels on the unpipelined critical path."""

CAS_COUNT = SORT_SUBSTAGES * (N // 2)
"""Compare-and-swap units in the whole network: 240. At ``8 + 2*ceil(KEY_W/2)`` = 30 LUT
each that is the 7,200 LUT (34.6%) in the area budget."""

PIPE_CUTS = 2
"""Register cuts inside the bitonic network, giving a 3-cycle sort. 0 is combinational and
will not close 100 MHz; 14 is fully pipelined and costs 10k FF for throughput nothing can
consume. Bounded above by SORT_SUBSTAGES -- one cut per sub-stage is as fine as it gets."""

CLOCK_HZ = 100_000_000

# --- wire protocol -----------------------------------------------------------------

MAGIC = (0xA5, 0x5A)
STATUS_OK = 0x00
STATUS_CRC_FAIL = 0x01
STATUS_BUSY = 0x02
STATUS_INTERNAL = 0x03

FRAME_BYTES_IN = len(MAGIC) + N * RECORD_BYTES + 4 + 1 + 1
"""magic + 32 records + present_mask + seq + crc8."""

REPLY_BYTES = 1 + 1 + 4
"""status + seq + keep_mask."""

BAUD = 1_000_000
"""Divider from CLOCK_HZ is exactly 100, so zero baud error."""


def latency_cycles(p: int = P_DEFAULT, *, n: int = N) -> int:
    """Return the exact batch latency in clock cycles.

    There is no data-dependent term, so this is an equality rather than a bound: worst
    case equals best case for every possible input.

    Args:
        p: Number of IoU lanes.
        n: Boxes per batch.

    Returns:
        Cycles from ``SORT`` to ``DONE``.
    """
    return n * n // p + LANE_LATENCY + PIPE_CUTS + 2


def quantise_score(confidence: float) -> int:
    """Map a detector confidence in [0, 1] to the stored integer score.

    Args:
        confidence: Confidence in ``[0, 1]``.

    Returns:
        The 16-bit integer score.
    """
    return round(max(0.0, min(1.0, confidence)) * SCORE_MAX)


def validate() -> list[str]:
    """Check the constants against the derivations in ``docs/architecture.md``.

    Returns:
        A list of human-readable failures; empty when everything agrees.
    """
    problems: list[str] = []

    def want(label: str, got: object, expected: object) -> None:
        if got != expected:
            problems.append(f"{label}: got {got!r}, expected {expected!r}")

    area_max = COORD_MAX * COORD_MAX
    union_max = 2 * area_max

    want("N is a power of two", N & (N - 1), 0)
    want("INDEX_W addresses N slots", 1 << INDEX_W, N)
    want("RECORD_BITS", RECORD_BITS, 64)
    want("RECORD_BYTES", RECORD_BYTES, 8)
    want("record has no spare bits", 4 * COORD_W + SCORE_W, RECORD_BITS)
    want("X_SHIFT places x at the MSB", X_SHIFT + COORD_W, RECORD_BITS)

    want("COORD_MAX", COORD_MAX, 4095)
    want("max area value", area_max, 16_769_025)
    want("AREA_W holds max area", area_max.bit_length(), AREA_W)
    want("max union value", union_max, 33_538_050)
    want("UNION_W holds max union", union_max.bit_length(), UNION_W)
    want("LHS_W holds area<<K_SHIFT", (area_max << K_SHIFT).bit_length(), LHS_W)
    want("max LHS value", area_max << K_SHIFT, 4_292_870_400)
    want(
        "RHS_W holds T_INT_max*union",
        (((1 << T_INT_W) - 1) * union_max).bit_length(),
        RHS_W,
    )
    want("max RHS value", ((1 << T_INT_W) - 1) * union_max, 8_552_202_750)
    want("T_INT is representable", T_INT <= (1 << T_INT_W) - 1, True)
    want("T_INT encodes IoU 0.5", T_INT, 1 << (K_SHIFT - 1))

    want("KEY_W holds score*N + index", (SCORE_MAX * N + N - 1).bit_length(), KEY_W)
    want(
        "t_w intermediate is signed-wide enough",
        COORD_MAX.bit_length() + 1,
        T_INTERMEDIATE_W,
    )

    want("SORT_SUBSTAGES", SORT_SUBSTAGES, 15)
    want("CAS_COUNT", CAS_COUNT, 240)
    want("PIPE_CUTS fits the network", PIPE_CUTS <= SORT_SUBSTAGES, True)
    want("P_DEFAULT divides N", N % P_DEFAULT, 0)
    want("latency at P_DEFAULT", latency_cycles(), 72)
    want("FRAME_BYTES_IN", FRAME_BYTES_IN, 264)
    want("REPLY_BYTES", REPLY_BYTES, 6)
    want("baud divider is exact", CLOCK_HZ % BAUD, 0)

    want("quantise_score(1.0)", quantise_score(1.0), SCORE_MAX)
    want("quantise_score(0.0)", quantise_score(0.0), 0)

    return problems


def summary() -> str:
    """Render the frozen constants as a table.

    Returns:
        A multi-line report, matching the tables in ``docs/architecture.md``.
    """
    rows = [
        ("N (boxes per batch)", N),
        ("COORD_W / SCORE_W / INDEX_W", f"{COORD_W} / {SCORE_W} / {INDEX_W}"),
        ("RECORD_BITS / BYTES", f"{RECORD_BITS} / {RECORD_BYTES}"),
        (
            "field shifts x,y,a,b,score",
            f"{X_SHIFT},{Y_SHIFT},{A_SHIFT},{B_SHIFT},{SCORE_SHIFT}",
        ),
        ("AREA_W / UNION_W", f"{AREA_W} / {UNION_W}"),
        ("LHS_W / RHS_W / COMPARE_W", f"{LHS_W} / {RHS_W} / {COMPARE_W}"),
        (
            "K_SHIFT / T_INT (= IoU)",
            f"{K_SHIFT} / {T_INT} (= {T_INT / (1 << K_SHIFT)})",
        ),
        ("KEY_W", KEY_W),
        (
            "P_DEFAULT / LANE_LATENCY / PIPE_CUTS",
            f"{P_DEFAULT} / {LANE_LATENCY} / {PIPE_CUTS}",
        ),
        (
            "latency at P_DEFAULT",
            f"{latency_cycles()} cycles = {latency_cycles() / CLOCK_HZ * 1e6:.2f} us",
        ),
        ("frame in / reply", f"{FRAME_BYTES_IN} B / {REPLY_BYTES} B"),
        ("baud (divider)", f"{BAUD:,} ({CLOCK_HZ // BAUD})"),
    ]
    width = max(len(label) for label, _ in rows)
    return "\n".join(f"  {label:<{width}} = {value}" for label, value in rows)
