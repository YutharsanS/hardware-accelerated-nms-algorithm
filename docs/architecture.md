08/31/2026

Target video frame (max) : 1080P - 1920 x 1080

Determines the upper bound for the coordinates (integers) 

$log_2(1920) \approx$ $log_2(1080) \approx 11\text{bits}$

So, the minimum data width we need is **22 bits** per coordinate (x, y) 
- 2 coordinate points (2 x 22 bits) and 16 bit confidence score = 60 bits per bounding box, doesn't quite fit standard byte offset
- **2 coordinate points (2 x 24 bits) and 16 bit confidence score = 64 bits per boudning box (8 bytes).**
**Fits the standard byte offset**

Total Block RAM usage = 32 boxes x 64 bits per box $\longrightarrow$ 2048 bits (0.001%)

- - -

- Confidence score : **16 bits** (carried in the 64-bit box word, byte aligned)
- IoU threshold `T_INT` : **8 bits** (Q0.8, granularity 1/256 < 0.01)
- The LHS and RHS sides should be able to compare **33 bits** values

- - -

09/04/2026

# Fixed-point format contract

All arithmetic in the NMS datapath is integer. The only floating-point values in
the problem (confidence scores and the IoU threshold) are quantised **once at the
software boundary**; nothing downstream of that boundary sees a float. The
conversions below therefore run on the host, never on the FPGA.

## Field formats

| Field | Signed | Int bits | Frac bits | Width | Conversion from float | Valid domain |
| --- | --- | --- | --- | --- | --- | --- |
| x / y / a / b | u | 12 | 0 | 12 | none, already integral | 0 to 4095 |
| confidence | u | 0 | 16 | 16 | `floor(c * (2^16 - 1))` | c in [0, 1] |
| threshold `T_INT` | u | 0 | 8 | 8 | `min(floor(T * 2^8), 255)` | T in [0, 1] |
| sort key | u | 21 | 0 | 21 | `(conf << 5) or (~idx & 0x1F)` | 32 boxes max |

Intermediate signal widths (`area`, `t_w`/`t_h`, `w`/`h`, `intersection_area`,
`union_area`, LHS, RHS) are tabulated in `models/golden-model.ipynb` and are
unchanged by this contract.

Confidence uses a full-scale map (denominator `2^16 - 1`, so 1.0 maps to 65535)
rather than a true Q0.16. This is safe because confidence is **only ever compared
against other confidences** in the sorting network -- the map only has to be
monotonic, the scale factor never appears in any arithmetic.

The threshold is a true Q0.8, and its scale factor **must** stay a power of two:
see the rejection criterion below.

## Rounding: truncation

Both conversions truncate. Since the conversion happens offline in software,
truncation and round-to-nearest cost the same (i.e. nothing) in hardware, so the
choice is made on behaviour rather than cost:

`T_INT = floor(T * 256) <= T * 256`, so the effective threshold is always at or
slightly below the requested one, by at most 1/256. The bias is **one-sided and
predictable**: fixed-point NMS suppresses at least as many boxes as the
floating-point reference, never fewer. Any divergence found during verification
should be explicable in that direction.

`T = 1.0` truncates to 256, which does not fit 8 bits, so the conversion
**saturates** to 255 (effective threshold 0.99609).

## Rejection criterion

```
IoU >= T   <=>   intersection / union >= T_INT / 2^k   <=>   intersection * 2^k >= T_INT * union
```

with `k = 8`, matching the fractional bits of `T_INT`. Division is never
performed. Because `k` is a fixed elaboration-time constant, `intersection * 2^k`
is a **left shift by 8** -- wiring only, no multiplier. This is why the threshold
scale factor must remain a power of two.

Both sides are zero-extended to **33 bits** for the comparison.

Worst-case check on the LHS: `4095 * 4095 * 256 = 4,292,870,400` against a 32-bit
ceiling of `4,294,967,295` -- it fits with roughly 0.05% headroom. The golden
model asserts every intermediate against its declared width rather than trusting
this table.

## Tie-breaking

A bitonic sorting network is not stable, so two boxes with equal quantised
confidence would be ordered arbitrarily. That is not merely cosmetic: if two tied
boxes overlap, whichever is processed first survives and suppresses the other, so
the tie-break changes the **survivor set**.

Resolution: sort on the 21-bit concatenation `{confidence[15:0], ~index[4:0]}` as
a single descending unsigned comparison. Indices are unique, so ties cannot occur.
The index is bitwise complemented (5 inverters in hardware, `~idx & 0x1F` in
Python, which must be masked because Python integers are unbounded) so that one
descending sort yields confidence descending **and** index ascending -- i.e. on a
tie, the earlier box wins.

The 5 index bits fix the capacity at **32 boxes**, matching the Block RAM budget
above. Widening the key leaves the network structure untouched; only the
comparators get wider.

## Open items

- Behaviour when `union_area == 0` (two degenerate boxes). The floating-point
  model raises `ZeroDivisionError`; the integer criterion evaluates without error.
  The hardware behaviour must be chosen deliberately and mirrored in the model.
- Recovery of the original box index after sorting, needed for the keep-mask
  export consumed by the VHDL testbench.

# Basys 3: AMD Artix™ 7 FPGA Trainer Board

## Features
* On-chip analog-to-digital converter (XADC)

---

## Key Specifications

| Specification | Details |
| :--- | :--- |
| **FPGA Part #** | XC7A35T-1CPG236C |
| **Logic Cells** | 33,280 (in 5,200 slices) |
| **Block RAM** | 1,800 Kbits |
| **DSP Slices** | 90 |
| **Internal Clock** | 450 MHz+ |
| **Quad-SPI Flash** | 4 MB |

---

## Connectivity and Onboard I/O

| Peripheral | Count / Details |
| :--- | :--- |
| **Pmod Connectors** | 3 |
| **User Switches** | 16 |
| **Push Buttons** | 5 |
| **User LEDs** | 16 |
| **7-Segment Display**| 4-Digit |
| **VGA Port** | 12-bit |
| **USB** | HID Host (Keyboard, Mouse, Mass Storage) |

---

## Electrical Specifications

* **Power Sources:** USB or 5V external supply (via pins)
* **Logic Level:** 3.3V

---

## Physical Dimensions

* **Width:** 2.8 in
* **Length:** 4.8 in
