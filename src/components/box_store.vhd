-- box_store -- the batch: 32 payload records and their 32 areas, in registers.
--
-- REGISTERS, NOT BLOCK RAM (docs/design/architecture.md section 8). Every cycle, each of the
-- P lanes reads one candidate box and they all share one row box: about 768 bits per cycle
-- at P = 16. A block RAM port can supply only 72 bits per cycle, far too few. So the store
-- is built from flip-flops instead: 2,048 for the box payloads and 768 for their areas.
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
-- The row-source mux is the path docs/design/fsm_design.md section 9 flags: it sits between
-- nms_ctrl's index_table and lane stage 1, and is measured in context in the integrated core.
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

    -- register array of 32 entries : the box storage itself
    signal payload : record_array_t := (others => (others => '0'));
    -- area entries calculated alongside with the payload at write stage
    signal area    : area_array_t   := (others => (others => '0'));

    -- the incoming record's fields
    signal w_x, w_y, w_a, w_b : coord_t := (others => '0');

    -- candidate
    signal c_w, c_h : coord_t;

begin
    -- Ensures even distribution among the data lanes
    assert N mod P = 0
        report "box_store: P = " & integer'image(P) & " does not divide N = "
             & integer'image(N)
        severity failure;
    -- Extracting data from the line
    w_x <= wdata(X_SHIFT + COORD_W - 1 downto X_SHIFT);
    w_y <= wdata(Y_SHIFT + COORD_W - 1 downto Y_SHIFT);
    w_a <= wdata(A_SHIFT + COORD_W - 1 downto A_SHIFT);
    w_b <= wdata(B_SHIFT + COORD_W - 1 downto B_SHIFT);

    -- flag to ensure data transfer - tied to `regs` process : 2 clock cycles
    c_w <= w_a - w_x when w_a > w_x else (others => '0');
    c_h <= w_b - w_y when w_b > w_y else (others => '0');    
    settled <= '1';

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
            -- clk edge:   1    2    3    4    5
            -- we:         1    0    1    1    0
            -- a_v:        -    1    0    1    1    0   (a_v after edge n = we just before edge n)

            -- Why it's needed: it's a valid bit, a flag that travels through the two-step area calculation alongside the data.
            if we = '1' then
                payload(to_integer(waddr)) <= wdata;
                area(to_integer(waddr)) <= c_w * c_h;
            end if;
        end if;
    end process regs;

end architecture rtl;
