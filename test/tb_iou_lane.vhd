-- tb_iou_lane -- self-checking testbench for one IoU lane.
--
-- Streams every row of every `.pairs` file named in the manifest for this run's threshold
-- through the lane BACK TO BACK, one pair per cycle, and compares `suppress` against the
-- verdict the golden model computed. At the shipped T_INT = 128 that is 15,120 pairs:
-- 5 x 1,024 from the curated batches plus 10,000 hostile random ones.
--
-- Back to back is deliberate. Driving one pair, waiting for it to emerge and then driving
-- the next would leave the pipeline's stage-to-stage handover untested -- and that is
-- where a missing register or a mis-ordered stage shows up, not in a single pair.
--
-- RUN AT TWO THRESHOLDS (see SWEEP_tb_iou_lane in scripts/Makefile). T_INT is a generic,
-- and at 128 the multiply degenerates to a shift, so a 128-only test set cannot tell the
-- generic path from a hard-coded one. Measured: mutating the RHS to `to_unsigned(128,...)`
-- or to a shift by K_SHIFT-1 passes every one of the 15,120 pairs at T_INT = 128 and is
-- killed at T_INT = 255. The second run is what makes the generic mean anything.
--
-- The expected verdicts come from the file. The testbench never computes I or U, because
-- that is precisely the datapath under test: a testbench that derived them itself would
-- duplicate the clamp logic, and a shared mistake would cancel out instead of failing.
-- For the same reason it does not check that its stimulus is *interesting* -- that would
-- also mean recomputing I and U. models/nms/test_vectors.py does it instead, asserting the
-- random file holds 3,644 pairs with a zero-area box, 1,429 with an inverted box, 715
-- exactly on 2I == U, 1,428 identical and 1,428 near-maximal, 43% of them suppressing.
--
-- Areas arrive as inputs rather than being derived here, matching the hardware: they are
-- per-box, computed once by box_store during LOAD, so the lane needs no second multiplier.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use std.textio.all;

use work.nms_pkg.all;

entity tb_iou_lane is
    generic (
        -- One flag selects both the DUT's threshold and the stimulus that matches it: the
        -- manifest name is derived below, exactly as models/nms/vectors.py names it.
        T_INT      : natural := work.nms_pkg.T_INT;
        VECTOR_DIR : string  := "models/data/vectors/"
    );
end entity tb_iou_lane;

architecture sim of tb_iou_lane is

    -- Hex field widths in the .pairs rows, matching models/nms/vectors.py. Both are whole
    -- nibbles already, which is what hread requires.
    constant COORD_FILE_BITS : natural := 4 * ((COORD_W + 3) / 4);
    constant AREA_FILE_BITS  : natural := 4 * ((AREA_W + 3) / 4);

    -- random_pairs is 10,000 rows; the batch files are N*N = 1,024.
    constant MAX_ROWS : natural := 12_000;

    -- The shipped threshold gets the five batch files plus the random one; any other gets
    -- only the random file, because the batch boxes are tens of units across and cannot
    -- reach the wide end of the datapath whatever the threshold.
    constant IS_DEFAULT : boolean := T_INT = work.nms_pkg.T_INT;

    -- Derived, not passed in, so a single -gT_INT= flag picks the threshold and the
    -- stimulus together and the two can never be mismatched. Matches
    -- models/nms/vectors.py's pair_manifest_name exactly.
    function manifest_name return string is
    begin
        if IS_DEFAULT then
            return "pairs.txt";
        end if;
        return "pairs_t" & integer'image(T_INT) & ".txt";
    end function manifest_name;

    function derive_min_total return natural is
    begin
        if IS_DEFAULT then
            return 10_000 + 5 * N * N;
        end if;
        return 10_000;
    end function derive_min_total;

    constant MANIFEST  : string  := manifest_name;
    constant MIN_TOTAL : natural := derive_min_total;

    -- Prefix of the random pair file, which is called random_pairs at the shipped
    -- threshold and random_pairs_t<T_INT> at any other.
    constant RANDOM_STEM : string := "random_pairs";

    type pair_t is record
        k_x, k_y, k_a, k_b : coord_t;
        k_area             : area_t;
        c_x, c_y, c_a, c_b : coord_t;
        c_area             : area_t;
        expected           : std_logic;
    end record;

    type pair_array_t is array (0 to MAX_ROWS - 1) of pair_t;

    signal clk      : std_logic := '0';
    signal rst      : std_logic := '1';
    signal running  : boolean   := true;

    signal valid_in : std_logic := '0';
    signal stim     : pair_t := (
        (others => '0'), (others => '0'), (others => '0'), (others => '0'),
        (others => '0'),
        (others => '0'), (others => '0'), (others => '0'), (others => '0'),
        (others => '0'),
        '0');

    signal valid_out : std_logic;
    signal suppress  : std_logic;

begin

    dut : entity work.iou_lane
        generic map (T_INT => T_INT)
        port map (
            clk      => clk,
            rst      => rst,
            valid_in => valid_in,
            k_x      => stim.k_x,
            k_y      => stim.k_y,
            k_a      => stim.k_a,
            k_b      => stim.k_b,
            k_area   => stim.k_area,
            c_x      => stim.c_x,
            c_y      => stim.c_y,
            c_a      => stim.c_a,
            c_b      => stim.c_b,
            c_area   => stim.c_area,
            valid_out => valid_out,
            suppress  => suppress
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
        variable rows          : pair_array_t;
        variable count         : natural;
        variable total_checked : natural := 0;
        variable files_done    : natural := 0;
        variable saw_random    : boolean := false;
        variable saw_degenerate : boolean := false;
        variable saw_boundary   : boolean := false;

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

        -- Read one .pairs file into `rows` and set `count`.
        procedure load (name : string) is
            file pairs_file : text;
            variable status   : file_open_status;
            variable buf      : line;
            variable coord    : std_logic_vector(COORD_FILE_BITS - 1 downto 0);
            variable area     : std_logic_vector(AREA_FILE_BITS - 1 downto 0);
            variable verdict  : std_logic_vector(3 downto 0);
        begin
            count := 0;
            file_open(status, pairs_file, VECTOR_DIR & name & ".pairs", read_mode);
            assert status = open_ok
                report "cannot open " & VECTOR_DIR & name & ".pairs -- vector paths are "
                     & "relative to the working directory; run from the repository root "
                     & "or override -gVECTOR_DIR"
                severity failure;
            while not endfile(pairs_file) loop
                readline(pairs_file, buf);
                if trim_len(buf.all) > 0 then
                    assert count < MAX_ROWS
                        report name & ".pairs has more than " & integer'image(MAX_ROWS)
                             & " rows; raise MAX_ROWS"
                        severity failure;
                    hread(buf, coord);
                    rows(count).k_x := unsigned(coord(COORD_W - 1 downto 0));
                    hread(buf, coord);
                    rows(count).k_y := unsigned(coord(COORD_W - 1 downto 0));
                    hread(buf, coord);
                    rows(count).k_a := unsigned(coord(COORD_W - 1 downto 0));
                    hread(buf, coord);
                    rows(count).k_b := unsigned(coord(COORD_W - 1 downto 0));
                    hread(buf, area);
                    rows(count).k_area := unsigned(area(AREA_W - 1 downto 0));
                    hread(buf, coord);
                    rows(count).c_x := unsigned(coord(COORD_W - 1 downto 0));
                    hread(buf, coord);
                    rows(count).c_y := unsigned(coord(COORD_W - 1 downto 0));
                    hread(buf, coord);
                    rows(count).c_a := unsigned(coord(COORD_W - 1 downto 0));
                    hread(buf, coord);
                    rows(count).c_b := unsigned(coord(COORD_W - 1 downto 0));
                    hread(buf, area);
                    rows(count).c_area := unsigned(area(AREA_W - 1 downto 0));
                    -- hread, not read: `read` of a CHARACTER takes the next character
                    -- verbatim, including the separating space, whereas hread skips
                    -- leading whitespace like the ten calls above it. The verdict is one
                    -- hex digit, so it arrives as four bits of which only the LSB matters.
                    hread(buf, verdict);
                    assert unsigned(verdict) <= 1
                        report name & ".pairs row " & integer'image(count + 1)
                             & " has verdict " & integer'image(to_integer(unsigned(verdict)))
                             & ", expected 0 or 1"
                        severity failure;
                    rows(count).expected := verdict(0);
                    count := count + 1;
                end if;
            end loop;
            file_close(pairs_file);
            assert count > 0
                report name & ".pairs is empty"
                severity failure;
        end procedure load;

        -- Compare the output now emerging against the row that produced it.
        procedure check (name : string; index : natural) is
        begin
            assert valid_out = '1'
                report name & " row " & integer'image(index + 1)
                     & ": valid_out is low, so the lane's valid chain does not match "
                     & "LANE_LATENCY = " & integer'image(LANE_LATENCY)
                severity error;
            assert suppress = rows(index).expected
                report name & " row " & integer'image(index + 1) & ": suppress = "
                     & std_logic'image(suppress) & ", model says "
                     & std_logic'image(rows(index).expected) & " for keeper ("
                     & integer'image(to_integer(rows(index).k_x)) & ","
                     & integer'image(to_integer(rows(index).k_y)) & ","
                     & integer'image(to_integer(rows(index).k_a)) & ","
                     & integer'image(to_integer(rows(index).k_b)) & ") area "
                     & integer'image(to_integer(rows(index).k_area))
                     & " vs candidate ("
                     & integer'image(to_integer(rows(index).c_x)) & ","
                     & integer'image(to_integer(rows(index).c_y)) & ","
                     & integer'image(to_integer(rows(index).c_a)) & ","
                     & integer'image(to_integer(rows(index).c_b)) & ") area "
                     & integer'image(to_integer(rows(index).c_area))
                severity error;
            total_checked := total_checked + 1;
        end procedure check;

        procedure run_file (name : string) is
        begin
            load(name);

            -- Stream every row, one per cycle. The result for row i emerges after the
            -- (i+1)-th edge shifted back by LANE_LATENCY.
            for i in 0 to count - 1 loop
                valid_in <= '1';
                stim     <= rows(i);
                wait until rising_edge(clk);
                wait for 1 ns;
                if i >= LANE_LATENCY - 1 then
                    check(name, i + 1 - LANE_LATENCY);
                end if;
            end loop;

            -- Drain the LANE_LATENCY-1 rows still in flight.
            valid_in <= '0';
            for d in 1 to LANE_LATENCY - 1 loop
                wait until rising_edge(clk);
                wait for 1 ns;
                check(name, count - LANE_LATENCY + d);
            end loop;

            -- One more edge and the last valid must have shifted out. This is what makes
            -- valid_out an equality rather than a lower bound: a chain one stage too long
            -- would still be asserting here.
            wait until rising_edge(clk);
            wait for 1 ns;
            assert valid_out = '0'
                report name & ": valid_out is still high " & integer'image(LANE_LATENCY)
                     & " cycles after the last valid input, so the valid chain is longer "
                     & "than LANE_LATENCY"
                severity error;

            files_done := files_done + 1;

            -- Note which hazard files ran, by name. A count alone would pass a truncated
            -- manifest that had skipped exactly the interesting stimulus. The random file
            -- is matched on its prefix, since at a non-default threshold it is called
            -- random_pairs_t<T_INT>.
            if name'length >= RANDOM_STEM'length
               and name(name'low to name'low + RANDOM_STEM'length - 1) = RANDOM_STEM
            then
                saw_random := true;
            elsif name = "degenerate" then
                saw_degenerate := true;
            elsif name = "boundary" then
                saw_boundary := true;
            end if;

            report "  " & name & ": " & integer'image(count) & " pairs checked";
        end procedure run_file;

        file manifest_file : text;
        variable status   : file_open_status;
        variable buf      : line;
        variable name_len : natural;
    begin
        report "tb_iou_lane: starting with T_INT = " & integer'image(T_INT)
             & ", reading " & MANIFEST;

        -- Reset must gate the valid chain: drive valid inputs while held in reset and
        -- nothing may come out. Without this the lane could emit a suppression bit for a
        -- pair that was never dispatched.
        rst      <= '1';
        valid_in <= '1';
        for i in 1 to LANE_LATENCY + 2 loop
            wait until rising_edge(clk);
            wait for 1 ns;
            assert valid_out = '0'
                report "valid_out went high while rst was asserted"
                severity error;
        end loop;
        valid_in <= '0';
        rst      <= '0';
        wait until rising_edge(clk);

        file_open(status, manifest_file, VECTOR_DIR & MANIFEST, read_mode);
        assert status = open_ok
            report "cannot open " & VECTOR_DIR & MANIFEST & " -- run "
                 & "`uv run python -m models.nms vectors` from the repository root"
            severity failure;

        while not endfile(manifest_file) loop
            readline(manifest_file, buf);
            name_len := trim_len(buf.all);
            if name_len > 0 then
                run_file(buf.all(buf.all'low to buf.all'low + name_len - 1));
            end if;
        end loop;
        file_close(manifest_file);

        -- Named rather than merely counted: random_pairs carries the zero-area, inverted
        -- and exact-boundary pairs, degenerate carries the clamp cases, and boundary
        -- carries the `>=` cases. A truncated manifest would otherwise pass while having
        -- skipped every one of them.
        assert saw_random
            report "random_pairs did not run -- it carries the zero-area, inverted, "
                 & "exact-boundary and near-maximal pairs"
            severity error;
        assert saw_degenerate = IS_DEFAULT and saw_boundary = IS_DEFAULT
            report "the batch hazard files ran when they should not have, or the other "
                 & "way round: they exist only at the shipped threshold"
            severity error;
        assert total_checked >= MIN_TOTAL
            report "only " & integer'image(total_checked) & " pairs checked, expected at "
                 & "least " & integer'image(MIN_TOTAL) & " -- is pairs.txt truncated?"
            severity error;

        report "tb_iou_lane: T_INT = " & integer'image(T_INT) & ", "
             & integer'image(total_checked) & " pairs from " & integer'image(files_done)
             & " files (" & MANIFEST & "), bit-exact against the golden model, streamed "
             & "one per cycle at LANE_LATENCY = " & integer'image(LANE_LATENCY);
        report "PASS";
        running <= false;
        wait;
    end process stimulus;

end architecture sim;
