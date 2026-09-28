-- tb_nms_ctrl -- self-checking testbench for the all-pairs control FSM.
--
-- The REAL bitonic32 feeds the DUT, because the rank reversal index_table(r) =
-- idx(keys_out(N-1-r)) is the wiring error an integration test exists to catch, and only
-- the real output ordering can catch it. The LANES ARE STUBBED: a process replays each
-- row's suppression bits from the golden model's <case>.trace with exactly LANE_LATENCY
-- cycles of delay, and asserts that rows are issued in rank order with groups ascending.
-- No box_store is needed -- the payload muxes sit outside nms_ctrl -- and the lane itself
-- is already bit-exact against the model in tb_iou_lane.
--
-- Checks per case, over every case in cases.txt, run back to back with no reset:
--
--   1. keep_mask = <case>.mask, the 32-bit equality
--   2. after every resolve edge, keep_mask and valid_mask equal the .trace row for that
--      rank -- which also pins WHEN each rank resolves
--   3. latency is an EQUALITY: done is still '0' after edge T-2 and '1' after edge T-1,
--      with T computed here from the generics rather than copied from nms_pkg
--   4. busy high from edge 0 until DONE ends, done high for exactly one cycle, status OK
--
-- Then directed cases: start pulsed during FILL (ignored), rst during FILL (no done, and
-- the next batch is clean), and a stub one cycle slower than the FSM expects (status
-- 0x03 from the consistency check that stands in for a watchdog).
--
-- Why .trace and not just .mask: the final mask summarises 32 decisions in one number, so
-- errors that cancel -- a row applied a rank late, a kept flag read from the wrong rank --
-- can leave it right. The trace checks every decision at the edge it must happen.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use std.textio.all;

use work.nms_pkg.all;

entity tb_nms_ctrl is
    generic (
        P          : positive := work.nms_pkg.P_DEFAULT;
        PIPE_CUTS  : natural  := work.nms_pkg.PIPE_CUTS;
        VECTOR_DIR : string   := "models/data/vectors/"
    );
end entity tb_nms_ctrl;

architecture sim of tb_nms_ctrl is

    constant L : positive := LANE_LATENCY;
    constant G : positive := N / P;

    -- The latency the design claims, from first principles (fsm_design.md §6).
    constant T : positive := N * N / P + L + PIPE_CUTS + 2;

    constant MIN_CASES : natural := 20;

    constant KEY_FILE_BITS : natural := 4 * ((KEY_W + 3) / 4);

    type row_table_t   is array (0 to N - 1) of mask_t;
    type slot_order_t  is array (0 to N - 1) of natural range 0 to N - 1;

    signal clk     : std_logic := '0';
    signal running : boolean   := true;

    signal rst          : std_logic := '1';
    signal start        : std_logic := '0';
    signal present_mask : mask_t := (others => '0');
    signal keys         : key_array_t := (others => (others => '0'));
    signal keys_sorted  : key_array_t;

    signal busy          : std_logic;
    signal issue_valid   : std_logic;
    signal row_src       : index_t;
    signal col_grp       : natural range 0 to N / P - 1;
    signal lane_valid    : std_logic := '0';
    signal lane_suppress : std_logic_vector(P - 1 downto 0) := (others => '0');
    signal done          : std_logic;
    signal status        : unsigned(7 downto 0);
    signal keep_mask     : mask_t;

    -- What the lane stub replays: each slot's suppression row, and the rank order the
    -- issues must follow.
    signal slot_row    : row_table_t  := (others => (others => '0'));
    signal order_tb    : slot_order_t := (others => 0);
    signal stub_lat    : natural range L to L + 1 := L;
    signal issues_seen : natural := 0;

begin

    sorter : entity work.bitonic32
        generic map (PIPE_CUTS => PIPE_CUTS)
        port map (clk => clk, keys_in => keys, keys_out => keys_sorted);

    dut : entity work.nms_ctrl
        generic map (P => P, PIPE_CUTS => PIPE_CUTS, LANE_LATENCY => L)
        port map (
            clk           => clk,
            rst           => rst,
            start         => start,
            present_mask  => present_mask,
            busy          => busy,
            keys_sorted   => keys_sorted,
            issue_valid   => issue_valid,
            row_src       => row_src,
            col_grp       => col_grp,
            lane_valid    => lane_valid,
            lane_suppress => lane_suppress,
            done          => done,
            status        => status,
            keep_mask     => keep_mask
        );

    clock : process
    begin
        while running loop
            clk <= '0';
            wait for 5 ns;
            clk <= '1';
            wait for 5 ns;
        end loop;
        wait;
    end process clock;

    -- --- lane stub ------------------------------------------------------------------
    --
    -- Same timing as iou_lane: a result issued in the cycle after edge X is visible after
    -- edge X + stub_lat. Reset clears the valid chain, as the real lanes' does.
    lanes : process (clk)
        type v_pipe_t is array (1 to L + 1) of std_logic;
        type s_pipe_t is array (1 to L + 1) of std_logic_vector(P - 1 downto 0);
        variable pv      : v_pipe_t := (others => '0');
        variable ps      : s_pipe_t := (others => (others => '0'));
        variable resp    : std_logic_vector(P - 1 downto 0);
        variable issue_k : natural := 0;
    begin
        if rising_edge(clk) then
            for j in 0 to P - 1 loop
                resp(j) := slot_row(to_integer(row_src))(j + col_grp * P);
            end loop;

            -- The schedule: row r = issue_k / G must be the rank-r slot, and groups must
            -- ascend within it. A wrong rank order or reversed index_table fails here.
            if issue_valid = '1' then
                assert issue_k < N * G
                    report "more than N*G issues in one batch" severity error;
                if issue_k < N * G then
                    assert to_integer(row_src) = order_tb(issue_k / G)
                        report "issue " & integer'image(issue_k) & ": row source is slot "
                             & integer'image(to_integer(row_src)) & ", rank "
                             & integer'image(issue_k / G) & " is slot "
                             & integer'image(order_tb(issue_k / G))
                        severity error;
                    assert col_grp = issue_k mod G
                        report "issue " & integer'image(issue_k) & ": column group "
                             & integer'image(col_grp) & ", expected "
                             & integer'image(issue_k mod G)
                        severity error;
                end if;
                issue_k := issue_k + 1;
            end if;
            if start = '1' and busy = '0' then
                issue_k := 0;
            end if;
            issues_seen <= issue_k;

            for k in L + 1 downto 2 loop
                pv(k) := pv(k - 1);
                ps(k) := ps(k - 1);
            end loop;
            pv(1) := issue_valid;
            ps(1) := resp;
            if rst = '1' then
                pv := (others => '0');
            end if;
            lane_valid    <= pv(stub_lat);
            lane_suppress <= ps(stub_lat);
        end if;
    end process lanes;

    -- --- stimulus and checks --------------------------------------------------------

    stimulus : process
        alias valid_dut is <<signal .tb_nms_ctrl.dut.valid_mask : mask_t>>;

        variable cases_checked : natural := 0;
        variable saw_none      : boolean := false;
        variable saw_partial   : boolean := false;
        variable saw_ties      : boolean := false;

        type trace_t is record
            kept  : std_logic;
            valid : mask_t;
            keep  : mask_t;
        end record;
        type trace_array_t is array (0 to N - 1) of trace_t;

        variable tr       : trace_array_t;
        variable want     : mask_t;

        function trim_len (s : string) return natural is
            variable last : natural := 0;
        begin
            for i in s'range loop
                if s(i) /= ' ' and s(i) /= CR and s(i) /= HT and s(i) /= NUL then
                    last := i;
                end if;
            end loop;
            return last;
        end function trim_len;

        function hex (v : std_logic_vector) return string is
        begin
            return to_hstring(v);
        end function hex;

        -- Load a case into the stub and the sorter input, and its expectations into tr
        -- and want. Signals are driven here and take effect at the next delta.
        procedure load_case (name : string) is
            file f          : text;
            variable fstat  : file_open_status;
            variable buf    : line;
            variable key_raw  : std_logic_vector(KEY_FILE_BITS - 1 downto 0);
            variable rec_raw  : std_logic_vector(RECORD_BITS - 1 downto 0);
            variable mask_raw : mask_t;
            variable b8_rank  : std_logic_vector(7 downto 0);
            variable b8_slot  : std_logic_vector(7 downto 0);
            variable b4_kept  : std_logic_vector(3 downto 0);
            variable row_raw  : mask_t;
            variable keys_v   : key_array_t;
            variable rows_v   : row_table_t;
            variable order_v  : slot_order_t;
            variable slot     : natural;
        begin
            file_open(fstat, f, VECTOR_DIR & name & ".keys", read_mode);
            assert fstat = open_ok
                report "cannot open " & VECTOR_DIR & name & ".keys -- run from the "
                     & "repository root or override -gVECTOR_DIR"
                severity failure;
            for i in 0 to N - 1 loop
                readline(f, buf);
                hread(buf, key_raw);
                keys_v(i) := unsigned(key_raw(KEY_W - 1 downto 0));
            end loop;
            file_close(f);

            file_open(fstat, f, VECTOR_DIR & name & ".hex", read_mode);
            assert fstat = open_ok report "cannot open " & name & ".hex" severity failure;
            for i in 0 to N - 1 loop
                readline(f, buf);
                hread(buf, rec_raw);
            end loop;
            readline(f, buf);
            hread(buf, mask_raw);
            file_close(f);
            present_mask <= mask_raw;

            file_open(fstat, f, VECTOR_DIR & name & ".mask", read_mode);
            assert fstat = open_ok report "cannot open " & name & ".mask" severity failure;
            readline(f, buf);
            hread(buf, want);
            file_close(f);

            file_open(fstat, f, VECTOR_DIR & name & ".trace", read_mode);
            assert fstat = open_ok report "cannot open " & name & ".trace" severity failure;
            for r in 0 to N - 1 loop
                readline(f, buf);
                hread(buf, b8_rank);
                hread(buf, b8_slot);
                hread(buf, b4_kept);
                hread(buf, row_raw);
                hread(buf, tr(r).valid);
                hread(buf, tr(r).keep);
                assert to_integer(unsigned(b8_rank)) = r
                    report name & ".trace line " & integer'image(r + 1) & " is not rank "
                         & integer'image(r)
                    severity failure;
                slot         := to_integer(unsigned(b8_slot));
                tr(r).kept   := b4_kept(0);
                order_v(r)   := slot;
                rows_v(slot) := row_raw;
            end loop;
            file_close(f);

            keys     <= keys_v;
            slot_row <= rows_v;
            order_tb <= order_v;
        end procedure load_case;

        -- Run one batch from start to one cycle past DONE, checking as it goes. Enters
        -- and leaves just after a rising edge, with the DUT in IDLE.
        --   extra_start_at  : edge after which to pulse start again (ignored by the DUT)
        --   expect_internal : the stub is mistimed; only latency and status are checked
        procedure run_batch (name : string; extra_start_at : integer := -1;
                             expect_internal : boolean := false) is
            variable r_edge : natural;
        begin
            load_case(name);
            start <= '1';
            wait until rising_edge(clk);            -- edge 0 samples start
            wait for 1 ns;
            start <= '0';

            for e in 0 to T loop
                if e > 0 then
                    wait until rising_edge(clk);
                    wait for 1 ns;
                end if;
                if e = extra_start_at then
                    start <= '1';
                elsif e = extra_start_at + 1 then
                    start <= '0';
                end if;

                if e <= T - 1 then
                    assert busy = '1'
                        report name & ": busy low after edge " & integer'image(e)
                             & ", before DONE ended"
                        severity error;
                end if;

                -- 3. latency, from both sides
                if e < T - 1 then
                    assert done = '0'
                        report name & ": done after only " & integer'image(e)
                             & " edges; the FSM is faster than T = " & integer'image(T)
                        severity error;
                elsif e = T - 1 then
                    assert done = '1'
                        report name & ": done still low after edge " & integer'image(e)
                             & "; the FSM is slower than T = " & integer'image(T)
                        severity error;
                else
                    assert done = '0' and busy = '0'
                        report name & ": done or busy still high a cycle after DONE"
                        severity error;
                end if;

                -- 2. every resolve, at the edge it must happen
                if not expect_internal then
                    for r in 0 to N - 1 loop
                        r_edge := PIPE_CUTS + (r + 1) * G + L + 1;
                        if e = r_edge then
                            assert keep_mask = tr(r).keep
                                report name & ": after resolving rank " & integer'image(r)
                                     & " keep_mask = " & hex(keep_mask) & ", model says "
                                     & hex(tr(r).keep)
                                severity error;
                            assert valid_dut = tr(r).valid
                                report name & ": after resolving rank " & integer'image(r)
                                     & " valid_mask = " & hex(valid_dut) & ", model says "
                                     & hex(tr(r).valid)
                                severity error;
                        end if;
                    end loop;
                end if;

                if e = T - 1 then
                    if expect_internal then
                        assert to_integer(status) = STATUS_INTERNAL
                            report name & ": mistimed lanes went undetected, status = "
                                 & integer'image(to_integer(status))
                            severity error;
                    else
                        -- 1. and 4.
                        assert keep_mask = want
                            report name & ": keep_mask = " & hex(keep_mask)
                                 & ", expected " & hex(want)
                            severity error;
                        assert to_integer(status) = STATUS_OK
                            report name & ": status = " & integer'image(to_integer(status))
                                 & ", expected OK"
                            severity error;
                        assert issues_seen = N * G
                            report name & ": " & integer'image(issues_seen)
                                 & " issues, expected " & integer'image(N * G)
                            severity error;
                    end if;
                end if;
            end loop;
        end procedure run_batch;

        file manifest    : text;
        variable fstat   : file_open_status;
        variable buf     : line;
        variable nlen    : natural;
    begin
        report "tb_nms_ctrl: P = " & integer'image(P) & ", PIPE_CUTS = "
             & integer'image(PIPE_CUTS) & ", T = " & integer'image(T);

        rst <= '1';
        wait until rising_edge(clk);
        wait until rising_edge(clk);
        wait for 1 ns;
        rst <= '0';
        wait until rising_edge(clk);
        wait for 1 ns;

        -- --- every generated case, back to back --------------------------------------
        file_open(fstat, manifest, VECTOR_DIR & "cases.txt", read_mode);
        assert fstat = open_ok
            report "cannot open " & VECTOR_DIR & "cases.txt" severity failure;
        while not endfile(manifest) loop
            readline(manifest, buf);
            nlen := trim_len(buf.all);
            if nlen > 0 then
                run_batch(buf.all(buf.all'low to buf.all'low + nlen - 1));
                cases_checked := cases_checked + 1;
                if buf.all(buf.all'low to buf.all'low + nlen - 1) = "none_present" then
                    saw_none := true;
                elsif buf.all(buf.all'low to buf.all'low + nlen - 1) = "partial_present" then
                    saw_partial := true;
                elsif buf.all(buf.all'low to buf.all'low + nlen - 1) = "ties" then
                    saw_ties := true;
                end if;
            end if;
        end loop;
        file_close(manifest);

        assert cases_checked >= MIN_CASES
            report "only " & integer'image(cases_checked) & " cases ran" severity error;
        assert saw_none and saw_partial and saw_ties
            report "present_mask = 0, a partial present_mask, or the tie case did not run"
            severity error;

        -- --- directed: start pulsed during FILL is ignored ---------------------------
        run_batch("notebook32", extra_start_at => PIPE_CUTS + 3);

        -- --- directed: rst during FILL -------------------------------------------------
        load_case("rand_seed0");
        start <= '1';
        wait until rising_edge(clk);
        wait for 1 ns;
        start <= '0';
        for e in 1 to PIPE_CUTS + 5 loop
            wait until rising_edge(clk);
        end loop;
        wait for 1 ns;
        rst <= '1';
        wait until rising_edge(clk);
        wait for 1 ns;
        rst <= '0';
        assert busy = '0' and keep_mask = (keep_mask'range => '0')
            report "rst mid-batch did not return to IDLE with keep_mask cleared"
            severity error;
        for e in 1 to T + 5 loop
            wait until rising_edge(clk);
            wait for 1 ns;
            assert done = '0' report "done after a batch aborted by rst" severity error;
        end loop;
        run_batch("rand_seed0");

        -- --- directed: lanes one cycle slower than the FSM was built for -------------
        stub_lat <= L + 1;
        run_batch("notebook32", expect_internal => true);
        stub_lat <= L;
        wait until rising_edge(clk);
        wait for 1 ns;
        run_batch("notebook32");                 -- and the next batch is clean again

        report "tb_nms_ctrl: P = " & integer'image(P) & ", PIPE_CUTS = "
             & integer'image(PIPE_CUTS) & ", " & integer'image(cases_checked)
             & " cases x " & integer'image(N) & " ranks traced, latency T = "
             & integer'image(T) & " pinned both sides, 4 directed cases";
        report "PASS";
        running <= false;
        wait;
    end process stimulus;

end architecture sim;
