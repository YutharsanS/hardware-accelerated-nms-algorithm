# De-risking plan: NMS accelerator on Basys 3

## Context

The proposal claims two things that drive the whole architecture: sorting in "O(1)" via a
combinational bitonic network, and NMS in "O(n)" via parallel IoU lanes. Before any RTL is
written, those claims need to be checked against a fixed target (Basys 3 / XC7A35T: 90 DSPs,
~20,800 LUTs, ~41,600 FFs) and against contradictions that currently exist between
[architecture.md](architecture.md), [NMS.md](NMS.md), the golden model notebook, and
the proposal text.

Decisions already made by the team:
- Target board: **Basys 3 (XC7A35T)** — the proposal's "Nexys A7" is stale, fix it.
- N is **always 32** boxes per batch; host guarantees this.
- Output format: **echo the 64-bit box records back**, using one of the 4 spare bits as the
  keep/suppress flag.

This plan is a sequence of things to resolve and measure, not an implementation of the design.
The design judgments are left to the team.

---

## 1. Reality-check the two complexity claims

These are the numbers to verify yourselves before committing. Show your own derivation next to
each.

**Sorting.** A bitonic network on N=32 has `k(k+1)/2` stages where `k = log2(32) = 5`, so
**15 stages** of `N/2 = 16` compare-and-swap units = **240 CAS**. It is O(1) in *cycles* only if
fully combinational; the work does not disappear, it moves into **area** (O(N log²N) comparators)
and **critical path** (15 comparator+mux delays in series).

Estimate to confirm: with a 21-bit payload (16-bit score + 5-bit index) each CAS is roughly a
16-bit comparator plus two 21-bit 2:1 swap muxes ≈ 30 LUT6. 240 × 30 ≈ **7k LUTs ≈ 34% of the
Basys 3**. A 15-deep combinational path plausibly lands Fmax in the 20 MHz range against the
board's 100 MHz oscillator.

Things to decide from that:
- The proposal says "fully pipelined" in §2 and "purely combinatorial" in §3.1. Those are
  different designs with different costs. Which is it, and why?
- If you pipeline every stage: 15 × 32 × 21 ≈ 10k FFs ≈ 24% of the device. Acceptable?
- Confirm *why* routing only `{score, index}` through the sorter matters. Re-run the LUT estimate
  with the full 64-bit payload routed instead and compare. That number is the justification for
  §3.1 of your proposal.
- Harder question worth answering explicitly in the report: NMS only ever needs *the argmax of
  the still-valid boxes*, one keeper at a time. A 32→1 masked max-reduction tree is 31
  comparators, not 240. What does the full sort buy you that the max tree does not? If the answer
  is "nothing functional", say so and justify the sorter on other grounds (throughput,
  reusability, project scope) rather than leaving it unstated.

**NMS.** Throughout this document **`P` = the number of parallel IoU lanes** instantiated in the
suppression stage (proposal §3.4). One lane is one complete keeper-vs-candidate IoU datapath;
P lanes let the FSM dispatch P candidates against the current keeper in a single cycle.

Worst case is `N(N-1)/2 = 496` keeper/candidate pairs — inherently O(N²) work. With P
parallel lanes and a barrier between keepers, cycles ≈ `Σ ceil(remaining_i / P) + 32·L` where L is
the IoU pipeline latency. That is **O(N²/P)**, and reaches O(N) only when **P scales with N**
(P = 32).

DSP budget check to run: a naive lane needs multiplies for `area_keeper`, `area_candidate`,
`intersection`, and `T_INT × union` — about 3 DSPs per lane after sharing the keeper's area, so
`3P + 1`. At P=32 that is **97 DSPs against a budget of 90 — it does not fit.**

Before shrinking P, ask: which of those four products actually depends on the *pair*, and which
is a property of a single box that never changes for the whole batch? Recompute the per-lane DSP
cost after moving the pair-independent ones somewhere else, then see what P you can afford.
Record the P you choose and the worst-case cycle count it implies.

---

## 2. Freeze the data format — three specs currently disagree

These must be reconciled into a single normative section in [architecture.md](architecture.md)
before RTL, because both the VHDL and the Python vector generator read from it.

| Item | architecture.md | golden model notebook | Action |
|---|---|---|---|
| Coordinate field width | 11 bits ("22 bits per coordinate") | **12 bits** (cell 3: "take 12 bit for granted") | Pick one; re-derive every downstream width |
| Spare bits | 4 (with 11-bit fields) | 0 (with 12-bit fields) | The keep-flag decision needs ≥1 spare bit |
| RHS compare width | 33 bits | 33 bits (derived from 12-bit coords) | If you move to 11-bit coords this is **wrong** — re-derive |
| Board | Basys 3 | — (proposal says Nexys A7) | Fix the proposal |
| Threshold `2^k` | — | listed as "8 bits, 0 to 126" | `2^8 = 256` needs 9 bits, and it is a shift not a multiply — clarify the row |

Re-derive the compare width yourself for the 11-bit case: `w,h ≤ 2047` → area width? → union
width? → `LHS = area_int << 8` width? → `RHS = T_INT(8) × union` width? You should land on a
number that is *not* 33. Whichever coordinate width you keep, the notebook's width table in cell 3
must be regenerated to match.

Also settle, and write down:
- **Packing/unpacking cost.** A 12/12 split is nibble-aligned and unpacks with plain byte slicing
  in both Python and VHDL. An 11/11 split crosses byte boundaries and needs shift-and-mask on both
  sides — a real source of endianness/bit-order bugs. If you need only one flag bit, is stealing
  it from the coordinates the cheapest place to get it?
- **Corner convention.** The notebook uses lower-left `(x,y)` / upper-right `(a,b)`. Image
  coordinates conventionally grow downward. State the convention once; the maths is consistent
  either way, but the Python generator and the testbench must agree.
- **The invariant `a > x` and `b > y`.** In Python an inverted box silently yields a *negative*
  area; in VHDL `unsigned` subtraction **wraps to a huge positive number**. Same input, different
  answer, and the testbench will look like an RTL bug. Decide: reject at the host, clamp in
  hardware, or use signed arithmetic — and make the random vector generator actually produce the
  degenerate cases so the decision gets tested.
- **Score quantisation.** The golden model uses a Python float in [0,1]; hardware sorts a 16-bit
  integer. Define the mapping (and whether the 16-bit field shrinks to make room for the keep
  flag) in one place, and have the Python generator emit the integer form.
- **Class labels.** Real NMS runs *per class*. Your 64-bit record has no class ID. If the intended
  input is a single-class detector, state that as an explicit scope limit in the report rather
  than leaving it unmentioned.

---

## 3. The two bugs most likely to cost you a week

**Tie-breaking.** [golden-model.ipynb](../models/golden-model.ipynb) sorts with
`sorted(..., key=score, reverse=True)`. Python's sort is **stable** — equal scores keep input
order. A bitonic network is **not stable**; equal keys come out in an order determined by the
network topology. A different keeper is chosen, a different set survives, and the testbench fails
for a reason that has nothing to do with your RTL. Your current 31-box test set has no duplicate
scores, so this will stay hidden until you switch to random vectors.

Think about how to make ties impossible rather than how to make the network stable — you already
carry a unique 5-bit index through the sorter. Then make the Python model use the *same* total
order.

**Float threshold vs integer cross-multiply.** The notebook computes `iou = I / U` as a float and
tests `iou >= threshold`. The hardware tests `I·2^k >= T_INT·U`. These agree only when the
threshold is exactly representable in Q0.8. For 0.5 they agree; for something like 0.3
(`0.3 × 256 = 76.8`) they do not, and boxes sitting exactly on the boundary flip. Note also that
the notebook uses `>=` while the proposal §3.2 writes `>` — pick one and make both sides match.

The fix is to make the golden model compute the *same integer predicate* the hardware does, so the
comparison is bit-exact rather than tolerance-based. A tolerance band is the wrong tool here
because the disagreements are precisely at the boundary.

---

## 4. Sequential equivalence of the parallel lanes

Worth convincing yourselves of before building the FSM, and worth a paragraph in the report:
in the reference algorithm the inner loop only ever compares candidates against the **current
keeper**, and a suppressed box can never later become a keeper. Reason through whether that makes
a batch of P lanes dispatched against one keeper exactly equivalent to the sequential loop, and
what ordering constraint the FSM must therefore enforce between keeper iterations. State the
constraint explicitly — it is the thing that makes the parallelisation safe, and it is also what
costs you the `32 × L` drain cycles in the timing estimate above.

Related hazard to plan for: the IoU pipeline is multi-cycle (DSP output registers). The FSM must
handle results returning several cycles after dispatch, and must not select the next keeper from
the valid mask while suppression writes for the previous keeper are still in flight.

---

## 5. The system-level claim to re-examine

Run this arithmetic yourselves and put the result in the report:

- Compute time: with your chosen P, worst-case cycles at 100 MHz → microseconds.
- Transport time: 32 boxes × 8 bytes = 256 bytes in, 256 bytes back, at your chosen baud rate.
  At 115200 baud, one byte ≈ 87 µs.
- A 1080p30 frame budget is 33 ms.

The proposal's motivation is "deterministic, low-latency execution for real-time computer vision."
Compare the two numbers you just computed and decide honestly whether the *system* is compute-bound
or I/O-bound, and by what factor. Then either change something (baud rate, interface) or re-scope
the claim to "accelerator core latency" and say so plainly. Note that echoing full 64-bit records
back costs 64× the wire time of returning a 32-bit valid mask — that was your call to make, but
the report should show you knew the cost.

Second-order point on the same theme: P parallel lanes need P candidate boxes *per cycle*. At P=16
that is 1024 bits/cycle, which no BRAM port can deliver. The payload store will have to be
registers or distributed RAM, which makes the "2048 bits of Block RAM (0.001%)" line in
[architecture.md](architecture.md) misleading — update it once you know where the payloads
actually live.

---

## 6. Suggested order of work

Sequenced so the riskiest unknowns fail early, while staying inside the 28 Aug behavioural-simulation
milestone in [CLAUDE.md](../CLAUDE.md) (note: that date has already passed — confirm the real
deadline).

1. **Rewrite the golden model to integer arithmetic** (`models/golden-model.ipynb` → a `.py` module
   under `models/`), matching the frozen format from §2, the total order from §3, and the exact
   integer predicate the hardware will use. Everything downstream depends on this being right.
2. **Vector generator + file format** — random batches of exactly 32 boxes including the nasty
   cases: duplicate scores, zero-area boxes, boxes exactly on the IoU threshold, non-overlapping
   boxes, all-32-survive (worst case for cycle count).
3. **Single CAS unit + testbench** — smallest thing that proves the toolchain and the self-checking
   `assert`/`report` pattern end to end.
4. **Bitonic network** built from the CAS, with a testbench that checks a full permutation against
   the Python order. Synthesise it *alone* and record actual LUT/FF/Fmax — this is the go/no-go on
   the §1 area estimate.
5. **Single IoU lane** (one keeper-vs-candidate datapath), testbench against the integer golden
   predicate. Synthesise it *on its own* and read the real DSP/LUT count off the utilisation
   report, then divide the 90-DSP budget by it to fix **P**, the lane count. This has to happen
   before step 6, because P is the FSM's dispatch width — you cannot design the control logic
   until synthesis tells you how many lanes actually fit.
6. **Valid-mask + control FSM**, with the ordering constraint from §4.
7. **Top-level integration**, file-I/O testbench over the full vector set.
8. **UART wrappers last** — they are the least interesting risk and the most tedious.

Directory placement, the GHDL analyse/elaborate/run workflow, and the testbench naming convention
(`tb_` prefix) are all in [development_guide.md](development_guide.md) and [README.md](README.md) —
read those before creating `test/`.

---

## Verification

- Every module gets a self-checking testbench that fails loudly via `assert`, per the
  `hello_tb.vhdl` pattern and the convention in [CLAUDE.md](../CLAUDE.md).
- The end-to-end check is: Python generator writes vectors → file-I/O testbench drives the RTL →
  RTL output compared **bit-exactly** against the Python integer model. No tolerance band.
- The test set must include the adversarial cases from step 2, not just the 31 clustered boxes
  currently in the notebook. A pass on the current test set proves very little, because it contains
  no ties, no degenerate boxes, and no boundary cases.
- Record post-synthesis LUT / FF / DSP / BRAM utilisation and Fmax after steps 4, 5, and 7 —
  these are a stated deliverable, and they are also the evidence for or against the O(1)/O(n)
  claims.
