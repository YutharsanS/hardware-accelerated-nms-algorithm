# NMS Golden Model

This document explains, in plain terms, what the golden model notebook (`models/golden-model.ipynb`) does and how it's put together. It's meant for a reviewer who wants to understand the notebook's logic without necessarily running it.

## What problem is this solving?

An object detector (e.g. something that finds cats, dogs, cars in an image) usually doesn't just draw one box around an object — it proposes many overlapping boxes for the same object, each with its own confidence score. **Non-Maximum Suppression (NMS)** is the clean-up step that takes all those overlapping guesses and keeps only the best one per object, discarding the redundant ones.

The "golden model" is a plain Python reference implementation of this algorithm. It doesn't run on the FPGA — its job is to produce trusted, known-correct outputs that the VHDL hardware implementation can later be checked against.

## What a box looks like

Every detection is represented as a tuple of 5 numbers:

```
(x, y, a, b, confidence)
```

- `(x, y)` — the lower-left corner of the box
- `(a, b)` — the upper-right corner of the box
- `confidence` — how sure the detector is that this box contains an object (0 to 1)

## Step 1: Measuring overlap with IoU

To decide whether two boxes are "duplicates" of the same object, the notebook needs a way to measure how much two boxes overlap. That's what **IoU (Intersection over Union)** does — it's a ratio between 0 and 1:

- `IoU = 0` means the boxes don't overlap at all
- `IoU = 1` means the boxes are identical

It's calculated as:

```
IoU = (area where the boxes overlap) / (total area covered by both boxes combined)
```

The notebook builds this up in stages:

1. A first cell works through the IoU math manually on two example boxes, just to sanity-check the arithmetic step by step (find each box's area, find the overlapping region, then divide).
2. That logic is then wrapped into a reusable function, `calculate_iou(bbox1, bbox2)`, which takes two boxes and returns their IoU as a float.

## Step 2: The NMS algorithm itself

The `nms(boxes, iou_threshold)` function is the core of the notebook. Given a list of boxes and a threshold, it decides which boxes to keep:

1. **Sort by confidence** — all boxes are ordered from most confident to least confident.
2. **Walk through the list, most confident first.** Each box that hasn't already been thrown out is kept as a "winner" for its object.
3. **Suppress overlapping boxes** — every remaining box is compared against that winner using IoU. If the overlap is greater than or equal to `iou_threshold`, it's considered a duplicate of the winner and is discarded.
4. This repeats until every box has either been kept as a winner or discarded as a duplicate.

The result is a shorter list containing just one box per real-world object — the one the detector was most confident about.

## Step 3: Trying it on realistic data

The last part of the notebook builds a test set (`test_boxes`) of 32 boxes, designed to look like a detector's raw output:

- **Cluster A, B, C** — three groups of ~8 heavily overlapping boxes, simulating a detector finding the same cat/dog/car multiple times with different confidence levels.
- **Cluster D** — a smaller group of 5 overlapping boxes (a "person" detection).
- **Isolated boxes** — a few boxes placed far from everything else, which don't overlap anything and should always survive NMS untouched.

Running `nms(test_boxes, 0.5)` (a 0.5 IoU threshold) reduces the 32 boxes down to 7 — one winner from each cluster, plus the isolated boxes — confirming that the algorithm correctly collapses each group of duplicates into a single best detection while leaving unrelated boxes alone.

The survivors sit at input-order slots 0, 8, 16, 24, 29, 30 and 31, which as a 32-bit mask is
**`keep_mask = 0xE1010101`**. That single value is the regression anchor for the whole hardware
build: the integer golden model, every VHDL testbench and the board must all reproduce it. Note the
set contains **no duplicate scores and no pairs exactly on the IoU threshold**, so it exercises
neither tie-breaking nor the boundary predicate — the two subtlest parts of the design. Those need
the synthetic adversarial cases from the vector generator, and a build that passes only this set has
tested very little.

## Where this fits in the bigger picture

This notebook is the software "answer key." Once the VHDL hardware pipeline (bitonic sort + comparison logic) is built, its output on the same test data should match what this notebook produces. Differences between the two would point to a bug in the hardware implementation rather than in the underlying algorithm.
