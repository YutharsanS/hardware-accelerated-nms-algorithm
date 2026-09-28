-- box_store -- the batch: 32 payload records and their 32 areas, in registers.
--
-- REGISTERS, NOT BLOCK RAM (docs/architecture.md section 8). The lanes read P candidate
-- payloads plus one row-source payload every cycle -- 768 bits/cycle at P = 16, against the
-- 72 bits/cycle a BRAM36 port can deliver -- so the store is 2,048 + 768 flip-flops.
--
-- AREAS ARE COMPUTED ON WRITE. Each record's clamped area is a property of the box, not of
-- a pair, so it is computed once as the record lands, by one shared multiplier, and never
-- again. That is the difference between 1 DSP here and a second DSP in every lane. The
-- area lands one edge after its record; `settled` is low while one is in flight, and
-- frame_rx must not pulse start until it is high.
--
-- WRITE: one whole 64-bit record per `we`, slot `waddr`. frame_rx packs the 8 bytes first.
-- Back-to-back writes on consecutive cycles are supported, though the wire delivers at
-- most one per 80 us.
--
-- READ, all combinational from the registers:
--   keys       the 21-bit sort key per slot, score & not(index), for bitonic32
--   row_*      the row-source box, slot row_src, broadcast to every lane
--   cand_*(j)  lane j's candidate, slot j + col_grp*P -- static column striping, so each
--              lane muxes N/P payloads rather than needing an N:1 crossbar
--
-- The row-source mux is the path docs/fsm_design.md section 9 flags: it sits between
-- nms_ctrl's index_table and lane stage 1, and is measured in context at C4.
--
-- `rst` clears the area pipeline's valid bit only. Payloads have no validity -- present_mask
-- says which slots mean anything -- so resetting 2,816 flip-flops would buy nothing. A reset
-- that lands while an area is in flight drops that area; frame_rx resets with it and the
-- next frame rewrites every slot.
--
-- VHDL-93 subset; combinational logic as concurrent assignments, one clocked process.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

use work.nms_pkg.all;

entity box_store is
    generic (
        -- IoU lanes, which fixes the candidate striping. Must divide N.
        P : positive := work.nms_pkg.P_DEFAULT
    );
    port (
        clk : in std_logic;
        rst : in std_logic;

        -- write side, from frame_rx
        we      : in  std_logic;
        waddr   : in  index_t;
        wdata   : in  record_t;
        settled : out std_logic;

        -- to bitonic32
        keys : out key_array_t;

        -- row source, selected by nms_ctrl
        row_src  : in  index_t;
        row_rec  : out record_t;
        row_area : out area_t;

        -- the P lane candidates, selected by nms_ctrl
        col_grp   : in  natural range 0 to N / P - 1;
        cand_rec  : out record_vec_t(0 to P - 1);
        cand_area : out area_vec_t(0 to P - 1)
    );
end entity box_store;

architecture rtl of box_store is

    signal payload : record_array_t := (others => (others => '0'));
    signal area    : area_array_t   := (others => (others => '0'));

    -- the incoming record's fields
    signal w_x, w_y, w_a, w_b : coord_t := (others => '0');

    -- area pipeline: clamped width and height, then the product
    signal a_v    : std_logic := '0';
    signal a_addr : index_t   := (others => '0');
    signal a_w    : coord_t   := (others => '0');
    signal a_h    : coord_t   := (others => '0');

begin

    assert N mod P = 0
        report "box_store: P = " & integer'image(P) & " does not divide N = "
             & integer'image(N)
        severity failure;

    w_x <= wdata(X_SHIFT + COORD_W - 1 downto X_SHIFT);
    w_y <= wdata(Y_SHIFT + COORD_W - 1 downto Y_SHIFT);
    w_a <= wdata(A_SHIFT + COORD_W - 1 downto A_SHIFT);
    w_b <= wdata(B_SHIFT + COORD_W - 1 downto B_SHIFT);

    settled <= not a_v;

    -- --- read side ------------------------------------------------------------------

    key_gen : for i in 0 to N - 1 generate
        keys(i) <= payload(i)(SCORE_SHIFT + SCORE_W - 1 downto SCORE_SHIFT)
                   & not to_unsigned(i, INDEX_W);
    end generate key_gen;

    row_rec  <= payload(to_integer(row_src));
    row_area <= area(to_integer(row_src));

    cand_gen : for j in 0 to P - 1 generate
        cand_rec(j)  <= payload(j + col_grp * P);
        cand_area(j) <= area(j + col_grp * P);
    end generate cand_gen;

    -- --- registers ------------------------------------------------------------------

    regs : process (clk)
    begin
        if rising_edge(clk) then
            a_v <= we;
            if we = '1' then
                payload(to_integer(waddr)) <= wdata;
                a_addr <= waddr;
                -- The clamp: an inverted or degenerate box contributes 0, not a wrapped
                -- unsigned value. Identical to model.box_area and to the lane's clamp.
                if w_a > w_x then
                    a_w <= w_a - w_x;
                else
                    a_w <= (others => '0');
                end if;
                if w_b > w_y then
                    a_h <= w_b - w_y;
                else
                    a_h <= (others => '0');
                end if;
            end if;

            -- the one multiplier, shared by all 32 slots
            if a_v = '1' then
                area(to_integer(a_addr)) <= a_w * a_h;
            end if;

            if rst = '1' then
                a_v <= '0';
            end if;
        end if;
    end process regs;

end architecture rtl;
