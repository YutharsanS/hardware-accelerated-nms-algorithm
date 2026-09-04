# Hardware-accelerated NMS with a bitonic sorting network

Non-Maximum Suppression for 32 bounding boxes, in VHDL, on a **Digilent Basys 3**
(Xilinx XC7A35T-1CPG236C) at 100 MHz. A bitonic sorting network ranks the boxes by
confidence, 16 parallel IoU lanes evaluate every pair, and a resolve loop produces a
32-bit keep mask.

**Core latency is `N²/P + L + C + 2` = 72 cycles = 0.72 µs at P = 16 — an equality, not a
bound.** No term depends on the data, so worst case equals best case for every input.
Everything is verified **bit-exactly** against a Python golden model: a single 32-bit
equality, no tolerance band.

This file is the general guide — environment setup, simulation, synthesis, board bring-up.
Read [Scope and honest limits](#scope-and-honest-limits) before quoting any performance
figure.

---

## Contents

1. [Install the toolchain](#1-install-the-toolchain)
2. [Simulate](#2-simulate)
3. [Waveforms](#3-waveforms)
4. [The golden model and test vectors](#4-the-golden-model-and-test-vectors)
5. [Synthesis and implementation](#5-synthesis-and-implementation)
6. [Program the board](#6-program-the-board)
7. [Talk to it from the host](#7-talk-to-it-from-the-host)
8. [Layout](#layout) · [Conventions](#conventions) · [Status](#status) · [Limits](#scope-and-honest-limits)

Sections 5–7 describe the full intended flow. Where a file has not been written yet the
text says so — nothing below is a command that silently does not exist.

---

## 1. Install the toolchain

| tool | needed for | verified here |
|---|---|---|
| [GHDL](https://github.com/ghdl/ghdl) | analyse, elaborate, simulate | 4.1.0, mcode backend |
| [uv](https://docs.astral.sh/uv/) | Python env, golden model, lint | 0.11.19 / Python 3.12 |
| [GTKWave](https://gtkwave.sourceforge.net/) | waveform viewing | any |
| Vivado ML Standard | synthesis, timing, bitstream | 2026.1 — **see §5** |
| Basys 3 board | sections 6 and 7 | — |

GHDL, GTKWave and `make` come from your package manager:

```bash
sudo apt install ghdl gtkwave build-essential      # Debian / Ubuntu
```

Then, from the repository root:

```bash
uv sync                                                   # creates .venv/
uv run nbstripout --install --attributes .gitattributes   # once per clone
make test                                                 # confirm it all works
```

`nbstripout` strips notebook outputs at commit time. The filter lives in your local
`.git/config`, so it is not committed and **every contributor runs that line once**.
Check it with `uv run nbstripout --status`.

`make test` is the single command that gates everything: ruff, the full pytest suite, and
every VHDL testbench. About 35 seconds, no Vivado required. Run it after every change.

---

## 2. Simulate

```bash
make help                 # list targets
make test                 # lint + pytest + every testbench     <- the standing regression
make tb_cas               # one testbench: analyse, elaborate, run
make tb_bitonic32         # runs twice, at PIPE_CUTS = 0 and 2
make tb_params            # constants and their derivations
make lint                 # ruff format --check + ruff check
make clean                # remove build/ and __pycache__
```

Useful overrides:

```bash
make test WARN=-Wall                  # drop --warn-error for one run
make tb_cas STOP_TIME=1ms             # tighten the hang safety net
make tb_cas GHDL=/opt/ghdl/bin/ghdl   # a different GHDL build
```

Every testbench is **self-checking** and **terminates on its own**, ending with
`report "PASS"`. The `--stop-time` in the Makefile is a hang safety net only — a run that
needs it to finish is a failure, and a missing `PASS` line is itself a failure signal.

**Run from the repository root.** Testbenches read `models/data/vectors/`, and the path
arrives through a `VECTOR_DIR` generic whose default assumes that working directory.
From anywhere else, pass it explicitly:

```bash
ghdl -r --std=08 --workdir=build tb_bitonic32 -gVECTOR_DIR=/abs/path/to/vectors/
```

### Adding a module or a testbench

- A new `test/tb_*.vhd` is picked up automatically and gets its own `make` target.
- A new `src/**/*.vhd` must be **added to `RTL` in `scripts/Makefile`, in dependency
  order** — GHDL analyses in the order given, and `make analyse` refuses to run while any
  RTL file is unlisted rather than guessing a position for it.
- To run one testbench in several configurations, add a `SWEEP_tb_<name>` line listing the
  extra `ghdl -r` flags per run. `SWEEP_tb_bitonic32 = -gPIPE_CUTS=0 -gPIPE_CUTS=2` is the
  worked example.

---

## 3. Waveforms

The Makefile does not dump waveforms: they are megabytes per run, and the testbenches
self-check, so nothing needs eyeballing to know whether a gate passed. Ask for one when
you actually want to look at signals:

```bash
make tb_bitonic32                                  # analyse + elaborate first
ghdl -r --std=08 --workdir=build tb_bitonic32 \
     -gPIPE_CUTS=2 --wave=wave.ghw
gtkwave wave.ghw
```

`.ghw`, not `.vcd` — it preserves VHDL composite types, and this design is full of arrays
of `unsigned`. `wave.ghw` is gitignored.

---

## 4. The golden model and test vectors

```bash
uv run pytest -q                          # the whole Python suite
uv run python -m models.nms params        # print the frozen constants and check them
uv run python -m models.nms vectors       # regenerate models/data/vectors/
uv run python -m models.nms               # both
jupyter lab models/golden-model.ipynb     # the algorithm, explained
```

`models/nms/model.py` implements NMS **twice** on purpose: `nms_sequential` (the textbook
loop — the authority on what NMS means) and `nms_allpairs` (the structure the RTL
implements). A test asserts they agree over 20,000 adversarial batches. Without both, the
RTL would only ever be compared against a model that shares its restructuring, and a shared
misconception would pass silently.

The vector files are **committed**, so a clean checkout can run the testbenches
immediately. If you change the model or add a case, regenerate them — `test_vectors.py`
fails when the committed files are stale, so the RTL can never be checked against
expectations the model no longer produces.

Cases live in `models/nms/batches.py`. Adding one there puts it in `cases.txt`, which the
VHDL testbenches iterate, so **new cases reach the RTL gates without editing any VHDL**.

`src/components/nms_pkg.vhd` and `models/nms/params.py` hold the same 40 constants on the
two sides of the build, and they cannot drift: `tb_params` reports every one of them via
`integer'image` and `models/nms/test_params_agree.py` runs GHDL and compares its output
against the Python values, name by name, in both directions.

**`docs/architecture.md` is normative.** Change it first; the two constant files mirror it.

---

## 5. Synthesis and implementation

Vivado is **not** on `PATH`, so every session starts with:

```bash
source ~/Vivado/2026.1/Vivado/settings64.sh
```

Confirm the toolchain can see the part before anything else — 30 seconds now saves an
afternoon later:

```bash
cat > /tmp/part_probe.tcl <<'EOF'
puts "PARTS: [llength [get_parts -quiet xc7a35tcpg236-1]]"
puts "ALL:   [llength [get_parts -quiet]]"
exit
EOF
vivado -mode batch -nolog -nojournal -notrace -source /tmp/part_probe.tcl | grep -E "PARTS|ALL"
```

Expect `PARTS: 1`.

> **Known blocker on this machine: it prints `PARTS: 0` and `ALL: 0`.** The install
> enumerates zero parts for *every* device family, so there is nothing to synthesise for,
> and `create_project -part xc7a35tcpg236-1` fails with `Coretcl 2-106`. The full
> diagnosis — everything ruled out, including the licence, the device data and the shared
> libraries — plus the reinstall procedure is **M1** in [docs/plan.md](docs/plan.md). This
> blocks area and timing measurement only. **Every correctness gate runs on GHDL and is
> unaffected.**

Once the probe passes:

```bash
make synth MOD=cas          # or bitonic32, iou_lane, nms_top
```

This expects `scripts/synth.tcl`, which **has not been written yet** — it lands with the
first area gate (B2.2). It will use plain `read_vhdl` with no `-vhdl2008`, per the language
policy, and run `synth_design` for `xc7a35tcpg236-1` under a 100 MHz constraint, reporting
`report_utilization` and `report_timing_summary`.

What each synthesis gate is checking, so a number can be judged rather than just recorded:

| module | expected | why it matters |
|---|---|---|
| `cas` | 0 DSP, LUT within 2× of `8 + 2·⌈W/2⌉` | a wild figure means the coding style is defeating inference — fix it here, not three modules later |
| `bitonic32` | meets 100 MHz at `PIPE_CUTS=2`, ≤ 9,000 LUT | **the load-bearing gate.** Cuts are placed by sub-stage count, which is not the same as by delay |
| `iou_lane` | **exactly 1 DSP**, ≤ 300 LUT | check the DSP count *first* — if it reports 0 the multiply went to LUTs and the LUT figure is meaningless |
| `nms_top` | the deliverable utilisation table, Fmax, and the `P` sweep | — |

**Read the synthesis warnings, not just the timing summary.** Inferred latches, unhandled
`case` branches and width mismatches are how a GHDL-clean design becomes wrong hardware.

If `bitonic32` misses timing, the documented fallbacks in order are: re-place the cuts from
the actual critical paths via the `CUT_AFTER` generic, then `PIPE_CUTS=3`, then a 50 MHz
core clock — which costs 0.72 → 1.44 µs and changes nothing that matters against 2.70 ms of
link time. Correctness is already verified at every `PIPE_CUTS` from 0 to 15, so whichever
value the timing report demands is known-good.

### Optional fast area loop (Yosys)

Seconds per iteration instead of minutes, for *relative* comparisons only:

```bash
source /opt/oss-cad-suite/environment
yosys -m ghdl -p "ghdl --std=08 src/components/nms_pkg.vhd src/components/cas.vhd \
                        src/components/bitonic32.vhd -e bitonic32; \
                  synth_xilinx -family xc7 -dsp; stat"
```

Use the suite's own GHDL, not `/usr/bin/ghdl` — the plugin needs the synth-enabled build.
Yosys lands 10–30% off Vivado and can miss DSP inference entirely, so trust **ratios**
(21-bit vs 64-bit keys, sorter vs argmax tree) and never absolute pass/fail.

---

## 6. Program the board

Three one-time setup steps, none of which are automatable.

**Cable drivers.** Without them the Hardware Manager cannot see the board over JTAG, which
looks exactly like a dead board:

```bash
cd ~/Vivado/2026.1/data/xicom/cable_drivers/lin64/install_script/install_drivers
sudo ./install_drivers
```

**Serial port permissions.** Not being in `dialout` produces a permission error that looks
like a missing device:

```bash
sudo usermod -aG dialout $USER      # then log out and back in
id -nG | tr ' ' '\n' | grep -x dialout       # verify
```

**Constraints.** `deployment/` is empty; the XDC is hand-written at B7.2 from Digilent's
`Basys-3-Master.xdc` (`Digilent/digilent-xdc` on GitHub) rather than from memory — a wrong
pin is a silent failure. The pins needed are clock **W5**, `RsRx` **B18**, `RsTx` **A18**,
BTNC **U18**; confirm each against that file. Board files are not required, since the
design targets the raw part.

Then plug in the board, switch it on, and check the host sees it:

```bash
lsusb | grep -i ftdi      # expect an FTDI FT2232
ls /dev/ttyUSB*           # expect at least one node
```

Program via Vivado's **Hardware Manager → Open Target → Auto Connect → Program Device**,
selecting the `.bit` from implementation. The first bring-up test needs no host attached:
the 16 user LEDs show the low 16 bits of `keep_mask` for a hard-coded vector.

---

## 7. Talk to it from the host

`scripts/host_nms.py` is written at B7.3 and is not in the repository yet. The protocol it
speaks is frozen in [docs/architecture.md](docs/architecture.md) §3: **264 bytes in, 6 out**,
every multi-byte field most-significant byte first.

```
host -> FPGA   magic 0xA5 0x5A | 32 records x 8 B | present_mask 4 B | seq 1 B | crc8 1 B
FPGA -> host   status 1 B | seq echoed 1 B | keep_mask 4 B
```

Bit *i* of `keep_mask` means "the *i*-th box I sent survived" — indexed by **arrival
order**, so the host never reorders anything and a testbench check is one 32-bit equality.

One trap that would invalidate every latency measurement while the arithmetic still looked
right: **the FTDI driver's latency timer defaults to 16 ms**, which alone exceeds half a
frame budget and punishes exactly the short-read case a 6-byte reply presents.

```bash
echo 1 | sudo tee /sys/bus/usb-serial/devices/ttyUSB0/latency_timer
cat /sys/bus/usb-serial/devices/ttyUSB0/latency_timer      # verify: 1
```

**It resets whenever the board is re-plugged**, so the host script sets it (or warns) at
startup rather than trusting that someone did it once. Measure round-trip latency before
and after and report both — the delta is the difference between a measured number and a
theoretical one.

---

## Layout

```text
src/components/     datapath blocks: nms_pkg, cas, bitonic32
src/pipeline/       top-level integration (not yet written)
test/               self-checking VHDL testbenches, tb_ prefixed
models/nms/         golden model, frozen constants, vector generator, pytest suite
models/gs/          3D Gaussian Splatting analysis — the extension path (Phase 0, measured)
models/data/        generated test vectors, committed
scripts/            the real Makefile; the root Makefile just includes it
deployment/         XDC constraints and synthesis scripts (empty until B7.2)
docs/               see below
```

| document | what it is |
|---|---|
| [docs/architecture.md](docs/architecture.md) | **normative.** Record format, wire protocol, datapath widths, predicate, sort key, Basys 3 reference |
| [docs/plan.md](docs/plan.md) | the gated build sequence, design rationale, and the manual steps only a human can do |
| [docs/build_log.md](docs/build_log.md) | one entry per gate: what was built, what was measured, what failed on the way |
| [docs/development_guide.md](docs/development_guide.md) | GHDL/GTKWave background, `uv` and `nbstripout` setup |
| [docs/NMS.md](docs/NMS.md) | narrative introduction to the algorithm |
| [docs/phase0_findings.md](docs/phase0_findings.md) | the 3DGS measurements behind the extension path |
| [docs/README.md](docs/README.md) | repository structure and the `/commit` · `/pr` Claude Code skills |

> The manual GHDL walkthrough in `development_guide.md` predates this Makefile, and its
> "Template Makefile" section describes a file that no longer resembles `scripts/Makefile`.
> Treat this README as authoritative for commands and that guide as background on the tools.

---

## Conventions

- **Synthesisable RTL in the VHDL-93 subset; testbenches in VHDL-2008** for `HREAD`.
  Everything is analysed at `--std=08`, which accepts both. Vivado's VHDL-2008 synthesis
  support is a documented subset and the RTL needs no 2008 feature, so it uses none.
- **Combinational logic as concurrent assignments, never combinational processes.**
  Processes are clocked only, with `(clk)` as the whole sensitivity list — which makes an
  incomplete sensitivity list *unrepresentable* rather than merely discouraged. GHDL
  analyses with `-Wall --warn-error`, so any new warning stops the build.
- The UART RX pin is the only asynchronous input: 2-flop synchroniser, no exceptions.
- Testbenches are `tb_`-prefixed, self-checking, and end in `report "PASS"`.
- Files `snake_case`; constants `UPPER_SNAKE_CASE`; Python `snake_case` with Google
  docstrings, formatted and linted by `ruff`.
- Branches `feature/…` or `fix/…`; commits follow
  [Conventional Commits](https://www.conventionalcommits.org/); a PR and one approval
  before merge. `/commit` and `/pr` automate the format.

---

## Status

| stage | gate | state |
|---|---|---|
| B0 | toolchain and Python environment | done |
| B1 | frozen constants, golden model, 20 vector cases, CPU baseline | done |
| B2.1 | `nms_pkg`, `cas`, exhaustive testbench, anti-drift check | done |
| B2.2 | `cas` area in Vivado | blocked on M1 |
| B3.1 | `bitonic32` + testbench, verified at every `PIPE_CUTS` 0–15 | done |
| B3.2 | sorter timing at 100 MHz — the load-bearing gate | blocked on M1 |
| B4 | `iou_lane` | next |
| B5 | `box_store`, the all-pairs FSM | not started |
| B6 | `nms_top`, full-core synthesis | not started |
| B7 | UART framing, XDC and bitstream, host script | not started |

A step is done when its gate passes **and** its `build_log.md` entry is written. The
measured numbers are themselves deliverables, and recording them when they are produced is
the only way they survive.

---

## Scope and honest limits

Stated here rather than buried, because they bound every claim above.

- **The UART is a test harness, not the datapath.** Transport is 2.70 ms at 1 Mbaud against
  0.72 µs of compute. End to end, doing this NMS on the host CPU is faster. What the design
  demonstrates is **core latency and determinism**, not a system speedup. An on-chip
  AXI-Stream interface is where such a block belongs, and that is not built.
- **Name the processor class whenever you say "faster".** Measured on the development
  machine, the core is 115–810× faster than Python, ~80× faster than a Cortex-M7, and
  roughly **at parity with hand-tuned AVX2** on a 3 GHz x86. "Faster than any processor" is
  false — a 100 MHz fabric cannot out-run a 30×-clock superscalar SIMD core at N = 32.
- **N = 32 is a hard limit, not a starting point.** The combinational network is Θ(N log²N):
  N = 64 needs 672 CAS ≈ 20,160 LUT and does not fit this device at all. A folded sorter is
  the path past it.
- **Single class.** Real NMS runs per class; this record carries no class ID.
- **The host must not sort.** Reaching 32 boxes from a real detector's output requires
  host-side selection, and if that selection sorts, the premise collapses. The sanitisation
  contract uses a Θ(n) confidence threshold and never a sort.

[docs/plan.md](docs/plan.md) Part 1e and Part 6 carry the measurements behind each of these,
including the several that contradicted earlier versions of this design.

---

## Licence

MIT — see [LICENSE](LICENSE).
