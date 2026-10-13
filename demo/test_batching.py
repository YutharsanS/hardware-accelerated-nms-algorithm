"""Tests for the frame-to-batch step, on synthetic detector output (no Ultralytics)."""

from __future__ import annotations

import numpy as np
import pytest

from demo import batching
from models.nms import params as p


def detector_output(
    rng: np.random.Generator, anchors: int, classes: int
) -> tuple[np.ndarray, np.ndarray]:
    xy = rng.uniform(0, 600, (anchors, 2))
    wh = rng.uniform(5, 80, (anchors, 2))
    xyxy = np.concatenate([xy, xy + wh], 1)
    return xyxy, rng.uniform(0, 1, (anchors, classes))


def test_candidates_threshold_label_and_quantise() -> None:
    xyxy = np.array(
        [[10.4, 20.6, 50.0, 60.0], [0, 0, 9000, 9000], [5, 5, 5.2, 30], [1, 1, 9, 9]],
    )
    scores = np.array([[0.9, 0.1], [0.2, 0.8], [0.7, 0.0], [0.1, 0.2]])
    c = batching.candidates(xyxy, scores)
    # anchor 2 has no area once rounded; anchor 3 is below the threshold
    assert c.anchors.tolist() == [0, 1]
    assert c.corners.tolist() == [[10, 21, 50, 60], [0, 0, p.COORD_MAX, p.COORD_MAX]]
    assert c.scores.tolist() == [round(0.9 * p.SCORE_MAX), round(0.8 * p.SCORE_MAX)]
    assert c.classes.tolist() == [0, 1]


def test_one_batch_per_class_and_the_cap() -> None:
    rng = np.random.default_rng(1)
    xyxy, scores = detector_output(rng, 400, 3)
    cands = batching.candidates(xyxy, scores)
    batches = batching.make_batches(cands)

    assert [b.cls for b in batches] == sorted(set(cands.classes.tolist()))
    for b in batches:
        total = int((cands.classes == b.cls).sum())
        assert b.count == min(total, p.N)
        assert len(b.over_cap) == total - b.count
        assert len(b.boxes) == p.N
        assert b.present_mask == (1 << b.count) - 1
        assert all(box == batching.PAD_BOX for box in b.boxes[b.count :])
        assert set(b.anchors.tolist()).isdisjoint(b.over_cap.tolist())


def test_the_cap_keeps_the_highest_scores() -> None:
    rng = np.random.default_rng(2)
    xyxy, scores = detector_output(rng, 300, 1)
    cands = batching.candidates(xyxy, scores)
    (b,) = batching.make_batches(cands)
    sent = {box.score for box in b.boxes[: b.count]}
    left = cands.scores[np.isin(cands.anchors, b.over_cap)]
    assert min(sent) >= left.max()


def test_slots_are_in_anchor_order_not_score_order() -> None:
    rng = np.random.default_rng(3)
    xyxy, scores = detector_output(rng, 300, 1)
    (b,) = batching.make_batches(batching.candidates(xyxy, scores))
    assert np.all(np.diff(b.anchors) > 0)
    slot_scores = [box.score for box in b.boxes[: b.count]]
    assert slot_scores != sorted(slot_scores, reverse=True)


@pytest.mark.parametrize("tied_at_cut", [True, False])
def test_ties_at_the_cut_go_to_the_lower_anchor(tied_at_cut: bool) -> None:
    n = 4
    scores = np.array([9, 5, 5, 5, 5, 1] if tied_at_cut else [9, 8, 7, 6, 5, 1])
    anchors = np.arange(len(scores)) * 10
    chosen = batching.select_top(scores, anchors, n)
    assert np.flatnonzero(chosen).tolist() == [0, 1, 2, 3]


def test_no_candidates_no_batches() -> None:
    xyxy = np.zeros((10, 4))
    cands = batching.candidates(xyxy, np.zeros((10, 80)))
    assert batching.make_batches(cands) == []
