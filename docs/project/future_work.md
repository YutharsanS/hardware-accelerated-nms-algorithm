# Future work — what was not done, and how to start

The project closed on 2026-09-29 with the block built, verified on silicon and benchmarked
([benchmarks.md](../results/benchmarks.md)). This is everything that was planned or considered but
not done, ordered by how much it would change what the project can claim. Each item says why it
matters, what is already known, and where to start.

Effort estimates are rough, for one person who knows this codebase.

---

## 1. Put the block where it belongs: inside the fabric, beside a detector

**Why.** The one claim the project argues but cannot measure. Over the UART the round trip is
~5 ms against the core's 1.13 µs, so the Basys 3 demonstrates core latency and determinism, not a
system speed-up. The block's case is as an IP beside a detector's accelerator (for example a DPU
on a Zynq or Kria), where it would replace a DDR → interrupt → software-NMS round trip with a fixed
113 cycles.

**Known.** In AMD's own Vitis AI example (YOLOv3 on a ZCU102), CPU post-processing takes about
32 ms against 8 ms of DPU time, but that figure includes decode and dequantise, not NMS alone
([plan.md](plan.md) E.2 and the E0 build-log entry; agent-sourced, verify before citing). The core's
interface (`we`/`waddr`/`wdata`, `start`, `done`, `keep_mask`) is already a simple register
interface, so an AXI-Stream or AXI-Lite wrapper is thin.

**Start.** Wrap `nms_core` in an AXI-Stream slave; on a Zynq-7000 (PYNQ-Z2 class) board, feed it
detections from the ARM and time the whole round trip against software NMS on the same A9.
The 32 load cycles and the settle cycle, today pinned only in simulation, would then be measured
on the chip too. **Effort:** 1–2 weeks with a board.

## 2. Batches larger than 32

**Why.** N = 32 is an application constraint of the target niche, not a property of detection in
general. On 500 COCO val2017 images, about 10% of per-class batches exceed 32 boxes, and cutting
them to their top 32 drops about 11% of the keepers ([benchmarks.md](../results/benchmarks.md) §8).

**Known.** The combinational sorter is Θ(N log² N): N = 64 needs 672 compare-and-swaps,
≈ 20,160 LUT, and does not fit the XC7A35T ([architecture.md](../design/architecture.md) §11). The
all-pairs fill costs N²/P cycles, so at N = 1,000 and P = 16 it would take ~62,500 cycles, no
faster than software. Published large-N designs stream or fold their sorters instead (DATE 2022:
1,000 boxes in 12.79 µs at 400 MHz).

**Start.** A folded sorter reusing one 16-CAS layer over 15 cycles (sketched in [plan.md](plan.md)
Part 3), then a streaming resolve. **Effort:** weeks; an architecture change, not a parameter.

## 3. More than one class

**Why.** Real detectors run NMS per class; the 64-bit record carries no class ID.

**Known.** Running one batch per class needs no RTL change. A 4-bit class field carved from the
score would allow 16 classes in 16 × 0.80 µs = 12.8 µs ([architecture.md](../design/architecture.md)
§11). The record has no spare bits, so either the score loses resolution or the record grows.

**Start.** Decide the record format first; then a per-class `present_mask` from the host is the
smallest change. **Effort:** days.

## 4. A literature comparison (plan stage E5)

**Why.** The only external hardware point cited is one DATE 2022 figure. An examiner will ask how
this block compares with published FPGA NMS designs.

**Known.** Candidate sources, gathered by a research agent and **not yet verified**: DATE 2022
"Scalable Hardware Acceleration of NMS"; Anupreetham et al., ACM TRETS 2023 (pipelined NMS,
Stratix 10); a YOLOv2 NMS on Alveo U50 (APPT 2023); Zhang et al., ISVLSI 2020; MDPI *Information*
2025 (bitonic top-K post-processing IP). None found uses N = 32.

**Start.** 3–5 designs in one table — N, clock, latency, LUT, DSP, device — normalised to µs per
batch and cycles per box pair where the paper allows, "as reported by the authors" otherwise.
**Effort:** ½ day.

## 5. The same RTL on faster parts (plan stage E7)

**Why.** Answers "is 100 MHz just the Basys 3?". The cycle count carries over to any part; the
nanoseconds do not.

**Known.** All candidate parts are installed and allowed by the BASIC licence (checked
2026-09-29): Artix-7 35T at −2 and −3, Zynq-7020, Kintex-7 325T (and 70T/160T). Kria (Zynq
UltraScale+) is not installed. A two-pass sweep tool was written and then removed unrun
(commit `771beb4`; the tool is in `908c10c`).

**Start.** Restore `PART=` in `scripts/synth.tcl` from `908c10c`, then implement `nms_core` out of
context on each part at P = 16 and 32. Report every figure as post-route static timing, not run
on silicon. **Effort:** ~1 hour unattended.

## 6. The lane-count curve (plan stage D2)

**Why.** P (the number of IoU lanes) is a generic, `P ∈ {1, 2, 4, 8, 16, 32}`, but only P = 16 was
implemented and measured. Until the curve exists, P should be presented as fixed at 16, not as a
tuned design axis.

**Known.** `tb_nms_core` already verifies P = 32 (T = 48) and, under `make test-full`, P = 1. T is
`N²/P + L + I + C + 2`, so each doubling of P roughly halves the fill.

**Start.** `make synth MOD=nms_core GENERICS="P=…"` for each P; plot LUT, DSP, Fmax and T against P.
**Effort:** ~2 hours of batch runs.

## 7. More processors

- **Raspberry Pi 5 (Cortex-A76).** The planned main competitor; a Pi 4 was used instead. The same
  commands run unchanged ([user_guide.md](../user_guide.md) §9). A Pi 5 would narrow the gaps in
  [benchmarks.md](../results/benchmarks.md) §6. **Effort:** an afternoon with the board.
- **Cortex-A53** (Kria, Zynq UltraScale+): the ARM that actually sits beside FPGA fabric. Slower
  than the Pi 4's A72.
- **The laptop under load.** Only idle laptop runs are committed; the load conditions ran on the
  Pi 4 alone.
- **MicroBlaze in the same fabric (plan stage E6).** The same C on a soft CPU in the XC7A35T at
  100 MHz: the strictest same-silicon comparison, reported per LUT as well as per batch (a
  MicroBlaze is ~1–2k LUT against the block's 12,570). Vitis is installed. **Effort:** 2–3 days.

## 8. Smaller hardware improvements

- **Recover the `T_INT × U` constant fold.** Vivado maps it to a second DSP, so each lane costs 2
  DSPs, not 1 ([architecture.md](../design/architecture.md) §5). With `T_INT = 128` it is a shift;
  folding it would save 16 DSPs and change nothing else.
- **Masked-argmax keeper (plan stage C5).** An alternative to the sorter, designed in
  [plan.md](plan.md) Part 3 so that it is a yes/no decision, not a redesign. **Effort:** ~1 day.
- **Widen the timing margin.** The board design closes 100 MHz with +0.200 ns; the documented
  remedies are in [hardware.md](../results/hardware.md) §5.

## 9. Demonstration and housekeeping

- **A detector front-end demo (plan stage D4).** A host script that runs a detector, streams its
  boxes to the board and draws the survivors from the returned `keep_mask`. The FPGA side does
  not change. **Effort:** 1–2 days.
- **The UART round trip at the FTDI default 16 ms timer.** A five-minute measurement with the board
  ([hardware.md](../results/hardware.md) §6).
- **Merge to `main` and tag a release.** The finished README is on `prototype/bench`; GitHub shows
  `main`. A tagged release with `nms_top.bit` attached would let a reviewer program a Basys 3
  without installing Vivado.

## 10. 3D Gaussian Splatting depth sorting

The project's Phase 0 measured 3DGS's per-tile depth sort as a candidate workload: a real
bottleneck (504–662 ms on a CPU against a 33 ms frame budget) that needs a full sort, which the
bitonic network provides. The team chose NMS; the sorter half of this design transfers unchanged.
The measurements and code are in
[explorations/gaussian_splatting/](../../explorations/gaussian_splatting/README.md).
