# Measured results

The measured counterpart to the estimates in [architecture.md](architecture.md) §9. Every
figure here comes from the real part after full place and route — nothing is projected
unless it says so.

**Method.** `make synth MOD=<module>`, which runs [scripts/synth.tcl](../scripts/synth.tcl):
out-of-context synthesis through `route_design` on `xc7a35tcpg236-1`, 10 ns constraint
unless stated, with **zero input and output delay against the clock on every data port**. Area is taken from `report_utilization` and timing from `get_timing_paths`.

Out-of-context is required, not a convenience: `bitonic32` presents 2 × 32 × 21 bits at its
boundary, which no 236-pin package can carry. Numbers are taken after routing rather than
after synthesis because a post-synthesis figure omits routing delay, and the 100 MHz claim is
about real Fmax.

Device totals for `xc7a35tcpg236-1`: **20,800 LUT, 41,600 FF, 90 DSP**.

Reports for the last run of each module land in `build/synth/<module>/`.

> **Method correction (2026-09-24).** Before this date `synth.tcl` set only `create_clock`, so
> input-port → register and register → output-port paths were *unconstrained* and left out of
> WNS. That excluded `iou_lane`'s whole stage 1 and `bitonic32`'s first and last segments. The
> script now constrains every data port with a zero delay, which models each neighbour as a
> register at the boundary. The clocked figures below are re-measured under that rule. The
> earlier ones are kept in brackets where they differ. Area is unaffected by the change.

---

## 1. Per-module, measured

| module | LUT | % LUT | FF | CARRY4 | DSP | critical path | Fmax |
|---|---|---|---|---|---|---|---|
| `cas` (one unit) | 32 | 0.2% | 0 | 3 | 0 | 4.348 ns | 230.0 MHz |
| `bitonic32` (`PIPE_CUTS = 2`) | 9,596 | 46.1% | 1,344 | 720 | 0 | 18.570 ns | 53.9 MHz |
| `bitonic32` (`PIPE_CUTS = 8`) | 8,112 | 39.0% | 5,376 | 720 | 0 | 8.566 ns | 116.7 MHz *(119.2)* |
| `iou_lane` (one lane) | 109 | 0.5% | 101 | 30 | 2 | 7.161 ns | 139.6 MHz *(170.1)* |
| `nms_ctrl` (P = 16, C = 8) | 382 | 1.8% | 300 | 0 | 0 | 5.714 ns | 175.0 MHz |

`iou_lane` at shipped generics (`T_INT = 128`, `K_SHIFT = 8`).

---

## 2. `bitonic32` across pipelining depth

| `PIPE_CUTS` | LUT | % LUT | LUT/CAS | FF | critical path | Fmax | meets 100 MHz |
|---|---|---|---|---|---|---|---|
| 2 (current default) | 9,596 | 46.1% | 40.0 | 1,344 | 18.570 ns | 53.9 MHz | no |
| 4 | 9,758 | 46.9% | 40.7 | 2,688 | 11.392 ns | 87.8 MHz | no |
| **8** | **8,112** | **39.0%** | 33.8 | 5,376 | 8.386 ns | **119.2 MHz** | **yes, WNS +1.614 ns** (re-measured with ports constrained: 116.7 MHz, +1.434 ns) |
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
| `bitonic32` (`PIPE_CUTS = 8`) | 116.7 MHz | WNS +1.434 ns |
| `iou_lane` | 139.6 MHz | WNS +2.839 ns |
| `cas` | 230.0 MHz | — |

**With port paths constrained, both modules still clear 100 MHz, but the margins shrink.**
- **`bitonic32`**: still limited by an internal two-sub-stage segment (after 10 → after 12).
  8.566 ns against 8.386 before is placement variation, so the port segments are not its
  critical path.
- **`iou_lane`**: now limited by **stage 1 from the input ports**, `k_a` → the DSP's
  synchronous-reset pin. Vivado folded the clamp into the multiplier's reset, so min/max,
  subtract and clamp all land in front of the DSP in one cycle.
- **The integration risk this exposes**: in the full design, the lane inputs are fed from
  `index_table` through a 32:1 × 72 b row-source mux whose selects fan out to 16 lanes. That
  mux has to fit in the lane's **2.84 ns of remaining slack**, which is tight.
- **If it misses at C4**, the fix is a registered keeper/candidate stage in the datapath. That
  is one more lane stage (`LANE_LATENCY` 5), which `nms_ctrl` absorbs through its generic,
  costing 1 cycle (T 78 → 79). See [fsm_design.md](fsm_design.md) §9.

The `PIPE_CUTS` sweep in §2 predates the method correction. Its internal-segment figures stand,
but the table has not been re-run with ports constrained.

**P1 — the weakest load-bearing estimate in the plan — is settled, and it was pessimistic.**
It projected 22–37 ns / 27–45 MHz for the network combinationally; `PIPE_CUTS = 2` measures
18.570 ns / 53.9 MHz. Per sub-stage that is ~3.7 ns against the 1.5–2.5 ns assumed, but across
fewer levels in the critical segment than the estimate's arithmetic implied.

**The 100 MHz claim survives, at `PIPE_CUTS = 8` rather than the shipped 2.** `CLOCK_HZ` is
frozen at 100 MHz and `BAUD_DIV` derives from it, so this is a system constant rather than a
target, and it fixes the setting.

### Resolved: `PIPE_CUTS` is now 8

The constant moved from 2 to 8 in architecture.md §9, `params.py` and `nms_pkg.vhd` together, so
`test_params_agree` still holds. At 2 the sorter ran 53.9 MHz and did **not** meet the clock the
rest of the design assumes. The 6 extra cycles of sorter latency take architecture.md §9's
`T = N²/P + L + C + 2` from 72 to **78 cycles (0.78 µs)** at `P = 16`. The FSM design absorbs
them ([fsm_design.md](fsm_design.md) §6). Correctness at 8 is verified: B3.1 checked every
`PIPE_CUTS` from 0 to 15, and `tb_bitonic32` now runs at 8 in the standing regression.

---

## 6. Not yet measured

`frame_rx` / `frame_tx` and `box_store` are not written, so their rows above remain estimates.
The FSM, row buffer and resolve are built and measured as `nms_ctrl` (§1). The integrated design has never been synthesised as a
whole, and the per-module figures here exclude inter-block routing.
