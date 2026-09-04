"""B1.3 gate: vector files round-trip, and every case tests what it claims to."""

from __future__ import annotations

from pathlib import Path

import pytest

from models.nms import batches, model, vectors
from models.nms import params as p


@pytest.fixture(scope="module")
def written(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Write every case to a temporary directory once for the whole module."""
    outdir = tmp_path_factory.mktemp("vectors")
    vectors.write_all(outdir)
    return outdir


def _suppress_pairs(boxes: list[model.Box]) -> int:
    """Count ordered pairs where one box suppresses another."""
    return sum(
        1
        for i in range(len(boxes))
        for j in range(len(boxes))
        if i != j and model.suppresses(boxes[i], boxes[j])
    )


def _boundary_pairs(boxes: list[model.Box]) -> int:
    """Count unordered pairs sitting exactly on ``2I == U``."""
    total = 0
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            inter = model.intersection_area(boxes[i], boxes[j])
            union = model.box_area(boxes[i]) + model.box_area(boxes[j]) - inter
            if union and 2 * inter == union:
                total += 1
    return total


def _intersecting_pairs(boxes: list[model.Box]) -> int:
    """Count unordered pairs with a non-zero intersection."""
    return sum(
        1
        for i in range(len(boxes))
        for j in range(i + 1, len(boxes))
        if model.intersection_area(boxes[i], boxes[j]) > 0
    )


# --- round trip, the B1.3 gate -----------------------------------------------------


@pytest.mark.parametrize("name", sorted(vectors.all_cases()))
def test_case_round_trips(name: str, written: Path) -> None:
    expected = vectors.all_cases()[name]
    got = vectors.read_case(name, written)
    assert got.boxes == expected.boxes, "records did not survive the round trip"
    assert got.present_mask == expected.present_mask
    assert got.keep_mask == expected.keep_mask


@pytest.mark.parametrize("name", sorted(vectors.all_cases()))
def test_mask_file_matches_the_model(name: str, written: Path) -> None:
    case = vectors.read_case(name, written)
    assert case.keep_mask == model.nms_allpairs(case.boxes, case.present_mask)
    # and the sequential form agrees, so the file is not blessed by one implementation
    assert case.keep_mask == model.nms_sequential(case.boxes, case.present_mask)


@pytest.mark.parametrize("name", sorted(vectors.all_cases()))
def test_keys_and_order_files_match_the_model(name: str, written: Path) -> None:
    case = vectors.read_case(name, written)
    keys = vectors.read_keys(name, written)
    order = vectors.read_order(name, written)
    assert keys == [model.sort_key(b.score, i) for i, b in enumerate(case.boxes)]
    assert order == model.sort_order(case.boxes)
    # the order must be a permutation of every slot exactly once
    assert sorted(order) == list(range(p.N))


@pytest.mark.parametrize("name", sorted(vectors.all_cases()))
def test_trace_file_matches_the_model(name: str, written: Path) -> None:
    case = vectors.read_case(name, written)
    _, expected = model.nms_allpairs(case.boxes, case.present_mask, trace=True)
    assert vectors.read_trace(name, written) == expected


@pytest.mark.parametrize("name", sorted(vectors.PAIR_CASES))
def test_pairs_file_matches_the_model(name: str, written: Path) -> None:
    case = vectors.read_case(name, written)
    rows = (written / f"{name}.pairs").read_text().splitlines()
    assert len(rows) == p.N * p.N
    for row, (i, j) in zip(
        rows,
        [(i, j) for i in range(p.N) for j in range(p.N)],
        strict=True,
    ):
        fields = row.split()
        assert len(fields) == 11
        keeper = model.Box(*[int(f, 16) for f in fields[0:4]], case.boxes[i].score)
        candidate = model.Box(*[int(f, 16) for f in fields[5:9]], case.boxes[j].score)
        assert keeper[:4] == case.boxes[i][:4]
        assert candidate[:4] == case.boxes[j][:4]
        assert int(fields[4], 16) == model.box_area(case.boxes[i])
        assert int(fields[9], 16) == model.box_area(case.boxes[j])
        assert (fields[10] == "1") == model.suppresses(case.boxes[i], case.boxes[j])


def test_every_field_is_fixed_width_hex(written: Path) -> None:
    # A ragged file would make the VHDL reader's hread calls drift silently.
    for name in vectors.all_cases():
        hex_lines = (written / f"{name}.hex").read_text().splitlines()
        assert len(hex_lines) == p.N + 1
        assert all(len(line) == vectors.RECORD_HEX for line in hex_lines[: p.N])
        assert len(hex_lines[p.N]) == vectors.MASK_HEX
        assert all(
            len(line) == vectors.KEY_HEX
            for line in (written / f"{name}.keys").read_text().splitlines()
        )
        assert all(
            len(fields) == 6
            for fields in (
                line.split()
                for line in (written / f"{name}.trace").read_text().splitlines()
            )
        )


# --- the cases must test what they claim -------------------------------------------
#
# This is the guard that caught two defects: low_res_scores triggered zero suppressions,
# so its ties were never decisive, and all_survive was byte-identical to disjoint.


def test_tie_cases_actually_contain_ties_and_suppress() -> None:
    for name in ("ties", "all_equal", "low_res_scores"):
        boxes = batches.named_cases()[name]
        scores = [b.score for b in boxes]
        duplicates = len(scores) - len(set(scores))
        assert duplicates > 0, f"{name} has no duplicate scores"
        assert _suppress_pairs(boxes) > 0, (
            f"{name} triggers no suppression, so its ties never decide anything"
        )


def test_boundary_case_contains_exact_boundary_pairs() -> None:
    boxes = batches.named_cases()["boundary"]
    assert _boundary_pairs(boxes) > 0, "boundary case has no pair with 2I == U"
    assert _suppress_pairs(boxes) > 0


def test_degenerate_case_contains_degenerate_boxes() -> None:
    boxes = batches.named_cases()["degenerate"]
    zero_area = sum(1 for b in boxes if model.box_area(b) == 0)
    inverted = sum(1 for b in boxes if b.a < b.x or b.b < b.y)
    assert zero_area >= 5, f"only {zero_area} zero-area boxes"
    assert inverted >= 3, f"only {inverted} inverted boxes"


def test_disjoint_and_all_survive_are_different_paths() -> None:
    disjoint = batches.named_cases()["disjoint"]
    all_survive = batches.named_cases()["all_survive"]
    assert disjoint != all_survive, "all_survive is a duplicate of disjoint"

    # disjoint: nothing even intersects
    assert _intersecting_pairs(disjoint) == 0
    assert _suppress_pairs(disjoint) == 0
    assert model.nms_allpairs(disjoint) == (1 << p.N) - 1

    # all_survive: plenty intersect, but no row ever fires
    assert _intersecting_pairs(all_survive) > 0, "all_survive has no overlap to compute"
    assert _suppress_pairs(all_survive) == 0, "all_survive suppresses something"
    assert model.nms_allpairs(all_survive) == (1 << p.N) - 1


def test_present_mask_cases_exercise_absent_slots() -> None:
    cases = vectors.all_cases()
    partial = cases["partial_present"]
    assert partial.present_mask == 0x0000FFFF
    assert partial.keep_mask & ~partial.present_mask == 0
    assert partial.keep_mask.bit_count() > 0, "partial case keeps nothing to compare"
    # the same boxes with everything present must keep strictly more
    full = cases["notebook32"]
    assert full.keep_mask.bit_count() > partial.keep_mask.bit_count()

    assert cases["none_present"].keep_mask == 0


def test_committed_vectors_are_current(written: Path) -> None:
    # The files under models/data/vectors are committed so a clean checkout can run the
    # testbenches. If the generator changes without regenerating them, the RTL would be
    # verified against stale expectations.
    if not vectors.DEFAULT_DIR.exists():
        pytest.skip(
            "vectors not generated yet; run: uv run python -m models.nms vectors"
        )
    for name in sorted(vectors.all_cases()):
        for suffix in ("hex", "mask", "keys", "order", "trace"):
            committed = (vectors.DEFAULT_DIR / f"{name}.{suffix}").read_text()
            fresh = (written / f"{name}.{suffix}").read_text()
            assert committed == fresh, (
                f"{name}.{suffix} is stale; run: uv run python -m models.nms vectors"
            )
