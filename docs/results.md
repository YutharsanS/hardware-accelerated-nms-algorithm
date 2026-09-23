# Measured results

The measured counterpart to the estimates in [architecture.md](architecture.md) §9. Every
figure here comes from the real part after full place and route — nothing is projected
unless it says so.

**Method.** `make synth MOD=<module>`, which runs [scripts/synth.tcl](../scripts/synth.tcl):
out-of-context synthesis through `route_design` on `xc7a35tcpg236-1`, 10 ns constraint
unless stated. Area is taken from `report_utilization` and timing from `get_timing_paths`.

Out-of-context is required, not a convenience: `bitonic32` presents 2 × 32 × 21 bits at its
boundary, which no 236-pin package can carry. Numbers are taken after routing rather than
after synthesis because a post-synthesis figure omits routing delay, and the 100 MHz claim is
about real Fmax.

Device totals for `xc7a35tcpg236-1`: **20,800 LUT, 41,600 FF, 90 DSP**.

Reports for the last run of each module land in `build/synth/<module>/`.

---

## 1. Per-module, measured

| module | LUT | % LUT | FF | CARRY4 | DSP | critical path | Fmax |
|---|---|---|---|---|---|---|---|
| `cas` (one unit) | 32 | 0.2% | 0 | 3 | 0 | 4.348 ns | 230.0 MHz |
| `bitonic32` (`PIPE_CUTS = 2`) | 9,596 | 46.1% | 1,344 | 720 | 0 | 18.570 ns | 53.9 MHz |
| `bitonic32` (`PIPE_CUTS = 8`) | 8,112 | 39.0% | 5,376 | 720 | 0 | 8.386 ns | 119.2 MHz |
| `iou_lane` (one lane) | 109 | 0.5% | 101 | 30 | 2 | 5.880 ns | 170.1 MHz |

`iou_lane` at shipped generics (`T_INT = 128`, `K_SHIFT = 8`).

---

## 2. `bitonic32` across pipelining depth

| `PIPE_CUTS` | LUT | % LUT | LUT/CAS | FF | critical path | Fmax | meets 100 MHz |
|---|---|---|---|---|---|---|---|
| 2 (current default) | 9,596 | 46.1% | 40.0 | 1,344 | 18.570 ns | 53.9 MHz | no |
| 4 | 9,758 | 46.9% | 40.7 | 2,688 | 11.392 ns | 87.8 MHz | no |
| **8** | **8,112** | **39.0%** | 33.8 | 5,376 | 8.386 ns | **119.2 MHz** | **yes, WNS +1.614 ns** |
| 15 | 7,680 | 36.9% | 32.0 | 10,080 | 5.335 ns | 187.4 MHz | yes |

**Pipelining reduces area here rather than costing it.** LUT is flat from 2 to 4 and then
falls 20% by 15, while Fmax rises 3.5×. Long combinational chains force logic replication;
cutting them lets the tool share instead. The flip-flops are the only real cost, and at
10,080 they are still 24% of those available.

**`PIPE_CUTS = 8` is the lowest swept value meeting 100 MHz**, and it is also cheaper in LUTs
than the current default of 2.

Two structural invariants hold at every point, which is what makes these numbers trustworthy
rather than a mis-elaboration: **CARRY4 = 720 = 240 × 3** always, and **FF = `PIPE_CUTS` × 672**
exactly (672 = 32 keys × 21 bits).

---

## 3. Estimated vs measured

Against the architecture.md §9 table. "16 × `iou_lane`" is one measured lane × 16; the lanes
are independent, so this scales cleanly.

| block | est. LUT | meas. LUT | est. FF | meas. FF | est. DSP | meas. DSP |
|---|---|---|---|---|---|---|
| `bitonic32` (`PIPE_CUTS = 2`) | 7,200 | 9,596 | 1,344 | 1,344 | 0 | 0 |
| `bitonic32` (`PIPE_CUTS = 8`) | — | 8,112 | — | 5,376 | — | 0 |
| 16 × `iou_lane` | 3,424 | 1,744 | ~2,400 | 1,616 | 16 | **32** |

**The per-CAS cost model is sound.** architecture.md and `cas.vhd` both predict
`8 + 2·⌈W/2⌉` = 30 LUT at W = 21. A standalone `cas` measures **32** — 6.7% over. In-network
the figure is higher and depends on pipelining (40.0 LUT/CAS at `PIPE_CUTS = 2`, converging
to 32.0 at 15, where it meets the standalone cost). The 7,200 LUT network total is met at
`PIPE_CUTS = 15` (7,680) and exceeded by 13% at the setting actually required.

**The flip-flop prediction for the sorter was exact** — 1,344 estimated, 1,344 measured.

**The lanes are half the estimated LUT cost** — 1,744 against 3,424.

**The lanes use twice the targeted DSPs.** 2 per lane, not 1, so 32 rather than 16. The cause
is known and was predicted in advance: `T_INT × U` is a constant multiply that was expected
to fold into shifts, and it did not. Vivado's log names both:

```
DSP Report: Generating DSP union, operation Mode is: C-(A2*B2)'.
DSP Report: Generating DSP s2_inter_reg, operation Mode is: (A2*B2)'.
```

Nothing structural is wrong — area and timing both pass comfortably — but recovering the fold
would halve the DSP budget. See [build_log.md](build_log.md) B4.2.

---

## 4. Projected full-chip total

Measured where a block exists, architecture.md §9 estimate where it does not. Sorter at
`PIPE_CUTS = 8`, `P = 16`.

| block | LUT | FF | DSP | source |
|---|---|---|---|---|
| `bitonic32` (`PIPE_CUTS = 8`) | 8,112 | 5,376 | 0 | measured |
| 16 × `iou_lane` | 1,744 | 1,616 | 32 | measured |
| row-source payload mux | 530 | — | 0 | estimate |
| row-source area mux | 264 | — | 0 | estimate |
| payload/area/row/index registers | — | 3,050 | 1 | estimate |
| masks, resolve, FSM, UART | ~900 | ~450 | 0 | estimate |
| **projected total** | **≈11,550 (55.5%)** | **≈10,492 (25.2%)** | **33 (36.7%)** | |

**The design fits, with margin.** The two largest blocks are both measured rather than
guessed, and together they are **47.4% of LUTs and 35.6% of DSPs** — leaving over half the
device for everything still to be written. This settles `P = 16`, which was provisional until
these numbers existed.

The projected LUT total (55.5%) comes in *below* architecture.md's 59% estimate, because the
lanes undershot by more than the sorter overshot.

---

## 5. Timing

Every module that exists clears 100 MHz, with one configuration condition:

| module | Fmax | margin at 100 MHz |
|---|---|---|
| `bitonic32` (`PIPE_CUTS = 8`) | 119.2 MHz | WNS +1.614 ns |
| `iou_lane` | 170.1 MHz | WNS +4.120 ns |
| `cas` | 230.0 MHz | — |

**P1 — the weakest load-bearing estimate in the plan — is settled, and it was pessimistic.**
It projected 22–37 ns / 27–45 MHz for the network combinationally; `PIPE_CUTS = 2` measures
18.570 ns / 53.9 MHz. Per sub-stage that is ~3.7 ns against the 1.5–2.5 ns assumed, but across
fewer levels in the critical segment than the estimate's arithmetic implied.

**The 100 MHz claim survives, at `PIPE_CUTS = 8` rather than the shipped 2.** `CLOCK_HZ` is
frozen at 100 MHz and `BAUD_DIV` derives from it, so this is a system constant rather than a
target, and it fixes the setting.

### Open: `nms_pkg.PIPE_CUTS` is still 2

The constant has not been changed. At 2 the sorter runs 53.9 MHz and does **not** meet the
clock the rest of the design assumes. Moving it to 8 costs 6 extra cycles of sorter latency,
which the FSM must absorb: architecture.md §9's `T = N²/P + L + C + 2` = 72 cycles at `P = 16`
becomes ≈78 cycles (≈0.78 µs). Correctness at 8 is already verified — B3.1 checked every
`PIPE_CUTS` from 0 to 15 — so this is a scheduling decision, not a verification one.

---

## 6. Not yet measured

`frame_rx` / `frame_tx`, `box_store`, the row buffer, resolve, and the FSM are not written, so
their rows above remain estimates. The integrated design has never been synthesised as a
whole, and the per-module figures here exclude inter-block routing.
