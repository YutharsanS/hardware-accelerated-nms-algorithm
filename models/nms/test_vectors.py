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


def test_manifest_lists_exactly_the_cases(written: Path) -> None:
    # The VHDL testbenches loop over this file rather than a hard-coded list, so a case
    # added in Python is covered by the RTL gates automatically. If the manifest could
    # fall behind, that guarantee would be silently worthless.
    listed = vectors.read_manifest(written)
    assert listed == sorted(vectors.all_cases())
    assert len(listed) == len(set(listed)), "the manifest repeats a case"
    for name in listed:
        assert (written / f"{name}.keys").exists(), f"{name} is listed but has no files"


def test_random_pairs_round_trip_and_match_the_model(written: Path) -> None:
    rows = vectors.read_pairs(vectors.RANDOM_PAIRS, written)
    assert len(rows) == vectors.RANDOM_PAIR_COUNT
    for keeper, candidate, expected in rows:
        assert model.suppresses(keeper, candidate) == expected


def test_random_pairs_cover_every_hazard(written: Path) -> None:
    # The whole point of this file is the cases uniform random coordinates never produce.
    # Asserting the counts here means the VHDL testbench does not have to recompute I and
    # U to know its stimulus is interesting -- which it must not do, since that is the
    # datapath it is checking.
    rows = vectors.read_pairs(vectors.RANDOM_PAIRS, written)
    zero_area = 0
    inverted = 0
    boundary = 0
    identical = 0
    max_lhs = 0
    suppressed = 0
    for keeper, candidate, expected in rows:
        areas = (model.box_area(keeper), model.box_area(candidate))
        inter = model.intersection_area(keeper, candidate)
        union = areas[0] + areas[1] - inter
        if 0 in areas:
            zero_area += 1
        if any(box.a < box.x or box.b < box.y for box in (keeper, candidate)):
            inverted += 1
        if union and 2 * inter == union:
            boundary += 1
        if keeper[:4] == candidate[:4] and areas[0] > 0:
            identical += 1
        max_lhs = max(max_lhs, inter << p.K_SHIFT)
        suppressed += expected

    assert zero_area > 1_000, f"only {zero_area} pairs with a zero-area box"
    assert inverted > 1_000, f"only {inverted} pairs with an inverted box"
    assert boundary > 100, f"only {boundary} pairs exactly on 2I == U"
    assert identical > 1_000, f"only {identical} identical pairs (I == U)"
    # Near-maximal boxes are the only way to reach the wide end of the datapath: without
    # them the largest LHS is around 2**26 and LHS_W = 32 goes untested. Measured max LHS
    # with them is 4,267,735,040, which needs all 32 bits.
    assert max_lhs.bit_length() == p.LHS_W, (
        f"max LHS is {max_lhs} ({max_lhs.bit_length()} bits), so LHS_W = {p.LHS_W} "
        f"is not exercised -- the stimulus has no near-maximal boxes"
    )
    # Both verdicts must be well represented, or the file only tests one branch.
    assert 0.2 < suppressed / len(rows) < 0.8, (
        f"{suppressed}/{len(rows)} suppress -- the stimulus is one-sided"
    )


def test_pair_manifests_list_every_pairs_file(written: Path) -> None:
    # One manifest per threshold, and between them they must account for every .pairs file
    # on disk. A file nothing lists would be stimulus the RTL gate silently never reads.
    everything: set[str] = set()
    for threshold in vectors.PAIR_THRESHOLDS:
        listed = vectors.read_pair_manifest(written, threshold)
        assert vectors.pair_stem(threshold) in listed
        for name in listed:
            assert (written / f"{name}.pairs").exists(), f"{name} listed, no .pairs"
        everything |= set(listed)

    on_disk = {path.stem for path in written.glob("*.pairs")}
    assert on_disk == everything, (
        f"listed but absent: {sorted(everything - on_disk)}; "
        f"on disk but unlisted: {sorted(on_disk - everything)}"
    )

    # The batch cases belong to the shipped threshold only.
    assert set(vectors.read_pair_manifest(written, p.T_INT)) == {
        *vectors.PAIR_CASES,
        vectors.RANDOM_PAIRS,
    }


def test_the_two_thresholds_disagree_somewhere(written: Path) -> None:
    # If T_INT = 128 and T_INT = 255 produced identical verdicts on the same geometry, the
    # second testbench run would be pure cost. Measured: they differ on thousands of pairs.
    at_128 = vectors.read_pairs(vectors.pair_stem(p.T_INT), written)
    at_255 = vectors.read_pairs(vectors.pair_stem(2**p.T_INT_W - 1), written)
    assert len(at_128) == len(at_255)
    differing = 0
    for (k1, c1, e1), (k2, c2, e2) in zip(at_128, at_255, strict=True):
        assert (k1, c1) == (k2, c2), "the two files are not the same geometry"
        differing += e1 != e2
    assert differing > 1_000, f"only {differing} verdicts differ between the thresholds"


def test_committed_vectors_are_current(written: Path) -> None:
    # The files under models/data/vectors are committed so a clean checkout can run the
    # testbenches. If the generator changes without regenerating them, the RTL would be
    # verified against stale expectations.
    if not vectors.DEFAULT_DIR.exists():
        pytest.skip(
            "vectors not generated yet; run: uv run python -m models.nms vectors"
        )
    stale = "; run: uv run python -m models.nms vectors"
    for name in sorted(vectors.all_cases()):
        for suffix in ("hex", "mask", "keys", "order", "trace"):
            committed = (vectors.DEFAULT_DIR / f"{name}.{suffix}").read_text()
            fresh = (written / f"{name}.{suffix}").read_text()
            assert committed == fresh, f"{name}.{suffix} is stale{stale}"
    for extra in (
        vectors.MANIFEST,
        vectors.PAIR_MANIFEST,
        f"{vectors.RANDOM_PAIRS}.pairs",
        *(f"{name}.pairs" for name in vectors.PAIR_CASES),
    ):
        assert (vectors.DEFAULT_DIR / extra).read_text() == (
            written / extra
        ).read_text(), f"{extra} is stale{stale}"
