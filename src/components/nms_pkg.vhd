-- nms_pkg -- frozen constants and types for the NMS accelerator.
--
-- Mirrors docs/architecture.md, which is normative, and models/nms/params.py, which
-- mirrors the same numbers on the Python side. test/tb_params.vhd reports every constant
-- below and models/nms/test_params_agree.py fails the build if the two ever diverge.
--
-- As in params.py, widths are *derived* wherever a derivation exists, so changing COORD_W
-- or SCORE_W propagates rather than silently contradicting the rest of the file. The
-- handful that cannot be derived in the VHDL-93 subset without a helper function (INDEX_W,
-- COMPARE_W) are asserted in tb_params instead.
--
-- VHDL-93 subset: this package is analysed at --std=08 with everything else, but uses no
-- VHDL-2008 feature, so Vivado's partial 2008 synthesis support can never bite.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

package nms_pkg is

    -- --- batch ---------------------------------------------------------------------

    -- Boxes per batch. Fixed at 32: the combinational sorter is Theta(N log^2 N) and
    -- N = 64 needs 672 CAS ~ 20,160 LUT, which does not fit an XC7A35T at all.
    constant N : natural := 32;

    -- Bits to address a slot, i.e. log2(N). tb_params asserts 2**INDEX_W = N.
    constant INDEX_W : natural := 5;

    -- --- record --------------------------------------------------------------------

    -- 12 bits per coordinate: 11 is the 1080p minimum, 12 rounds an (x, y) pair to
    -- 24 bits so a whole record is byte-aligned at 64.
    constant COORD_W : natural := 12;

    -- 16 bits of detector confidence. Normative -- the 8-bit figure in older revisions
    -- of the architecture document referred to T_INT, not to the score.
    constant SCORE_W : natural := 16;

    constant RECORD_BITS  : natural := 4 * COORD_W + SCORE_W;
    constant RECORD_BYTES : natural := RECORD_BITS / 8;

    constant COORD_MAX : natural := 2 ** COORD_W - 1;
    constant SCORE_MAX : natural := 2 ** SCORE_W - 1;

    -- LSB position of each field within the 64-bit record, MSB-first as x, y, a, b,
    -- score. There are no spare bits: 4*12 + 16 = 64 exactly.
    constant SCORE_SHIFT : natural := 0;
    constant B_SHIFT     : natural := SCORE_SHIFT + SCORE_W;
    constant A_SHIFT     : natural := B_SHIFT + COORD_W;
    constant Y_SHIFT     : natural := A_SHIFT + COORD_W;
    constant X_SHIFT     : natural := Y_SHIFT + COORD_W;

    -- --- datapath ------------------------------------------------------------------

    -- COORD_MAX**2 = 16,769,025 fits 2*COORD_W bits.
    constant AREA_W : natural := 2 * COORD_W;

    -- 2 * COORD_MAX**2 = 33,538,050 needs one bit more than an area.
    constant UNION_W : natural := AREA_W + 1;

    -- Signed width of t_w / t_h before clamping, spanning [-COORD_MAX, COORD_MAX].
    constant T_INTERMEDIATE_W : natural := COORD_W + 1;

    -- Q0.8 fixed point. Not a stored signal: this is the shift amount in I << K_SHIFT.
    constant K_SHIFT : natural := 8;

    -- Threshold field width, and the threshold *value* in Q0.8: 128 / 2**8 = 0.5. The
    -- field range 0..255 covers thresholds up to 255/256; the two are different numbers.
    constant T_INT_W : natural := 8;
    constant T_INT   : natural := 128;

    constant LHS_W : natural := AREA_W + K_SHIFT;
    constant RHS_W : natural := T_INT_W + UNION_W;

    -- RHS is the wider of the two, so the comparison happens at RHS_W with LHS
    -- zero-extended. tb_params asserts RHS_W >= LHS_W rather than leaving it implied.
    constant COMPARE_W : natural := RHS_W;

    -- Sort key score & not(index): a strict total order, so ties are impossible and the
    -- bitonic network's instability can never be observed.
    constant KEY_W : natural := SCORE_W + INDEX_W;

    -- --- architecture --------------------------------------------------------------

    -- IoU lanes. Must divide N so every lane owns whole columns.
    constant P_DEFAULT : natural := 16;

    -- Registered stages in iou_lane: min/max + clamp, multiply, union + RHS, compare.
    constant LANE_LATENCY : natural := 4;

    -- Sub-stages in a Batcher bitonic network of N elements: k(k+1)/2 for k = log2(N).
    -- 15 at N = 32, which is also the CAS depth of the unpipelined critical path.
    constant SORT_SUBSTAGES : natural := INDEX_W * (INDEX_W + 1) / 2;

    -- Compare-and-swap units in the whole network: 240. At 8 + 2*ceil(KEY_W/2) = 30 LUT
    -- each, that is the 7,200 LUT (34.6%) the area budget is built on.
    constant CAS_COUNT : natural := SORT_SUBSTAGES * (N / 2);

    -- Register cuts inside the bitonic network, giving a 3-cycle sort. 0 is purely
    -- combinational and will not close 100 MHz; 14 is fully pipelined and costs 10k FF
    -- for throughput nothing can consume. One cut per sub-stage is the finest possible,
    -- so this is bounded above by SORT_SUBSTAGES.
    constant PIPE_CUTS : natural := 2;

    -- Exact batch latency at P_DEFAULT. An equality, not a bound: no term depends on the
    -- data, so worst case equals best case for every possible input.
    constant LATENCY_CYCLES : natural := N * N / P_DEFAULT + LANE_LATENCY + PIPE_CUTS + 2;

    -- --- wire protocol -------------------------------------------------------------

    constant MAGIC_0 : natural := 16#A5#;
    constant MAGIC_1 : natural := 16#5A#;

    constant STATUS_OK       : natural := 16#00#;
    constant STATUS_CRC_FAIL : natural := 16#01#;
    constant STATUS_BUSY     : natural := 16#02#;
    constant STATUS_INTERNAL : natural := 16#03#;

    -- magic(2) + 32 records + present_mask(4) + seq(1) + crc8(1).
    constant FRAME_BYTES_IN : natural := 2 + N * RECORD_BYTES + 4 + 1 + 1;

    -- status + seq + keep_mask.
    constant REPLY_BYTES : natural := 1 + 1 + 4;

    constant CLOCK_HZ : natural := 100_000_000;
    constant BAUD     : natural := 1_000_000;

    -- Exactly 100, so there is zero baud error. tb_params asserts the division is exact.
    constant BAUD_DIV : natural := CLOCK_HZ / BAUD;

    -- --- types ---------------------------------------------------------------------

    subtype coord_t  is unsigned(COORD_W - 1 downto 0);
    subtype score_t  is unsigned(SCORE_W - 1 downto 0);
    subtype index_t  is unsigned(INDEX_W - 1 downto 0);
    subtype key_t    is unsigned(KEY_W - 1 downto 0);
    subtype area_t   is unsigned(AREA_W - 1 downto 0);
    subtype record_t is unsigned(RECORD_BITS - 1 downto 0);
    subtype mask_t   is std_logic_vector(N - 1 downto 0);

    type key_array_t    is array (0 to N - 1) of key_t;
    type index_array_t  is array (0 to N - 1) of index_t;
    type area_array_t   is array (0 to N - 1) of area_t;
    type record_array_t is array (0 to N - 1) of record_t;

end package nms_pkg;
