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

- Fixed Thresholds and confidence variables : **8 bits** (two decimal precision, graunarailty is less than 0.01)
- The LHS and RHS sides should be able to compare **33 bits** values

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
