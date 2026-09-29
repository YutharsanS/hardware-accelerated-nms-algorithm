# User guide — build, verify, run and benchmark the NMS block

Everything needed to go from a clean checkout to a programmed Basys 3 and a benchmark table.
Run every command from the **repository root**: testbenches read `models/data/` through paths
that assume it.

1. [Install the toolchain](#1-install-the-toolchain)
2. [Simulate](#2-simulate)
3. [Waveforms](#3-waveforms)
4. [The golden model and test vectors](#4-the-golden-model-and-test-vectors)
5. [Synthesis and implementation](#5-synthesis-and-implementation)
6. [Program the board](#6-program-the-board)
7. [Talk to it from the host](#7-talk-to-it-from-the-host)
8. [Capture the latency on silicon](#8-capture-the-latency-on-silicon)
9. [Benchmark against software](#9-benchmark-against-software)
10. [Conventions and adding code](#10-conventions-and-adding-code)

---

## 1. Install the toolchain

| tool | needed for | verified here |
|---|---|---|
| [GHDL](https://github.com/ghdl/ghdl) | analyse, elaborate, simulate | 4.1.0, mcode backend |
| [uv](https://docs.astral.sh/uv/) | Python env, golden model, lint, host program, benchmarks | Python 3.12 |
| [GTKWave](https://gtkwave.sourceforge.net/) | waveform viewing | any |
| Vivado ML Standard | synthesis, timing, bitstream, programming, xsim, ILA | 2026.1 (BASIC licence) — **see §5** |
| Basys 3 board | §6–8 | — |

```bash
sudo apt install ghdl gtkwave build-essential              # Debian / Ubuntu
uv sync                                                    # creates .venv/
uv run nbstripout --install --attributes .gitattributes   # once per clone
make test                                                  # confirm it all works
```

`uv sync` installs the core dependencies and the `dev` group; `pyproject.toml` and `uv.lock` are
committed, `.venv/` is not. The benchmark libraries are optional extras (§9).

**`nbstripout`** strips cell outputs from `models/golden-model.ipynb` at commit time, via the
`filter=nbstripout` rule in `.gitattributes`, so re-running the notebook doesn't bloat the history.
The filter lives in your local `.git/config`, so **every contributor runs that line once**; check
it with `uv run nbstripout --status`. Your working copy still shows outputs.

---

## 2. Simulate

There are **two test tiers**. Both run every check; they differ only in volume.

```bash
make test         # what CI runs on every pull request: ruff, pytest, every testbench  (~1.5 min)
make test-full    # the same checks at full volume                                    (~12 min)
```

**Run `make test-full` before merging anything that touches the datapath** — the core RTL, the
testbenches or the golden model:

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
| `tb_nms_core` | the whole compute core, nothing stubbed, bit-exact on every batch, across configurations; T, load and settle pinned as equalities |
| `tb_uart`, `tb_frame_rx` | the serial link, and the frame parser including its busy / CRC-reject paths |
| `tb_nms_top` | the board design at its pins, bit by bit: replies, LEDs, CRC reject, resync, timeout, reset |
| `tb_params` | the frozen constants, checked against the Python side |

Every testbench is **self-checking** and **terminates on its own**, ending with `report "PASS"`.
The `--stop-time` in the Makefile is a hang safety net only.

---

## 3. Waveforms

The Makefile does not dump waveforms: they are megabytes per run, and the testbenches self-check.
Ask for one when you want to look at signals:

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

`models/nms/model.py` implements NMS **twice**, on purpose: `nms_sequential` is the textbook loop
and the authority on what NMS means; `nms_allpairs` has the structure the RTL implements. A test
asserts they agree. Without both, the RTL would only ever be compared against a model that shares
its restructuring, and a shared misconception would pass silently. The algorithm in plain terms is
in [nms_primer.md](design/nms_primer.md).

**Committed vectors** (`models/data/vectors/`) let a clean checkout run the testbenches at once:
per case, records, masks, sort keys, areas, rank order and the per-rank resolve trace; and
`frames.txt`, every case as a 264-byte wire frame with its expected reply, built by the same
encoder the host uses (`models/nms/wire.py`). `test_vectors.py` fails when the committed files are
stale. Cases live in `models/nms/batches.py`; adding one there reaches every VHDL testbench.

**Random batches** (`models/data/random/`, gitignored) are 1,000 hostile batches that
`tb_nms_core` reads, reproducible from their seed and regenerated by `make` whenever the model
changes.

`src/components/nms_pkg.vhd` and `models/nms/params.py` hold the same frozen constants on the two
sides of the build; `models/nms/test_params_agree.py` runs GHDL and compares every one by name.
**[architecture.md](design/architecture.md) is normative**: change it first; both files mirror it.

---

## 5. Synthesis and implementation

Vivado is **not** on `PATH`, so every session starts with:

```bash
source ~/Vivado/2026.1/Vivado/settings64.sh
```

```bash
make synth MOD=bitonic32 GENERICS="PIPE_CUTS=8"   # out of context: one module's area and timing
make impl                                         # nms_top on the real pins, plus a bitstream
make impl ILA=1                                   # the same, plus an on-chip logic analyser (§8)
make xsim TB=tb_nms_core                          # second-simulator cross-check
```

- **`make synth`** runs out of context through `route_design` under a 100 MHz constraint, with
  every data port constrained, so input and output paths count. Reports go to `build/synth/<module>/`.
- **`make impl`** implements `nms_top` with `deployment/basys3.xdc` on real I/O and writes
  `build/impl/nms_top.bit` **only if setup and hold both pass**. The ILA build goes to
  `build/impl_ila/` and never overwrites it.

Measured ([hardware.md](results/hardware.md)):

| design | LUT | FF | DSP | timing at 100 MHz |
|---|---|---|---|---|
| `bitonic32`, `PIPE_CUTS = 8` | 8,112 | 5,376 | 0 | 116.7 MHz |
| `iou_lane`, one lane | 109 | 101 | 2 | 139.6 MHz |
| `nms_core`, whole compute core | 12,384 (59.5%) | 9,616 | 33 | WNS +0.202 ns |
| **`nms_top`, board design** | **12,570 (60.4%)** | **9,979** | **33** | **WNS +0.200 ns, WHS +0.043 ns** |

**The timing margin is thin, and the sorter is the critical path.** If a change makes `make impl`
fail timing, the documented remedies, cheapest first ([hardware.md](results/hardware.md) §5): re-place
the sorter's register cuts with `CUT_AFTER`; `PIPE_CUTS = 9` (+1 cycle); duplicate the payload
register that fans out to the 16 lanes; a third issue register (+1 cycle). Correctness is verified
at every `PIPE_CUTS` from 0 to 15.

**Read the synthesis warnings, not just the timing summary.** Inferred latches, unhandled `case`
branches and width mismatches are how a GHDL-clean design becomes wrong hardware.

---

## 6. Program the board

Three one-time setup steps:

- **Cable drivers.** Without them the hardware manager can't see the board, which looks exactly
  like a dead board:
  ```bash
  cd ~/Vivado/2026.1/data/xicom/cable_drivers/lin64/install_script/install_drivers
  sudo ./install_drivers          # then replug the board
  ```
- **Serial port permission.** Without `dialout` the error looks like a missing device:
  ```bash
  sudo usermod -aG dialout $USER  # takes effect in a new login session
  getent group dialout            # verify (id -nG in the old shell won't show it)
  ```
- **FTDI latency timer, persistently.** The driver holds a short read for 16 ms by default, which
  is exactly the 6-byte reply. A udev rule sets 1 ms on every plug-in:
  ```bash
  echo 'ACTION=="add", SUBSYSTEM=="usb-serial", DRIVER=="ftdi_sio", ATTR{latency_timer}="1"' \
      | sudo tee /etc/udev/rules.d/99-ftdi-latency.rules
  ```

`deployment/basys3.xdc` is Digilent's `Basys-3-Master.xdc` (Rev. B) with only the used lines
enabled: clock **W5**, `btnC` **U18**, `RsRx` **B18**, `RsTx` **A18** and the 16 LEDs.

Plug in the board, switch it on, and program it:

```bash
lsusb | grep 0403         # expect 0403:6010 (FTDI; "grep -i ftdi" finds nothing: the vendor
                          # string is "Future Technology Devices International")
ls /dev/ttyUSB*           # ttyUSB0 is JTAG, ttyUSB1 the UART
make program              # loads build/impl/nms_top.bit over JTAG
make host                 # then check it: --selftest, see §7
```

`make program` loads configuration RAM only: the design is gone at power-off, so reprogram after
every power cycle. The 16 LEDs show the low 16 bits of the last batch's `keep_mask` (dark until a
batch runs); **BTNC** is reset.

---

## 7. Talk to it from the host

The host program is `models/nms/host.py`, talking to the UART, **`/dev/ttyUSB1`**. The wire
protocol is frozen in [architecture.md](design/architecture.md) §3: **264 bytes in, 6 out**, every
multi-byte field most-significant byte first.

```
host -> FPGA   magic A5 5A | 32 records x 8 B | present_mask 4 B | seq 1 B | crc8 1 B
FPGA -> host   status 1 B | seq echoed 1 B | keep_mask 4 B (zero unless status = 0x00)
```

```bash
uv run python -m models.nms.host --selftest       # all 20 committed frames
uv run python -m models.nms.host --crc-test       # one deliberately corrupted frame
uv run python -m models.nms.host --random 1000    # hostile batches vs the golden model
uv run python -m models.nms.host --latency 500    # round-trip time distribution
```

`make host` wraps the same program (`make host ARGS="--crc-test --random 1000"`, `PORT=...`).

| mode | a pass looks like |
|---|---|
| `--selftest` | `selftest: 20/20 frames answered exactly` |
| `--crc-test` | `crc-test: pass -- status 0x01`: the frame was rejected, not computed |
| `--random N` | `random: N/N batches bit-exact against the golden model` |
| `--latency N` | min / median / p99 / max round trip |

The exit status is 0 when everything passed, 1 on any failure or timeout, 2 when the port can't be
opened. To measure at the FTDI default, reset the timer to 16 and add `--leave-latency-timer`.

| symptom | likely cause |
|---|---|
| `only 0 of 6 reply bytes before the timeout` | wrong port (`ttyUSB0` is JTAG); board not programmed or power-cycled; BTNC held |
| `cannot open /dev/ttyUSB1: Permission denied` | not in `dialout` yet (new login needed) |
| every frame gets status `0x01` | bytes arriving corrupted: baud mismatch or a wrong UART pin |
| replies correct but the LEDs stay dark | an LED pin wrong in the XDC |
| `reply answers seq …, expected …` | a late reply from an earlier timeout; rerun, raise `--timeout` if it repeats |

---

## 8. Capture the latency on silicon

An ILA (Vivado's on-chip logic analyser) on the core's handshake — `start`, `done`, `busy`,
`settled`, `we` — measures T on the chip:

```bash
make impl ILA=1     # debug build into build/impl_ila/ (meets 100 MHz; ~8 min)
make ila            # program it, capture 16 random batches, check every one
make program        # afterwards: restore the production bitstream
```

`make ila` prints one line per window and ends `16 windows: every one measures T = 80 cycles on
silicon`, writing `build/ila/ila.csv` (and `.vcd` for GTKWave) and the waveform figure
`docs/images/onchip_latency.png`. The BASIC licence refuses post-synthesis debug-core insertion, so
the ILA is Vivado's catalogue IP, instantiated by `nms_top`'s `ILA` generic (default `false`: the
production netlist is unchanged). `test/stubs/ila_core.vhd` stands in for it in simulation.

---

## 9. Benchmark against software

`make bench` times six software NMS implementations on the machine it runs on — laptop or
Raspberry Pi, x86 or 64-bit ARM — and checks every answer before timing it:

```bash
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
make bench                                            # idle
make bench BENCH_ARGS="--tag run2"                    # the repeat (medians must agree within 10%)
make bench LOAD=pipeline BENCH_ARGS="--cases hostile --frames 100"   # YOLO before each call
make bench LOAD=concurrent                            # YOLO in another process
make bench LOAD=stress                                # needs: sudo apt install stress-ng
make bench TARGET=fpga                                # the board over the UART, end to end
make bench-report                                     # merge every run into tables
```

- The cpu target pulls the `bench` extra (torch from the CPU-only index, OpenCV, matplotlib); the
  detector loads add `bench-load` (Ultralytics, AGPL-3.0). CI installs neither.
- Each run writes `benchmarks/results/<target>-<host>-<date>-<load>.{csv,json}` plus every raw
  sample, and exits non-zero if any answer disagrees with its reference or a Raspberry Pi throttled.
- The figures are regenerated from committed results:
  ```bash
  uv run --extra bench python -m benchmarks.report --headline docs/images/headline_worst_case.png
  uv run --extra bench python -m benchmarks.report --compare-loads "Raspberry Pi 4" docs/images/pi4_nms_by_load.png
  uv run --extra bench python -m benchmarks.feasibility.summarise --dir benchmarks/results/feasibility \
      --png docs/images/nms_time_vs_boxes.png
  ```

What was measured and what it shows is in [benchmarks.md](results/benchmarks.md); the method is
[plan.md](project/plan.md) Phase E.

---

## 10. Conventions and adding code

- **Synthesisable RTL in the VHDL-93 subset; testbenches in VHDL-2008** for `HREAD`. Everything is
  analysed at `--std=08`, which accepts both.
- **Combinational logic as concurrent assignments, never combinational processes.** Processes are
  clocked only, with `(clk)` as the whole sensitivity list. GHDL analyses with `-Wall --warn-error`,
  so any new warning stops the build.
- **Two asynchronous inputs, each through a 2-flop synchroniser:** the UART RX pin (inside
  `uart_rx`) and the BTNC reset button (in `nms_top`).
- Testbenches are `tb_`-prefixed, self-checking, and end in `report "PASS"`. New checks are
  **mutation-tested**: shown to fail on deliberately broken RTL, with the results in
  [build_log.md](project/build_log.md).
- Files `snake_case`; constants `UPPER_SNAKE_CASE`; Python `snake_case` with Google docstrings,
  formatted and linted by `ruff`.
- Branches `feature/…` or `fix/…`; commits follow [Conventional Commits](https://www.conventionalcommits.org/);
  a PR and one approval before merge. The `/commit` and `/pr` Claude Code skills in `.claude/skills/`
  automate the format.

**Adding a module or a testbench:**
- A new `test/tb_*.vhd` is picked up automatically and gets its own `make` target.
- A new `src/**/*.vhd` must be **added to `RTL` in `scripts/Makefile`, in dependency order**, and to
  the file lists in `scripts/synth.tcl` and `scripts/impl.tcl`. `make analyse` refuses to run while
  any RTL file is unlisted.
- To run a testbench in several configurations, add a `SWEEP_tb_<name>` line: one word per run,
  several `-g` flags for one run joined by commas. A `SWEEP_FULL_tb_<name>` line replaces it under
  `make test-full`.
