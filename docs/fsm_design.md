# `nms_ctrl` — control FSM design (plan.md C3)

> **Status: reviewed 2026-09-24.** Every section answers the prompt above it and cites the spec
> line it rests on. Where the design disagrees with the spec, it says so and names the document
> that changes; those are collected in §9. The review found no fault in the FSM logic. It added
> the explicit start/busy rule in §1, the illegal-state row in §7, and the row-source timing
> note in §9.

**Normative sources:** [architecture.md](architecture.md) §3 (wire protocol), §6 (sort key),
§8 (storage), §9 (architecture); [plan.md](plan.md) Part 2 "Architecture" and "Control FSM"
(lines 746–831), Part 1d (latency, lines 533–590).

**Configuration this doc is written for:** `N = 32`, `P = 16`, `L = LANE_LATENCY = 4`,
`C = PIPE_CUTS = 8` (see [results.md](results.md) §5). Throughout, `G = N/P` is the number of
column groups per row: 2 at P = 16.

---

## 1. Scope and boundary

plan.md's FSM ([plan.md:814-831](plan.md#L814-L831)) folds frame reception and CRC checking into
`LOAD`. Before listing states, decide where `nms_ctrl` starts and ends.

- Which module owns magic detection, the CRC-8 and `seq` — `frame_rx` or `nms_ctrl`?
- What single event tells `nms_ctrl` a valid batch is ready, and what tells it a frame was
  rejected?
- Which module owns the reply (`status`, `seq`, `keep_mask`) — and who decides the `status` byte?
- Justify the split: what does each choice make easier or harder to test in isolation?

_Answer:_

**`frame_rx` owns everything about the wire:** the magic hunt, CRC-8, `seq`, the idle timeout,
byte-to-record packing, the `box_store` writes (areas are computed there as each record lands,
[plan.md:771](plan.md#L771)) and the `present_mask` register. plan.md's `LOAD` state is
therefore `frame_rx`'s receive phase, not a state of `nms_ctrl`. From `nms_ctrl`'s point of view
a batch arriving is just `IDLE` waiting.

**One event starts a batch:** a one-cycle `start` pulse, which `frame_rx` raises only on a good
frame ([plan.md:648](plan.md#L648)). `nms_ctrl` is **never told about a rejected frame**. On a
CRC failure `frame_rx` simply does not pulse `start`, and it asks `frame_tx` for the
`status = 0x01` reply itself. The busy case (`0x02`) goes the same way, which is why `busy` is an
output of `nms_ctrl` and an input of `frame_rx`.

**Who writes the reply:**

| field | decided by | carried to `frame_tx` by |
|---|---|---|
| `status` `0x00` / `0x03` | `nms_ctrl` (at `done`) | `nms_ctrl` |
| `status` `0x01` / `0x02` | `frame_rx` | `frame_rx` |
| `seq` | `frame_rx` (latched from byte 262) | `frame_rx`, bypassing `nms_ctrl` entirely |
| `keep_mask` | `nms_ctrl` | `nms_ctrl` |

`frame_tx` applies the single rule "mask is zero when `status ≠ 0`"
([architecture.md §3](architecture.md)) to every reply, so the rule has one implementation, not
three.

**The contract this boundary depends on — the start/busy rule.** `frame_rx` follows three
rules together:

1. It gates its `box_store` write enable with `busy`.
2. It remembers, for the rest of the frame, if **any** write was blocked.
3. At frame end, it asserts `start` **only if `busy = 0` and no write was blocked**. Otherwise
   it replies `status = 0x02` and does not start, even when the CRC passes.

Rule 2 matters because the CRC covers the bytes *received*, not the bytes *stored*. Without it,
a frame whose early records were blocked would pass its CRC and start a batch on a `box_store`
holding parts of two frames. Rule 3 matters because `nms_ctrl` silently ignores `start` in every
state except `IDLE`, including the one-cycle `DONE`. A `start` there would be lost and the host
would get no reply at all.

Together these guarantee that `keys_in` (and the areas) are stable from before the cycle that
samples `start` until `DONE`. At 1 Mbaud none of the three can trigger — the next record byte is
≥ 20 µs away and a batch takes 0.78 µs — but the rules are stated rather than relied on
([plan.md:1168](plan.md#L1168), L3).

**Why split it this way.** `nms_ctrl` can then be tested with no UART, no CRC and no framing at
all: the testbench drives one pulse and reads one mask (§8). The reject paths, which are all
about bytes, are tested in `tb_frame_rx` with no datapath behind them. Putting CRC inside
`nms_ctrl` would force every control test to build valid frames, and every framing test to wait
out a full batch. The cost is one more handshake, `busy`, crossing a module boundary.

---

## 2. Interface

One row per port. "Spec source" is the line in architecture.md or plan.md that fixes it; a port
with no source is a decision you are making — mark it **(new)**.

**Generics:** `P : positive := P_DEFAULT`, `PIPE_CUTS : natural := work.nms_pkg.PIPE_CUTS`,
`LANE_LATENCY : positive := work.nms_pkg.LANE_LATENCY`. An elaboration-time assertion checks
`N mod P = 0` ([plan.md:762](plan.md#L762)). The generics are spelled with the full package
name, as in [bitonic32.vhd:45](../src/components/bitonic32.vhd#L45).

| signal | dir | width | meaning | spec source |
|---|---|---|---|---|
| `clk` | in | 1 | 100 MHz system clock | [architecture.md §10](architecture.md) |
| `rst` | in | 1 | synchronous, active high | [architecture.md §10](architecture.md) |
| `start` | in | 1 | one-cycle pulse: a good frame is in `box_store`, and the areas and `present_mask` are stable | [plan.md:648](plan.md#L648) |
| `present_mask` | in | 32 | load value of `valid_mask` at `start` | [architecture.md §3](architecture.md) |
| `busy` | out | 1 | `state /= IDLE`; `frame_rx` gates `box_store` writes with it | [plan.md:872-874](plan.md#L872-L874) |
| `keys_sorted` | in | 32 × 21 | `bitonic32.keys_out`, ascending | [architecture.md §9](architecture.md) |
| `issue_valid` | out | 1 | a pair group is presented this cycle; drives every lane's `valid_in` | **(new)** — [iou_lane.vhd:47](../src/components/iou_lane.vhd#L47) |
| `row_src` | out | 5 | slot of the current row's box, i.e. `index_table(r)`; select for the shared 32:1 row-source mux | [plan.md:762-765](plan.md#L762-L765) |
| `col_grp` | out | `natural range 0 to N/P−1` | column group `g`; lane `j` compares against slot `j + g·P` | **(new)** — the lane column mux of [plan.md:762-763](plan.md#L762-L763) needs a select |
| `lane_valid` | in | 1 | lane 0's `valid_out`; cross-checked against the internal tag pipe (§7) | [iou_lane.vhd:57](../src/components/iou_lane.vhd#L57) |
| `lane_suppress` | in | P | the P lanes' `suppress` outputs | [iou_lane.vhd:58](../src/components/iou_lane.vhd#L58) |
| `done` | out | 1 | one-cycle pulse; `status` and `keep_mask` are final | **(new)** — "On `DONE`", [plan.md:657](plan.md#L657) |
| `status` | out | 8 | `STATUS_OK` or `STATUS_INTERNAL`, valid with `done` | [architecture.md §3](architecture.md), [plan.md:686](plan.md#L686) |
| `keep_mask` | out | 32 | arrival-order survivors; held until the next `start` | [architecture.md §3](architecture.md) |

Every item in the list below is either a port or internal:

- **start handshake** — `start` in, `busy` out (§1).
- **`box_store` read side** — *not a port of `nms_ctrl`*. The FSM never reads a payload or an
  area. It emits two selects, `row_src` (which box is the keeper for this row) and `col_grp`
  (which column group the lanes read), and the muxes sit in the datapath (`nms_core`, C4)
  beside `box_store`. The addresses are exactly those two values.
- **`bitonic32` `keys_in` / `keys_out`** — `keys_in` is built by a concurrent assignment in
  `nms_core`, `K(i) = score(i) & not(to_unsigned(i, 5))` ([architecture.md §6](architecture.md)),
  straight from the `box_store` registers. `keys_out` arrives here as `keys_sorted`.
- **`index_table`** — **internal register**, 32 × 5 b = 160 FF
  ([architecture.md §8](architecture.md)), written by `nms_ctrl` from `keys_sorted` (§3, §6). A
  wire would put the sorter's unregistered final sub-stage, the 32:1 row mux and lane stage 1 in
  a single cycle.
- **row-source mux select** — `row_src` port.
- **lane `valid_in` / `valid_out` / `suppress`** — `issue_valid` out, `lane_valid` in,
  `lane_suppress` in.
- **`present_mask`, `valid_mask`, `keep_mask`** — port, internal register, port respectively.
- **`busy` and `status`** — ports.

---

## 3. States

| state | entered when | duration (formula in N, P, L, C) | exits to | outputs asserted |
|---|---|---|---|---|
| `IDLE` | reset, or after `DONE` | until `start` (outside T) | `SORT`, or `FILL` directly if `C = 0` | none; `index_table` loads every cycle |
| `SORT` | edge 0 (`start` sampled) | `C` | `FILL` | `busy`; `index_table` loads every cycle |
| `FILL` | edge `C` | `N·G = N²/P` | `DRAIN` | `busy`, `issue_valid`, `row_src = index_table(cnt / G)`, `col_grp = cnt mod G` |
| `DRAIN` | edge `C + N·G` | `L + 1` | `DONE` | `busy` |
| `DONE` | edge `C + N·G + L + 1` | 1 | `IDLE` | `busy`, `done`, `status` |

One counter `cnt` is shared by all states and cleared on every transition. Its largest terminal
value is `N·G − 1 = 63` (§5). At `start`, `IDLE` also loads `valid_mask ← present_mask` and
clears `keep_mask`, `res_cnt` and `err`.

**`index_table` loads whenever the state is `IDLE` or `SORT`, and freezes in `FILL`.** At the
`SORT → FILL` edge the sorter has had `C` edges with a stable input, so the value captured on
that edge is the current batch's (§4). The `IDLE` half of the rule matters only at `C = 0`: the
sorter is then combinational, the right value is already present at edge 0, and `IDLE` goes
straight to `FILL`. The latency formula therefore holds at every `C`, including 0.

- **Is every duration a constant?** Yes. `SORT`, `FILL`, `DRAIN` and `DONE` are compared against
  generics only, never against data. The one state that waits on anything is `IDLE`, which
  waits for `start`; that wait is before edge 0 and outside T. So
  [plan.md:1061](plan.md#L1061) (Tier 1 #1) holds, and so does its consequence: `present_mask`,
  the scores and the overlap pattern cannot change how long a batch takes (§7 shows this for
  `present_mask = 0`).
- **Which states overlap, and how does one state register express that?** Fill and resolve
  overlap: resolve of rank `r` runs while the lanes are already computing rows `r + 1 … r + 2`.
  **Resolve is not a state.** It is driven by the pipeline's own valid bits (`row_rdy`, §5),
  which travel with the data through the lanes and the row buffer. The state register only
  decides *what is issued*. Whatever was issued resolves `L + 2` cycles later whatever the state
  is, which is why `DRAIN` exists: it does nothing but let the last `L + 1` cycles of pipeline
  empty.

---

## 4. Cycle-level timeline

Edge 0 = the edge on which the FSM leaves its last pre-sort state. Fill each row with the first
cycle in which the value is **final**. Trace each back to the last register that can change it.

Edge 0 is the edge that samples `start` and leaves `IDLE`. C = 8, G = 2, L = 4. "after e" means
the value is final in the cycle that follows edge e.

```
edge →                    0    7    8    9   12   13   14   15  ...  71   72  ...  77   78
                          |    |    |    |    |    |    |    |       |    |       |    |
keys_in valid          ===============================================================   (stable before 0)
keys_out valid                 ======================================================   after 7   = C−1
index_table(0) valid                ================================================   after 8   = C
issue r0 g0 / r0 g1                 [g0] [g1]                                          cycles after 8, 9
rank-0 box at lane s1                    ==                                             after 9   = C+1
rank-0 lane out g0 / g1                       [g0] [g1]                                 after 12, 13
acc_row holds r0 g0 half                           ==                                   after 13
rank-0 suppress row out                                 ==                              after 14  (row_buf)
resolve rank 0                                               X                          edge 15
resolve rank r                                                  X every G = 2 edges     edge 15 + 2r
last issue (r31 g1)                                                  []                 cycle after 71
FILL → DRAIN                                                              X             edge 72
resolve rank 31                                                                   X     edge 77
DONE (done, keep_mask)                                                            ===   after 77
back in IDLE                                                                          X edge 78
```

General form, where every row follows from the one above it:

| event | edge |
|---|---|
| `keys_out` final | `C − 1` |
| `index_table` written | `C` |
| issue `(r, g)` presented | cycle after `C + r·G + g` |
| lane output for `(r, g)` | after `C + r·G + g + L` |
| `row_buf ← S(r)`, `row_idx ← idx_r` | `C + (r+1)·G + L` |
| resolve rank `r` | `C + (r+1)·G + L + 1` |
| resolve rank `N−1`, enter `DONE` | `C + N·G + L + 1` |

- **What sits between `box_store` and `keys_in` — wiring or a register?** Wiring, and nothing
  more. The key is a concurrent assignment from the `box_store` registers, and the sorter's
  first net is its input port with no register
  ([bitonic32.vhd:186](../src/components/bitonic32.vhd#L186), `net(0) <= keys_in`). So the
  sorter's first cut captures the current batch on edge 0 itself, *provided `keys_in` is
  already stable before edge 0*. That is the §1 contract, and it is what makes `keys_out` final
  after edge `C − 1` rather than `C`.
- **What does [tb_bitonic32.vhd:166-192](../test/tb_bitonic32.vhd#L166-L192) guarantee,
  exactly?** That for inputs held stable from before the first edge, the output **still shows
  the previous batch after `PIPE_CUTS − 1` edges and shows the new batch after `PIPE_CUTS`
  edges**. Latency is pinned from both sides: a missing cut fails the first check and an extra
  cut fails the second. It guarantees nothing if `keys_in` changes mid-sort, and the check is
  skipped entirely at `PIPE_CUTS = 0`. `nms_ctrl` relies on exactly this and no more: `C` edges
  with a stable input, then read.
- **Resolve trails fill by `L`** ([plan.md:773](plan.md#L773)). On the timeline, the last group
  of rank `r` is issued in the cycle after edge `C + r·G + G − 1`, and rank `r` resolves at edge
  `C + r·G + G − 1 + L + 2`. That is `L` lane stages, one `row_buf` capture and the resolve edge
  itself. The `L` offset is the lanes; the `+2` is the row buffer and the resolve register.

---

## 5. Counters and registers

| name | width | reset value | changes when | read by |
|---|---|---|---|---|
| `state` | 3 (5 states) | `IDLE` | every transition in §3 | all control |
| `cnt` | 6 (max 63 = `N·G − 1`) | none — cleared on every transition | each cycle outside `IDLE` | state exits, `row_src`, `col_grp` |
| `index_table` | 32 × 5 | none | every cycle in `IDLE` / `SORT` | `row_src` mux (one read port) |
| `tag_v` | `L` × 1 | all `'0'` | every cycle (shift) | row buffer write, `err` check |
| `tag_g`, `tag_idx` | `L` × (⌈log₂G⌉ + 5) = 4 × 6 | none | every cycle (shift) | row buffer write |
| `acc_row` | 32 (only `N − P` bits are ever used) | none | tag valid, `g < G − 1` | row buffer merge |
| `row_buf` | 32 | none | tag valid, `g = G − 1` | resolve |
| `row_idx` | 5 | none | with `row_buf` | resolve |
| `row_rdy` | 1 | `'0'` | every cycle: `'1'` for exactly one cycle per row | resolve |
| `valid_mask` | 32 | none — loaded at `start` | `start`; each resolve | resolve |
| `keep_mask` | 32 | zeros | cleared at `start`; each resolve | port (`frame_tx`, LEDs) |
| `res_cnt` | 6 (max 32 = `N`) | none — cleared at `start` | each resolve | `status` at `DONE` |
| `err` | 1, sticky | `'0'` | set by the §7 checks, cleared at `start` | `status` at `DONE` |

The storage matches [architecture.md §8](architecture.md): `acc_row + row_buf` plus `row_idx`
plus the tag pipe's index column is the "2 × (32 + 5) b" row store. It is a streaming buffer,
not the 32 × 32 matrix.

- **Is every width the minimum?** Yes. `cnt` has to reach `max(C − 1, N·G − 1, L) = 63` at
  P = 16, which is 6 bits. It is declared as `natural range 0 to N*G − 1` so it resizes with
  `P`: 11 bits at P = 1, 5 at P = 32. `res_cnt` must hold `N = 32` itself, not just `N − 1`,
  because it is compared against `N` at `DONE`; that is 6 bits. `index_table` holds 0–31 in
  5 bits. `status` is sent as a full byte on the wire, but only two of its values originate
  here.
- **Which need `rst`?** The ones that can create a strobe or an output someone acts on:
  `state`, the `tag_v` chain, `row_rdy`, `err`, and `keep_mask` (it drives the LEDs, so a
  post-reset display of zeros is part of the spec'd behaviour). Everything else is written
  before anything reads it. `cnt` is cleared on every transition, `valid_mask` / `res_cnt` are
  loaded at `start`, `index_table` is overwritten `C` edges before `FILL` reads it, and
  `acc_row` / `row_buf` / `row_idx` / `tag_idx` are read only under a valid bit that reset has
  cleared. This is the same argument as [iou_lane.vhd](../src/components/iou_lane.vhd), which
  resets its 4-bit valid chain and not its ~130 datapath bits, and as
  [bitonic32.vhd:26-28](../src/components/bitonic32.vhd#L26-L28), which has no reset because
  nothing reads it before `PIPE_CUTS` edges of fresh input.

---

## 6. Latency derivation

From §4 alone, write T as a sum of named terms, then compare with the spec formula
`T = N²/P + L + C + 2` ([plan.md:540-544](plan.md#L540-L544)), which charges SORT `C + 1`.

From §4, counting edges 0 through the edge that enters `DONE` (inclusive):

```
T =   C          sorter cuts                   edges 0 .. C−1
    + 1          index_table                   edge  C
    + N·G        fill: lane stage-1 captures   edges C+1 .. C+N·G
    + (L − 1)    lanes, stages 2..L            edges C+N·G+1 .. C+N·G+L−1
    + 1          row_buf                       edge  C+N·G+L
    + 1          resolve rank N−1              edge  C+N·G+L+1
  = N²/P + L + C + 2
```

The convention is the one [tb_bitonic32](../test/tb_bitonic32.vhd#L166-L192) uses: drive
`start` before edge 1 (here edge 0), and `done` is valid after edge T (here edge T − 1). In
other words T counts the edge that samples `start`.

- **Your T at P = 16, C = 8:** 64 + 4 + 8 + 2 = **78 cycles = 0.78 µs.** The same equations give
  72 at C = 2 (the spec's number), 38 at P = 32 / C = 0, and 1038 at P = 1 / C = 8, and all of
  these agree with the spec formula.
- **Does it match?** **Yes, term for term — and now in number too.** When this section was first
  written, [architecture.md §9](architecture.md) and `nms_pkg.LATENCY_CYCLES` still said 72,
  because both assumed `PIPE_CUTS = 2`, which reaches only 53.9 MHz ([results.md §5](results.md)).
  All three sources (architecture.md, then `params.py` and `nms_pkg.vhd`) have since moved to
  `PIPE_CUTS = 8`, and `LATENCY_CYCLES` = 78 is asserted in `tb_params` and `test_params`.
- **If SORT costs `C + 1`, name the register that supplies the `+1`.** It is **`index_table`**,
  and the design needs it for timing, not bookkeeping. The sorter's last sub-stage has no
  register after it (at `PIPE_CUTS = 8` the last cut is after sub-stage 14, per
  [bitonic32.vhd:127-137](../src/components/bitonic32.vhd#L127-L137)). Without `index_table`,
  sub-stage 15's CAS, the 5-bit index extraction, the 32:1 × 72 b row-source mux and lane stage
  1's min/max/subtract would all sit in one 10 ns cycle. It is also the register that lets the
  sorter's output go stale during `FILL` without harm.

  Note that the spec's "`L + 1` for drain plus final resolve" hides a trade inside it. Stage 1
  of the last issue is counted in fill, so the drain really has only `L − 1` lane stages, and
  the remaining `+1` is `row_buf`. Dropping `row_buf` (resolving straight from the lane outputs)
  would save a cycle and bring T to 77. It is kept because it makes resolve independent of `P`:
  resolve always sees a whole, stable, `N`-bit row, whatever `P` and `G` are, instead of a mix
  of held and live lane bits whose split changes with `P`.

---

## 7. Corner cases

For each: what the FSM does, cycle by cycle if it matters, and the spec line it satisfies.

| case | behaviour | spec |
|---|---|---|
| `present_mask = 0` | **Runs the normal fixed walk.** `valid_mask` loads as 0, so every resolve sees `kept = 0` and `keep_mask` stays 0; `done` comes after exactly T. No early exit, because an early exit is a data-dependent trip count, which would break Tier 1 #1 and #5 for one input. "Terminate immediately" is read as "the result is immediately determined", not "finish early". **Spec wording to change** (§9). | [plan.md:696-703](plan.md#L696-L703) |
| frame arrives while busy | `start` is ignored outside `IDLE`, with a simulation assertion warning that it happened. By the §1 start/busy rule, `frame_rx` never sends it: it blocks the `box_store` writes, remembers that it did, and replies `0x02` without starting. `nms_ctrl` finishes the current batch unaffected. Unreachable at 1 Mbaud (a frame takes 2.64 ms, a batch 0.78 µs), but defined. | L3, [plan.md:1168](plan.md#L1168) |
| `start` arrives during `DONE` | Ignored, like any `start` outside `IDLE`. `busy` is still `'1'` in `DONE`, so the §1 rule makes `frame_rx` reply `0x02` instead of losing the frame. `DONE` is not "almost idle". | §1 |
| illegal state encoding | 5 states in 3 bits leaves 3 unused codes. The state register carries `fsm_safe_state = "reset_state"`, so Vivado's FSM encoding recovers to `IDLE` rather than hanging. A `when others` arm cannot do it: the `case` covers every enumeration value, so VHDL rejects the arm as redundant, and it says nothing about encodings synthesis leaves unused anyway. Recovery emits no `done`; the host times out (O7). | [architecture.md §10](architecture.md) |
| CRC fail | Never reaches `nms_ctrl`: no `start`, and `frame_rx` asks for the `0x01` reply. The bad frame may already have overwritten `box_store` slots, which is harmless: no computation uses them, and the next good frame rewrites all 32 slots because the frame is fixed-length. | O7, [plan.md:1191](plan.md#L1191) |
| watchdog fires | **Replaced by two consistency checks** (deviation, §9). A cycle-count watchdog on a fixed counter walk can only fire if that same counter is broken, in which case it is counting with the broken counter. Instead `err` is set if **(a)** `lane_valid ≠ tag_v(L)` on any cycle — the lanes' real latency disagrees with the `LANE_LATENCY` the FSM was built for — or **(b)** `res_cnt ≠ N` at `DONE`, meaning a row was lost or never resolved. Either one makes `status = 0x03`. ~10 LUT, 1 FF. | [plan.md:866-871](plan.md#L866-L871) |
| `rst` asserted mid-batch | The synchronous reset returns `state` to `IDLE`, clears `tag_v`, `row_rdy`, `err` and `keep_mask`, and emits no `done`. The lanes' valid chains take the same `rst` ([iou_lane.vhd:174-178](../src/components/iou_lane.vhd#L174-L178)), so no in-flight result can resolve into the next batch. The host sees no reply and times out (O7). The next `start` runs normally. | [architecture.md §10](architecture.md) |
| sorter still holds the previous batch (no reset) | Cannot be observed. `index_table` is captured at edge `C`, after `C` edges with `keys_in` stable, so every cut register holds the current batch ([tb_bitonic32](../test/tb_bitonic32.vhd#L166-L192) pins exactly this). The stale output during edges `0 … C−1` is loaded into `index_table` and then overwritten, never read. | [bitonic32.vhd:26-28](../src/components/bitonic32.vhd#L26-L28) |
| `idx_r` travels with its row | `tag_idx` shifts alongside `tag_v` through `L` stages and lands in `row_idx` on the same edge as `row_buf`. `index_table` therefore has **one** read port (fill, via `row_src`), and resolve never indexes it. 20 FF instead of a second 32:1 × 5 b mux. | [plan.md:758-761](plan.md#L758-L761) |
| a kept box's row touching earlier ranks | **The whole row is applied, unmasked.** That includes bits of earlier ranks and the diagonal: `suppresses(k, k)` is always true (`I = U` gives `256·U ≥ 128·U`, and `0 ≥ 0` for a zero-area box), and all 640 committed `.trace` rows carry their own bit. This is harmless because every resolve clears `valid_mask(idx_r)` whether the box was kept or not, and `valid_mask` only ever loses bits, so every earlier rank's bit is already 0. That is P10's argument. A simulation-only assertion keeps it true under future edits: at each resolve, `valid_mask and resolved = 0`, where `resolved` is a sim-only mask of slots already resolved (synthesis trims it, since only an `assert` reads it). | P10, [plan.md:1147](plan.md#L1147) |

Resolve itself, as concurrent logic feeding one clocked update under `row_rdy`:

```
kept       = valid_mask(row_idx)
valid_mask ← valid_mask and not onehot(row_idx) and not (row_buf when kept else 0)
keep_mask  ← keep_mask or (onehot(row_idx) when kept else 0)
res_cnt    ← res_cnt + 1
```

This is [plan.md:774-775](plan.md#L774-L775) exactly, and the same step as `nms_allpairs` in
[model.py](../models/nms/model.py).

---

## 8. Verification plan (`test/tb_nms_ctrl.vhd`)

- **What does the testbench drive, and what is stubbed or instantiated for real?**
  - **Real `bitonic32`**, fed from `<case>.keys`. The reversal
    `index_table(r) = idx(keys_out(31 − r))` is the wiring error the sorter's own header says an
    integration test must catch ([bitonic32.vhd:11-13](../src/components/bitonic32.vhd#L11-L13)),
    and it can only be caught with the real output ordering.
  - **Stubbed lanes.** A testbench process models P lanes as an `L`-deep delay line. For an
    issue `(row_src, col_grp)`, it looks up the rank whose slot is `row_src` in `.order`, and
    returns bits `j + g·P` of that rank's row from `.trace`. It raises `lane_valid` `L` cycles
    after `issue_valid`. The stub also **asserts the issue schedule**: rows arrive in rank order
    (`row_src = order(r)`), groups arrive ascending, and there are exactly `N·G` issues.
  - **No `box_store`.** The payload and area muxes sit outside `nms_ctrl` (§2). The lane itself
    is already bit-exact against the model by `tb_iou_lane`. Real lanes and a real `box_store`
    meet this FSM at C4 in `tb_nms_top`.
  - The testbench drives `start`, `present_mask` (the last line of `.hex`) and `rst`, holds
    `keys` stable from before `start` until `done`, and loops over `cases.txt` like the other
    testbenches, so new cases are covered without editing VHDL.
- **Checks per case:**
  1. `keep_mask = <case>.mask`: the 32-bit equality.
  2. After each resolve edge `C + (r+1)·G + L + 1`, `keep_mask` equals `.trace` row `r`'s `keep`.
     This also pins *when* each rank resolves.
  3. After the same edge, `valid_mask` equals `.trace`'s `valid`. It is read through a
     VHDL-2008 external name (`<<signal .tb_nms_ctrl.dut.valid_mask : mask_t>>`). If GHDL 4.1
     refuses the external name, a `dbg_valid` output port is used instead, which `nms_top`
     leaves open and Vivado trims.
  4. `status = STATUS_OK`, `done` high for exactly one cycle, `busy` high from edge 0 to the end
     of `DONE`.
- **What does checking `.trace` catch that `.mask` alone cannot?** Errors that cancel by the end.
  For example: a row applied one rank late, when the box it should have suppressed is never a
  keeper anyway; the `kept` decision taken from the wrong rank on a case where both ranks were
  kept; or a `valid_mask` bit cleared early that no later rank tests. The final mask is one
  32-bit number summarising 32 decisions; the trace checks all 32 decisions and their timing.
  On the committed vectors, `.mask` alone would pass several of the mutants listed below.
- **How does it prove latency is exactly T, in both directions?** As `tb_bitonic32` does it:
  after edge `T − 2`, `done` must still be `'0'`, and after edge `T − 1` it must be `'1'`. The
  first check catches an FSM that is too fast, the second one that is too slow. Check 2 pins
  every intermediate resolve edge too, not just the last. The expected T is computed in the
  testbench from the generics, `N*N/P + LANE_LATENCY + PIPE_CUTS + 2`, not copied from
  `nms_pkg.LATENCY_CYCLES`, so a stale package constant cannot make the check agree with itself.
- **Directed cases, beyond the vector loop:**
  - `present_mask = 0` on a normal case: `keep_mask = 0` after exactly T.
  - `rst` asserted during `FILL`: no `done`; the next case then passes all checks.
  - `start` pulsed during `FILL`: ignored, result and latency unchanged.
  - All cases back to back with no reset between them: the stale-sorter and stale-row-buffer
    cases.
  - Stub latency set to `L + 1`: `status = 0x03`. This is the only way to exercise the
    internal-error path.
- **Configuration sweep** in `scripts/Makefile`:
  `SWEEP_tb_nms_ctrl = -gPIPE_CUTS=8 -gPIPE_CUTS=2 -gPIPE_CUTS=0 -gP=1 -gP=32`. That covers the
  shipped point, the old default, the combinational sorter (the `IDLE → FILL` path), and the two
  extremes of `G`.
- **How does it prove the checks can fail?** Each of the following mutants is applied to
  `nms_ctrl.vhd` and must fail the unmodified testbench, recorded in
  [build_log.md](build_log.md) as for `cas` and `bitonic32`:

  **Run 2026-09-24** (results in [build_log.md](build_log.md) C3). Every mutant below was
  killed, except the one marked:

  | mutant | caught by |
  |---|---|
  | `index_table(r) = idx(keys_out(r))` (unreversed) | stub schedule assertion |
  | `SORT` waits `C − 1` | stub schedule assertion (stale `index_table`) |
  | `DRAIN` lasts `L` instead of `L + 1` | latency check |
  | row applied without the `kept` test | check 3 (`valid_mask` trace) |
  | column groups swapped in the row merge | check 3 |
  | lane-latency consistency check disabled | the stub `L + 1` directed case |
  | `kept` reads the next row's index | check 2, at rank 18 of `all_equal` — only mid-batch |
  | resolve does not clear `valid_mask(row_idx)` when not kept | **survives: an equivalent mutant.** `kept` *is* `valid_mask(row_idx)`, so the bit is already 0 whenever the box is not kept. |

---

## 9. Open decisions

| # | decision | options | chosen | why |
|---|---|---|---|---|
| O4 | LED semantics | [plan.md:1185](plan.md#L1185) | **Deferred to `nms_top`**; recommended `keep_mask(15:0)` | Not this module's concern: `keep_mask` is a port and `status` is available beside it. The low half of the mask is the one display that works with no host attached ([plan.md:658](plan.md#L658)). |
| O6 | where `P` / `PIPE_CUTS` are set | [plan.md:1189](plan.md#L1189) | Generics on `nms_ctrl` defaulting to `nms_pkg`, passed through from `nms_top`, overridable with `ghdl -r -gP=…` | One testbench sweeps every configuration (§8) without editing source. It is the pattern `bitonic32` and `iou_lane` already use. |
| — | SORT costs `C` or `C + 1` | §6 | **`C + 1`** | The `+1` is `index_table`, and timing requires it (§6). |
| — | `PIPE_CUTS` value | 2 or 8 | **8 → T = 78** | 2 reaches only 53.9 MHz ([results.md §5](results.md)). **Spec change — applied** to architecture.md §9, `params.py` and `nms_pkg.vhd`. |
| — | `present_mask = 0` | early exit, or fixed walk | **Fixed walk** | Keeps latency data-independent (§7). **Spec wording change — applied** in architecture.md §3 and plan.md Part 2. |
| — | watchdog | cycle counter, or consistency check | **Consistency check** (`lane_valid` vs tag pipe; `res_cnt = N`) | Detects a real class of control bug rather than re-measuring the counter it guards (§7). **plan.md Part 2 updated to match.** |
| — | where `LOAD` lives | `nms_ctrl`, or `frame_rx` | **`frame_rx`** | §1. Clarification only: architecture.md §9's FSM line describes the system, and still does. |
| — | resolve cadence | "one rank per cycle" ([plan.md:773](plan.md#L773)) | **One resolve per `G` cycles**, each taking one cycle | Rows complete every `G` cycles, so resolve fires every other cycle at P = 16 and every cycle at P = 32. plan.md's Gantt chart already draws it this way; only the sentence is loose. |
| — | row buffer register | resolve from lane outputs directly (T − 1), or through `row_buf` | **Through `row_buf`** | Resolve sees a whole stable row whatever `P` is. The cost is 1 cycle (10 ns), and it matches architecture.md §8's 2-row buffer (§6). |
| — | row-source path timing | measure at C4, or add an issue register now | **Measure at C4; absorb through `LANE_LATENCY` if needed** | With port paths constrained, lane stage 1 alone takes 7.16 ns ([results.md](results.md) §5). That leaves 2.84 ns for the 32:1 × 72 b row mux and its 16-lane fanout, which is tight. If C4 misses, a registered keeper/candidate stage in the datapath is one more lane stage. `nms_ctrl` takes it as `LANE_LATENCY = 5` with no logic change (the tag pipe and `DRAIN` are both built from the generic), and T becomes 79. |

---

## 10. Self-review checklist

Judge the design against these before writing VHDL. For each, cite the section that satisfies it,
or say why it does not.

- [x] No trip count depends on the data ([plan.md:1061](plan.md#L1061)): every state exit
      compares `cnt` with a generic (§3). `present_mask = 0` runs the full walk (§7).
- [x] Latency is an equality, and §6's T is the number the testbench will assert. T = 78 at
      P = 16, C = 8, asserted from both sides from the generics (§6, §8), and it now agrees with
      architecture.md §9 and `nms_pkg.LATENCY_CYCLES`.
- [x] RTL fits the VHDL-93 subset ([plan.md:833-843](plan.md#L833-L843)). The design needs
      integer-range counters and ports, a `case` on an enumerated state, and `if generate` pairs
      for `C = 0` and `G = 1`, all of which are VHDL-93. The only 2008 construct (the external
      name) is in the testbench.
- [x] Processes are clocked only; combinational logic is concurrent assignments
      ([plan.md:845-850](plan.md#L845-L850)). `row_src`, `col_grp`, `issue_valid`, `busy`,
      `done`, `status`, `kept` and the next-mask expressions are concurrent (§3, §7). One
      clocked process holds the registers of §5.
- [x] The only asynchronous input is UART RX, and it is not this module's concern
      ([plan.md:864](plan.md#L864)). Every `nms_ctrl` input is synchronous to `clk` (§2).
- [x] Every port in §2 has a spec source or is marked **(new)** with a reason: three are new
      (`issue_valid`, `col_grp`, `done`), each justified in its row.
- [x] Every corner case in §7 has a behaviour and a test in §8. `present_mask = 0`, start while
      busy, `rst` mid-batch, stale sorter (back to back) and watchdog (stub `L + 1`) are directed
      tests. CRC fail is tested in `tb_frame_rx`, because by §1 it never reaches this module.
      `idx_r` and P10 are covered by `.trace` checks 2–3 and the resolved-slot assertion.

**Implemented 2026-09-24:** `src/components/nms_ctrl.vhd` and `test/tb_nms_ctrl.vhd`. They pass
at every sweep point, the mutation run is above, and the module measures 382 LUT / 300 FF /
175 MHz ([build_log.md](build_log.md) C3). Next is C4, which wires the row-source mux and has to
confirm the §9 timing row.
