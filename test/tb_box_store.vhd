-- tb_box_store -- self-checking testbench for the payload and area registers.
--
-- Every case in cases.txt is written into the store, back to back with no reset, so every
-- case also overwrites the one before it. Expected values come from the golden model's
-- files, never from arithmetic in this testbench: .hex for the records, .keys for the sort
-- keys, and .areas for the clamped areas. A testbench that computed areas itself would
-- duplicate the clamp it is meant to check, and a shared mistake would cancel out.
--
-- Per case:
--
--   1. AREA LATENCY, pinned from both sides on the first write: after the write edge the
--      record is visible but the area still holds its old value and `settled` is low; one
--      edge later the area is new and `settled` is high
--   2. the remaining 31 records written in a scrambled slot order (stride 7, coprime to 32),
--      on consecutive cycles for even cases and with idle gaps for odd ones, so the area
--      pipeline sees both back-to-back writes and isolated ones
--   3. once settled: keys = .keys; every row_src 0..31 gives that slot's record and area;
--      every col_grp g gives lane j the slot j + g*P
--
-- Checking only the final register contents would pass a store whose area pipeline wrote
-- the right value to the wrong slot whenever two writes were adjacent. The scrambled order
-- and the back-to-back cases are what close that.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use std.textio.all;

use work.nms_pkg.all;

entity tb_box_store is
    generic (
        P          : positive := work.nms_pkg.P_DEFAULT;
        VECTOR_DIR : string   := "models/data/vectors/"
    );
end entity tb_box_store;

architecture sim of tb_box_store is

    constant G         : positive := N / P;
    constant MIN_CASES : natural  := 20;

    constant KEY_FILE_BITS : natural := 4 * ((KEY_W + 3) / 4);

    signal clk     : std_logic := '0';
    signal running : boolean   := true;

    signal rst       : std_logic := '1';
    signal we        : std_logic := '0';
    signal waddr     : index_t   := (others => '0');
    signal wdata     : record_t  := (others => '0');
    signal settled   : std_logic;
    signal keys      : key_array_t;
    signal row_src   : index_t := (others => '0');
    signal row_rec   : record_t;
    signal row_area  : area_t;
    signal col_grp   : natural range 0 to N / P - 1 := 0;
    signal cand_rec  : record_vec_t(0 to P - 1);
    signal cand_area : area_vec_t(0 to P - 1);

begin

    dut : entity work.box_store
        generic map (P => P)
        port map (
            clk       => clk,
            rst       => rst,
            we        => we,
            waddr     => waddr,
            wdata     => wdata,
            settled   => settled,
            keys      => keys,
            row_src   => row_src,
            row_rec   => row_rec,
            row_area  => row_area,
            col_grp   => col_grp,
            cand_rec  => cand_rec,
            cand_area => cand_area
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
        variable cases_checked : natural := 0;
        variable pinned        : natural := 0;
        variable saw_degen     : boolean := false;

        variable recs  : record_array_t;
        variable areas : area_array_t;
        variable kfile : key_array_t;

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

        procedure load_case (name : string) is
            file f          : text;
            variable fstat  : file_open_status;
            variable buf    : line;
            variable rec_raw  : std_logic_vector(RECORD_BITS - 1 downto 0);
            variable area_raw : std_logic_vector(AREA_W - 1 downto 0);
            variable key_raw  : std_logic_vector(KEY_FILE_BITS - 1 downto 0);
        begin
            file_open(fstat, f, VECTOR_DIR & name & ".hex", read_mode);
            assert fstat = open_ok
                report "cannot open " & VECTOR_DIR & name & ".hex -- run from the "
                     & "repository root or override -gVECTOR_DIR"
                severity failure;
            for i in 0 to N - 1 loop
                readline(f, buf);
                hread(buf, rec_raw);
                recs(i) := unsigned(rec_raw);
            end loop;
            file_close(f);

            file_open(fstat, f, VECTOR_DIR & name & ".areas", read_mode);
            assert fstat = open_ok
                report "cannot open " & name & ".areas -- regenerate with "
                     & "`uv run python -m models.nms vectors`"
                severity failure;
            for i in 0 to N - 1 loop
                readline(f, buf);
                hread(buf, area_raw);
                areas(i) := unsigned(area_raw);
            end loop;
            file_close(f);

            file_open(fstat, f, VECTOR_DIR & name & ".keys", read_mode);
            assert fstat = open_ok report "cannot open " & name & ".keys" severity failure;
            for i in 0 to N - 1 loop
                readline(f, buf);
                hread(buf, key_raw);
                kfile(i) := unsigned(key_raw(KEY_W - 1 downto 0));
            end loop;
            file_close(f);
        end procedure load_case;

        procedure check_case (name : string; gaps : boolean) is
            variable slot     : natural;
            variable old_area : area_t;
        begin
            load_case(name);

            -- --- 1. the first write, with its area latency pinned ------------------------
            slot := 3;
            row_src <= to_unsigned(slot, INDEX_W);
            wait for 1 ns;
            old_area := row_area;
            we    <= '1';
            waddr <= to_unsigned(slot, INDEX_W);
            wdata <= recs(slot);
            wait until rising_edge(clk);                 -- the write edge
            wait for 1 ns;
            we <= '0';
            assert row_rec = recs(slot)
                report name & ": record not visible after its write edge" severity error;
            assert settled = '0'
                report name & ": settled high with an area still in flight" severity error;
            assert row_area = old_area
                report name & ": area changed on the write edge itself; the multiplier "
                     & "stage is missing, so the area would race the record"
                severity error;
            wait until rising_edge(clk);
            wait for 1 ns;
            assert row_area = areas(slot)
                report name & ": slot " & integer'image(slot) & " area "
                     & integer'image(to_integer(row_area)) & " one edge after its write, "
                     & "model says " & integer'image(to_integer(areas(slot)))
                severity error;
            assert settled = '1'
                report name & ": settled still low one edge after the last write"
                severity error;
            if old_area /= areas(slot) then
                pinned := pinned + 1;
            end if;

            -- --- 2. the other 31, scrambled, back to back or with gaps -------------------
            for k in 1 to N - 1 loop
                slot := (3 + 7 * k) mod N;
                we    <= '1';
                waddr <= to_unsigned(slot, INDEX_W);
                wdata <= recs(slot);
                wait until rising_edge(clk);
                wait for 1 ns;
                if gaps then
                    we <= '0';
                    for idle in 1 to 1 + k mod 3 loop
                        wait until rising_edge(clk);
                        wait for 1 ns;
                    end loop;
                end if;
            end loop;
            we <= '0';
            wait until rising_edge(clk);
            wait for 1 ns;
            assert settled = '1'
                report name & ": not settled after the final write" severity error;

            -- --- 3. every read port --------------------------------------------------------
            for i in 0 to N - 1 loop
                assert keys(i) = kfile(i)
                    report name & ": key " & integer'image(i) & " = "
                         & integer'image(to_integer(keys(i))) & ", model says "
                         & integer'image(to_integer(kfile(i)))
                    severity error;
            end loop;

            for s in 0 to N - 1 loop
                row_src <= to_unsigned(s, INDEX_W);
                wait for 1 ns;
                assert row_rec = recs(s)
                    report name & ": row_src " & integer'image(s) & " reads the wrong record"
                    severity error;
                assert row_area = areas(s)
                    report name & ": row_src " & integer'image(s) & " area "
                         & integer'image(to_integer(row_area)) & ", model says "
                         & integer'image(to_integer(areas(s)))
                    severity error;
            end loop;

            for grp in 0 to G - 1 loop
                col_grp <= grp;
                wait for 1 ns;
                for j in 0 to P - 1 loop
                    assert cand_rec(j) = recs(j + grp * P)
                        report name & ": lane " & integer'image(j) & ", group "
                             & integer'image(grp) & " does not read slot "
                             & integer'image(j + grp * P)
                        severity error;
                    assert cand_area(j) = areas(j + grp * P)
                        report name & ": lane " & integer'image(j) & ", group "
                             & integer'image(grp) & " reads the wrong area"
                        severity error;
                end loop;
            end loop;

            if name = "degenerate" then
                saw_degen := true;
            end if;
        end procedure check_case;

        file manifest  : text;
        variable fstat : file_open_status;
        variable buf   : line;
        variable nlen  : natural;
    begin
        report "tb_box_store: P = " & integer'image(P);

        rst <= '1';
        wait until rising_edge(clk);
        wait for 1 ns;
        rst <= '0';

        file_open(fstat, manifest, VECTOR_DIR & "cases.txt", read_mode);
        assert fstat = open_ok
            report "cannot open " & VECTOR_DIR & "cases.txt" severity failure;
        while not endfile(manifest) loop
            readline(manifest, buf);
            nlen := trim_len(buf.all);
            if nlen > 0 then
                check_case(buf.all(buf.all'low to buf.all'low + nlen - 1),
                           gaps => cases_checked mod 2 = 1);
                cases_checked := cases_checked + 1;
            end if;
        end loop;
        file_close(manifest);

        assert cases_checked >= MIN_CASES
            report "only " & integer'image(cases_checked) & " cases ran" severity error;
        assert saw_degen
            report "the degenerate case did not run, so the clamp was never exercised"
            severity error;
        assert pinned > MIN_CASES / 2
            report "only " & integer'image(pinned) & " cases changed the pinned slot's "
                 & "area, so the area-latency check was mostly vacuous"
            severity error;

        report "tb_box_store: P = " & integer'image(P) & ", "
             & integer'image(cases_checked) & " cases x " & integer'image(N)
             & " slots, keys + row mux + " & integer'image(G) & " column groups checked, "
             & "area latency pinned in " & integer'image(pinned) & " cases";
        report "PASS";
        running <= false;
        wait;
    end process stimulus;

end architecture sim;
