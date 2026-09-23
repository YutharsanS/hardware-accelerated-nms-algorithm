-- iou_lane -- decides whether one keeper suppresses one candidate.
--
-- P of these run in parallel (P = 16 by default). Each owns matrix columns j, j+P, j+2P,
-- ... so it muxes 32/P payloads rather than needing a 32:1 crossbar; that mux lives in the
-- surrounding logic, and the lane sees one pair per cycle.
--
-- THE PREDICATE, and the reason it looks like this (docs/architecture.md sections 5 and 7):
--
--     suppress  when  I * 2**K_SHIFT  >=  T_INT * U
--
-- Cross-multiplied, so no divider and no divide-by-zero. `>=`, not `>`. At T_INT = 128 it
-- reduces to 2I >= U, two shifts and a compare, but it is written as a multiply so a
-- different threshold works and synthesis folds the constant.
--
-- Areas arrive as inputs. They are per-BOX, not per-pair, so box_store computes all 32
-- once during LOAD; recomputing them here would cost a second multiplier per lane and was
-- the error behind the plan's original "3 DSPs per lane" figure. One lane is 1 DSP.
--
-- FOUR REGISTERED STAGES, so LANE_LATENCY = 4:
--   1  min/max, subtract, clamp        -> w, h
--   2  I = w * h                       -> the DSP
--   3  U = k_area + c_area - I, LHS, RHS
--   4  33-bit compare                  -> suppress
--
-- `rst` clears the valid chain only, not the datapath. A stale coordinate is harmless
-- because its result is never marked valid, whereas a stale `valid` would write a
-- suppression bit that no box asked for. Resetting 4 flip-flops instead of ~130 is the
-- point, and it is why the datapath registers carry initial values rather than a reset.
--
-- VHDL-93 subset; combinational logic as concurrent assignments, processes clocked only.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

use work.nms_pkg.all;

entity iou_lane is
    generic (
        -- IoU threshold in Q0.(K_SHIFT). 128/2**8 = 0.5. Range 0 .. 2**T_INT_W - 1.
        T_INT   : natural := work.nms_pkg.T_INT;
        K_SHIFT : natural := work.nms_pkg.K_SHIFT
    );
    port (
        clk      : in  std_logic;
        rst      : in  std_logic;
        valid_in : in  std_logic;

        -- the keeper: the box that survived and is doing the suppressing
        k_x, k_y, k_a, k_b : in coord_t;
        k_area             : in area_t;

        -- the candidate being tested against it
        c_x, c_y, c_a, c_b : in coord_t;
        c_area             : in area_t;

        valid_out : out std_logic := '0';
        suppress  : out std_logic := '0'
    );
end entity iou_lane;

architecture rtl of iou_lane is

    -- --- stage 1, combinational: the intersection rectangle -------------------------
    --
    -- An intersection maximises the lower-left corner and minimises the upper-right.
    --
    -- These and the intermediates below carry initial values so the comparators read 0
    -- rather than 'U' during the first delta cycle. Same lesson as bitonic32: an
    -- uninitialised combinational net feeding numeric_std produces a "metavalue detected"
    -- warning per compare, and that noise would later hide a real one. Synthesis ignores
    -- an initial value on a combinational net.
    signal xx : coord_t := (others => '0');
    signal yy : coord_t := (others => '0');
    signal aa : coord_t := (others => '0');
    signal bb : coord_t := (others => '0');

    -- Signed, one bit wider than a coordinate, spanning -4095 .. 4095. The sign bit *is*
    -- the clamp decision, so a single 13-bit subtract yields both "do the boxes miss?" and
    -- the overlap extent -- cheaper than a separate comparator, and it is the
    -- T_INTERMEDIATE_W signal docs/architecture.md section 4 specifies.
    signal t_w : signed(T_INTERMEDIATE_W - 1 downto 0) := (others => '0');
    signal t_h : signed(T_INTERMEDIATE_W - 1 downto 0) := (others => '0');

    signal w_clamped : coord_t := (others => '0');
    signal h_clamped : coord_t := (others => '0');

    -- --- pipeline registers ---------------------------------------------------------
    signal s1_w, s1_h                 : coord_t := (others => '0');
    signal s1_k_area, s1_c_area       : area_t  := (others => '0');
    signal s2_inter                   : area_t  := (others => '0');
    signal s2_k_area, s2_c_area       : area_t  := (others => '0');
    signal s3_lhs                     : unsigned(LHS_W - 1 downto 0) := (others => '0');
    signal s3_rhs                     : unsigned(RHS_W - 1 downto 0) := (others => '0');
    signal valid_d                    : std_logic_vector(1 to LANE_LATENCY)
                                        := (others => '0');

    -- --- stage 3, combinational -----------------------------------------------------
    signal area_sum : unsigned(UNION_W - 1 downto 0) := (others => '0');
    signal union    : unsigned(UNION_W - 1 downto 0) := (others => '0');

begin

    -- The generic must fit the field the wire protocol reserves for it, or a threshold
    -- that cannot be transmitted would silently work in simulation.
    assert T_INT <= 2 ** T_INT_W - 1
        report "iou_lane: T_INT = " & integer'image(T_INT) & " does not fit "
             & integer'image(T_INT_W) & " bits"
        severity failure;

    -- --- stage 1 --------------------------------------------------------------------

    xx <= k_x when k_x > c_x else c_x;
    yy <= k_y when k_y > c_y else c_y;
    aa <= k_a when k_a < c_a else c_a;
    bb <= k_b when k_b < c_b else c_b;

    t_w <= signed('0' & aa) - signed('0' & xx);
    t_h <= signed('0' & bb) - signed('0' & yy);

    -- Clamp to zero when the boxes miss on that axis. Without this an inverted or
    -- degenerate box would wrap to a huge unsigned value instead of contributing nothing,
    -- and the model -- which clamps -- would disagree on the same input.
    w_clamped <= unsigned(t_w(COORD_W - 1 downto 0)) when t_w > 0
                 else (others => '0');
    h_clamped <= unsigned(t_h(COORD_W - 1 downto 0)) when t_h > 0
                 else (others => '0');

    -- --- stage 3 --------------------------------------------------------------------

    area_sum <= resize(s2_k_area, UNION_W) + resize(s2_c_area, UNION_W);
    union    <= area_sum - resize(s2_inter, UNION_W);

    -- --- the pipeline ---------------------------------------------------------------

    stages : process (clk)
    begin
        if rising_edge(clk) then
            -- stage 1 -> 2
            s1_w      <= w_clamped;
            s1_h      <= h_clamped;
            s1_k_area <= k_area;
            s1_c_area <= c_area;

            -- stage 2 -> 3: the one genuinely per-pair multiply, and the lane's only DSP
            s2_inter  <= s1_w * s1_h;
            s2_k_area <= s1_k_area;
            s2_c_area <= s1_c_area;

            -- stage 3 -> 4
            s3_lhs <= shift_left(resize(s2_inter, LHS_W), K_SHIFT);
            s3_rhs <= to_unsigned(T_INT, T_INT_W) * union;

            -- stage 4: the compare, at the width of the wider operand
            if resize(s3_lhs, COMPARE_W) >= resize(s3_rhs, COMPARE_W) then
                suppress <= '1';
            else
                suppress <= '0';
            end if;

            -- A live check of the proof in docs/architecture.md section 7: the clamps make
            -- area = 0 imply I = 0, and otherwise I <= min(area1, area2), so the union can
            -- never underflow. Simulation-only -- it mirrors the same assertion in the
            -- golden model, and an unsigned wrap here would otherwise look like a
            -- plausible wrong answer rather than a fault.
            assert area_sum >= resize(s2_inter, UNION_W)
                report "iou_lane: union underflow, I = "
                     & integer'image(to_integer(s2_inter)) & " exceeds the area sum "
                     & integer'image(to_integer(area_sum))
                severity error;

            -- Valid follows the data by exactly LANE_LATENCY cycles. Reset clears this
            -- chain and nothing else, so no stale result is ever marked valid.
            if rst = '1' then
                valid_d <= (others => '0');
            else
                valid_d <= valid_in & valid_d(1 to LANE_LATENCY - 1);
            end if;
        end if;
    end process stages;

    valid_out <= valid_d(LANE_LATENCY);

end architecture rtl;
