# Build log

One entry per gated step of [plan.md](plan.md) Part B, written when the gate goes green.
Records the **actual numbers**, not just "passed" — a gate logged without figures cannot be
compared against later, and half of these figures are report deliverables. Where the plan
predicted a value, the entry says how close the prediction was: that comparison either
validates the area/timing model or shows where it is wrong, and it is free to collect at
the time.

Failed attempts are logged too. A gate that failed and was fixed is more informative than
one that passed first time.

**The anchor for the whole build:** the notebook's 32-box test set at IoU threshold 0.5
keeps 7 boxes at input-order slots 0, 8, 16, 24, 29, 30, 31 →

```
keep_mask = 0xE1010101
```

---

### B0.0 — Python environment                                            2026-09-04

**Built:** `pyproject.toml` gained `numpy>=1.26` as a runtime dependency, `pytest>=8.0` in
the dev group, and `pillow>=10.0` as an optional `render` extra. Added `models/__init__.py`
so `models.nms` and `models.gs` are importable as packages rather than via `sys.path`
manipulation. Added a `**/__main__.py` ruff exemption for `T201`, since CLI entry points
exist to print their report while library modules return strings.

**Gate:** `uv run python -c "import numpy"` and `uv run pytest --version` both succeed.

**Result:** **PASS.** `numpy 2.5.2`, `pytest 9.1.1`. Both previously failed — the project
declared `dependencies = []` while `models/gs/` imports numpy in every module, so the
committed Phase 0 code could not run under `uv` at all. It only ever worked because the
system Python happened to have numpy 1.26.4 installed.

**Deviation:** `uv` resolved numpy to **2.5.2**, a major-version jump from the system's
1.26.4, so the Phase 0 code was re-verified against it rather than assumed compatible: the
synthetic `.ply` round-trip is still exact and projection still produces 183 splats / 502
instances on the planes scene. No numpy 2.x breakage.

**Bug found and fixed — an inert lint exemption.** `ruff check models/` flagged `D103` in
`models/golden-model.ipynb` (cell 9, the `nms()` definition, which has no docstring while
cell 7's `calculate_iou` does). The cause was the `per-file-ignores` glob:

```toml
"notebooks/**/*.ipynb" = ["T201", "D"]     # never matched anything
```

The repository has no `notebooks/` directory — the only notebook is
`models/golden-model.ipynb` — so the glob matched nothing and the full ruleset was being
applied to the notebook. **The exemption had been silently inert since it was written.**
Corrected to a depth-independent `"*.ipynb"`, which is what the config intended.

Fixed the glob rather than adding a docstring, because the config's intent (notebooks are
exploratory: printing is the point, docstrings are not required) was right and only its
path was wrong. The `nms()` docstring would also have been pointless work — B1.5 rewires
the notebook to import `models/nms/model.py`, deleting that function.

**Verified as an exemption, not a silent skip** — a masking fix would look identical from
`All checks passed`. With `--select ALL` the notebook yields `CPY001`, `RET504` and
`ERA001`, proving the file *is* scanned, while zero `D`/`T201` violations appear, proving
the ignore applies. `make lint` now covers `models/` as a whole, notebook included.

---

### B0.1 — Makefile and GHDL flow                                        2026-09-04

**Built:** `scripts/Makefile` with the real rules (per the layout in
[README.md](README.md)) plus a one-line root `Makefile` that includes it, so `make` works
from the repository root. Targets: `test` (the standing regression), `tb_<name>` per
testbench, `tb_hello` (toolchain smoke test), `lint`, `analyse`, `synth MOD=<mod>`,
`clean`. Created `test/`. Added `build/` and the Vivado/xsim artefacts (`xvlog.pb`,
`vivado*.log`, `.Xil/`, …) to `.gitignore` — `xvlog.pb` had been loose in the tree since
before this session.

**Gate:** `make tb_hello` terminates, exits 0, and prints both `report` lines.

**Result:** **PASS**, exit 0:

```
hello_tb.vhdl:15:9:@0ms:(report note): Simulation started!
hello_tb.vhdl:17:9:@50ns:(report note): Simulation finished!
ghdl:info: simulation stopped by --stop-time @100ns
```

`make test` also passes: ruff format + check clean over 10 files, pytest collects nothing
yet (tolerated only until B1.1), `tb_hello` green.

**The prediction that mattered:** the plan warned this gate would hang as written, and it
would have. `hello_tb.vhdl` drives `clk <= not clk after 5 ns` as a free-running concurrent
assignment while its process ends in `wait`, so events are generated forever. The
`ghdl:info: simulation stopped by --stop-time` line above is that behaviour confirmed —
without the limit the target never returns.

**Convention adopted as a result:** every testbench from here on must **terminate on its
own**, driving its clock from a process gated by a `done` signal. `--stop-time` stays in
`RUNFLAGS` purely as a hang safety net, because with a free-running clock a hung testbench
and a passing one are indistinguishable from the exit code. A run that needs the limit to
finish is a failure, not a pass. Every testbench ends with an explicit `report "PASS"`, so
the absence of that line is itself a failure signal.

**Flags:** `--std=08` throughout (accepts both the VHDL-93 RTL subset and the VHDL-2008
testbenches), `-Wall` to report everything, and `-Wsensitivity` — which catches incomplete
combinational sensitivity lists and is *not* on by default. `--warn-error` is deliberately
not enabled yet: it would fail the build on benign noise before a clean baseline exists.

**Observation:** GHDL 4.1.0 here uses the **mcode** backend, which JIT-compiles rather than
emitting an executable, so `build/` holds only `work-obj08.cf`. `build/` is still ignored
wholesale because the gcc/llvm backends do emit `.o` files and binaries.

---

### B1.0 — architecture.md corrected and frozen                         2026-09-04

**Built:** [architecture.md](architecture.md) rewritten as the normative interface spec —
record format, wire protocol, datapath widths, predicate, sort key, degenerate-box rules,
storage, architecture, conventions and scope limits. `params.py` and `nms_pkg.vhd` mirror
it, and a later test fails the build if the three diverge.

**Why this step comes before B1.1:** architecture.md is normative and the constants are
derived *from* it, so writing `params.py` first would have enshrined the errors below.
This ordering was a bug in the first draft of the plan, caught in the pre-implementation
audit.

**Gate:** every derived number in the document is self-consistent, checked
programmatically rather than by eye.

**Result:** **PASS** — 16/16 checks:

```
max area 4095^2 = 16,769,025 -> 24 b     max union = 33,538,050 -> 25 b
LHS = area<<8  = 4,292,870,400 -> 32 b   RHS = 255*union = 8,552,202,750 -> 33 b
sort key max   = 2,097,151 -> 21 b       T_INT for IoU 0.5 = 128
latency N^2/P + L + C + 2 = 72 cycles    frame 264 B in / 6 B out
area budget LUT 12,318 (59.2%)  FF 7,244 (17.4%)  DSP 17 (18.9%)
```

**Three errors corrected in the document:**

1. **16-bit vs 8-bit confidence contradiction.** The record carried a 16-bit score while a
   later line read *"Fixed Thresholds and confidence variables: 8 bits"*. Resolved: the
   score is 16 bits and normative; the 8-bit figure applies only to `T_INT`. Also separated
   two numbers that were being conflated — `T_INT = 128` is the *value* (128/2⁸ = 0.5),
   while 0–255 is the *range* an 8-bit field holds.
2. **The BRAM claim was wrong twice over.** *"32 boxes × 64 bits → 2048 bits (0.001%)"* —
   the percentage is actually **0.111%** of 1,800 Kbit, and more importantly **BRAM cannot
   be used at all**: at P=16 the store must deliver **768 bits/cycle** while a BRAM36 port
   delivers at most **72**. Payloads live in registers. BRAM would only suffice at P=1, so
   low P is not "free" either.
3. **The "22 bits per coordinate" phrasing** meant 2 × 11 for an (x, y) *pair*, but reads as
   22 bits per single coordinate. That misreading previously produced a false claim that
   this spec and the golden model disagreed on coordinate width. Both have always said 12;
   the wording is now explicit and the trap is documented in place.

**Also fixed:** [NMS.md](NMS.md) said the test set has **31** boxes. It has **32** —
verified by executing the notebook. That error had made a padding decision look necessary
when it is not. The anchor `keep_mask = 0xE1010101` and the caveat that the set contains no
ties and no boundary pairs are now recorded there too.

**Not an error, worth noting:** architecture.md already specified Basys 3 correctly. The
stale "Nexys A7" is in the LaTeX proposal, which lives outside this repository; the plan's
Part D carries a correction checklist for it.

---

### B1.2 — integer golden model, both algorithm forms                   2026-09-04

**Built:** `models/nms/model.py` (`Box`, `pack_record`/`unpack_record`, `box_area`,
`intersection_area`, `suppresses`, `sort_key`, `sort_order`, `nms_sequential`,
`nms_allpairs`, `suppression_matrix`, plus the `ResolveStep` trace) and
`models/nms/batches.py` (the adversarial generators, shared with B1.3's vector writer so
the property tests and the RTL vectors exercise identical data).

**Gate:** four assertions from the plan — the anchor, agreement between the two forms over
20,000 adversarial batches, no division, and the union lemma never firing.

**Result:** **PASS** — 43 tests in ~22 s.

```
anchor:   sequential = 0xE1010101   allpairs = 0xE1010101
sweep:    20,000 batches x 32^2 = 20,480,000 predicate evaluations, 0 mismatches
division: AST scan of model.py finds no Div / FloorDiv / Mod
lemma:    U >= 0 asserted inside suppresses(); never fired across every case + 2,000 batches
trace:    32 steps, final valid_mask = 0, keep = 0xE1010101
boundary: I=600 U=1200, 2I == U exactly -> suppresses (inclusive `>=` confirmed)
```

**Why two implementations.** `nms_sequential` is the textbook loop and the authority on
what NMS means; `nms_allpairs` is the structure the RTL implements. Had the model carried
only the all-pairs form, the RTL would be compared against a model that made the same
restructuring, and an error *in the restructuring* would be invisible. The 20,000-batch
agreement is what closes that hole.

**Named cases: 18** — `notebook32`, `ties`, `all_equal`, `degenerate`, `boundary`,
`disjoint`, `all_survive`, `low_res_scores`, `rand_seed0..9`. Each targets something the
anchor cannot reach.

**Constructing an exact boundary pair took algebra, not luck.** For two equal boxes offset
by `d` with width `W`: `I = (W−d)·H` and `U = H·(W+d)`, so `2I == U` exactly when
`W == 3d`. Setting `W = 3d + offset` lands just above or just below at will. That is the
only way to test a `>=` boundary without depending on floating-point rounding — and with
`d=10, H=30` it gives `I=600, U=1200` precisely on the threshold. `offset=-1` confirms the
predicate declines to suppress just below it.

**Three claims turned into tests rather than left as prose:**

* *The anchor exercises neither ties nor the boundary.* Asserted directly — zero duplicate
  scores, zero pairs with `2I == U`. If someone later edits the notebook set into
  something that does, the test says so instead of the claim quietly becoming false.
* *The predicate is symmetric.* The hardware evaluates all `N²` ordered pairs rather than
  the `N(N−1)/2` unordered ones and relies on symmetry; now checked.
* *Rows applied to earlier ranks are no-ops.* This is the invariant that makes the
  all-pairs restructure equivalent to the sequential loop. The trace is walked and every
  row bit landing on an earlier rank is asserted already clear.

**Embedded data kept honest:** the notebook's 32 boxes are embedded as a constant so tests
do not depend on executing a notebook, but a test re-executes it and asserts the constant
still matches, coordinates and quantised scores alike.

**Timing note:** the 20,000-batch sweep is 19 s of the ~22 s total, so `make test` now
takes about half a minute. Acceptable for now — running the real gate every time is worth
it — but if it grows further the sweep should move behind a `slow` marker with a reduced
default count.

---

### B1.3 — verification vectors                                         2026-09-04

**Built:** `models/nms/vectors.py` (writer, reader and round-trip support),
`models/nms/__main__.py` (CLI: `uv run python -m models.nms vectors`), and 125 files in
`models/data/vectors/` covering 20 cases. Six file types per case, all fixed-width hex
separated by whitespace so a testbench needs only `readline` plus successive `hread` —
no comment stripping, no tokenising. Human-readable notes go to a `.txt` nothing parses.

| file | contents |
|---|---|
| `.hex` | 32 records (16 hex) then `present_mask` (8 hex) |
| `.mask` | expected `keep_mask` (8 hex) |
| `.keys` | 21-bit sort key per slot, input order (6 hex) |
| `.order` | expected rank-to-slot table (2 hex per rank) |
| `.trace` | per-rank resolve state: `rank slot kept row valid keep` |
| `.pairs` | explicit IoU lane stimulus and expected result (5 cases) |

**Gate:** every case round-trips through re-parsing, and its `.mask` matches the model.

**Result:** **PASS** — 135 tests in ~29 s. Round trip exact for all 20 cases; `.mask`
verified against *both* model forms, so no file is blessed by a single implementation.

**Two real defects found — by a test written to catch exactly this.** A case can pass every
round-trip check while testing nothing, so `test_vectors.py` asserts that each case
contains the hazard it is named for. It immediately caught:

1. **`low_res_scores` triggered zero suppressions.** Spacing 70 against size 90 gives
   IoU 0.125, far below the 0.5 threshold. The case was full of ties, but with nothing
   ever suppressed the tie order never decided anything — it was testing nothing at all.
   Fixed to spacing 25: adjacent pairs now well above threshold, pairs two apart below,
   so the outcome genuinely depends on tie-breaking. **0 → 104 suppressions**, survivors
   32 → 13.
2. **`all_survive` was byte-identical to `disjoint`** — `case_all_survive()` simply
   returned `case_disjoint()`. Two names, one case, and the more interesting path
   untested. `all_survive` now overlaps *below* threshold, so rows are computed and come
   back empty (95 intersecting pairs, 0 suppressions), which is a different path from
   `disjoint`'s complete absence of overlap (0 intersecting).

**Case audit after the fixes** — every case now demonstrably carries its hazard:

```
case             intersect  suppress  boundary  dup-scores  zero-area  survivors
all_equal              496       810         0          31          0          2
all_survive             95         0         0           0          0         32
boundary                 4         6         2           0          0         29
degenerate               0        42         0           0          7         26
disjoint                 0         0         0           0          0         32
low_res_scores         358       104         0           3          0         13
notebook32              94       188         0           0          0          7
ties                   112       224         0          29          0          4
```

`degenerate` showing 0 intersecting but 42 suppressing is correct and worth noting: zero-
area boxes give `I = 0, U = 0`, hence `0 >= 0` and suppression, which is the specified
behaviour from architecture.md section 7 being exercised.

**Areas are written as explicit inputs in `.pairs`, not recomputed by the testbench.** The
lane receives them precomputed from `box_store`, and a testbench that derived them itself
would duplicate the clamp logic it is meant to be checking — a shared mistake would then
cancel out instead of failing.

**Two `present_mask` variants added** (`partial_present` = `0x0000FFFF`, `none_present` =
`0x00000000`) since the batch generators produce all-present batches only, and an absent
slot must provably never survive.

**Staleness guard:** the vectors are committed so a clean checkout can run the testbenches,
so a test regenerates them into a temporary directory and asserts the committed copies are
byte-identical. Without it, changing the generator would silently leave the RTL verified
against stale expectations.

**Size:** 724 kB in 125 files, of which `.pairs` is 240 kB. Written for 5 cases rather than
all 20 — all 20 would be ~900 kB of largely redundant rows, and the Python property test
covers the remainder.

---

### B1.4 — CPU baseline                                                 2026-09-04

**Built:** `models/nms/bench.py` — the notebook's float algorithm, both integer forms, a
vectorised numpy all-pairs, a thread-pool dispatch probe, and a multi-batch suite.

**Gate:** every implementation agrees on `keep_mask`, and the benchmark table prints.

**Result:** **PASS** — 41 passed, 1 skipped (deliberately, see below).

```
host: 13th Gen Intel(R) Core(TM) i5-13500H
accelerator core: 72 cycles at 100 MHz = 0.72 us

  implementation        notebook32  all_survive   all_equal  rand_seed0    best   worst
  notebook float              77.4        445.7        37.3       341.2    37.3   445.7
  integer sequential          85.5        476.7        44.6       367.7    44.6   476.7
  integer all-pairs          886.4        947.5       852.4       931.9   852.4   947.5
  numpy all-pairs             40.3         57.0        36.0        53.4    36.0    57.0

  thread-pool dispatch, zero work: 20.1 us (49% of the fastest implementation)
```

**A figure recorded earlier in the plan was wrong, and this gate corrected it.** Part 1e
originally quoted 583 / 474 / 83 µs and "115× faster than numpy", all measured on a *single
random batch*. Re-measured across four committed cases, the spread **between batches** is
up to **10.7×** — wider than the spread between implementations — because the sequential
loop short-circuits as boxes get suppressed. `all_survive` is its worst case (nothing
suppressed, so no short-circuit at all) and `all_equal` its best (everything suppressed
after the first keeper). Quoting one number without naming the batch was misleading.
Part 1e now carries ranges: **50–79× versus numpy**, 50–660× versus Python overall.

**The all-pairs form is the slowest in software while being the fastest in hardware** —
852–948 µs against the sequential loop's 45–477 µs. It evaluates all `N² = 1024` pairs
unconditionally where the sequential form short-circuits. That redundant work is exactly
what a CPU pays for and what P parallel lanes get for free, so this is the argument for the
restructure, quantified. Note also that its cost is nearly **constant** across batches
(852–948 µs, a 1.11× spread) — the same data-independence the hardware has, visible in
software.

**A second finding: the integer predicate does not merely suit the hardware, it removes a
crash.** The gate initially failed because `float_reference` disagreed with the integer
model on `degenerate` and on random hostile batches. The cause is not rounding — it is that
the notebook writes an unguarded `iou = intersection_area / union_area`, and two zero-area
boxes give `I = 0, U = 0`. The `degenerate` case alone contains **49** such pairs.

My first version had quietly guarded that division with `if union else 0.0`, i.e. it was
*nicer than the notebook* and hid the problem. Rewritten to be faithful, so
`float_reference` now raises `ZeroDivisionError` exactly where the original would, and a
test asserts precisely that while the integer forms return a defined answer (`0 >= 0`,
suppress). The skipped test is `test_float_agrees_where_it_is_defined[degenerate]`, skipped
for that reason rather than because it is inconvenient.

**Also asserted rather than assumed:** for coordinates up to 4095 the float and integer
predicates **cannot** disagree on non-degenerate input. Deciding `I/U >= 0.5` differently
from `2I >= U` would need `|2I − U| < 1` with both integers, i.e. `2I == U`, and 0.5 is
exactly representable. float64 carries 53 mantissa bits against values below 2²⁵ here,
leaving no room for the rounding that would make a boundary pair ambiguous. Verified over
5,000+ sampled pairs. The conclusion would **not** hold for float32 or much larger
coordinates, which is why it is a test and not a comment.

**Thread-pool dispatch costs 20.1 µs of zero work — 49% of the fastest implementation.**
"Use more cores" cannot pay at N=32: dispatch alone is half the computation, and the
resolve loop is inherently serial since each keeper depends on all previous ones.

---

### B1.5 — notebook rewired onto the shared module                      2026-09-04

**Built:** [golden-model.ipynb](../models/golden-model.ipynb) rewritten — 13 cells, 8
markdown and 5 code. It keeps the narrative (what a box is, how IoU works, the width
table, what the test set does) and **imports the algorithm** instead of defining it.

**Gate:** the notebook executes and still reports 7 survivors, now via the module.

**Result:** **PASS**, exit 0:

```
boxes in         32
survivors        7 at slots [0, 8, 16, 24, 29, 30, 31]
keep_mask        0xE1010101
both forms agree True
matches anchor   True
```

**This closes loophole L5** — two divergent implementations of the same algorithm.
`def calculate_iou` and `def nms` are gone from the notebook; there is now exactly one
implementation of NMS in the repository.

**`test_boxes` deliberately stays defined in the notebook.** It could have been imported
from `batches.NOTEBOOK_32`, but then `test_embedded_notebook_set_matches_the_notebook`
would be comparing `batches` against itself. Keeping the notebook as the source of truth,
with `batches` holding an independently-written quantised copy that a test asserts equal,
preserves a genuine cross-check.

**The test got stricter as a result.** It previously wrapped the notebook's execution in
`contextlib.suppress(Exception)`, because the old notebook ended in bare expressions that
raise outside Jupyter. The rewritten notebook executes cleanly, so the suppression is gone
and **a notebook that raises is now a test failure**.

**Two problems found and fixed while doing this:**

1. **My cell-source helper dropped the last line of every cell.** A `[:-1]` intended to
   trim a trailing blank ate a real line instead, truncating `test_boxes` mid-literal —
   `SyntaxError: '[' was never closed`. Caught immediately by running the notebook rather
   than eyeballing it. Regenerated from `git show HEAD:` with an assertion that the
   recovered `test_boxes` source still ends in `]`.
2. **`from models.nms import ...` would fail in Jupyter.** Jupyter puts the *notebook's*
   directory on `sys.path`, so launching from `models/` leaves `models.nms` unresolvable
   even though it works from the repository root. Added a bootstrap cell that walks up to
   the directory holding `pyproject.toml`. **Verified both ways** — from the repo root and
   with `cwd=models/`.

**nbstripout:** the repo has `*.ipynb filter=nbstripout` in `.gitattributes`, and it
renumbers cell IDs sequentially. Ran it explicitly so the committed file is already in its
canonical form; the only change is IDs, content is byte-identical. That avoids the filter
silently rewriting the file at commit time.

**Stage 1 complete.** 176 tests pass, 1 skipped by design (`float_reference` on
`degenerate`, which divides by zero as the original did). No RTL yet — B2.1 is next.

---

### B2.1 — first RTL: nms_pkg and cas                                   2026-09-04

**Built:** [nms_pkg.vhd](../src/components/nms_pkg.vhd) (38 constants + 11 types),
[cas.vhd](../src/components/cas.vhd), [tb_cas.vhd](../test/tb_cas.vhd),
[tb_params.vhd](../test/tb_params.vhd) and
[test_params_agree.py](../models/nms/test_params_agree.py).

**Gate:** `make tb_cas` — exhaustive over all 256 input pairs at W=4 in both directions
plus directed 21-bit cases, ending `report "PASS"` with zero assertion failures.

**Result:** **PASS**, 20,538 comparisons in 0.18 s:

```
tb_cas: exhaustive W=4 sweep done (512 checks)
tb_cas: directed W=21 cases done (538 checks)
tb_cas: 20538 comparisons checked (512 exhaustive at W=4,
        26 directed and 20000 random at W=21)
PASS
```

`cas` is nine lines of architecture: one comparator whose result is shared by two muxes,
written as three concurrent assignments with no process at all, per the house style in
[architecture.md](architecture.md) section 10. Cost model to measure at B2.2:
`8 + 2*ceil(W/2)` = **30 LUT at W=21**, and 240 of them = 7,200 LUT (34.6%).

#### The testbench was mutation-tested, because a passing test that cannot fail is worthless

Five mutants of `cas`, each analysed and run against the unmodified testbench:

| mutant | outcome |
|---|---|
| `swap <= (a > b)` — ignores `dir_desc` | killed at 4 ns, W=4 exhaustive |
| `not ((a > b) xor …)` — inverted swap | killed at 3 ns, W=4 exhaustive |
| compares `a(W-2 downto 0)` — drops the MSB | killed at 49 ns, W=4 exhaustive |
| compares `a(3 downto 0)` — **correct at W=4, wrong at W=21** | survived all 512 exhaustive cases, killed at 525 ns by the directed case `a=2^20, b=2^20-1` |
| `swap <= (a >= b) xor …` | **survived** |

**The fourth row is why the 21-bit layer exists.** A bug that is *exactly correct* at W=4
passes a complete exhaustive sweep of that width; only the wide directed cases catch it.
An exhaustive test at a narrow width proves less than its completeness suggests.

**The fifth is not a blind spot — it is an equivalent mutant.** `a >= b` and `a > b` differ
only when `a = b`, and then the swap exchanges two equal values, so the outputs are
identical for all 512 inputs (checked exhaustively in Python). Nothing distinguishes them
because there is nothing to distinguish, which also confirms the note in `cas.vhd` that the
tie direction is unobservable.

**The testbench counts its own checks** and asserts the total is 20,538. A testbench whose
loop bound silently broke would otherwise report PASS having verified nothing — the one
failure mode that a green build cannot show you.

#### Anti-drift: the comparison is made against GHDL's evaluation, not a regex's

`test_params_agree.py` does **not** parse constant expressions out of the VHDL. `tb_params`
reports all 38 constants via `integer'image`, and the Python test runs GHDL and parses that
output, so the numbers compared are the ones the analyser actually computed. A Python
re-implementation of VHDL constant folding could be wrong in the same direction as the
package it checks.

The closure is **three-way and total**: names declared in `nms_pkg.vhd` = names reported by
`tb_params` = names compared in `EXPECTED`, plus every integer constant in `params.py` has
a counterpart. All 34 Python constants map to 34 of the 38; the other four are `MAGIC_0`,
`MAGIC_1` (a tuple in Python), `LATENCY_CYCLES` and `BAUD_DIV` (expressions in Python).
**No exemption list was needed**, so adding a constant to either side without the other
fails.

Verified by mutating the package, three different failure paths each naming the cause:

```
T_INT 128 -> 129            tb_params.vhd:146: T_INT does not encode an IoU threshold of 0.5
T_INTERMEDIATE_W 13 -> 14   nms_pkg.vhd and params.py disagree (vhdl, python):
                                                          T_INTERMEDIATE_W=(14, 13)
new constant added           declared but not reported: ['UNREPORTED_W']
```

The middle one matters most: `T_INTERMEDIATE_W` has no self-assertion in `tb_params`, so it
is caught *only* by the Python comparison. That is the drift the gate was built for.

`tb_params` also checks what the VHDL-93 subset cannot state as a derivation — that the
subtype widths really are cut from those constants (`key_t'length = KEY_W` and seven more),
that `2**INDEX_W = N`, that `COORD_MAX**2 < 2**AREA_W`, that `SCORE_MAX*N + N-1` is exactly
`2**KEY_W - 1`, and that latency is 72 cycles. The max LHS (4,292,870,400) and max RHS
(8,552,202,750) are **absent by design**: they overflow GHDL's 32-bit `integer`, so
`params.validate()` checks those where integers are unbounded.

#### `--warn-error` is now on

Deferred at B0.1 because GHDL has no per-warning severity, so enabling it before a clean
baseline fails the build on benign categories. Measured now: the whole tree **analyses and
elaborates with zero warnings** under `-Wall --warn-error`, so it is enabled in
`scripts/Makefile` for both `-a` and `-e`. A new warning now means new code and stops the
build; the escape hatch for a genuinely benign category is `make test WARN=-Wall` rather
than weakening the default.

**Estimate vs measured:** nothing to compare yet — LUT and DSP figures need Vivado, which
still enumerates zero parts (M1). B2.2 stays deferred; correctness is unaffected.

**Standing regression:** `make test` = 180 pytest passing (1 skipped by design) + 2 VHDL
testbenches + the toolchain smoke test, in ~35 s.

---

### B3.1 — the bitonic sorting network                                  2026-09-04

**Built:** [bitonic32.vhd](../src/components/bitonic32.vhd) — 240 CAS on 21-bit keys in 15
generated sub-stages — and [tb_bitonic32.vhd](../test/tb_bitonic32.vhd). Plus
`cases.txt`, a manifest of the vector set, and the `SWEEP_` mechanism in
[scripts/Makefile](../scripts/Makefile) that runs one testbench in several configurations.

**Gate:** the full permutation matches the Python order on **every** vector set including
the tie cases, and the recovered indices match too, for `PIPE_CUTS` in {0, 2}.

**Result:** **PASS** in both configurations, and in fact across {0, 1, 2, 3, 5, 14, 15}:

```
tb_bitonic32: PIPE_CUTS = 0, 20 cases x 32 keys, ordering + permutation
              + rank table + latency all checked (18 cases pinned the latency)
tb_bitonic32: PIPE_CUTS = 2, 20 cases x 32 keys, ...
PASS
```

#### The testbench reads a manifest instead of a hard-coded case list

`models/data/vectors/cases.txt` lists all 20 cases and the testbench loops over it, so a
case added to `batches.py` is covered by this gate **without anyone editing a VHDL file**.
`test_vectors.py` asserts the manifest lists exactly the generated cases, so that
guarantee cannot quietly become false — which is the only thing that makes the indirection
worth having over a list written here.

A count of cases is not enough on its own, so the testbench also asserts by name that
`ties`, `all_equal`, `boundary` and `notebook32` ran. A truncated manifest would otherwise
report PASS having skipped exactly the cases that matter.

#### Four checks, and sorting correctly is the weakest of them

1. **strictly ascending** — the key is a strict total order, so equal neighbours mean a key
   was duplicated or lost;
2. **a permutation of the input** — each input key appears in the output exactly once;
3. **the recovered index** `N-1-out(N-1-r)(4 downto 0)` equals the model's `order(r)`;
4. **the key at the model's rank-*r* slot** is the key the sorter placed at rank *r*.

Checking only that the keys came out sorted would pass a network that sorted perfectly
while corrupting the index tag in the low 5 bits — and **that tag is the entire output**,
since payloads never move. Checks 3 and 4 close it from the two independent directions the
vector files allow. Verified by mutating the *testbench*: reading the rank table forward
instead of reversed is caught, so check 3 is genuinely asserting the reversal convention
rather than restating it.

#### Latency is checked as an equality, in both directions

The obvious form of this check — wait `PIPE_CUTS` edges, confirm the answer — only proves
latency is not *longer* than claimed. A sorter with one register too few passes it, because
by then its output has been correct for a cycle. So the testbench also samples **one edge
short** and requires the output to still be the previous batch's result, counting the cases
where the two differ (18 of 20) so the check cannot be vacuously true. That number is what
B5.2's cycle-count assertion depends on, so it is measured rather than assumed.

`bitonic32` additionally asserts at elaboration that `popcount(CUT_AFTER) = PIPE_CUTS`, so
the register *count* is checked structurally as well as behaviourally.

#### Mutation results

| mutant | outcome |
|---|---|
| direction bit inverted | killed — `all_equal` not ascending |
| partner `i+1` instead of `i xor jj` | killed — `all_equal` not ascending |
| last sub-stage bypassed | killed at both `PIPE_CUTS` values |
| one register dropped from the cut table | killed by the elaboration assert |
| testbench reads the rank table forward | killed |
| **CAS compares only the score, ignoring the index tag** | **killed by `all_equal`** |
| direction taken from `i+jj` instead of `i` | survived — **equivalent** |

**The sixth row is the whole reason the tie cases exist.** Ignoring the index tag is the
classic bitonic failure: a network that sorts distinct keys perfectly and is unstable on
ties. With `all_equal` every score is identical, so a score-only comparator swaps nothing
and the output is not ascending by full key. **A vector set without ties would have passed
that mutant** — and the anchor `notebook32` has no duplicate scores at all.

The last row is not a gap. `jj <= kk/2 < kk` always, so indices *i* and *i+jj* differ only
in a bit below `kk` and share the same direction bit — the two forms are the same circuit.

#### Two real defects found by running it

1. **1,680 metavalue warnings per run at time 0.** Chaining 240 comparators makes an
   ordinarily-invisible detail loud: an `out` port with no default is `'U'` until its first
   delta, and it feeds the next sub-stage's comparator, so `numeric_std` reports every such
   compare. **My first two attempts at this were wrong** — initialising `net` and `comb`
   inside `bitonic32` did nothing, because `comb` is driven by the CAS output ports and a
   signal initialiser is overridden by the port driver. Measured rather than reasoned about:
   initialising the `cas` outputs alone dropped it from 1,680 to 0 at `PIPE_CUTS=0`, and
   `net`'s initialiser is still needed for the first delta at sub-stage 1. Both are now
   present, `comb`'s is not, and the comments say which and why. Fixed at the source rather
   than with `--ieee-asserts=disable-at-0` so it stays fixed under xsim at B6.1.
2. **`bitonic32`'s own output port had the same problem**, which is how the new latency
   check first failed: `previous := keys_out` read `'U'` on the first case. Initialised —
   and it is the truthful power-on value, since a Xilinx flip-flop comes up at 0.

#### `make analyse` now refuses unlisted RTL

The RTL list was a wildcard, and **it broke the moment the second module landed**: the
wildcard sorts alphabetically, so `bitonic32.vhd` was analysed before the `cas.vhd` it
instantiates. GHDL is order-sensitive, so the list is now explicit, and any `.vhd` under
`src/` that is not in it fails with a message rather than being analysed at whatever
position a wildcard chose. The old comment claimed wildcards were "deliberately avoided
here because GHDL is order-sensitive" while using them for everything after the package.

**Estimate vs measured:** still nothing to compare — 7,200 LUT and the 100 MHz timing claim
(P1, the weakest load-bearing estimate in the plan) both need Vivado. **B3.2 remains the
gate that matters and it stays deferred on M1.** `PIPE_CUTS` and `CUT_AFTER` are generics
precisely so a bad timing result is a configuration change, and correctness is now verified
at every `PIPE_CUTS` from 0 to 15, so whichever value the timing report demands is already
known-correct.

**Standing regression:** `make test` = 181 pytest passing (1 skipped by design) + 4 VHDL
runs across 3 testbenches + the smoke test.

---

### B4.1 — the IoU lane                                                 2026-09-04

**Built:** [iou_lane.vhd](../src/components/iou_lane.vhd) — four registered stages, one
DSP — and [tb_iou_lane.vhd](../test/tb_iou_lane.vhd). Plus `random_pairs.pairs`
(10,000 hostile pairs), a second copy at `T_INT = 255`, the `pairs.txt` manifests, and
`model.suppresses_at`.

**Gate:** bit-exact against `model.suppresses` for every pair in every vector set, plus
10,000 random pairs from file through the RTL, and 10⁶ pairs model-vs-model in Python.

**Result:** **PASS** at both thresholds, zero warnings:

```
tb_iou_lane: T_INT = 128, 15120 pairs from 6 files (pairs.txt), bit-exact against
             the golden model, streamed one per cycle at LANE_LATENCY = 4
tb_iou_lane: T_INT = 255, 10000 pairs from 1 files (pairs_t255.txt), ...
PASS
```

Python side: **1,000,000 pairs, 0 mismatches** between the scalar model and an independent
numpy reimplementation, in 1.9 s. Split from the RTL run deliberately — a million pairs
through GHDL means a ~50 MB file or a PRNG mirrored bit-exactly in VHDL and Python, and
that mirror would need a gate of its own.

**Pairs are streamed back to back, one per cycle.** Driving one pair, waiting for it to
emerge, then driving the next would leave the stage-to-stage handover untested, and that is
where a missing or mis-ordered register shows up — not in a single pair.

#### Two coverage holes found by measuring instead of assuming

Both were invisible from a passing test, and both are about frozen widths going unexercised.

**1. `LHS_W = 32` and `RHS_W = 33` were untouched.** The first version of the random
generator built boxes a few hundred units across, so the largest achieved LHS was around
2²⁶ — the top six bits of a 32-bit signal never saw a 1. Fixed by adding a near-maximal box
category:

| | before | after |
|---|---|---|
| max LHS | ~2²⁶ | **4,267,735,040 — 32 bits of 32** |
| max RHS at `T_INT=128` | ~2²⁶ | 2,146,419,840 — 31 bits |
| max RHS at `T_INT=255` | — | 4,276,070,775 — 32 bits |

`test_vectors.py` now asserts `max_lhs.bit_length() == LHS_W`, so the hole cannot reopen.

**2. `T_INT = 128` cannot distinguish the generic from a hard-coded constant.** At 128 the
multiply degenerates to a shift. Measured, not guessed: mutating the RHS to
`to_unsigned(128, …) * union`, or to `shift_left(union, K_SHIFT-1)` — the plausible
"T_INT is 128, so just shift" optimisation — **passes all 15,120 pairs at `T_INT = 128`**
and is killed at 255. Hence the second run. One `-gT_INT=` flag selects the threshold *and*
the manifest, derived inside the testbench, so the two can never be mismatched.

#### A finding about the spec: `RHS_W`'s top bit is unreachable

Chasing the 33rd bit led somewhere more interesting. architecture.md §4 gives max
`U = 2·COORD_MAX² = 33,538,050` (25 bits) and max `RHS = 8,552,202,750` (33 bits). Those
are correct **safe** bounds but not tight ones, because `U = |A| + |B| − |A ∩ B|` is the
area of the *geometric* union and both boxes live inside the same 4096×4096 space:

```
true max U          = COORD_MAX**2      = 16,769,025      (24 bits)
spec's stated max U = 2 * COORD_MAX**2  = 33,538,050      (25 bits)
true max RHS        = 255 * 16,769,025  = 4,276,101,375   (32 bits)
spec's stated max   = 255 * 33,538,050  = 8,552,202,750   (33 bits)
```

Verified by construction and by 20,000 random pairs: the union **never** exceeded
16,769,025, and the arrangement that looks like it should double it — two disjoint
half-planes — gives exactly that same figure, because each box is then half the area.

**This is not a bug and the widths must not be narrowed.** `UNION_W = 25` is genuinely
required: the `k_area + c_area` intermediate does reach 33,538,050, and the stimulus
reaches 33,403,006 of it. Only the *difference* is bounded by the image. So the 25th bit of
`U` and the 33rd of `RHS` are unreachable by construction — recorded in
`test_union_is_bounded_by_the_image_not_by_twice_a_box` so that (a) nobody narrows `UNION_W`
to 24 on the strength of the `U` bound and breaks the adder, and (b) nobody spends time
chasing bit coverage that geometry forbids.

**Suggested for architecture.md:** state both bounds, the algebraic one that sets the width
and the geometric one that is actually reachable. The widths themselves are right, so this
is a precision improvement rather than a correction — left for a separate pass rather than
edited mid-gate.

#### Mutation results

| mutant | outcome |
|---|---|
| `>` instead of `>=` | killed — `boundary` row 2 |
| clamp removed from the width | killed — **by the union-underflow assertion**, `I = 108780` against an area sum of 1890 |
| union adds instead of subtracts | killed |
| LHS shifted one place too few | killed |
| `aa` takes max instead of min | killed |
| valid chain one stage too short | killed — `valid_out` low when a result was due |
| threshold hard-coded, generic ignored | survived at 128, **killed at 255** |
| multiply replaced by a shift | survived at 128, **killed at 255** |
| clamp threshold `t_w >= 0` instead of `> 0` | survived — **equivalent** |

The second row is the §7 proof earning its place as a live assertion: an unsigned wrap there
would otherwise have produced a plausible wrong answer rather than a fault. The last row is
not a gap — at `t_w = 0` the sliced value is 0, so both branches give `w = 0`.

#### Design notes

**`rst` clears the valid chain only**, not the datapath. A stale coordinate is harmless
because its result is never marked valid, whereas a stale `valid` would write a suppression
bit no box asked for. That is 4 resettable flip-flops instead of ~130, and the testbench
asserts it: driving `valid_in = '1'` throughout reset must produce nothing.

**Latency is an equality here too.** After the last valid input the testbench requires
`valid_out` to be low one cycle later, so a chain one stage too *long* fails as well.

**The clamp uses a signed 13-bit subtract**, which is the `T_INTERMEDIATE_W` signal
architecture.md §4 specifies: the sign bit *is* the clamp decision, so one subtract yields
both "do the boxes miss?" and the overlap extent, where a compare-then-subtract needs two
operators.

**Estimate vs measured:** the ≤ 300 LUT and **exactly 1 DSP** targets need Vivado, still
blocked on M1, so **B4.2 stays deferred and `P = 16` stays provisional**. One thing worth
noting for that gate: the design has exactly one `*` on a per-pair value (`s1_w * s1_h`);
`T_INT * union` is a constant multiply that folds, and the areas arrive precomputed. If
B4.2 reports more than 1 DSP, the cause will be `T_INT * union` failing to fold rather than
anything structural.

**Standing regression:** `make test` = 188 pytest passing (1 skipped by design) + 6 VHDL
runs across 4 testbenches + the smoke test, in 36 s.

---

### M1 — Vivado enumerates zero parts: the cause was the licence    2026-09-22

**The gate that blocked B2.2, B3.2 and B4.2 since 2026-09-04 is cleared.** `get_parts`
returns 1041 parts and `xc7a35tcpg236-1` resolves, reporting 20,800 LUT / 41,600 FF /
90 DSP.

**It was never a broken install.** Every hypothesis that treated it as one was wrong, and
each cost an install cycle:

- *Missing Digilent board files.* Wrong — `get_parts` returned 0 for `*`, so no part of any
  family was visible, not merely the Basys 3 preset.
- *Incomplete device install.* Wrong — `data/parts` held 6.5 GB, `CustomerPartsList.txt`
  listed `xc7a35t-cpg236-1`, and `devint/artix7/xc7a35t/cpg236/` was fully populated.
- *Missing generated device list.* Half right and a genuine second defect:
  `2026.1/data/installed.devices` did not exist. Regenerating it needs the installer to see
  a **new device family** — it short-circuits with "No new device families to add", so
  adding DocNav does nothing and adding SoCs works. The file is now correct, listing 16
  families including `artix7`. Fixing it alone did **not** restore the parts.

**The actual cause: Vivado 2026.1 gates devices by licence tier.** `libxv_device.so` carries
the logic, and its own strings state it plainly:

```
DBG_enableDevicesByLicense: ALVEO tier detected. Enabling Alveo devices.
DBG_enableDevicesByLicense: BASIC tier detected. Enabling Basic devices.
DBG_enableDevicesByLicense: PRO/ENTERPRISE/GOLD tier detected. Enabling Basic, Core, All and Alveo devices.
```

The licence carries **both** tiers — four features stamped `License_Tier:ALVEO` and four
stamped `License_Tier:BASIC`. Vivado latched onto ALVEO, which enables Alveo devices *only*;
with no Alveo card installed, every Artix-7 part was filtered out and `get_parts` returned 0
with no diagnostic. Dropping the four ALVEO blocks makes it report BASIC and enumerate
normally. `~/.Xilinx/Xilinx.lic` now holds the BASIC-only form; the original is at
`~/.Xilinx/Xilinx_full.lic.bak`.

**Why it took so long to find:** the failure is silent by construction. Vivado prints
`A valid Vivado Design Suite ALVEO license has been detected` as an *INFO* at every startup
and never connects it to the empty part list, so the one line naming the cause reads as
routine. plan.md Part 0 had recorded the Alveo licence and dismissed it as irrelevant
because Artix-7 35T is a no-charge device; that inference was the single wrong assumption,
and it is now corrected in place.

**Deliverable:** `scripts/synth.tcl`, out-of-context synthesis **through `route_design`**.
Post-synthesis numbers omit routing delay and P1 is a claim about real Fmax, so a
synth-only figure would flatter it. OOC is not optional either: `bitonic32` presents
2 × 32 × 21 bits at its boundary, which no 236-pin package can carry.

---

### B3.2 — bitonic32 area and timing, measured                      2026-09-22

`make synth MOD=bitonic32 PERIOD=10 GENERICS="PIPE_CUTS=<n>"`, post-route, OOC,
`xc7a35tcpg236-1`, 10 ns constraint:

| `PIPE_CUTS` | LUT | % device | LUT/CAS | FF | CARRY | critical | Fmax | 100 MHz |
|---|---|---|---|---|---|---|---|---|
| 2 (shipped) | 9,596 | 46.1% | 40.0 | 1,344 | 720 | 18.570 ns | 53.9 MHz | no |
| 4 | 9,758 | 46.9% | 40.7 | 2,688 | 720 | 11.392 ns | 87.8 MHz | no |
| **8** | **8,112** | **39.0%** | **33.8** | 5,376 | 720 | 8.386 ns | **119.2 MHz** | **yes, WNS +1.614 ns** |
| 15 | 7,680 | 36.9% | 32.0 | 10,080 | 720 | 5.335 ns | 187.4 MHz | yes |

#### The first version of these numbers was wrong, and the method is why

The initial `scripts/synth.tcl` counted primitives with
`get_cells -hier -filter {PRIMITIVE_GROUP == LUT}` instead of reading `report_utilization`.
That query does not agree with the report — it returned 13,344 where the report said 9,596,
and on `iou_lane` it reported 0 DSP where the report said 2. It is not a constant factor
either (2.00× on `bitonic32` at `PIPE_CUTS = 15`, 1.39× at `PIPE_CUTS = 2`, 1.55× on
`iou_lane`), so the first table could not be rescued by scaling and every point was
re-measured. **The gate is judged on `report_utilization`**, which is the figure the
datasheet, the timing reports and every other tool quote.

Recorded because the wrong numbers inverted the conclusion: they showed the sorter taking
64–74% of the device and the §1 cost model failing by 1.85×, which would have put P2 in
doubt and invited a redesign that the real numbers do not call for.

#### The structure is exactly as designed, and the cost model holds

Two invariants hold at every point, which is what makes the LUT figure trustworthy rather
than a mis-elaboration: **CARRY is 720 = 240 × 3** at every `PIPE_CUTS`, and **FF is exactly
`PIPE_CUTS` × 672** (672 = 32 keys × 21 bits). The netlist is the network the RTL describes.

**plan.md §1's per-CAS estimate is essentially right.** It predicts `8 + 2·⌈W/2⌉` = 30 LUT6
per CAS at W=21. A standalone `cas` measures **32 LUT / 3 CARRY**, critical path 4.348 ns —
6.7% over. Inside the network the figure is higher and depends on pipelining: 40.0 LUT/CAS
at `PIPE_CUTS = 2`, falling to 32.0 at `PIPE_CUTS = 15`, where it converges on the
standalone cost. The §1 total of 7,200 LUT / 34.6% is met almost exactly at
`PIPE_CUTS = 15` (7,680 / 36.9%) and exceeded by 13% at the setting actually chosen.

#### Pipelining buys timing *and* area, so `PIPE_CUTS = 8` is not a trade

The expected trade — more cuts, more area — does not appear. LUT is flat from 2 to 4
(9,596 → 9,758) and then **falls** to 8,112 at 8 and 7,680 at 15, a 20% reduction against
the shipped setting, while Fmax rises 3.5×. Deep combinational chains force logic
replication; cutting them lets the tool share instead. Flip-flops are the only price, and
at 10,080 they are still 24% of the 41,600 available.

**`PIPE_CUTS = 8` is the lowest swept value that meets 100 MHz**, at +1.614 ns, and it is
also cheaper in LUTs than the shipped 2. Correctness there is already covered: B3.1 verified
every `PIPE_CUTS` from 0 to 15, so whichever value the timing report demanded was going to
be known-good. That decision paid off exactly as intended.

#### Estimate vs measured

**P1 was pessimistic, not optimistic, and is now settled.** It projected 22–37 ns /
27–45 MHz for the network combinationally; `PIPE_CUTS = 2` measures 18.570 ns / 53.9 MHz.
Per sub-stage that is ~3.7 ns against the 1.5–2.5 ns assumed, but across fewer levels in the
critical segment than the estimate's arithmetic implied. **The 100 MHz claim survives — at
`PIPE_CUTS = 8`, not at the shipped 2.** `CLOCK_HZ` is frozen at 100 MHz and `BAUD_DIV`
derives from it, so this is a system constant rather than a target, and it fixes the
setting.

**Standing regression:** unchanged and green after the Makefile edit — `make test` = 188
pytest passing (1 skipped by design) + 6 VHDL runs across 4 testbenches + the smoke test.

---

### B4.2 — the IoU lane: area passes, the DSP target does not        2026-09-22

`make synth MOD=iou_lane`, post-route, OOC, `xc7a35tcpg236-1`, 10 ns constraint, shipped
generics (`T_INT = 128`, `K_SHIFT = 8`):

| | measured | target | verdict |
|---|---|---|---|
| LUT | 109 (0.5%) | ≤ 300 | **passes, with 2.8× headroom** |
| FF | 101 | — | — |
| CARRY4 | 30 | — | — |
| DSP | **2** | **exactly 1** | **misses** |
| Fmax | 170.1 MHz | ≥ 100 MHz | passes, WNS +4.120 ns |

**B4.1 predicted both the failure and its cause.** It recorded that the design has exactly
one `*` on a per-pair value (`s1_w * s1_h`), that `T_INT * union` is a constant multiply
which should fold, and that *"if B4.2 reports more than 1 DSP, the cause will be
`T_INT * union` failing to fold rather than anything structural."* That is precisely what
happened, and Vivado names it in the synthesis log:

```
DSP Report: Generating DSP union, operation Mode is: C-(A2*B2)'.
DSP Report: Generating DSP s2_inter_reg, operation Mode is: (A2*B2)'.
```

Two DSP48E1s are generated — one for `union`, one for `s2_inter_reg`. The constant multiply
did not fold; it was mapped to a DSP alongside the genuine one. Nothing structural is wrong,
which is why area and timing both pass comfortably.

**`P = 16` fits, so the miss is not fatal.** At 2 DSP per lane, 16 lanes use **32 of 90
DSPs (35.6%)** and **1,744 LUT (8.4%)**. With the sorter at `PIPE_CUTS = 8` that is
**47.4% of LUTs and 35.6% of DSPs for sorter plus lanes**, leaving over half the device for
the FSM and UART. **`P = 16` is no longer provisional.**

The DSP target remains worth recovering — at 1 DSP per lane the budget halves to 16 — but it
is an optimisation with headroom behind it, not a blocker. Forcing the fold is the next
candidate, and it does not change any interface.

---

### M2 — port paths were never timed; re-measured                     2026-09-24

A review of the `nms_ctrl` design found that `scripts/synth.tcl` set only `create_clock`. With
no `set_input_delay` / `set_output_delay`, Vivado treats input-port → register and register →
output-port paths as *unconstrained*, and leaves them out of the WNS the script reports. Every
clocked figure before this entry was therefore internal-segment only:

- **`iou_lane`'s whole stage 1** (min/max, subtract, clamp) sits between the ports and the `s1_*`
  registers, so it was never timed.
- **`bitonic32`'s first and last segments** were never timed.

The script now constrains every data port with a zero delay against `clk`, modelling each
neighbour as a register at the boundary. Re-measured:

| module | before | after | critical path now |
|---|---|---|---|
| `iou_lane` | 170.1 MHz | **139.6 MHz**, WNS +2.839 ns | port `k_a` → DSP reset pin (stage 1; the clamp was folded into the multiplier's reset) |
| `bitonic32` (`PIPE_CUTS = 8`) | 119.2 MHz | **116.7 MHz**, WNS +1.434 ns | internal, after sub-stage 10 → after 12 (placement variation) |

Both still clear 100 MHz. The finding that matters is the lane: with 2.84 ns left, the
integration path `index_table` → 32:1 × 72 b row-source mux → lane stage 1 is tight. It cannot
be measured until C4 wires the mux. If it misses, a datapath issue register is one more lane
stage, which `nms_ctrl` absorbs through `LANE_LATENCY` (T 78 → 79) with no logic change.

---

### C3 — `nms_ctrl`, the all-pairs control FSM                          2026-09-24

Built to [fsm_design.md](fsm_design.md), which was reviewed first. The review added the explicit
start/busy rule for `frame_rx` and illegal-state recovery. `src/components/nms_ctrl.vhd`, one
clocked process, VHDL-93:

`IDLE → SORT(C) → FILL(N·G) → DRAIN(L+1) → DONE → IDLE`, with resolve driven by a valid bit
that travels with the data rather than by the state register.

**`tb_nms_ctrl`** drives the real `bitonic32` and replays the lanes from `.trace`. It checks:

- the final mask;
- `keep_mask` and `valid_mask` after **every** resolve edge (via a VHDL-2008 external name,
  which GHDL 4.1 accepts, so no debug port was needed);
- latency from both sides;
- the issue schedule, in the stub.

It runs over all 20 cases back to back, then four directed cases: start during FILL, rst during
FILL, a stub one cycle slow (status `0x03`), and recovery after it.

**Passes at every sweep point:**

| configuration | T |
|---|---|
| `PIPE_CUTS` = 8 | 78 |
| `PIPE_CUTS` = 2 | 72 |
| `PIPE_CUTS` = 0 | 70 |
| P = 1 | 1038 |
| P = 32 | 46 |

#### Mutation results

| mutant | outcome |
|---|---|
| `index_table(r)` read unreversed | killed — stub: "issue 0: row source is slot 31, rank 0 is slot 0" |
| SORT waits `C − 1` | killed — stale `index_table`, same stub assertion |
| DRAIN lasts `L`, not `L + 1` | killed — "done after only 76 edges; faster than T = 78" |
| row applied without the `kept` test | killed — `valid_mask` trace mismatch at rank 1 |
| column groups swapped in the row merge | killed — `valid_mask` trace mismatch at rank 0 |
| lane-latency consistency check disabled | killed — "mistimed lanes went undetected" |
| `kept` reads the next row's index | killed — `keep_mask` trace mismatch at rank 18 of `all_equal` |
| resolve does not clear `valid_mask(idx_r)` when not kept | **survived** |

**The survivor is an equivalent mutant, not a blind spot.** `kept` *is* `valid_mask(idx_r)`,
so when a box is not kept its own bit is already 0, and clearing it changes nothing. The
"always clear `valid_mask(idx_r)`" in plan.md's resolve step is redundant as a matter of logic.
When the box is kept it is redundant a second time, because every row carries its own diagonal
bit. It is kept in the RTL as cheap insurance against a lane that ever fails to suppress itself.
The kept-index mutant replaced it as the check on that part of resolve. Note that it was caught
only mid-batch, at rank 18: the per-rank trace check was doing work the final mask alone could
not.

#### Measured

| LUT | FF | DSP | latches | Fmax |
|---|---|---|---|---|
| 382 (1.8%) | 300 | 0 | 0 | 175.0 MHz, WNS +4.286 ns |

The critical path is `cnt` → `row_src`, the `index_table` read. In the integrated design that
path continues into the row-source mux and lane stage 1, which is the C4 risk recorded in M2.

---

### C2 — `box_store`, the payload and area registers                   2026-09-24

`src/components/box_store.vhd`: 32 × 64 b records and 32 × 24 b areas, all in registers, with
three read sides:

- the 21-bit sort keys (`score & not index`) for `bitonic32`;
- the row-source record and area, selected by `row_src`;
- the P lane candidates, lane `j` reading slot `j + col_grp·P`.

The areas are computed on write by one shared multiplier (clamp, then multiply) and land one
edge after their record. `settled` is low while an area is in flight.

**Vector set:** `models/nms/vectors.py` now also writes `<case>.areas`, the model's clamped area
per slot. `test_vectors` checks it against `model.box_area` and against the committed copy. The
testbench reads it rather than computing areas, so it cannot share a clamp mistake with the RTL.

**`tb_box_store`** writes every case back to back (so each case overwrites the last), in a
scrambled slot order (stride 7). Even cases write on consecutive cycles and odd cases with idle
gaps. It pins the area latency from both sides and checks every key, every `row_src` and every
column group. It passes at P = 16, 1 and 32, with the latency pinned in 18 of 20 cases.

#### Mutation results

| mutant | outcome |
|---|---|
| width clamp removed | killed — `degenerate`: "row_src 4 area 242760, model says 0" |
| area written to the undelayed `waddr` | killed — back-to-back writes land in the wrong slot |
| sort key index not inverted | killed — "key 0 = 960000, model says 960031" |
| candidate striping `j·G + g` instead of `j + g·P` | killed — **at P = 16 only** |
| `settled` ignores the pipeline | killed — "settled high with an area still in flight" |
| height computed from `x` | killed — `all_survive` area mismatch |
| width clamp uses `>=` | **survived — equivalent**: at `a = x` both give width 0 |

The striping mutant is worth noting. At P = 1 and P = 32 the two formulas coincide, because
there is only one lane or only one column group, so only the P = 16 sweep point can
distinguish them. That is a concrete reason the sweep includes the shipped configuration and
not just the extremes.

#### Measured

| LUT | FF | DSP | latches | Fmax |
|---|---|---|---|---|
| 1,602 (7.7%) | 2,822 | 1 | 0 | 136.0 MHz, WNS +2.645 ns |

2,822 FF is exact: 2,048 payload bits, 768 area bits and 6 pipeline bits. The single DSP is the
shared area multiplier, with both operand registers absorbed into it. The critical path is that
DSP writing through the 32-way area decode.

**The row mux answers the open C4 question.** Timed alone, `row_src` → `row_rec` is 3.951 ns.
Added to `nms_ctrl`'s 5.714 ns and the lane's 7.161 ns stage 1, the path wired directly is
≈ 16.8 ns. C4 therefore needs two register stages (selects, then payloads), giving
`LANE_LATENCY = 6` and T = 80, with no change to `nms_ctrl` ([results.md](results.md) §5).
