-- tb_nms_core -- whole-core testbench: every batch through the real store, sorter, FSM
-- and lanes, compared with the golden model as one 32-bit equality.
--
-- This is the link in the verification chain that everything before it builds towards:
--
--   nms_sequential  ==  nms_allpairs  ==  nms_core RTL
--    (textbook)        (20k batches)     (this testbench)
--
-- Batches: every curated case in cases.txt, then the on-demand random file (1,000 hostile
-- batches, a quarter with random present masks and some all-absent; `make` regenerates it,
-- see scripts/Makefile). All run back to back with no reset, so each batch also overwrites
-- the last one's store, sorter and row buffer.
--
-- Per batch:
--   * write the 32 records through the write port, then wait for `settled`
--   * pulse start, and pin the latency as an EQUALITY: done is '0' after edge T-2 and '1'
--     after edge T-1, with T computed from the generics, including ISSUE_REGS
--   * keep_mask = expected, status = OK, and back in IDLE one edge later
--
-- Nothing here is stubbed. Where tb_nms_ctrl replays the lanes from traces, this drives
-- real iou_lanes fed through the real row-source and candidate muxes, so a striping or
-- field-slicing error between blocks -- invisible to every unit testbench -- fails here.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use std.textio.all;

use work.nms_pkg.all;

entity tb_nms_core is
    generic (
        P           : positive := work.nms_pkg.P_DEFAULT;
        PIPE_CUTS   : natural  := work.nms_pkg.PIPE_CUTS;
        ISSUE_REGS  : natural  := work.nms_pkg.ISSUE_REGS;
        VECTOR_DIR  : string   := "models/data/vectors/";
        RANDOM_FILE : string   := "models/data/random/random_batches.txt";
        -- How many of the file's random batches to run. `make test` (CI) runs 150 at the
        -- shipped configuration and none at the others; `make test-full` runs all 1,000
        -- everywhere. See scripts/Makefile.
        RANDOM_COUNT : natural := 1_000
    );
end entity tb_nms_core;

architecture sim of tb_nms_core is

    constant T : positive := N * N / P + LANE_LATENCY + ISSUE_REGS + PIPE_CUTS + 2;

    constant MIN_CASES  : natural := 20;

    signal clk     : std_logic := '0';
    signal running : boolean   := true;

    signal rst          : std_logic := '1';
    signal we           : std_logic := '0';
    signal waddr        : index_t   := (others => '0');
    signal wdata        : record_t  := (others => '0');
    signal settled      : std_logic;
    signal start        : std_logic := '0';
    signal present_mask : mask_t    := (others => '0');
    signal busy         : std_logic;
    signal done         : std_logic;
    signal status       : unsigned(7 downto 0);
    signal keep_mask    : mask_t;

begin

    dut : entity work.nms_core
        generic map (P => P, PIPE_CUTS => PIPE_CUTS, ISSUE_REGS => ISSUE_REGS)
        port map (
            clk          => clk,
            rst          => rst,
            we           => we,
            waddr        => waddr,
            wdata        => wdata,
            settled      => settled,
            start        => start,
            present_mask => present_mask,
            busy         => busy,
            done         => done,
            status       => status,
            keep_mask    => keep_mask
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

    stimulus : process
        variable curated   : natural := 0;
        variable randoms   : natural := 0;
        variable survivors : natural := 0;   -- total kept boxes, to prove masks vary
        variable nonfull   : natural := 0;   -- batches with some slot absent

        variable recs : record_array_t;
        variable pm   : mask_t;
        variable want : mask_t;

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

        function popcount (v : mask_t) return natural is
            variable n : natural := 0;
        begin
            for i in v'range loop
                if v(i) = '1' then
                    n := n + 1;
                end if;
            end loop;
            return n;
        end function popcount;

        -- One batch, start to one edge past DONE. Enters and leaves just after an edge.
        procedure run_batch (tag : string) is
        begin
            for i in 0 to N - 1 loop
                we    <= '1';
                waddr <= to_unsigned(i, INDEX_W);
                wdata <= recs(i);
                wait until rising_edge(clk);
                wait for 1 ns;
            end loop;
            we <= '0';
            wait until rising_edge(clk);
            wait for 1 ns;
            assert settled = '1' and busy = '0'
                report tag & ": not settled and idle before start" severity error;

            present_mask <= pm;
            start        <= '1';
            wait until rising_edge(clk);                 -- edge 0
            wait for 1 ns;
            start <= '0';

            for e in 0 to T loop
                if e > 0 then
                    wait until rising_edge(clk);
                    wait for 1 ns;
                end if;
                if e < T - 1 then
                    assert done = '0'
                        report tag & ": done after only " & integer'image(e)
                             & " edges; faster than T = " & integer'image(T)
                        severity error;
                elsif e = T - 1 then
                    assert done = '1'
                        report tag & ": done still low after edge " & integer'image(e)
                             & "; slower than T = " & integer'image(T)
                        severity error;
                    assert keep_mask = want
                        report tag & ": keep_mask = " & to_hstring(keep_mask)
                             & ", model says " & to_hstring(want)
                             & " (present " & to_hstring(pm) & ")"
                        severity error;
                    assert to_integer(status) = STATUS_OK
                        report tag & ": status = " & integer'image(to_integer(status))
                        severity error;
                else
                    assert done = '0' and busy = '0'
                        report tag & ": not back in IDLE a cycle after DONE"
                        severity error;
                end if;
            end loop;

            survivors := survivors + popcount(want);
            if pm /= (pm'range => '1') then
                nonfull := nonfull + 1;
            end if;
        end procedure run_batch;

        procedure load_curated (name : string) is
            file f         : text;
            variable fstat : file_open_status;
            variable buf   : line;
            variable rraw  : std_logic_vector(RECORD_BITS - 1 downto 0);
        begin
            file_open(fstat, f, VECTOR_DIR & name & ".hex", read_mode);
            assert fstat = open_ok
                report "cannot open " & VECTOR_DIR & name & ".hex -- run from the "
                     & "repository root or override -gVECTOR_DIR"
                severity failure;
            for i in 0 to N - 1 loop
                readline(f, buf);
                hread(buf, rraw);
                recs(i) := unsigned(rraw);
            end loop;
            readline(f, buf);
            hread(buf, pm);
            file_close(f);

            file_open(fstat, f, VECTOR_DIR & name & ".mask", read_mode);
            assert fstat = open_ok report "cannot open " & name & ".mask" severity failure;
            readline(f, buf);
            hread(buf, want);
            file_close(f);
        end procedure load_curated;

        file manifest  : text;
        file rfile     : text;
        variable fstat : file_open_status;
        variable buf   : line;
        variable nlen  : natural;
        variable count : integer;
        variable rraw  : std_logic_vector(RECORD_BITS - 1 downto 0);
    begin
        report "tb_nms_core: P = " & integer'image(P) & ", PIPE_CUTS = "
             & integer'image(PIPE_CUTS) & ", ISSUE_REGS = " & integer'image(ISSUE_REGS)
             & ", T = " & integer'image(T);

        rst <= '1';
        wait until rising_edge(clk);
        wait until rising_edge(clk);
        wait for 1 ns;
        rst <= '0';
        wait until rising_edge(clk);
        wait for 1 ns;

        -- --- curated cases -----------------------------------------------------------
        file_open(fstat, manifest, VECTOR_DIR & "cases.txt", read_mode);
        assert fstat = open_ok
            report "cannot open " & VECTOR_DIR & "cases.txt" severity failure;
        while not endfile(manifest) loop
            readline(manifest, buf);
            nlen := trim_len(buf.all);
            if nlen > 0 then
                load_curated(buf.all(buf.all'low to buf.all'low + nlen - 1));
                run_batch(buf.all(buf.all'low to buf.all'low + nlen - 1));
                curated := curated + 1;
            end if;
        end loop;
        file_close(manifest);

        -- --- random batches ----------------------------------------------------------
        file_open(fstat, rfile, RANDOM_FILE, read_mode);
        assert fstat = open_ok
            report "cannot open " & RANDOM_FILE & " -- generate it with "
                 & "`uv run python -m models.nms random` (make does this automatically)"
            severity failure;
        readline(rfile, buf);
        read(buf, count);
        assert count >= RANDOM_COUNT
            report RANDOM_FILE & " holds " & integer'image(count) & " batches, fewer than "
                 & "RANDOM_COUNT = " & integer'image(RANDOM_COUNT)
            severity failure;
        for b in 0 to RANDOM_COUNT - 1 loop
            for i in 0 to N - 1 loop
                readline(rfile, buf);
                hread(buf, rraw);
                recs(i) := unsigned(rraw);
            end loop;
            readline(rfile, buf);
            hread(buf, pm);
            readline(rfile, buf);
            hread(buf, want);
            run_batch("random batch " & integer'image(b));
            randoms := randoms + 1;
        end loop;
        file_close(rfile);

        assert curated >= MIN_CASES
            report "only " & integer'image(curated) & " curated cases ran" severity error;
        assert randoms = RANDOM_COUNT
            report "only " & integer'image(randoms) & " random batches ran" severity error;
        -- A quarter of the random batches draw a random present_mask, and the curated set has
        -- two absent-slot cases, so absent slots must have been exercised in proportion.
        assert nonfull >= 2 + randoms / 5
            report "only " & integer'image(nonfull) & " batches had absent slots"
            severity error;

        report "tb_nms_core: P = " & integer'image(P) & ", PIPE_CUTS = "
             & integer'image(PIPE_CUTS) & ", ISSUE_REGS = " & integer'image(ISSUE_REGS)
             & ": " & integer'image(curated) & " curated + " & integer'image(randoms)
             & " random batches bit-exact, latency T = " & integer'image(T)
             & " pinned both sides every batch (" & integer'image(survivors)
             & " survivors, " & integer'image(nonfull) & " batches with absent slots)";
        report "PASS";
        running <= false;
        wait;
    end process stimulus;

end architecture sim;
