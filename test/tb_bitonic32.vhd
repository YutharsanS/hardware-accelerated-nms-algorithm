-- tb_bitonic32 -- self-checking testbench for the 32-key bitonic sorting network.
--
-- Runs every case in models/data/vectors/cases.txt. The manifest is read rather than a
-- case list being written here, so a case added to models/nms/batches.py is covered by
-- this gate without anyone editing a VHDL file -- and test_vectors.py asserts the manifest
-- lists exactly the generated cases, so the coverage cannot quietly fall behind.
--
-- LATENCY IS A GENERIC. PIPE_CUTS is passed to the DUT and used as the number of clock
-- edges to wait before sampling, so this one testbench covers the combinational
-- configuration (PIPE_CUTS = 0, sample immediately) and every registered one. The
-- Makefile runs it at 0 and 2; docs/plan.md B3.2 sweeps {0, 2, 3, 14} through Vivado.
--
-- Four checks per case, and the last two are the ones that matter:
--
--   1. strictly ascending -- the sort key is a strict total order, so equal neighbours
--      would mean a key was duplicated or lost
--   2. a permutation of the input -- every input key appears in the output exactly once
--   3. the RECOVERED INDEX matches the model's rank table, reading the ascending output
--      in reverse: index_table(r) = N-1-out(N-1-r)(4 downto 0) must equal order(r)
--   4. the key at the model's rank-r slot is the key the sorter put at rank r
--
-- Checking only that the keys came out sorted would pass a network that sorted correctly
-- while corrupting the index tag riding in the low 5 bits -- and that tag is the entire
-- output, since the payloads never move. Checks 3 and 4 are what close that gap, from the
-- two independent directions the vector files allow.
--
-- File paths come in through the VECTOR_DIR generic (docs/plan.md L7): the default assumes
-- the working directory is the repository root, which is what `make` guarantees, and any
-- other caller overrides -gVECTOR_DIR rather than discovering a silent open failure.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use std.textio.all;

use work.nms_pkg.all;

entity tb_bitonic32 is
    generic (
        PIPE_CUTS  : natural := work.nms_pkg.PIPE_CUTS;
        VECTOR_DIR : string  := "models/data/vectors/"
    );
end entity tb_bitonic32;

architecture sim of tb_bitonic32 is

    -- Field widths in the vector files, matching models/nms/vectors.py: a hex field is
    -- rounded up to whole nibbles, and hread requires a whole number of them.
    constant KEY_FILE_BITS  : natural := 4 * ((KEY_W + 3) / 4);
    constant SLOT_FILE_BITS : natural := 4 * ((INDEX_W + 3) / 4);

    -- The generated set is 18 batches plus the two present_mask variants. A floor rather
    -- than an equality so adding cases is free, but losing them is not.
    constant MIN_CASES : natural := 20;

    type slot_array_t is array (0 to N - 1) of natural;

    signal clk      : std_logic := '0';
    signal running  : boolean   := true;
    signal keys_in  : key_array_t := (others => (others => '0'));
    signal keys_out : key_array_t;

begin

    dut : entity work.bitonic32
        generic map (PIPE_CUTS => PIPE_CUTS)
        port map (
            clk      => clk,
            keys_in  => keys_in,
            keys_out => keys_out
        );

    -- Gated rather than free-running: the simulation ends when the checks do, so a hung
    -- testbench and a passing one are not indistinguishable from an exit code.
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
        variable cases_checked : natural := 0;
        -- Cases where the result actually differed from the previous one, so the
        -- one-edge-short check above was not vacuously true.
        variable latency_pinned : natural := 0;
        variable saw_ties      : boolean := false;
        variable saw_all_equal : boolean := false;
        variable saw_boundary  : boolean := false;
        variable saw_anchor    : boolean := false;

        -- Number of characters up to the last non-blank, so a trailing CR or space in the
        -- manifest does not become part of a filename.
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

        procedure check_case (name : string) is
            file keys_file  : text;
            file order_file : text;
            variable status   : file_open_status;
            variable buf      : line;
            variable key_raw  : std_logic_vector(KEY_FILE_BITS - 1 downto 0);
            variable slot_raw : std_logic_vector(SLOT_FILE_BITS - 1 downto 0);
            variable keys     : key_array_t;
            variable sorted   : key_array_t;
            variable previous : key_array_t;
            variable order    : slot_array_t;
            variable seen     : natural;
            variable slot     : natural;
        begin
            -- --- the keys the model would feed the network -------------------------
            file_open(status, keys_file, VECTOR_DIR & name & ".keys", read_mode);
            assert status = open_ok
                report "cannot open " & VECTOR_DIR & name & ".keys -- vector paths are "
                     & "relative to the working directory; run from the repository root "
                     & "or override -gVECTOR_DIR"
                severity failure;
            for i in 0 to N - 1 loop
                assert not endfile(keys_file)
                    report name & ".keys has fewer than " & integer'image(N) & " lines"
                    severity failure;
                readline(keys_file, buf);
                hread(buf, key_raw);
                assert to_integer(unsigned(key_raw)) < 2 ** KEY_W
                    report name & ".keys line " & integer'image(i + 1) & " needs more "
                         & "than KEY_W bits"
                    severity error;
                keys(i) := unsigned(key_raw(KEY_W - 1 downto 0));
            end loop;
            assert endfile(keys_file)
                report name & ".keys has more than " & integer'image(N) & " lines"
                severity error;
            file_close(keys_file);

            -- --- the rank-to-slot table the model expects --------------------------
            file_open(status, order_file, VECTOR_DIR & name & ".order", read_mode);
            assert status = open_ok
                report "cannot open " & VECTOR_DIR & name & ".order"
                severity failure;
            for r in 0 to N - 1 loop
                assert not endfile(order_file)
                    report name & ".order has fewer than " & integer'image(N) & " lines"
                    severity failure;
                readline(order_file, buf);
                hread(buf, slot_raw);
                order(r) := to_integer(unsigned(slot_raw));
                assert order(r) < N
                    report name & ".order rank " & integer'image(r) & " names slot "
                         & integer'image(order(r)) & ", which does not exist"
                    severity error;
            end loop;
            file_close(order_file);

            -- --- drive, then wait exactly PIPE_CUTS edges --------------------------
            --
            -- Latency is checked as an EQUALITY, in both directions. Waiting PIPE_CUTS
            -- edges and finding the right answer only proves latency is not *longer*
            -- than claimed -- a sorter with one register too few would pass, because by
            -- then its output has been correct for a cycle. So one edge short, the output
            -- must still be the previous batch's result. That is the number B5.2's
            -- cycle-count assertion depends on, so it is measured rather than assumed.
            previous := keys_out;
            keys_in  <= keys;
            if PIPE_CUTS > 0 then
                for c in 1 to PIPE_CUTS - 1 loop
                    wait until rising_edge(clk);
                end loop;
                wait for 1 ns;
                assert keys_out = previous
                    report name & ": output changed after only "
                         & integer'image(PIPE_CUTS - 1) & " edges, so the sorter is "
                         & "faster than the PIPE_CUTS = " & integer'image(PIPE_CUTS)
                         & " cycles the controller will wait -- a register is missing"
                    severity error;
                wait until rising_edge(clk);
            end if;
            wait for 1 ns;
            sorted := keys_out;
            if sorted /= previous then
                latency_pinned := latency_pinned + 1;
            end if;

            -- --- 1. strictly ascending ---------------------------------------------
            for r in 0 to N - 2 loop
                assert sorted(r) < sorted(r + 1)
                    report name & ": output is not strictly ascending at " &
                         integer'image(r) & " -- " & integer'image(to_integer(sorted(r)))
                         & " then " & integer'image(to_integer(sorted(r + 1)))
                    severity error;
            end loop;

            -- --- 2. a permutation of the input -------------------------------------
            for i in 0 to N - 1 loop
                seen := 0;
                for r in 0 to N - 1 loop
                    if sorted(r) = keys(i) then
                        seen := seen + 1;
                    end if;
                end loop;
                assert seen = 1
                    report name & ": input key " & integer'image(to_integer(keys(i)))
                         & " from slot " & integer'image(i) & " appears "
                         & integer'image(seen) & " times in the output, not once"
                    severity error;
            end loop;

            -- --- 3. recovered index vs the model's rank table ----------------------
            --
            -- Rank 0 is the HIGHEST key and the output is ascending, hence the reversal.
            -- A sorter that got this backwards would still pass checks 1 and 2.
            for r in 0 to N - 1 loop
                slot := N - 1 - to_integer(sorted(N - 1 - r)(INDEX_W - 1 downto 0));
                assert slot = order(r)
                    report name & ": rank " & integer'image(r) & " recovers slot "
                         & integer'image(slot) & ", model says " & integer'image(order(r))
                    severity error;
            end loop;

            -- --- 4. and the key itself, from the other direction -------------------
            for r in 0 to N - 1 loop
                assert sorted(N - 1 - r) = keys(order(r))
                    report name & ": rank " & integer'image(r) & " holds key "
                         & integer'image(to_integer(sorted(N - 1 - r)))
                         & ", but the model's slot " & integer'image(order(r))
                         & " holds " & integer'image(to_integer(keys(order(r))))
                    severity error;
            end loop;

            if name = "ties" then
                saw_ties := true;
            elsif name = "all_equal" then
                saw_all_equal := true;
            elsif name = "boundary" then
                saw_boundary := true;
            elsif name = "notebook32" then
                saw_anchor := true;
            end if;
        end procedure check_case;

        file manifest : text;
        variable status  : file_open_status;
        variable buf     : line;
        variable name_len : natural;
    begin
        report "tb_bitonic32: starting with PIPE_CUTS = " & integer'image(PIPE_CUTS);

        file_open(status, manifest, VECTOR_DIR & "cases.txt", read_mode);
        assert status = open_ok
            report "cannot open " & VECTOR_DIR & "cases.txt -- run "
                 & "`uv run python -m models.nms vectors` and invoke this from the "
                 & "repository root, or override -gVECTOR_DIR"
            severity failure;

        while not endfile(manifest) loop
            readline(manifest, buf);
            name_len := trim_len(buf.all);
            if name_len > 0 then
                check_case(buf.all(buf.all'low to buf.all'low + name_len - 1));
                cases_checked := cases_checked + 1;
            end if;
        end loop;
        file_close(manifest);

        -- A count alone would not prove the *interesting* cases ran, and the tie sets are
        -- the whole reason this gate exists: a network that sorts distinct keys correctly
        -- and mishandles ties is the classic bitonic failure.
        assert saw_ties and saw_all_equal
            report "the tie cases did not run, so the tie-break was never exercised"
            severity error;
        assert saw_boundary and saw_anchor
            report "the boundary case or the anchor did not run"
            severity error;
        assert cases_checked >= MIN_CASES
            report "only " & integer'image(cases_checked) & " cases ran, expected at "
                 & "least " & integer'image(MIN_CASES) & " -- is cases.txt truncated?"
            severity error;
        assert latency_pinned > MIN_CASES / 2
            report "only " & integer'image(latency_pinned) & " cases changed the output, "
                 & "so the one-edge-short latency check was mostly vacuous"
            severity error;

        report "tb_bitonic32: PIPE_CUTS = " & integer'image(PIPE_CUTS) & ", "
             & integer'image(cases_checked) & " cases x " & integer'image(N)
             & " keys, ordering + permutation + rank table + latency all checked ("
             & integer'image(latency_pinned) & " cases pinned the latency)";
        report "PASS";
        running <= false;
        wait;
    end process stimulus;

end architecture sim;
