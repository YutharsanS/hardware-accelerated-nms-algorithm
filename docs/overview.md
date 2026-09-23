# Project overview — what exists, and how each piece works

A plain-language tour of the whole project. It assumes no memory of how any of it was built.

The other docs each answer a narrower question, and this one points at them rather than
repeating them: [architecture.md](architecture.md) is the frozen interface spec (exact bit
widths and formats), [plan.md](plan.md) is the forward plan, [build_log.md](build_log.md) is
the record of what each step did, and [results.md](results.md) is the measured area and
timing data.

---

## 1. What the project does

An object detector looking at an image doesn't find a cat once. It finds the same cat eight
times, with eight slightly different boxes and eight different confidence scores.
**Non-Maximum Suppression (NMS)** is the clean-up pass that throws away the duplicates and
keeps the best box per object.

The algorithm is short:

1. Sort the boxes by confidence, highest first.
2. Take the highest-scoring box that is still alive. It's a keeper.
3. Throw away every remaining box that overlaps it too much — those are duplicates of it.
4. Repeat until every box is either a keeper or discarded.

This project builds that in hardware on an FPGA, because step 1 (sorting) and step 3
(comparing every box against the keeper) are both things an FPGA can do many-at-once where
a CPU must do them one-at-a-time.

**Fixed problem size: 32 boxes per batch.** That's the `N = 32` you'll see everywhere.

---

## 2. The shape of the whole thing

```
   32 boxes in
        │
        ▼
  ┌─────────────┐   Only the 21-bit sort keys go through here.
  │  bitonic32  │   The boxes themselves never move.
  │   (sorter)  │   Built from 240 copies of `cas`.
  └─────────────┘
        │  sorted order (highest score first)
        ▼
  ┌─────────────┐   Tests one keeper against one candidate:
  │  iou_lane   │   "do these overlap enough to suppress?"
  │  (×16)      │   16 of them run in parallel.
  └─────────────┘
        │
        ▼
   32-bit keep mask  (bit i = 1 means box i survived)
```

The sorter and the lane are the two components that exist and are verified today. The FSM
that sequences them, and the UART that gets data on and off the board, are still to come —
see §8.

---

## 3. Four decisions that explain most of the code

These are the choices that, once you know them, make the rest of the RTL read as obvious.
All four are frozen in [architecture.md](architecture.md).

**Integers only, never floating point.** Confidence is `0.0–1.0` on paper, but FPGAs are bad
at floating point and excellent at integers. So everything is scaled to whole numbers. The
IoU threshold of 0.5 becomes `T_INT = 128` in units of 1/256.

**Never divide.** IoU is *intersection ÷ union*, and division is expensive in hardware. The
test "is `I/U ≥ 0.5`?" is rearranged into `I × 256 ≥ T_INT × U` — multiplication and a
comparison, no division anywhere. Same answer, far cheaper.

**Sort keys, not boxes.** A full box record is 64 bits. Dragging 32 of those through a
sorting network would be enormous. Instead only a 21-bit *key* is sorted, and the box data
stays put. The key is:

```
K = score (16 bits)  ‖  NOT index (5 bits)      = 21 bits
```

Sorting `K` descending gives highest score first, and where two scores tie, the lower
original index wins. The `NOT` on the index is what makes ties break that way. Afterwards
the original position is recovered as `31 − K(4 downto 0)`.

Bundling the index in has a second benefit: because indices are unique, **no two keys can
ever be equal**. That matters because bitonic sorting networks are not stable — but a tie is
needed to observe instability, and ties are now impossible. The problem is designed out at
zero hardware cost. (This is not a corner case: at 8-bit score resolution, 32 boxes collide
with probability ≈ 86%.)

**A Python "golden model" is the reference.** Before any VHDL was written, the algorithm was
implemented in Python with the *same integer arithmetic*. The hardware is checked against it
bit-for-bit. When they disagree, the hardware is wrong.

---

## 4. `nms_pkg.vhd` — the single source of truth

A VHDL package holding every constant and type the design uses: `N = 32`, `COORD_W = 12`,
`SCORE_W = 16`, `T_INT = 128`, and the derived types (`key_t`, `coord_t`, `key_array_t`, …).

Nothing hardcodes a width. Change a constant here and the whole design follows. There's a
dedicated testbench, `tb_params`, whose only job is to print all 40 constants and check the
derived ones are consistent — so a bad edit is caught immediately rather than showing up as
a mysterious mismatch later.

---

## 5. `cas.vhd` — compare-and-swap, the atom

The smallest building block. Two values in, two values out, plus a direction bit:

```
dir_desc = '0'  (ascending)    y0 = min(a,b)    y1 = max(a,b)
dir_desc = '1'  (descending)   y0 = max(a,b)    y1 = min(a,b)
```

That's it. It either passes the pair through or swaps it. The entire implementation is three
lines:

```vhdl
swap <= (a > b) xor (dir_desc = '1');
y0   <= b when swap else a;
y1   <= a when swap else b;
```

The `xor` is the neat part: one comparator serves both directions. Ascending swaps when
`a > b`; descending wants the opposite, and the `xor` flips it without a second comparator.

It is purely combinational — no clock. Measured cost: **32 LUTs**, delay 4.35 ns.

---

## 6. `bitonic32.vhd` — the sorting network

### Why a sorting network at all

Quicksort is fast on a CPU but useless here: the comparisons it makes depend on the data, so
you can't build fixed wiring for it. A **sorting network** compares a *fixed* set of
positions in a *fixed* order, always the same regardless of input. That's exactly what
hardware wants — no branches, no variable timing, just wires.

Bitonic sort is the standard choice. It needs more comparisons than quicksort in theory, but
it does them all at once, which is the whole point.

### The idea

A **bitonic sequence** goes up then down (or any rotation of that) — e.g. `1 4 7 6 3 2`.

The one fact the whole algorithm rests on: take a bitonic sequence of length `n`, compare
element `i` against element `i + n/2` across the whole thing, and swap where needed. You get
two halves where *everything in the first half is ≤ everything in the second*, **and both
halves are themselves still bitonic**. So you recurse into each half, and the halves never
need to talk to each other again.

Sorting 32 elements then works in two movements: build bigger and bigger bitonic sequences
out of the unsorted input, then merge each one down. The schedule comes out as:

- **5 stages** (because 2⁵ = 32)
- stage `k` contains `k` **sub-stages**, giving **15 sub-stages** total
- each sub-stage does **16 comparisons** (32 elements ÷ 2)
- **240 `cas` units** in total

### How the code expresses it

Rather than listing 240 instantiations by hand, the wiring is *computed* at elaboration time:

- `stage_kk(s)` / `stage_jj(s)` walk the standard bitonic schedule to find which partner
  distance sub-stage `s` uses.
- `build_dir` decides each comparator's direction: descending when bit `kk` of the lane
  index is set. (Written as `(i / kk) mod 2` because VHDL-93 has no integer `and`.)
- A nested `generate` loop then creates one `cas` for each lane `i` whose `jj` bit is clear,
  pairing it with `i xor jj`.

So the network is generated from the rule, not transcribed. Change `N` and it rebuilds.

### Pipelining: the `PIPE_CUTS` generic

240 comparators chained back-to-back is a *very* long path for a signal to cross in one clock
tick. `PIPE_CUTS` inserts register cuts between sub-stages to break it up:

- `PIPE_CUTS = 0` — no registers, answer in one (slow) cycle
- `PIPE_CUTS = n` — `n` registers, so `n` cycles of latency, each shorter and faster

The cuts are spread evenly unless `CUT_AFTER` names exact positions. Two `assert`s check the
configuration at elaboration, so a mistake fails the build rather than producing a silently
mistimed sorter.

**Measured** (see §8): `PIPE_CUTS = 8` is the setting that reaches 100 MHz.

---

## 7. `iou_lane.vhd` — the suppression test

One lane answers a single question: *given a keeper box and a candidate box, does the
candidate overlap enough to be thrown away?*

It's a 4-cycle pipeline:

1. **Intersection box.** Overlap width is `min(a₁,a₂) − max(x₁,x₂)`, and likewise for height.
   If the boxes miss entirely this goes negative — and the sign bit *is* the answer, so one
   subtraction tells you both "do they miss?" and "by how much do they overlap". A
   compare-then-subtract would need two operations.
2. **Intersection area** = width × height.
3. **Union** = area₁ + area₂ − intersection. (Both box areas arrive precomputed.)
4. **Compare** `I × 256 ≥ T_INT × U`. No division, as per §3.

`rst` deliberately clears only the *valid* flags, not the datapath. Stale coordinates are
harmless because their result is never marked valid, whereas a stale valid bit would write a
suppression that nothing asked for. That's 4 resettable flip-flops instead of ~130.

**Measured:** 109 LUTs, 2 DSPs, 170 MHz. The design targets 1 DSP; see §8.

---

## 8. Where it stands (measured, not estimated)

Everything below was measured on the real part, `xc7a35tcpg236-1`, after full place and
route — not estimated. Reproduce with `make synth MOD=<module>`.

**The sorter** (`bitonic32`), swept across pipelining depth:

| `PIPE_CUTS` | LUT | % of chip | latency | Fmax | meets 100 MHz? |
|---|---|---|---|---|---|
| 2 (current default) | 9,596 | 46.1% | 2 cycles | 53.9 MHz | no |
| 4 | 9,758 | 46.9% | 4 cycles | 87.8 MHz | no |
| **8** | **8,112** | **39.0%** | 8 cycles | 119.2 MHz | **yes** |
| 15 | 7,680 | 36.9% | 15 cycles | 187.4 MHz | yes |

The surprise: **more pipelining made it smaller as well as faster.** Normally you'd pay area
for speed. Here, breaking up the long combinational chains lets the tool share logic instead
of duplicating it, so `PIPE_CUTS = 8` is better on both counts than the current default.

**The lane** (`iou_lane`): 109 LUT, 101 FF, **2 DSPs**, 170 MHz. Area passes its ≤300 LUT
budget with room to spare, but the design targets **1** DSP and gets 2 — a constant multiply
that was expected to optimise away didn't. Not a correctness problem, and there's plenty of
headroom, but worth recovering later.

**Does the full design fit?** Sorter at `PIPE_CUTS = 8` plus 16 lanes ≈ **47% of the LUTs and
36% of the DSPs**. Over half the chip is still free for the FSM and UART. Yes, comfortably.

### Done
- `nms_pkg`, `cas`, `bitonic32`, `iou_lane` — written, simulated, synthesised, timed
- Self-checking testbenches for all four, passing under GHDL
- Python golden model, test vectors, and the integer NMS reference
- Area and timing measured on real silicon

### Not done
- The FSM that sequences sorter → lanes → keep mask
- UART wire protocol (spec'd in [architecture.md](architecture.md) §3, not built)
- `nms_pkg.PIPE_CUTS` is still `2`, which does **not** meet the 100 MHz the system assumes.
  The measurements say it should be `8`. Changing it moves the sorter's latency from 2 to 8
  cycles, which the FSM will need to account for — an open decision, not an oversight.
- The 2-DSP-per-lane result above

---

## 9. How it's tested

**Self-checking testbenches.** Every testbench decides pass/fail itself and reports it. You
never have to squint at a waveform to know whether something worked.

**Checked against the golden model.** The Python model generates test vectors into
`models/data/vectors/`; the VHDL testbenches read those files and compare. Both sides compute
the *same integer expressions*, so agreement is exact rather than approximate.

**Mutation testing.** The testbenches were themselves tested — by deliberately breaking the
RTL and confirming the tests caught it. A test that cannot fail is worthless, and this is how
you find out which ones those are. It found two real defects (see [build_log.md](build_log.md) B3.1).

**Latency checked as equality, not a minimum.** A design one cycle *too fast* fails just as a
design one cycle too slow does.

Run everything with `make test` — Python tests plus every VHDL testbench, about 36 seconds.

---

## 10. Running it

```bash
make test                      # everything: Python + all VHDL testbenches
make tb_bitonic32              # one testbench
make lint                      # Python formatting and linting
make synth MOD=bitonic32       # Vivado: area + timing on the real part
make synth MOD=bitonic32 PERIOD=10 GENERICS="PIPE_CUTS=8"
```

Vivado is not on `PATH` by default:

```bash
source ~/Vivado/2026.1/Vivado/settings64.sh
```

Full GHDL/GTKWave workflow: [development_guide.md](development_guide.md).

### One trap worth knowing

If Vivado ever reports **zero parts** (`get_parts` returns nothing, no devices in the GUI),
it is almost certainly the **licence tier**, not a broken install. Vivado 2026.1 gates which
devices you may target by licence tier, and an Alveo-tier licence enables Alveo cards *only*
— silently hiding every Artix-7 part while the device files sit perfectly intact on disk. The
fix is in [build_log.md](build_log.md) M1. This cost several days; it is written down so it
costs nobody else any.
