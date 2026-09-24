-- nms_core -- the complete NMS compute core: store, sorter, control, and P IoU lanes.
--
-- Everything except the wire. D1 wraps this with frame_rx / frame_tx (UART, magic, CRC-8,
-- seq) to make the board-level top. The interface here is the one frame_rx drives: a record
-- write port, `start`, and the result.
--
--   frame_rx --we/waddr/wdata--> box_store --keys--> bitonic32 --keys_sorted--> nms_ctrl
--                                    ^  |                                          |
--                     row_src/col_grp|  +--row / candidates--> [issue regs] --> P x iou_lane
--                                    +-------------------------------------------+   |
--                                                            lane_valid / suppress <-+
--
-- ISSUE REGISTERS. The path from nms_ctrl's counter, through the index_table read, the
-- 32:1 x 72 b row-source mux and into lane stage 1, measures about 16.8 ns in segments
-- (docs/results.md section 5) -- it cannot close 100 MHz in one cycle. ISSUE_REGS inserts
-- registers along it:
--
--   0  none                     (functional reference; will not meet timing)
--   1  after the payload muxes  (select path ~9.7 ns: marginal)
--   2  after the selects, then after the payload muxes   <- shipped
--
-- To nms_ctrl these are simply more lane stages, so it is built with
-- LANE_LATENCY + ISSUE_REGS and needs no other change: its tag pipe and DRAIN come from that
-- generic, and its consistency check compares it against lane 0's real valid_out. Latency:
--
--   T = N*N/P + LANE_LATENCY + ISSUE_REGS + PIPE_CUTS + 2       (80 at P = 16, C = 8)
--
-- Contract with frame_rx (docs/fsm_design.md section 1): no write while busy, and start
-- only when busy = '0' and settled = '1'. Both are asserted in simulation below.
--
-- VHDL-93 subset; combinational logic as concurrent assignments, processes clocked only.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

use work.nms_pkg.all;

entity nms_core is
    generic (
        P          : positive := work.nms_pkg.P_DEFAULT;
        PIPE_CUTS  : natural  := work.nms_pkg.PIPE_CUTS;
        ISSUE_REGS : natural  := work.nms_pkg.ISSUE_REGS
    );
    port (
        clk : in std_logic;
        rst : in std_logic;

        -- record write port, from frame_rx
        we      : in  std_logic;
        waddr   : in  index_t;
        wdata   : in  record_t;
        settled : out std_logic;

        -- batch control
        start        : in  std_logic;
        present_mask : in  mask_t;
        busy         : out std_logic;

        -- result: keep_mask held until the next start
        done      : out std_logic;
        status    : out unsigned(7 downto 0);
        keep_mask : out mask_t
    );
end entity nms_core;

architecture rtl of nms_core is

    constant G : positive := N / P;

    -- box_store <-> bitonic32 <-> nms_ctrl
    signal keys        : key_array_t;
    signal keys_sorted : key_array_t;
    signal busy_i      : std_logic;
    signal settled_i   : std_logic;

    -- nms_ctrl's issue, as it leaves the FSM
    signal ctl_valid : std_logic;
    signal ctl_row   : index_t;
    signal ctl_grp   : natural range 0 to G - 1;

    -- the selects box_store actually sees (after issue register 1, if present)
    signal sel_valid : std_logic := '0';
    signal sel_row   : index_t   := (others => '0');
    signal sel_grp   : natural range 0 to G - 1 := 0;

    -- box_store's muxed outputs
    signal st_row_rec   : record_t;
    signal st_row_area  : area_t;
    signal st_cand_rec  : record_vec_t(0 to P - 1);
    signal st_cand_area : area_vec_t(0 to P - 1);

    -- what the lanes see (after issue register 2, if present)
    signal ln_valid     : std_logic := '0';
    signal ln_row_rec   : record_t  := (others => '0');
    signal ln_row_area  : area_t    := (others => '0');
    signal ln_cand_rec  : record_vec_t(0 to P - 1) := (others => (others => '0'));
    signal ln_cand_area : area_vec_t(0 to P - 1)   := (others => (others => '0'));

    -- lane results
    signal lane_valid_v  : std_logic_vector(P - 1 downto 0);
    signal lane_suppress : std_logic_vector(P - 1 downto 0);

begin

    assert ISSUE_REGS <= 2
        report "nms_core: ISSUE_REGS must be 0, 1 or 2" severity failure;

    busy    <= busy_i;
    settled <= settled_i;

    -- --- the blocks -----------------------------------------------------------------

    store : entity work.box_store
        generic map (P => P)
        port map (
            clk       => clk,
            rst       => rst,
            we        => we,
            waddr     => waddr,
            wdata     => wdata,
            settled   => settled_i,
            keys      => keys,
            row_src   => sel_row,
            row_rec   => st_row_rec,
            row_area  => st_row_area,
            col_grp   => sel_grp,
            cand_rec  => st_cand_rec,
            cand_area => st_cand_area
        );

    sorter : entity work.bitonic32
        generic map (PIPE_CUTS => PIPE_CUTS)
        port map (clk => clk, keys_in => keys, keys_out => keys_sorted);

    ctrl : entity work.nms_ctrl
        generic map (
            P            => P,
            PIPE_CUTS    => PIPE_CUTS,
            LANE_LATENCY => LANE_LATENCY + ISSUE_REGS
        )
        port map (
            clk           => clk,
            rst           => rst,
            start         => start,
            present_mask  => present_mask,
            busy          => busy_i,
            keys_sorted   => keys_sorted,
            issue_valid   => ctl_valid,
            row_src       => ctl_row,
            col_grp       => ctl_grp,
            lane_valid    => lane_valid_v(0),
            lane_suppress => lane_suppress,
            done          => done,
            status        => status,
            keep_mask     => keep_mask
        );

    -- --- issue register 1: the selects ----------------------------------------------

    sel_reg : if ISSUE_REGS = 2 generate
        process (clk)
        begin
            if rising_edge(clk) then
                sel_valid <= ctl_valid;
                sel_row   <= ctl_row;
                sel_grp   <= ctl_grp;
                if rst = '1' then
                    sel_valid <= '0';
                end if;
            end if;
        end process;
    end generate sel_reg;

    sel_wire : if ISSUE_REGS /= 2 generate
        sel_valid <= ctl_valid;
        sel_row   <= ctl_row;
        sel_grp   <= ctl_grp;
    end generate sel_wire;

    -- --- issue register 2: the muxed payloads ---------------------------------------

    pay_reg : if ISSUE_REGS >= 1 generate
        process (clk)
        begin
            if rising_edge(clk) then
                ln_valid     <= sel_valid;
                ln_row_rec   <= st_row_rec;
                ln_row_area  <= st_row_area;
                ln_cand_rec  <= st_cand_rec;
                ln_cand_area <= st_cand_area;
                if rst = '1' then
                    ln_valid <= '0';
                end if;
            end if;
        end process;
    end generate pay_reg;

    pay_wire : if ISSUE_REGS = 0 generate
        ln_valid     <= sel_valid;
        ln_row_rec   <= st_row_rec;
        ln_row_area  <= st_row_area;
        ln_cand_rec  <= st_cand_rec;
        ln_cand_area <= st_cand_area;
    end generate pay_wire;

    -- --- the lanes ------------------------------------------------------------------
    --
    -- Lane j compares the broadcast row-source box (the keeper) against candidate slot
    -- j + g*P. nms_ctrl reads lane 0's valid_out; all lanes share one valid_in, so the
    -- others are identical and left for synthesis to trim.

    lanes : for j in 0 to P - 1 generate
        lane : entity work.iou_lane
            port map (
                clk       => clk,
                rst       => rst,
                valid_in  => ln_valid,
                k_x       => ln_row_rec(X_SHIFT + COORD_W - 1 downto X_SHIFT),
                k_y       => ln_row_rec(Y_SHIFT + COORD_W - 1 downto Y_SHIFT),
                k_a       => ln_row_rec(A_SHIFT + COORD_W - 1 downto A_SHIFT),
                k_b       => ln_row_rec(B_SHIFT + COORD_W - 1 downto B_SHIFT),
                k_area    => ln_row_area,
                c_x       => ln_cand_rec(j)(X_SHIFT + COORD_W - 1 downto X_SHIFT),
                c_y       => ln_cand_rec(j)(Y_SHIFT + COORD_W - 1 downto Y_SHIFT),
                c_a       => ln_cand_rec(j)(A_SHIFT + COORD_W - 1 downto A_SHIFT),
                c_b       => ln_cand_rec(j)(B_SHIFT + COORD_W - 1 downto B_SHIFT),
                c_area    => ln_cand_area(j),
                valid_out => lane_valid_v(j),
                suppress  => lane_suppress(j)
            );
    end generate lanes;

    -- --- the frame_rx contract, checked in simulation -------------------------------

    contract : process (clk)
    begin
        if rising_edge(clk) then
            assert not (we = '1' and busy_i = '1')
                report "nms_core: box_store written while busy -- frame_rx must block "
                     & "the write and reply 0x02"
                severity error;
            assert not (start = '1' and (busy_i = '1' or settled_i = '0'))
                report "nms_core: start while busy or with an area still in flight"
                severity error;
        end if;
    end process contract;

end architecture rtl;
