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
