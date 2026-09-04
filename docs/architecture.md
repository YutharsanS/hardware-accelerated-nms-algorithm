# NMS accelerator — architecture and frozen interface spec

**This document is normative.** `models/nms/params.py` and `src/components/nms_pkg.vhd` both
mirror the numbers below, and `models/nms/test_params_agree.py` fails the build if the three
ever disagree. Change this file first, then the two that follow it.

Target: **Basys 3 (XC7A35T-1CPG236C)**, 100 MHz, single 1080p video stream, **N = 32** bounding
boxes per batch.

---

## 1. Coordinate width

1080p gives the upper bound on coordinate values:

$$\log_2(1920) \approx \log_2(1080) \approx 11 \text{ bits}$$

so 11 bits per coordinate is the minimum, i.e. **22 bits for an (x, y) pair**. Packing a box as
two pairs plus a 16-bit score gives 2×22 + 16 = **60 bits**, which is not byte-aligned. Rounding
each pair up to 24 bits gives:

$$2 \times 24 + 16 = 64 \text{ bits} = 8 \text{ bytes per box}$$

So **each coordinate is 12 bits**, spanning 0–4095, which covers 1920×1080 with headroom.

> **Read the "22 bits" above as 2 × 11 for the pair, not 22 bits per single coordinate.** That
> phrasing was misread once already, as a claim that this spec used 11-bit fields and therefore
> disagreed with the golden model's 12. Both have always said 12; only the wording differed.

---

## 2. Record format — frozen

64 bits per box, **MSB → LSB**:

| bits | 63:52 | 51:40 | 39:28 | 27:16 | 15:0 |
|---|---|---|---|---|---|
| field | `x` | `y` | `a` | `b` | `score` |
| width | u12 | u12 | u12 | u12 | u16 |

There are **no spare bits**: 4×12 + 16 = 64 exactly. The keep/suppress result is therefore
returned as a separate mask (§3), not as a flag inside the record.

**Corner convention:** `(x, y)` is the lower-left corner and `(a, b)` the upper-right, with y
increasing upward. Image pipelines usually emit y-down; IoU is invariant under a global y-flip, so
the host may supply either provided `b > y` holds after its own transform. The generator and the
testbenches both use y-up.

**Score:** `score = round(f × 65535)` for a detector confidence `f` in [0, 1]. Floats never cross
into the RTL or the vector files.

---

## 3. Wire protocol — frozen

**One byte-order rule, no exceptions: every multi-byte field is transmitted most-significant byte
first.** The host uses `int.from_bytes(b, "big")` in both directions.

| direction | bytes | content |
|---|---|---|
| host → FPGA | 0..1 | `magic` = `0xA5 0x5A` |
| host → FPGA | 2..257 | 32 records, 8 B each. Slot *i* is the *i*-th record. |
| host → FPGA | 258..261 | `present_mask`, 4 B |
| host → FPGA | 262 | `seq`, 1 B frame counter, wraps at 256 |
| host → FPGA | 263 | `crc8` over bytes 2..262 |
| FPGA → host | 0 | `status`: `0x00` OK, `0x01` CRC fail, `0x02` busy, `0x03` internal error |
| FPGA → host | 1 | `seq`, echoed |
| FPGA → host | 2..5 | `keep_mask`, 4 B (zero when `status ≠ 0`) |

**264 bytes in, 6 out.** At 1 Mbaud (8N1, divider exactly 100 from the 100 MHz clock) that is
2.70 ms round trip — 8.1% of a 33 ms frame budget.

A plain byte counter is **not** sufficient framing: one dropped or spurious byte would
desynchronise permanently and corrupt every later frame in a way that looks exactly like an RTL
bug. The magic word gives resynchronisation, the CRC rejects corrupt frames before they reach the
datapath, `seq` prevents a late reply being mis-attributed after a timeout, and an idle timeout (no
byte for more than two byte-times mid-frame) returns the receiver to hunting.

**`present_mask`** — bit *i* means slot *i* holds a real detection. Its only use is as the load
value of `valid_mask` at start-of-batch instead of all-ones, so an absent slot is never a keeper
and never dispatched; since `keep_mask` resets to zero its output bit is provably zero.
`present_mask = 0` is well defined: terminate immediately with `keep_mask = 0`. All 32 record slots
are always transmitted regardless — only the mask says which are meaningful — which keeps the frame
fixed-length.

**`keep_mask`** — bit *i* means the *i*-th box sent survived. Both masks are indexed by **arrival
order**, so the host never reorders and a testbench check is a single 32-bit equality.

---

## 4. Datapath widths — frozen

All values derived for 12-bit coordinates. `k = 8` (Q0.8).

| signal | sign | width | max value | note |
|---|---|---|---|---|
| `x, y, a, b` | u | 12 | 4,095 | |
| `bw = a-x`, `bh = b-y` | u | 12 | 4,095 | **clamped**: `(a > x) ? a - x : 0` |
| `area1, area2` | u | 24 | 16,769,025 | per **box**, not per pair — precomputed once |
| `xx, yy, aa, bb` | u | 12 | 4,095 | max/max/min/min of the pair |
| `t_w, t_h` | **s** | 13 | ±4,095 | signed intermediate |
| `w, h` | u | 12 | 4,095 | clamped to 0 |
| `I = w·h` | u | 24 | 16,769,025 | 1 DSP |
| `U = area1 + area2 − I` | u | 25 | 33,538,050 | via an s26 intermediate |
| `LHS = I << 8` | u | 32 | 4,292,870,400 | fits 2³² |
| `RHS = T_INT · U` | u | 33 | 8,552,202,750 | fits 2³³ |
| compare | — | 33 | — | zero-extend LHS |

**`T_INT` is u8, range 0 to 255, default 128.** The value 128 is the threshold itself in Q0.8:
`128 / 2⁸ = 0.5`. The range 0–255 is what the 8-bit field can hold, i.e. thresholds from 0 to
255/256 ≈ 0.996. These are two different numbers and both are needed.

**`2^k` is not a signal.** `k = 8` is a shift compiled into the RTL as `I << 8`; nothing stores it.

> **Correction to an earlier revision of this document**, which read *"Fixed Thresholds and
> confidence variables: 8 bits"*. That contradicted the 16-bit score in the record above.
> **The score is 16 bits and is normative**; the 8-bit figure applies only to `T_INT`.

---

## 5. Suppression predicate — frozen

Suppress a candidate when

$$\text{I} \times 2^{8} \ \ge\ \text{T\_INT} \times \text{U}$$

Division is never performed. `>=`, not `>`. The golden model evaluates the identical integer
expression, so agreement with the RTL is bit-exact by construction rather than by tolerance.

With `T_INT = 128` this reduces to `2·I ≥ U` — two shifts and a compare, costing **zero DSPs** for
that half. It is still written as a multiply so a different threshold works; synthesis folds the
constant.

---

## 6. Sort key and tie-breaking — frozen

Sort **descending** on the 21-bit unsigned key

```
K = score(16) & not(index)(5)        i.e.  K = score·32 + (31 − index)
```

Indices are unique, so `K` is a **strict total order** and ties are impossible — the bitonic
network's instability can never be observed, at zero hardware cost. Descending `K` means
descending score with ties broken by **lower index first**, matching a stable Python sort on input
order. The Python equivalent, asserted in a unit test, is `sorted(range(32), key=lambda i:
(-score[i], i))`. The index is recovered as `31 − K(4 downto 0)`.

This matters more than it appears: at 8-bit score resolution, 32 draws from 256 values collide with
probability ≈ 86%, so ties are the common case rather than an edge case.

Only `{score, index}` — 21 bits — traverses the sorter. Payloads never move.

---

## 7. Degenerate boxes — frozen

Clamp, in both hardware and the model: `bw = (a > x) ? a − x : 0`, and likewise for the
intersection. An inverted box therefore has area 0 rather than wrapping to a huge unsigned value.

With `I = 0` and `U = 0` the predicate gives `0 ≥ 0` → **suppress**. This is specified behaviour,
and it also removes a divide-by-zero that a float formulation would hit.

**Provable, not hoped for:** `U` can never underflow. If either box is degenerate or inverted then
`min(a₁,a₂) ≤ aᵢ ≤ xᵢ ≤ max(x₁,x₂)`, so `t_w ≤ 0` and the clamp forces `I = 0`. Hence
`area = 0 ⟹ I = 0`; otherwise `I ≤ min(area₁, area₂)`. Either way `U ≥ 0`.

---

## 8. Storage — registers, not block RAM

| store | size | implementation |
|---|---|---|
| Box payloads | 32 × 64 b = 2,048 bits | **registers** (2,048 FF, 4.9%) |
| Per-box areas | 32 × 24 b = 768 bits | **registers** (768 FF) |
| Suppression rows | 2 × (32 + 5) b | **registers** (~74 FF) |
| `index_table` | 32 × 5 b | registers (160 FF) |

> **Correction to an earlier revision**, which read *"Total Block RAM usage = 32 boxes × 64 bits
> → 2048 bits (0.001%)"*. Two problems. The percentage was wrong — 2,048 of 1,800 Kbit is 0.11%,
> not 0.001%. More importantly **BRAM cannot be used at all**: with P lanes reading in parallel the
> store must deliver 768 bits/cycle at P=16, while a BRAM36 port delivers at most 72 bits/cycle.
> BRAM would only suffice at P=1, so low P is not "free" either.

---

## 9. Architecture

| block | function |
|---|---|
| `frame_rx` / `frame_tx` | magic hunt, CRC-8, `seq`, idle timeout, byte↔record packing |
| `box_store` | 32×64 b payloads + 32×24 b areas, areas computed during `LOAD` |
| `bitonic32` | 240 CAS on 21-bit keys, `PIPE_CUTS = 2` → 3 cycles. Output ascending, so the rank table reads reversed: `index_table(r) = idx(out(31−r))` |
| `iou_lane` × P | `P = 16` default, `P ∈ {1,2,4,8,16,32}`. Lane *j* owns columns j, j+P, …, so it muxes 32/P payloads rather than a 32:1 crossbar |
| row buffer | 2 rows of `S` plus `idx_r`, streaming |
| resolve | one rank per cycle, trailing the fill by `L` |

**Lane pipeline, L = 4:** (1) min/max, subtract, clamp; (2) `I = w·h` (DSP); (3) `U`, `RHS`, `LHS`;
(4) 33-bit compare → `suppress`.

**FSM:** `IDLE → LOAD → SORT(3) → FILL(N·⌈N/P⌉, resolve overlapped) → DRAIN(L) → DONE`.

**Latency is an equality, not a bound:**

```
T = N²/P + L + C + 2
```

At P = 16 that is **72 cycles = 0.72 µs** on the 100 MHz clock. There is no data-dependent term, so
worst case = best case for every possible input. That is the "deterministic execution" property, and
it is the claim that actually holds.

**Area budget at P = 16** (estimates; measured figures go to `docs/results.md`):

| block | LUT | FF | DSP |
|---|---|---|---|
| `bitonic32` | 7,200 | 1,344 | 0 |
| 16 × `iou_lane` | 3,424 | ~2,400 | 16 |
| row-source payload mux (32:1 × 48 b) | 530 | — | 0 |
| row-source area mux (32:1 × 24 b) | 264 | — | 0 |
| payload + area + row + index registers | — | 3,050 | 1 |
| masks, resolve, FSM, UART | ~900 | ~450 | 0 |
| **total** | **≈12,320 (59%)** | **≈7,240 (17%)** | **17 (19%)** |

---

## 10. Conventions

* Single **100 MHz** clock domain, Basys 3 pin **W5**.
* Synchronous **active-high** `rst`, from a debounced BTNC plus power-on reset.
* `ieee.numeric_std` throughout; no `std_logic_arith`.
* **Synthesisable RTL in the VHDL-93 subset**; testbenches in VHDL-2008 for `HREAD`. Everything is
  analysed at `--std=08`, which accepts both. Rationale: Vivado's VHDL-2008 synthesis support is a
  documented subset, and the RTL needs no 2008 feature. "VHDL-93 everywhere" is *not* available —
  the system GHDL ships no `ieee.std_logic_textio`, and `HREAD` lives in VHDL-2008's
  `std_logic_1164`.
* **Combinational logic as concurrent assignments, never combinational processes**; processes are
  clocked only, with `(clk)` as the whole sensitivity list. This makes an incomplete sensitivity
  list — itself a sim/synth mismatch — unrepresentable. GHDL also analyses with `-Wsensitivity`.
* The UART RX pin is the **only** asynchronous input: 2-flop synchroniser, no exceptions.
* Every testbench **terminates on its own** and ends with `report "PASS"`.

---

## 11. Scope limits, stated rather than left unmentioned

* **Single class.** Real NMS runs per class; this record carries no class ID. A 4-bit class carved
  from the score would give 16 classes and re-run the FSM per class in 16 × 0.72 µs = 12 µs, still
  trivial against 2.70 ms of link time — but that is not built.
* **N = 32 fixed.** The combinational sorter is `Θ(N log²N)`: N = 64 needs 672 CAS ≈ 20,160 LUT and
  does **not** fit this device. A folded sorter is the path past that.
* **The UART is a test harness, not the datapath.** Transport is 2.70 ms against 0.72 µs of
  compute. The report must present **core latency** and determinism, not a system speedup;
  end-to-end, doing NMS on the host CPU is faster. See `docs/build_log.md` and the plan's Part 1e.

---

# Basys 3: AMD Artix™ 7 FPGA Trainer Board

## Key specifications

| Specification | Details |
| :--- | :--- |
| **FPGA Part #** | XC7A35T-1CPG236C |
| **Logic Cells** | 33,280 (in 5,200 slices) |
| **LUT6 / Flip-flops** | 20,800 / 41,600 |
| **Block RAM** | 1,800 Kbits (50 × 36 Kb) |
| **DSP Slices** | 90 (DSP48E1, 25×18) |
| **Clock** | 100 MHz oscillator on pin W5; MMCM to 450 MHz+ |
| **Quad-SPI Flash** | 4 MB |
| **External DRAM** | **none** |

## Connectivity and onboard I/O

| Peripheral | Count / Details |
| :--- | :--- |
| **USB-UART** | FT2232HQ, 2 pins to the FPGA (`RsRx` B18, `RsTx` A18) — no FIFO mode |
| **Pmod Connectors** | 3 |
| **User Switches** | 16 |
| **Push Buttons** | 5 |
| **User LEDs** | 16 |
| **7-Segment Display** | 4-digit |
| **VGA Port** | 12-bit |
| **USB** | HID Host (keyboard, mouse, mass storage) |
| **Ethernet** | none |

## Electrical and physical

* **Power:** USB or 5 V external supply (via pins); logic level **3.3 V**
* **Dimensions:** 2.8 in × 4.8 in
* **On-chip ADC:** XADC
