# NMS primer — what the block computes, in plain terms

For a reader who wants to know what Non-Maximum Suppression is and exactly which version of it
this project implements, without reading the spec. The precise, normative definitions are in
[architecture.md](architecture.md); the reference implementation is
[`models/nms/model.py`](../../models/nms/model.py).

## What problem NMS solves

An object detector doesn't draw one box per object. It proposes many overlapping boxes for the
same object, each with a confidence score. **Non-Maximum Suppression** is the clean-up step: it
keeps the most confident box for each object and discards the overlapping duplicates.

## A box

Each detection is five numbers, packed into one 64-bit record:

```
(x, y, a, b, score)        x, y, a, b: 12 bits each     score: 16 bits
```

- `(x, y)` is the lower-left corner and `(a, b)` the upper-right.
- `score` is the detector's confidence in [0, 1], stored as `round(confidence × 65535)`.

A batch is **32 slots**. A `present_mask` says which slots hold real detections; an empty slot
is never kept.

## Step 1: how much do two boxes overlap?

The usual measure is **IoU**, intersection over union:

```
IoU = (area the two boxes share) / (area they cover together)
```

It runs from 0 (no overlap) to 1 (identical). A box is a duplicate of a better one when their
IoU reaches a threshold, here **0.5**.

**This project never divides.** The hardware checks the same condition in whole numbers:

```
suppress  when   I × 256  ≥  T_INT × U        with T_INT = 128, i.e. a threshold of 128/256 = 0.5
```

where `I` is the intersection area and `U` the union area. Multiplying across instead of
dividing costs no precision, needs no divider, and gives a defined answer even when both boxes
have zero area (`0 ≥ 0`: suppress). The Python model evaluates exactly the same expression, so
the hardware and the model agree bit for bit, not within a tolerance.

Two details make it exact:
- **`≥`, not `>`.** A pair exactly at IoU 0.5 is suppressed. Common libraries use `>`, and so
  sometimes keep a different set of boxes ([benchmarks.md](../results/benchmarks.md) §6.5).
- **Inverted boxes count as empty.** A box with `a < x` or `b < y` has area 0 rather than a
  negative or wrapped-around one.

## Step 2: the algorithm

1. **Rank the boxes by score**, highest first. Equal scores are broken by **lower slot number
   first**, so the order is always defined and the hardware's sorter can never disagree with the
   model about it.
2. **Walk down the ranking.** The first box still standing is kept.
3. **Suppress every lower-ranked box** that overlaps it by IoU ≥ 0.5.
4. Repeat with the next box still standing, until none are left.

The result is a **32-bit `keep_mask`**: bit *i* is 1 when slot *i* survived. The host never has
to reorder anything, and checking the hardware is a single 32-bit comparison.

## How the hardware does the same thing faster

The textbook loop above is sequential: each decision waits for the previous one. The block
restructures it without changing the answer:
- a **bitonic sorting network** ranks all 32 boxes at once;
- **16 IoU lanes** compute the suppression test for **every pair** in parallel, before any
  decision is made;
- a **resolve** pass then walks the ranking once, applying those precomputed results.

This works because a box that is suppressed can never come back, so applying a keeper's row of
results to boxes ranked above it changes nothing. The golden model implements NMS both ways —
`nms_sequential` (the textbook loop) and `nms_allpairs` (the hardware's structure) — and a test
checks they agree on 2,000 hostile batches (20,000 under `make test-full`).

Because the amount of work is the same for every batch, the block takes **exactly 113 cycles**
from the first box in to the result, whatever the boxes are ([architecture.md](architecture.md)
§9, [hardware.md](../results/hardware.md) §6).

## The anchor batch

[`models/golden-model.ipynb`](../../models/golden-model.ipynb) builds a 32-box test set that
looks like a detector's raw output: four clusters of overlapping boxes (a cat, a dog, a car, a
person, each detected several times) and a few isolated boxes. NMS reduces it to **7 survivors**,
at slots 0, 8, 16, 24, 29, 30 and 31:

```
keep_mask = 0xE1010101
```

That value is the regression anchor for the whole build: the golden model, every VHDL testbench
and the board all reproduce it. It is not a hard test, though — it has no tied scores and no pair
exactly on the threshold. Those, the subtlest parts of the design, are covered by the adversarial
cases in [`models/nms/batches.py`](../../models/nms/batches.py).
