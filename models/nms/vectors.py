"""Write and read the vector files the VHDL testbenches consume.

Five file types per case, each a plain table of fixed-width hex fields separated by
whitespace. Deliberately dull: ``readline`` plus successive ``hread`` calls is all a
testbench needs, with no comment stripping or tokenising. Human-readable notes go to a
separate ``.txt`` that nothing parses.

===============  ==============================================================
``<case>.hex``   32 records (16 hex) then one ``present_mask`` (8 hex)
``<case>.mask``  the expected ``keep_mask`` (8 hex)
``<case>.keys``  the 21-bit sort key per slot, in input order (6 hex)
``<case>.order`` the expected rank-to-slot table (2 hex per rank)
``<case>.trace`` per-rank resolve state: rank slot kept row valid keep
``<case>.pairs`` explicit IoU lane stimulus and expected result
===============  ==============================================================

One further file, ``cases.txt``, lists every case name one per line. The VHDL testbenches
read it and loop, so a case added here is covered by the RTL gates without anyone editing
a VHDL file -- which is the only way to stop the two lists drifting apart.

The expected values come from :mod:`models.nms.model`, so a testbench comparing against
them is comparing against the golden model rather than against a second implementation
written into the testbench. That distinction matters for ``.pairs`` in particular: were the
testbench to compute areas itself it would duplicate the clamp logic, and a shared mistake
would cancel out instead of failing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from models.nms import batches, model
from models.nms import params as p

DEFAULT_DIR = Path(__file__).resolve().parents[2] / "models" / "data" / "vectors"

MANIFEST = "cases.txt"
"""Lists every case name, one per line, for the VHDL testbenches to iterate."""

# Cases that also get an explicit .pairs file for the IoU lane testbench. All 18 cases
# would be ~900 kB of mostly redundant rows; these five carry every hazard between them and
# the Python property test covers the rest.
PAIR_CASES = ("notebook32", "degenerate", "boundary", "ties", "all_equal")

RECORD_HEX = p.RECORD_BITS // 4
MASK_HEX = p.N // 4
KEY_HEX = -(-p.KEY_W // 4)
SLOT_HEX = 2
COORD_HEX = -(-p.COORD_W // 4)
AREA_HEX = p.AREA_W // 4


@dataclass(frozen=True)
class Case:
    """One test case as written to disk.

    Attributes:
        name: Case name, used as the file stem.
        boxes: The batch, indexed by slot.
        present_mask: Which slots hold real detections.
        keep_mask: Expected result from the golden model.
    """

    name: str
    boxes: list[model.Box]
    present_mask: int
    keep_mask: int


def _hex(value: int, digits: int) -> str:
    """Format an integer as fixed-width uppercase hex.

    Args:
        value: Non-negative value.
        digits: Field width in hex digits.

    Returns:
        The zero-padded hex string.

    Raises:
        ValueError: If the value does not fit the field.
    """
    if not 0 <= value < (1 << (4 * digits)):
        msg = f"{value} does not fit {digits} hex digits"
        raise ValueError(msg)
    return f"{value:0{digits}X}"


def build_case(
    name: str, boxes: list[model.Box], present_mask: int | None = None
) -> Case:
    """Evaluate the golden model for a batch and bundle it as a case.

    Args:
        name: Case name.
        boxes: The batch.
        present_mask: Which slots are present; defaults to all.

    Returns:
        The case with its expected ``keep_mask`` filled in.
    """
    mask = (1 << p.N) - 1 if present_mask is None else present_mask
    return Case(
        name=name,
        boxes=list(boxes),
        present_mask=mask,
        keep_mask=model.nms_allpairs(boxes, mask),
    )


# --- writing -----------------------------------------------------------------------


def write_case(
    case: Case, outdir: Path = DEFAULT_DIR, *, pairs: bool = False
) -> list[Path]:
    """Write every file for one case.

    Args:
        case: The case to write.
        outdir: Destination directory, created if absent.
        pairs: Whether to also write the ``.pairs`` IoU stimulus.

    Returns:
        The paths written.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    written = []

    records = [_hex(model.pack_record(b), RECORD_HEX) for b in case.boxes]
    records.append(_hex(case.present_mask, MASK_HEX))
    written.append(_write(outdir / f"{case.name}.hex", records))

    written.append(
        _write(outdir / f"{case.name}.mask", [_hex(case.keep_mask, MASK_HEX)])
    )

    keys = [_hex(model.sort_key(b.score, i), KEY_HEX) for i, b in enumerate(case.boxes)]
    written.append(_write(outdir / f"{case.name}.keys", keys))

    order = [_hex(slot, SLOT_HEX) for slot in model.sort_order(case.boxes)]
    written.append(_write(outdir / f"{case.name}.order", order))

    _, steps = model.nms_allpairs(case.boxes, case.present_mask, trace=True)
    trace = [
        " ".join(
            (
                _hex(s.rank, SLOT_HEX),
                _hex(s.slot, SLOT_HEX),
                "1" if s.kept else "0",
                _hex(s.suppress_row, MASK_HEX),
                _hex(s.valid_mask, MASK_HEX),
                _hex(s.keep_mask, MASK_HEX),
            ),
        )
        for s in steps
    ]
    written.append(_write(outdir / f"{case.name}.trace", trace))

    if pairs:
        written.append(_write(outdir / f"{case.name}.pairs", _pair_rows(case.boxes)))

    written.append(_write(outdir / f"{case.name}.txt", _summary(case)))
    return written


def _pair_rows(boxes: list[model.Box]) -> list[str]:
    """Build explicit IoU lane stimulus for every ordered pair.

    Areas are supplied as inputs rather than recomputed by the testbench, because the lane
    receives them precomputed from ``box_store`` and because a testbench that derived them
    itself would duplicate the clamp logic it is meant to be checking.

    Args:
        boxes: The batch.

    Returns:
        One row per ordered pair: keeper coords and area, candidate coords and area, then
        the expected suppress bit.
    """
    rows = []
    areas = [model.box_area(b) for b in boxes]
    for i, keeper in enumerate(boxes):
        for j, candidate in enumerate(boxes):
            rows.append(
                " ".join(
                    (
                        _hex(keeper.x, COORD_HEX),
                        _hex(keeper.y, COORD_HEX),
                        _hex(keeper.a, COORD_HEX),
                        _hex(keeper.b, COORD_HEX),
                        _hex(areas[i], AREA_HEX),
                        _hex(candidate.x, COORD_HEX),
                        _hex(candidate.y, COORD_HEX),
                        _hex(candidate.a, COORD_HEX),
                        _hex(candidate.b, COORD_HEX),
                        _hex(areas[j], AREA_HEX),
                        "1" if model.suppresses(keeper, candidate) else "0",
                    ),
                ),
            )
    return rows


def _summary(case: Case) -> list[str]:
    """Build the human-readable companion file.

    Args:
        case: The case.

    Returns:
        Lines of prose and per-slot detail. Nothing parses this.
    """
    order = model.sort_order(case.boxes)
    kept = [i for i in range(p.N) if case.keep_mask >> i & 1]
    scores = [b.score for b in case.boxes]
    duplicate = len(set(scores)) != len(scores)

    boundary = 0
    for i in range(p.N):
        for j in range(i + 1, p.N):
            inter = model.intersection_area(case.boxes[i], case.boxes[j])
            union = (
                model.box_area(case.boxes[i]) + model.box_area(case.boxes[j]) - inter
            )
            if union and 2 * inter == union:
                boundary += 1

    lines = [
        f"case          {case.name}",
        f"present_mask  0x{case.present_mask:08X}",
        f"keep_mask     0x{case.keep_mask:08X}  ({len(kept)} survivors: {kept})",
        f"rank order    {order}",
        f"duplicate scores present  {duplicate}",
        f"pairs exactly on 2I == U  {boundary}",
        "",
        f"{'slot':>4} {'x':>5} {'y':>5} {'a':>5} {'b':>5} {'score':>6} {'area':>9} {'rank':>5} {'kept':>5}",
    ]
    rank_of = {slot: r for r, slot in enumerate(order)}
    for i, box in enumerate(case.boxes):
        lines.append(
            f"{i:>4} {box.x:>5} {box.y:>5} {box.a:>5} {box.b:>5} {box.score:>6} "
            f"{model.box_area(box):>9} {rank_of[i]:>5} {'yes' if i in kept else '':>5}",
        )
    return lines


def _write(path: Path, lines: list[str]) -> Path:
    """Write lines to a file with a trailing newline.

    Args:
        path: Destination.
        lines: Lines without terminators.

    Returns:
        The path written.
    """
    path.write_text("\n".join(lines) + "\n")
    return path


# --- reading back ------------------------------------------------------------------


def read_case(name: str, outdir: Path = DEFAULT_DIR) -> Case:
    """Read a case back from its files.

    Exists so the generator can be checked by round-trip rather than by inspection: if
    parsing what was written does not reproduce the batch, the format is wrong and the
    testbenches would have silently consumed nonsense.

    Args:
        name: Case name.
        outdir: Directory holding the files.

    Returns:
        The reconstructed case.

    Raises:
        ValueError: If the ``.hex`` file has the wrong number of lines.
    """
    hex_lines = (outdir / f"{name}.hex").read_text().split()
    if len(hex_lines) != p.N + 1:
        msg = f"{name}.hex has {len(hex_lines)} values, expected {p.N + 1}"
        raise ValueError(msg)
    boxes = [model.unpack_record(int(h, 16)) for h in hex_lines[: p.N]]
    present = int(hex_lines[p.N], 16)
    keep = int((outdir / f"{name}.mask").read_text().strip(), 16)
    return Case(name=name, boxes=boxes, present_mask=present, keep_mask=keep)


def read_order(name: str, outdir: Path = DEFAULT_DIR) -> list[int]:
    """Read the expected rank-to-slot table.

    Args:
        name: Case name.
        outdir: Directory holding the files.

    Returns:
        Slot index at each rank.
    """
    return [int(v, 16) for v in (outdir / f"{name}.order").read_text().split()]


def read_keys(name: str, outdir: Path = DEFAULT_DIR) -> list[int]:
    """Read the per-slot sort keys.

    Args:
        name: Case name.
        outdir: Directory holding the files.

    Returns:
        The 21-bit key for each slot, in input order.
    """
    return [int(v, 16) for v in (outdir / f"{name}.keys").read_text().split()]


def read_trace(name: str, outdir: Path = DEFAULT_DIR) -> list[model.ResolveStep]:
    """Read the per-rank resolve trace.

    Args:
        name: Case name.
        outdir: Directory holding the files.

    Returns:
        One :class:`~models.nms.model.ResolveStep` per rank.
    """
    steps = []
    for line in (outdir / f"{name}.trace").read_text().splitlines():
        rank, slot, kept, row, valid, keep = line.split()
        steps.append(
            model.ResolveStep(
                rank=int(rank, 16),
                slot=int(slot, 16),
                kept=kept == "1",
                suppress_row=int(row, 16),
                valid_mask=int(valid, 16),
                keep_mask=int(keep, 16),
            ),
        )
    return steps


# --- the whole set -----------------------------------------------------------------


def all_cases() -> dict[str, Case]:
    """Build every committed case, expected values included.

    Returns:
        Mapping of case name to case. Adds two ``present_mask`` variants that the
        generators in :mod:`models.nms.batches` do not cover, since an absent slot must
        never survive and that is worth exercising on real data.
    """
    cases = {
        name: build_case(name, boxes) for name, boxes in batches.named_cases().items()
    }
    anchor = list(batches.NOTEBOOK_32)
    cases["partial_present"] = build_case("partial_present", anchor, 0x0000FFFF)
    cases["none_present"] = build_case("none_present", anchor, 0x00000000)
    return cases


def write_all(outdir: Path = DEFAULT_DIR) -> dict[str, list[Path]]:
    """Write every case to disk, plus the manifest the testbenches iterate.

    Args:
        outdir: Destination directory.

    Returns:
        Mapping of case name to the paths written. The manifest is listed under the key
        ``"cases.txt"``.
    """
    cases = all_cases()
    written = {
        name: write_case(case, outdir, pairs=name in PAIR_CASES)
        for name, case in cases.items()
    }
    written[MANIFEST] = [_write(outdir / MANIFEST, sorted(cases))]
    return written


def read_manifest(outdir: Path = DEFAULT_DIR) -> list[str]:
    """Read the case names a VHDL testbench would iterate.

    Args:
        outdir: Directory holding the files.

    Returns:
        The case names, in the order the testbenches see them.
    """
    return (outdir / MANIFEST).read_text().split()
