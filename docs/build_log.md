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
