# NMS Golden Model

This document explains, in plain terms, what the golden model (`models/nms_model.py`, narrated
in `models/golden-model.ipynb`) does and how it's put together. It's meant for a reviewer who
wants to understand the logic without necessarily running it.

**The notebook is narrative, not the implementation.** It imports `models/nms_model.py` for the
algorithm rather than redefining it — one implementation only, so the two can never drift apart.
The float `calculate_iou`/`nms` shown early in the notebook are kept purely as the "as originally
written" comparison baseline (`models/bench_cpu.py` times it against the real thing); everything
downstream of that uses the integer model.

## What problem is this solving?

An object detector (e.g. something that finds cats, dogs, cars in an image) usually doesn't just draw one box around an object — it proposes many overlapping boxes for the same object, each with its own confidence score. **Non-Maximum Suppression (NMS)** is the clean-up step that takes all those overlapping guesses and keeps only the best one per object, discarding the redundant ones.

The golden model is a plain Python reference implementation of this algorithm. It doesn't run on the FPGA — its job is to produce trusted, known-correct outputs that the VHDL hardware implementation can later be checked against, bit-exact.

## What a box looks like

Every detection is a `Box(x, y, a, b, score)`, all integers, exactly the widths the wire format uses (`docs/architecture.md`'s frozen interface spec):

- `(x, y)` — the lower-left corner of the box, u12
- `(a, b)` — the upper-right corner of the box, u12
- `score` — the detector's quantised confidence, u16: `score = round(confidence * 65535)`

No floats reach the model past `quantise_score`. This is deliberate: the RTL has no divider and
no floating-point unit, so the golden model has to prove the algorithm works entirely in integer
arithmetic before any VHDL is written.

## Step 1: Measuring overlap without dividing

Real NMS compares `IoU = intersection / union` against a threshold. The hardware never computes
that ratio — dividing is expensive and IoU is only ever used as one side of a `>=` comparison, so
the division is cross-multiplied away instead:

```
(intersection << 8) >= T_INT * union      -- T_INT = 128 means threshold 0.5
```

`nms_model.suppresses(keeper, candidate)` evaluates exactly this. Both `intersection_area` and
`box_area` clamp width and height to `max(0, ...)` before multiplying, so a zero-area or inverted
box (`a <= x` or `b <= y`) always contributes `0`, never a negative number — there is no
`ZeroDivisionError` and no signed arithmetic anywhere in the predicate. Two degenerate boxes
compared against each other give `intersection=0, union=0`, and since `0 >= 0` is true, they
suppress each other regardless of position — a deliberate consequence of the `>=` rule, not a
special case.

## Step 2: The NMS algorithm itself

Two implementations are provided, and they are required to produce identical output on every
input:

- **`nms_sequential`** — the textbook winner-takes-all loop: walk boxes in ranked order, each
  still-valid box becomes a keeper and immediately suppresses every other still-valid box it
  beats. This is the authority on what NMS *means*.
- **`nms_allpairs`** — the rank-ordered matrix-then-resolve structure the RTL actually
  implements: every pairwise suppression is evaluated up front, independent of which boxes turn
  out to be keepers, then resolved one rank at a time. This is what lets the hardware overlap the
  suppression-row computation with the resolve step instead of draining the pipeline once per
  keeper (`docs/plan.md` Part 1e).

Ranking uses `sort_key(score, index) = score * 32 + (31 - index)` rather than sorting on score
alone. Because every index is unique, this key is a **strict total order** — ties are
structurally impossible, and descending `sort_key` is descending score with ties broken by the
lower index. That matters more than it looks: at 16-bit score resolution, 32 draws from the same
distribution collide with probability ≈86%, so ties are the common case, not an edge case.

An **absent slot** (`present_mask` bit clear) is never selected as a keeper and never suppresses
anything; since `keep_mask` starts at all-zero, an absent box's output bit stays provably 0.

## Step 3: Trying it on realistic data

`models/gen_vectors.py`'s `case_notebook31()` reuses the notebook's own 32-box dataset — three
clusters of ~8 heavily overlapping boxes (cat/dog/car) plus a 5-box cluster (person) plus 3
isolated boxes that never overlap anything. Running the golden model over it with every slot
present collapses it to **7 survivors** — one winner per cluster, plus the 3 isolated boxes —
and `models/test_model.py` asserts this exact count as its primary acceptance check. (The case is
still named `notebook31` per the original plan, even though the dataset itself has always held 32
entries.)

## Where this fits in the bigger picture

The golden model is the software "answer key." `models/gen_vectors.py` emits `models/data/*.hex`
(the packed 8-byte records plus `present_mask`) and `*.mask` (the expected `keep_mask`), computed
by this same model, for the VHDL testbenches to replay. Once the hardware pipeline (bitonic sort
+ IoU lanes + resolve) is built, its output on the same vectors must match these files bit-exact.
Any difference points to a bug in the hardware implementation, never in the algorithm — the two
Python implementations above already had to agree with each other first.
