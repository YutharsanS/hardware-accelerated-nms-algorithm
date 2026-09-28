-- nms_ctrl -- the all-pairs control FSM: rank-ordered row fill, resolve trailing by L+2.
--
-- The design, with the cycle-level timeline this file implements, is docs/fsm_design.md.
-- In brief:
--
--   IDLE --start--> SORT (C) --> FILL (N*G) --> DRAIN (L+1) --> DONE (1) --> IDLE
--
-- where C = PIPE_CUTS, L = LANE_LATENCY and G = N/P column groups per row. Every exit
-- compares one shared counter with a generic, so no trip count depends on the data and the
-- latency is an equality: done appears T - 1 edges after the edge that samples start, with
--
--   T = N*N/P + LANE_LATENCY + PIPE_CUTS + 2          (78 at P = 16, C = 8)
--
-- FILL issues row r, group g to the lanes for r = 0..N-1 in RANK order. Resolve is not a
-- state: each row resolves the cycle after the row buffer captures it, driven by a valid
-- bit that travels with the data, so it overlaps the fill of later rows.
--
-- What this module does NOT do: read payloads or areas (the datapath muxes them using
-- row_src and col_grp), build sort keys (a concurrent assignment beside box_store), or
-- anything about framing. frame_rx pulses start only when busy = '0' and no box_store
-- write was blocked, which is what keeps the sorter's input stable for a whole batch.
--
-- VHDL-93 subset; combinational logic as concurrent assignments, one clocked process.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

use work.nms_pkg.all;

entity nms_ctrl is
    generic (
        -- IoU lanes. Must divide N, so every lane owns whole columns.
        P            : positive := work.nms_pkg.P_DEFAULT;
        -- Latency of bitonic32 in cycles; the SORT state lasts exactly this long.
        PIPE_CUTS    : natural  := work.nms_pkg.PIPE_CUTS;
        -- Registered stages between issue_valid and lane_valid. A datapath issue register,
        -- if timing ever needs one, is absorbed here as one more stage.
        LANE_LATENCY : positive := work.nms_pkg.LANE_LATENCY
    );
    port (
        clk : in std_logic;
        rst : in std_logic;

        -- one-cycle pulse from frame_rx: a good frame is in box_store, areas settled
        start        : in  std_logic;
        present_mask : in  mask_t;
        busy         : out std_logic;

        -- bitonic32.keys_out: ascending, so rank r is element N-1-r
        keys_sorted : in key_array_t;

        -- to the datapath and lanes
        issue_valid : out std_logic;
        row_src     : out index_t;
        col_grp     : out natural range 0 to N / P - 1;

        -- from the lanes
        lane_valid    : in std_logic;
        lane_suppress : in std_logic_vector(P - 1 downto 0);

        -- result, held until the next start
        done      : out std_logic;
        status    : out unsigned(7 downto 0);
        keep_mask : out mask_t
    );
end entity nms_ctrl;

architecture rtl of nms_ctrl is

    constant G : positive := N / P;

    type state_t is (S_IDLE, S_SORT, S_FILL, S_DRAIN, S_DONE);

    type grp_pipe_t is array (1 to LANE_LATENCY) of natural range 0 to G - 1;
    type idx_pipe_t is array (1 to LANE_LATENCY) of index_t;

    -- Bit i set: slot i. The resolve mask for one rank.
    function onehot (i : index_t) return mask_t is
        variable m : mask_t := (others => '0');
    begin
        m(to_integer(i)) := '1';
        return m;
    end function onehot;

    signal state : state_t := S_IDLE;

    -- Unused encodings recover to the reset state rather than hanging (fsm_design.md §7).
    -- This attribute is the mechanism, not a `when others` arm: every enumeration value is
    -- already covered, so VHDL rejects such an arm as redundant, and in any case a `case` on
    -- an enumeration says nothing about the encodings synthesis leaves unused.
    attribute fsm_safe_state : string;
    attribute fsm_safe_state of state : signal is "reset_state";

    -- One counter for every state, cleared on each transition. Its largest terminal value
    -- is N*G - 1 (FILL); SORT needs PIPE_CUTS - 1 and DRAIN needs LANE_LATENCY, both
    -- asserted below to fit.
    signal cnt : natural range 0 to N * G - 1 := 0;

    -- Rank -> slot, captured from the sorter while IDLE or SORT and frozen during FILL.
    -- The register is the "+1" in SORT's C + 1: without it the sorter's unregistered last
    -- sub-stage, the row-source mux and lane stage 1 would share one cycle.
    signal index_table : index_array_t := (others => (others => '0'));

    -- What was issued, travelling alongside the lanes so each result knows its row and
    -- column group. tag_idx is idx_r riding with its row, so index_table has one read port.
    signal tag_v   : std_logic_vector(1 to LANE_LATENCY) := (others => '0');
    signal tag_g   : grp_pipe_t := (others => 0);
    signal tag_idx : idx_pipe_t := (others => (others => '0'));

    -- The 2-row streaming buffer: acc_row gathers groups 0..G-2 of the row in flight,
    -- row_buf holds the completed row that resolve reads next cycle.
    signal acc_row   : mask_t  := (others => '0');
    signal row_merge : mask_t  := (others => '0');
    signal row_buf   : mask_t  := (others => '0');
    signal row_idx   : index_t := (others => '0');
    signal row_rdy   : std_logic := '0';

    signal valid_mask : mask_t := (others => '0');
    signal keep_reg   : mask_t := (others => '0');
    signal res_cnt    : natural range 0 to N := 0;
    signal err        : std_logic := '0';

    -- resolve, combinationally
    signal self_bit   : mask_t := (others => '0');
    signal kept       : std_logic := '0';
    signal valid_next : mask_t := (others => '0');
    signal keep_next  : mask_t := (others => '0');

    -- Simulation only: slots already resolved in this batch. Read by an assertion and
    -- nothing else, so synthesis removes it.
    signal resolved_sim : mask_t := (others => '0');

begin

    assert N mod P = 0
        report "nms_ctrl: P = " & integer'image(P) & " does not divide N = "
             & integer'image(N) & ", so a lane would own a partial column"
        severity failure;
    assert PIPE_CUTS <= N * G
        report "nms_ctrl: PIPE_CUTS does not fit the shared counter"
        severity failure;
    assert LANE_LATENCY <= N * G - 1
        report "nms_ctrl: LANE_LATENCY does not fit the shared counter"
        severity failure;

    -- --- issue ----------------------------------------------------------------------

    issue_valid <= '1' when state = S_FILL else '0';
    row_src     <= index_table(cnt / G);
    col_grp     <= cnt mod G;

    -- --- row buffer merge: the last group comes straight from the lanes -------------

    merge : for c in 0 to N - 1 generate
        last : if c / P = G - 1 generate
            row_merge(c) <= lane_suppress(c mod P);
        end generate last;
        held : if c / P /= G - 1 generate
            row_merge(c) <= acc_row(c);
        end generate held;
    end generate merge;

    -- --- resolve --------------------------------------------------------------------
    --
    -- The whole row is applied, earlier ranks and the diagonal included: every resolved
    -- slot's valid bit is already clear, so those bits are no-ops (plan.md P10).

    self_bit   <= onehot(row_idx);
    kept       <= valid_mask(to_integer(row_idx));
    valid_next <= valid_mask and not self_bit and not row_buf when kept = '1'
                  else valid_mask and not self_bit;
    keep_next  <= keep_reg or self_bit when kept = '1' else keep_reg;

    -- --- outputs --------------------------------------------------------------------

    busy      <= '0' when state = S_IDLE else '1';
    done      <= '1' when state = S_DONE else '0';
    keep_mask <= keep_reg;
    status    <= to_unsigned(STATUS_INTERNAL, 8) when err = '1' or res_cnt /= N
                 else to_unsigned(STATUS_OK, 8);

    -- --- the registers --------------------------------------------------------------

    regs : process (clk)
    begin
        if rising_edge(clk) then

            -- Pipeline tags: shift every cycle.
            tag_v(1)   <= '0';
            tag_g(1)   <= cnt mod G;
            tag_idx(1) <= index_table(cnt / G);
            if state = S_FILL then
                tag_v(1) <= '1';
            end if;
            for k in 2 to LANE_LATENCY loop
                tag_v(k)   <= tag_v(k - 1);
                tag_g(k)   <= tag_g(k - 1);
                tag_idx(k) <= tag_idx(k - 1);
            end loop;

            -- Row buffer: gather groups, complete the row on the last one.
            row_rdy <= '0';
            if tag_v(LANE_LATENCY) = '1' then
                for grp in 0 to G - 2 loop
                    if tag_g(LANE_LATENCY) = grp then
                        for j in 0 to P - 1 loop
                            acc_row(grp * P + j) <= lane_suppress(j);
                        end loop;
                    end if;
                end loop;
                if tag_g(LANE_LATENCY) = G - 1 then
                    row_buf <= row_merge;
                    row_idx <= tag_idx(LANE_LATENCY);
                    row_rdy <= '1';
                end if;
            end if;

            -- Resolve one rank.
            if row_rdy = '1' then
                valid_mask   <= valid_next;
                keep_reg     <= keep_next;
                res_cnt      <= res_cnt + 1;
                resolved_sim <= resolved_sim or self_bit;
            end if;

            -- index_table follows the sorter until FILL begins.
            if state = S_IDLE or state = S_SORT then
                for r in 0 to N - 1 loop
                    index_table(r) <= not keys_sorted(N - 1 - r)(INDEX_W - 1 downto 0);
                end loop;
            end if;

            -- The walk. Every exit compares cnt with a generic.
            case state is
                when S_IDLE =>
                    if start = '1' then
                        valid_mask   <= present_mask;
                        keep_reg     <= (others => '0');
                        res_cnt      <= 0;
                        err          <= '0';
                        resolved_sim <= (others => '0');
                        cnt          <= 0;
                        if PIPE_CUTS = 0 then
                            state <= S_FILL;
                        else
                            state <= S_SORT;
                        end if;
                    end if;

                when S_SORT =>
                    if cnt = PIPE_CUTS - 1 then
                        cnt   <= 0;
                        state <= S_FILL;
                    else
                        cnt <= cnt + 1;
                    end if;

                when S_FILL =>
                    if cnt = N * G - 1 then
                        cnt   <= 0;
                        state <= S_DRAIN;
                    else
                        cnt <= cnt + 1;
                    end if;

                when S_DRAIN =>
                    if cnt = LANE_LATENCY then
                        cnt   <= 0;
                        state <= S_DONE;
                    else
                        cnt <= cnt + 1;
                    end if;

                when S_DONE =>
                    state <= S_IDLE;
            end case;

            -- Consistency checks, standing in for a watchdog (fsm_design.md §7): the lanes'
            -- real latency must match the tag pipe, and no row may arrive outside the walk.
            if lane_valid /= tag_v(LANE_LATENCY) then
                err <= '1';
            end if;
            if row_rdy = '1' and (state = S_IDLE or state = S_SORT or state = S_DONE) then
                err <= '1';
            end if;

            if rst = '1' then
                state    <= S_IDLE;
                tag_v    <= (others => '0');
                row_rdy  <= '0';
                err      <= '0';
                keep_reg <= (others => '0');
            end if;

            -- Simulation-only checks.
            assert not (start = '1' and state /= S_IDLE)
                report "nms_ctrl: start while busy is ignored; frame_rx should have "
                     & "replied 0x02 instead of starting"
                severity warning;
            assert (valid_mask and resolved_sim) = (valid_mask'range => '0')
                report "nms_ctrl: an already-resolved slot is still valid, so applying a "
                     & "whole row is no longer a no-op on earlier ranks (plan.md P10)"
                severity error;
        end if;
    end process regs;

end architecture rtl;
