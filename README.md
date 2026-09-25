# Hardware-accelerated NMS with a bitonic sorting network

Non-Maximum Suppression for 32 bounding boxes, in VHDL, on a **Digilent Basys 3**
(Xilinx XC7A35T-1CPG236C) at 100 MHz. A bitonic sorting network ranks the boxes by
confidence, 16 parallel IoU lanes evaluate every pair, and a resolve loop produces a
32-bit keep mask. A UART link carries batches in and results out.

**Core latency is `N²/P + L + I + C + 2` = 80 cycles = 0.80 µs at P = 16 — an equality, not
a bound.** No term depends on the data, so worst case equals best case for every input.
Everything is verified **bit-exactly** against a Python golden model, a single 32-bit
equality with no tolerance band, under two independent simulators. The full board design
meets 100 MHz on the real part, and its bitstream is built
([docs/results.md](docs/results.md) §5).

This file is the general guide: environment setup, simulation, synthesis, board bring-up.
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

---

## 1. Install the toolchain

| tool | needed for | verified here |
|---|---|---|
| [GHDL](https://github.com/ghdl/ghdl) | analyse, elaborate, simulate | 4.1.0, mcode backend |
| [uv](https://docs.astral.sh/uv/) | Python env, golden model, lint, host program | Python 3.12 |
| [GTKWave](https://gtkwave.sourceforge.net/) | waveform viewing | any |
| Vivado ML Standard | synthesis, timing, bitstream, programming, xsim | 2026.1 — **see §5** |
| Basys 3 board | sections 6 and 7 | — |

GHDL, GTKWave and `make` come from your package manager:

```bash
sudo apt install ghdl gtkwave build-essential      # Debian / Ubuntu
```

Then, from the repository root:

```bash
uv sync                                                   # creates .venv/ (includes pyserial)
uv run nbstripout --install --attributes .gitattributes   # once per clone
make test                                                 # confirm it all works
```

`nbstripout` strips notebook outputs at commit time. The filter lives in your local
`.git/config`, so it is not committed, and **every contributor runs that line once**.
Check it with `uv run nbstripout --status`.

---

## 2. Simulate

There are **two test tiers**. Both run every check; they differ only in volume
([docs/build_log.md](docs/build_log.md) M3).

```bash
make test         # what CI runs on every pull request: ruff, pytest, every testbench  (~1.5 min)
make test-full    # the same checks at full volume                                    (~12 min)
```

**Run `make test-full` before merging anything that touches the datapath**: the core RTL, the
testbenches or the golden model. It differs from `make test` in four places:

| check | `make test` | `make test-full` |
|---|---|---|
| `tb_nms_core`, shipped configuration | 20 curated + 150 random batches | 20 curated + 1,000 random |
| `tb_nms_core`, 4 other configurations | 20 curated each | 1,000 random each, plus P = 1 |
| `tb_nms_top`, at the pins | 2 frames + all directed scenarios | 20 frames + all directed |
| pytest model-vs-model sweep | 2,000 batches | 20,000 batches |

Other targets:

```bash
make help                 # list targets
make tb_nms_core          # one testbench: analyse, elaborate, run its sweep
make tb_nms_core FULL=1   # the same testbench at full volume
make lint                 # ruff format --check + ruff check
make xsim TB=tb_nms_core  # the same testbench under Vivado xsim, as a second simulator
make clean                # remove build/
```

| testbench | what it proves |
|---|---|
| `tb_cas`, `tb_bitonic32` | the compare-and-swap unit and the 32-key sorter, at every `PIPE_CUTS` swept |
| `tb_iou_lane` | the IoU predicate, bit-exact on 10,000 hostile pairs, at `T_INT` 128 and 255 |
| `tb_box_store` | the payload/area registers and every read port |
| `tb_nms_ctrl` | the control FSM, rank by rank against the model's traces, latency pinned from both sides |
| `tb_nms_core` | the whole compute core, nothing stubbed, bit-exact on every batch, across configurations |
| `tb_uart`, `tb_frame_rx` | the serial link, and the frame parser including its busy / CRC-reject paths |
| `tb_nms_top` | the board design at its pins, bit by bit: replies, LEDs, CRC reject, resync, timeout, reset |
| `tb_params` | the frozen constants, checked against the Python side |

Every testbench is **self-checking** and **terminates on its own**, ending with
`report "PASS"`. The `--stop-time` in the Makefile is a hang safety net only.

**Run from the repository root.** Testbenches read `models/data/`, and the paths arrive
through generics whose defaults assume that working directory.

### Adding a module or a testbench

- A new `test/tb_*.vhd` is picked up automatically and gets its own `make` target.
- A new `src/**/*.vhd` must be **added to `RTL` in `scripts/Makefile`, in dependency
  order**, and to the file lists in `scripts/synth.tcl` and `scripts/impl.tcl`. GHDL analyses
  in the order given, and `make analyse` refuses to run while any RTL file is unlisted.
- To run a testbench in several configurations, add a `SWEEP_tb_<name>` line: one word per
  run, several `-g` flags for one run joined by commas. A `SWEEP_FULL_tb_<name>` line, if
  present, replaces it under `make test-full`.

---

## 3. Waveforms

The Makefile does not dump waveforms: they are megabytes per run, and the testbenches
self-check. Ask for one when you actually want to look at signals:

```bash
make tb_bitonic32                                  # analyse + elaborate first
ghdl -r --std=08 --workdir=build tb_bitonic32 -gPIPE_CUTS=2 --wave=wave.ghw
gtkwave wave.ghw
```

Use `.ghw`, not `.vcd`: it preserves VHDL composite types, and this design is full of arrays of
`unsigned`. `wave.ghw` is gitignored.

---

## 4. The golden model and test vectors

```bash
uv run pytest -q                          # the whole Python suite
uv run python -m models.nms params        # print the frozen constants and check them
uv run python -m models.nms vectors       # regenerate models/data/vectors/
uv run python -m models.nms random        # regenerate the random batches (make does this itself)
jupyter lab models/golden-model.ipynb     # the algorithm, explained
```

`models/nms/model.py` implements NMS **twice**, on purpose:
- `nms_sequential` is the textbook loop, and the authority on what NMS means.
- `nms_allpairs` has the structure the RTL implements.

A test asserts they agree (2,000 hostile batches in `make test`, 20,000 in `make test-full`).
Without both, the RTL would only ever be compared against a model that shares its
restructuring, and a shared misconception would pass silently.

**Committed vectors** (`models/data/vectors/`), so a clean checkout can run the testbenches
immediately:
- per case: records, masks, sort keys, areas, rank order and the per-rank resolve trace;
- `frames.txt`: every case as a complete 264-byte wire frame plus its expected reply, built
  with the same encoder the host program uses (`models/nms/wire.py`).

`test_vectors.py` fails when the committed files are stale, so the RTL can never be checked
against expectations the model no longer produces. Cases live in `models/nms/batches.py`;
adding one there reaches every VHDL testbench without editing any VHDL.

**Random batches** (`models/data/random/`, gitignored) are 1,000 hostile batches that
`tb_nms_core` reads. They are reproducible from their seed and regenerated automatically by
`make` whenever the model changes.

`src/components/nms_pkg.vhd` and `models/nms/params.py` hold the same frozen constants on the
two sides of the build, and they cannot drift: `models/nms/test_params_agree.py` runs GHDL and
compares every constant by name, in both directions.

**`docs/architecture.md` is normative.** Change it first; the two constant files mirror it.

---

## 5. Synthesis and implementation

Vivado is **not** on `PATH`, so every session starts with:

```bash
source ~/Vivado/2026.1/Vivado/settings64.sh
```

Then:

```bash
make synth MOD=bitonic32 GENERICS="PIPE_CUTS=8"   # out of context: one module's area and timing
make impl                                         # nms_top on the real pins, plus a bitstream
make xsim TB=tb_nms_core                          # second-simulator cross-check
```

- **`make synth`** runs out of context through `route_design` under a 100 MHz constraint,
  with every data port constrained, so input and output paths count in the timing. Reports go
  to `build/synth/<module>/`.
- **`make impl`** is not out of context. It implements `nms_top` with
  `deployment/basys3.xdc` on real I/O, and writes `build/impl/nms_top.bit` **only if setup and
  hold both pass**.

Measured, from [docs/results.md](docs/results.md):

| design | LUT | FF | DSP | timing at 100 MHz |
|---|---|---|---|---|
| `bitonic32`, `PIPE_CUTS = 8` | 8,112 | 5,376 | 0 | 116.7 MHz |
| `iou_lane`, one lane | 109 | 101 | 2 | 139.6 MHz |
| `nms_core`, whole compute core | 12,384 (59.5%) | 9,616 | 33 | WNS +0.202 ns |
| **`nms_top`, board design** | **12,570 (60.4%)** | **9,979** | **33** | **WNS +0.200 ns, WHS +0.043 ns** |

**The timing margin is thin, and the sorter is the critical path.** If a change makes
`make impl` fail timing, these are the documented remedies, cheapest first
([docs/results.md](docs/results.md) §5):
1. re-place the sorter's register cuts with the `CUT_AFTER` generic;
2. `PIPE_CUTS = 9`, costing one cycle;
3. duplicate the payload register that fans out to the 16 lanes;
4. a third issue register, costing one cycle.

Correctness is verified at every `PIPE_CUTS` from 0 to 15.

**Read the synthesis warnings, not just the timing summary.** Inferred latches, unhandled
`case` branches and width mismatches are how a GHDL-clean design becomes wrong hardware.

---

## 6. Program the board

Three one-time setup steps.

**Cable drivers.** Without them the hardware manager cannot see the board over JTAG, which
looks exactly like a dead board:

```bash
cd ~/Vivado/2026.1/data/xicom/cable_drivers/lin64/install_script/install_drivers
sudo ./install_drivers
```

**Serial port permissions.** Not being in `dialout` produces a permission error that looks
like a missing device:

```bash
sudo usermod -aG dialout $USER               # then log out and back in
id -nG | tr ' ' '\n' | grep -x dialout       # verify
```

**Constraints.** `deployment/basys3.xdc` is hand-written from Digilent's `Basys-3-Master.xdc`
(`Digilent/digilent-xdc` on GitHub), because Digilent's board files do not exist for Vivado
2026.1. **Check every pin in it against that master file before the first programming.** A
wrong pin fails silently. The pins are clock **W5**, `RsRx` **B18**, `RsTx` **A18**, BTNC
**U18** and the 16 LEDs.

Then plug in the board, switch it on, and program it:

```bash
lsusb | grep -i ftdi      # expect an FTDI FT2232
ls /dev/ttyUSB*           # usually ttyUSB0 (JTAG) and ttyUSB1 (UART), see below
ls /dev/serial/by-id/     # stable names; the UART is the one ending if01-port0
make impl                 # if build/impl/nms_top.bit is not already there
make program              # loads it over JTAG
make host                 # then check it: --selftest, see §7
```

The FT2232HQ's JTAG interface usually appears as `ttyUSB0` and vanishes while Vivado's
hardware server holds it; the UART stays, usually as `ttyUSB1`. Another USB serial device can
shift the numbering, so the `/dev/serial/by-id/` name is the reliable one.

`make program` loads the configuration RAM only, so the design is gone at power-off and the
board's flash is untouched. The Vivado GUI works too: **Hardware Manager → Open Target → Auto
Connect → Program Device**.

**What the board shows.** The 16 user LEDs show the low 16 bits of the last batch's
`keep_mask`. They are dark after programming, because no batch has run yet. **BTNC** is reset:
it aborts a batch in progress and clears the LEDs.

---

## 7. Talk to it from the host

The host program is `models/nms/host.py`. It runs on the PC plugged into the board's
micro-USB port and talks to the UART, which is **`/dev/ttyUSB1`**; `ttyUSB0` is the JTAG
interface Vivado uses. The wire protocol is frozen in
[docs/architecture.md](docs/architecture.md) §3: **264 bytes in, 6 out**, every multi-byte
field most-significant byte first.

```
host -> FPGA   magic A5 5A | 32 records x 8 B | present_mask 4 B | seq 1 B | crc8 1 B
FPGA -> host   status 1 B | seq echoed 1 B | keep_mask 4 B (zero unless status = 0x00)
```

Run these in order the first time the board is programmed. `make host` wraps the same
program: `make host` runs `--selftest`, `make host ARGS="--crc-test --random 1000"` runs any
other modes, and `PORT=...` picks the port.

```bash
uv run python -m models.nms.host --selftest       # all 20 committed frames
uv run python -m models.nms.host --crc-test       # one deliberately corrupted frame
uv run python -m models.nms.host --random 1000    # hostile batches vs the golden model
uv run python -m models.nms.host --latency 500    # round-trip time distribution
```

| mode | a pass looks like |
|---|---|
| `--selftest` | `selftest: 20/20 frames answered exactly`: every reply byte matches the model |
| `--crc-test` | `crc-test: pass -- status 0x01`: the frame was rejected, not computed |
| `--random N` | `random: N/N batches bit-exact against the golden model` |
| `--latency N` | min / median / p99 / max round-trip time |

Add `--port` if the UART is not `ttyUSB1`. A `/dev/serial/by-id/…` path works and survives
replugging. The exit status is 0 when everything passed, 1 on any failure or timeout, and 2
when the port cannot be opened.

**The FTDI latency timer.** The driver defaults to 16 ms and holds a short read for that long,
which is exactly the 6-byte reply. It alone exceeds half a frame budget. The host sets it to
1 ms at startup, or prints the `sudo` command when it lacks permission. It resets whenever the
board is re-plugged:

```bash
echo 1 | sudo tee /sys/bus/usb-serial/devices/ttyUSB1/latency_timer
```

Measure latency both ways: `--latency 500 --leave-latency-timer` at the 16 ms default, then
`--latency 500` at 1 ms. The difference is the gap between a measured number and a
theoretical one ([docs/plan.md](docs/plan.md) Part 1b).

**When `--selftest` does not pass:**

| symptom | likely cause |
|---|---|
| `only 0 of 6 reply bytes before the timeout` | wrong port (`ttyUSB0` is JTAG); board not programmed; BTNC held down; wrong `--baud` |
| `cannot open /dev/ttyUSB1: Permission denied` | not in `dialout` (§6) |
| every frame gets status `0x01` | bytes arriving corrupted: baud mismatch, or a wrong UART pin in the XDC |
| replies correct but the LEDs stay dark | an LED pin wrong in the XDC |
| `reply answers seq …, expected …` | a late reply from an earlier timeout; rerun, and raise `--timeout` if it repeats |

---

## Layout

```text
src/components/     datapath and link blocks: nms_pkg, cas, bitonic32, iou_lane, box_store,
                    nms_ctrl, uart_rx, uart_tx, frame_rx, frame_tx
src/pipeline/       integration: nms_core (the compute core), nms_top (the board top)
test/               self-checking VHDL testbenches, tb_ prefixed
models/nms/         golden model, frozen constants, vectors, wire format, host program, pytest
models/gs/          3D Gaussian Splatting analysis: future work (Phase 0, measured)
models/data/        committed test vectors; random/ is generated and gitignored
scripts/            Makefile (the root Makefile includes it), synth.tcl, impl.tcl, program.tcl
deployment/         basys3.xdc
docs/               see below
```

| document | what it is |
|---|---|
| [docs/architecture.md](docs/architecture.md) | **normative.** Record format, wire protocol, datapath widths, predicate, sort key, latency, Basys 3 reference |
| [docs/plan.md](docs/plan.md) | design rationale, the build sequence, decisions and their evidence |
| [docs/fsm_design.md](docs/fsm_design.md) | the control FSM's cycle-level design, reviewed before any RTL |
| [docs/results.md](docs/results.md) | every measured area and timing figure |
| [docs/build_log.md](docs/build_log.md) | one entry per step: what was built, measured, and what failed on the way |
| [docs/development_guide.md](docs/development_guide.md) | GHDL/GTKWave background, `uv` and `nbstripout` setup |
| [docs/NMS.md](docs/NMS.md) | narrative introduction to the algorithm |
| [docs/phase0_findings.md](docs/phase0_findings.md) | the 3DGS measurements behind the future-work path |
| [docs/README.md](docs/README.md) | repository structure and the `/commit` · `/pr` Claude Code skills |

> The manual GHDL walkthrough in `development_guide.md` predates `scripts/Makefile`. Treat
> this README as authoritative for commands, and that guide as background on the tools.

---

## Conventions

- **Synthesisable RTL in the VHDL-93 subset; testbenches in VHDL-2008** for `HREAD`.
  Everything is analysed at `--std=08`, which accepts both.
- **Combinational logic as concurrent assignments, never combinational processes.**
  Processes are clocked only, with `(clk)` as the whole sensitivity list. GHDL analyses with
  `-Wall --warn-error`, so any new warning stops the build.
- **Two asynchronous inputs, each through a 2-flop synchroniser:** the UART RX pin (inside
  `uart_rx`) and the BTNC reset button (in `nms_top`).
- Testbenches are `tb_`-prefixed, self-checking, and end in `report "PASS"`. New checks are
  **mutation-tested**: each testbench is shown to fail on deliberately broken RTL, and the
  results are logged in `build_log.md`.
- Files `snake_case`; constants `UPPER_SNAKE_CASE`; Python `snake_case` with Google
  docstrings, formatted and linted by `ruff`.
- Branches `feature/…` or `fix/…`. Commits follow
  [Conventional Commits](https://www.conventionalcommits.org/). A PR and one approval before
  merge. `/commit` and `/pr` automate the format.

---

## Status

Stage labels follow [docs/plan.md](docs/plan.md) Part 3.

| stage | what | state |
|---|---|---|
| A | frozen spec, integer golden model, vector generator, CPU baseline | done |
| B0–B5 | Vivado smoke test, `nms_pkg`, `cas`, `bitonic32`, sorter area and timing | done |
| C1 | `iou_lane` | done |
| C2 | `box_store` | done |
| C3 | `nms_ctrl`, the all-pairs control FSM | done |
| C4 | `nms_core` integration, bit-exact under GHDL and xsim, placed at 100 MHz | done |
| D1 | UART, frame layer, `nms_top`, XDC, implementation and bitstream | done |
| D2 | the `P` scaling curve and the `PIPE_CUTS` re-sweep with ports constrained | not started |
| D3 | host program (`models/nms/host.py`) and `make program` | **written; not yet run on a board** |
| — | **first hardware run**: `--selftest` on a real Basys 3, real USB latency | **next** |

A step is done when its gate passes **and** its `build_log.md` entry is written.

---

## Scope and honest limits

Stated here rather than buried, because they bound every claim above.

- **The UART is a test harness, not the datapath.** Transport is 2.70 ms at 1 Mbaud against
  0.80 µs of compute. End to end, doing this NMS on the host CPU is faster. What the design
  demonstrates is **core latency and determinism**, not a system speedup. An on-chip
  AXI-Stream interface is where such a block belongs, and that is not built.
- **Name the processor class whenever you say "faster".** Against Python the core is 45–600×
  faster (measured). It is ~40× faster than a Cortex-M7 and ~3.5× *slower* than hand-tuned
  AVX2 on a 3 GHz x86 (both estimates, [docs/plan.md](docs/plan.md) Part 1e). "Faster than any
  processor" is false.
- **N = 32 is a hard limit, not a starting point.** The combinational network is Θ(N log²N):
  N = 64 needs 672 CAS ≈ 20,160 LUT and does not fit this device at all. A folded sorter is
  the path past it.
- **Single class.** Real NMS runs per class; this record carries no class ID.
- **The host must not produce an ordering.** Reaching 32 boxes from a real detector's output
  needs host-side selection. The contract is a Θ(n) confidence threshold, then the top 32 by
  confidence if more survive, which is a selection and not a sort. Any other cap changes the
  result compared with true NMS ([docs/plan.md](docs/plan.md) Part 2).

[docs/plan.md](docs/plan.md) Part 1e and Part 6 carry the measurements behind each of these,
including the several that contradicted earlier versions of this design.

---

## Licence

MIT — see [LICENSE](LICENSE).
