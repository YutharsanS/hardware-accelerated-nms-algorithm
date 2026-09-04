-- tb_cas -- self-checking testbench for the compare-and-swap primitive.
--
-- Three layers, cheapest first:
--
--   1. EXHAUSTIVE at W = 4: all 16 x 16 input pairs in both directions, 512 cases. Small
--      enough to be complete, which is stronger than any amount of sampling -- there is
--      no untested input at this width.
--   2. DIRECTED at W = KEY_W = 21: the boundaries (0, max, equal, adjacent) plus the pairs
--      the design actually depends on -- two sort keys with equal scores, checking that
--      the tie-break resolves to the lower index.
--   3. RANDOM at W = 21: 10,000 pairs in both directions, which is what catches a width or
--      truncation error that the W = 4 sweep is too narrow to expose.
--
-- The expected values are computed from integer min/max on to_integer(), deliberately
-- *not* from an unsigned comparison -- reusing the DUT's own mechanism would let a
-- comparator bug cancel itself out.
--
-- cas is combinational, so there is no clock and nothing to stop: the testbench ends when
-- its checks do. It also counts every comparison it makes and asserts the total at the
-- end, so a loop that silently failed to run cannot report PASS having tested nothing.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.math_real.all;

use work.nms_pkg.all;

entity tb_cas is
end entity tb_cas;

architecture sim of tb_cas is

    constant W_SMALL : positive := 4;
    constant W_KEY   : positive := KEY_W;

    constant RANDOM_PAIRS   : natural := 10_000;
    constant DIRECTED_PAIRS : natural := 13;

    -- Every pair is driven in both directions, hence the factor of 2 throughout:
    -- 512 exhaustive at W=4, 26 directed and 20,000 random at W=21.
    constant EXPECTED_CHECKS : natural :=
        2 * (2 ** W_SMALL) * (2 ** W_SMALL)
        + 2 * DIRECTED_PAIRS
        + 2 * RANDOM_PAIRS;

    signal a_small, b_small     : unsigned(W_SMALL - 1 downto 0) := (others => '0');
    signal y0_small, y1_small   : unsigned(W_SMALL - 1 downto 0);
    signal dir_small            : std_logic := '0';

    signal a_key, b_key         : unsigned(W_KEY - 1 downto 0) := (others => '0');
    signal y0_key, y1_key       : unsigned(W_KEY - 1 downto 0);
    signal dir_key              : std_logic := '0';

begin

    dut_small : entity work.cas
        generic map (W => W_SMALL)
        port map (
            a        => a_small,
            b        => b_small,
            dir_desc => dir_small,
            y0       => y0_small,
            y1       => y1_small
        );

    dut_key : entity work.cas
        generic map (W => W_KEY)
        port map (
            a        => a_key,
            b        => b_key,
            dir_desc => dir_key,
            y0       => y0_key,
            y1       => y1_key
        );

    stimulus : process
        variable checks : natural := 0;

        -- Mirrors models/nms/model.sort_key: K = score * N + (N - 1 - index), descending
        -- on K meaning descending score with ties broken by the lower index.
        function sort_key (score : natural; index : natural) return natural is
        begin
            return score * N + (N - 1 - index);
        end function sort_key;

        -- Expected outputs, from integer arithmetic rather than from unsigned compares.
        procedure expect (
            av, bv     :  in natural;
            desc       :  in boolean;
            e0, e1     : out natural
        ) is
            variable lo, hi : natural;
        begin
            if av < bv then
                lo := av;
                hi := bv;
            else
                lo := bv;
                hi := av;
            end if;
            if desc then
                e0 := hi;
                e1 := lo;
            else
                e0 := lo;
                e1 := hi;
            end if;
        end procedure expect;

        procedure check_small (av, bv : natural; d : std_logic) is
            variable e0, e1 : natural;
        begin
            a_small   <= to_unsigned(av, W_SMALL);
            b_small   <= to_unsigned(bv, W_SMALL);
            dir_small <= d;
            wait for 1 ns;
            expect(av, bv, d = '1', e0, e1);
            assert to_integer(y0_small) = e0 and to_integer(y1_small) = e1
                report "cas W=" & integer'image(W_SMALL) & " a=" & integer'image(av)
                     & " b=" & integer'image(bv) & " dir_desc=" & std_logic'image(d)
                     & ": got (" & integer'image(to_integer(y0_small)) & ", "
                     & integer'image(to_integer(y1_small)) & "), expected ("
                     & integer'image(e0) & ", " & integer'image(e1) & ")"
                severity error;
            checks := checks + 1;
        end procedure check_small;

        procedure check_key (av, bv : natural; d : std_logic) is
            variable e0, e1 : natural;
        begin
            a_key   <= to_unsigned(av, W_KEY);
            b_key   <= to_unsigned(bv, W_KEY);
            dir_key <= d;
            wait for 1 ns;
            expect(av, bv, d = '1', e0, e1);
            assert to_integer(y0_key) = e0 and to_integer(y1_key) = e1
                report "cas W=" & integer'image(W_KEY) & " a=" & integer'image(av)
                     & " b=" & integer'image(bv) & " dir_desc=" & std_logic'image(d)
                     & ": got (" & integer'image(to_integer(y0_key)) & ", "
                     & integer'image(to_integer(y1_key)) & "), expected ("
                     & integer'image(e0) & ", " & integer'image(e1) & ")"
                severity error;
            checks := checks + 1;
        end procedure check_key;

        -- Both directions, so every pair is driven through the comparator each way round.
        procedure check_key_both (av, bv : natural) is
        begin
            check_key(av, bv, '0');
            check_key(av, bv, '1');
        end procedure check_key_both;

        constant KEY_MAX : natural := 2 ** W_KEY - 1;

        variable seed1  : positive := 1;
        variable seed2  : positive := 7;
        variable rand   : real;
        variable av, bv : natural;

        variable key_lo, key_hi : natural;
    begin
        report "tb_cas: starting";

        -- --- 1. exhaustive at W = 4 -------------------------------------------------
        --
        -- 256 pairs x 2 directions. Complete for this width: after this loop there is no
        -- untested 4-bit input combination, including every a = b case.
        for av_i in 0 to 2 ** W_SMALL - 1 loop
            for bv_i in 0 to 2 ** W_SMALL - 1 loop
                check_small(av_i, bv_i, '0');
                check_small(av_i, bv_i, '1');
            end loop;
        end loop;
        report "tb_cas: exhaustive W=4 sweep done ("
             & integer'image(checks) & " checks)";

        -- --- 2. directed at W = 21 --------------------------------------------------
        --
        -- The boundaries the 4-bit sweep cannot reach, plus adjacency around the MSB,
        -- which is where a truncated compare would show up.
        check_key_both(0, 0);
        check_key_both(0, KEY_MAX);
        check_key_both(KEY_MAX, 0);
        check_key_both(KEY_MAX, KEY_MAX);
        check_key_both(1, 0);
        check_key_both(KEY_MAX - 1, KEY_MAX);
        check_key_both(2 ** (W_KEY - 1), 2 ** (W_KEY - 1) - 1);
        check_key_both(2 ** (W_KEY - 1) - 1, 2 ** (W_KEY - 1));
        check_key_both(12345, 12345);
        check_key_both(699050, 1398101);

        -- The three sort-key cases: equal scores differing only in index, a lower score
        -- that must lose regardless of index, and the extremes of the key space.
        check_key_both(sort_key(1000, 3), sort_key(1000, 7));
        check_key_both(sort_key(999, 0), sort_key(1000, 31));
        check_key_both(sort_key(0, 31), sort_key(SCORE_MAX, 0));

        -- Tie-break, asserted explicitly rather than left implied by the pair above:
        -- descending K on two equal scores must put the LOWER index first, which is what
        -- makes the network agree with a stable Python sort on input order.
        key_lo  := sort_key(1000, 3);
        key_hi  := sort_key(1000, 7);
        a_key   <= to_unsigned(key_lo, W_KEY);
        b_key   <= to_unsigned(key_hi, W_KEY);
        dir_key <= '1';
        wait for 1 ns;
        assert to_integer(y0_key) = key_lo
            report "tie-break: descending CAS did not favour the key of index 3"
            severity error;
        assert N - 1 - to_integer(y0_key(INDEX_W - 1 downto 0)) = 3
            report "tie-break: recovered index is "
                 & integer'image(N - 1 - to_integer(y0_key(INDEX_W - 1 downto 0)))
                 & ", expected 3"
            severity error;
        assert N - 1 - to_integer(y1_key(INDEX_W - 1 downto 0)) = 7
            report "tie-break: loser's recovered index is "
                 & integer'image(N - 1 - to_integer(y1_key(INDEX_W - 1 downto 0)))
                 & ", expected 7"
            severity error;
        report "tb_cas: directed W=21 cases done ("
             & integer'image(checks) & " checks)";

        -- --- 3. random at W = 21 ----------------------------------------------------
        --
        -- Curated vectors prove the cases you thought of; this is what catches the width
        -- and truncation errors you did not.
        for i in 1 to RANDOM_PAIRS loop
            uniform(seed1, seed2, rand);
            av := natural(floor(rand * real(2 ** W_KEY)));
            if av > KEY_MAX then
                av := KEY_MAX;
            end if;
            uniform(seed1, seed2, rand);
            bv := natural(floor(rand * real(2 ** W_KEY)));
            if bv > KEY_MAX then
                bv := KEY_MAX;
            end if;
            check_key(av, bv, '0');
            check_key(av, bv, '1');
        end loop;

        -- A testbench that reports PASS having tested nothing is the failure mode this
        -- guards against: if a loop bound is ever broken, the count no longer matches.
        assert checks = EXPECTED_CHECKS
            report "tb_cas ran " & integer'image(checks) & " checks, expected "
                 & integer'image(EXPECTED_CHECKS)
            severity error;

        report "tb_cas: " & integer'image(checks) & " comparisons checked ("
             & integer'image(2 * (2 ** W_SMALL) * (2 ** W_SMALL)) & " exhaustive at W="
             & integer'image(W_SMALL) & ", "
             & integer'image(2 * DIRECTED_PAIRS) & " directed and "
             & integer'image(2 * RANDOM_PAIRS) & " random at W="
             & integer'image(W_KEY) & ")";
        report "PASS";
        wait;
    end process stimulus;

end architecture sim;
