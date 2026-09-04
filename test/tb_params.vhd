-- tb_params -- reports every constant in nms_pkg and checks the derivations it cannot
-- express as derivations in the VHDL-93 subset.
--
-- This is half of the anti-drift check. models/nms/test_params_agree.py runs this
-- testbench, parses the PARAM lines below and asserts each value equals the corresponding
-- number in models/nms/params.py. Comparing *GHDL's own* evaluation of the package rather
-- than a regex's guess at it is the point: a Python re-implementation of VHDL constant
-- folding could itself be wrong, and then the two sides would agree about nothing.
--
-- The same test also asserts that the set of names dumped here equals the set of constants
-- declared in nms_pkg.vhd, so a constant added to the package but not to this file cannot
-- quietly escape the comparison.
--
-- Values above 2**31 - 1 (max LHS = 4,292,870,400 and max RHS = 8,552,202,750) are absent
-- deliberately: they overflow GHDL's 32-bit integer. params.validate() checks those on the
-- Python side, where integers are unbounded.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

use work.nms_pkg.all;

entity tb_params is
end entity tb_params;

architecture sim of tb_params is
begin

    checker : process
        variable dumped : natural := 0;

        procedure dump (name : string; value : integer) is
        begin
            report "PARAM " & name & " " & integer'image(value);
            dumped := dumped + 1;
        end procedure dump;
    begin
        -- --- batch ------------------------------------------------------------------
        dump("N", N);
        dump("INDEX_W", INDEX_W);

        -- --- record -----------------------------------------------------------------
        dump("COORD_W", COORD_W);
        dump("SCORE_W", SCORE_W);
        dump("RECORD_BITS", RECORD_BITS);
        dump("RECORD_BYTES", RECORD_BYTES);
        dump("COORD_MAX", COORD_MAX);
        dump("SCORE_MAX", SCORE_MAX);
        dump("SCORE_SHIFT", SCORE_SHIFT);
        dump("B_SHIFT", B_SHIFT);
        dump("A_SHIFT", A_SHIFT);
        dump("Y_SHIFT", Y_SHIFT);
        dump("X_SHIFT", X_SHIFT);

        -- --- datapath ---------------------------------------------------------------
        dump("AREA_W", AREA_W);
        dump("UNION_W", UNION_W);
        dump("T_INTERMEDIATE_W", T_INTERMEDIATE_W);
        dump("K_SHIFT", K_SHIFT);
        dump("T_INT_W", T_INT_W);
        dump("T_INT", T_INT);
        dump("LHS_W", LHS_W);
        dump("RHS_W", RHS_W);
        dump("COMPARE_W", COMPARE_W);
        dump("KEY_W", KEY_W);

        -- --- architecture -----------------------------------------------------------
        dump("P_DEFAULT", P_DEFAULT);
        dump("LANE_LATENCY", LANE_LATENCY);
        dump("SORT_SUBSTAGES", SORT_SUBSTAGES);
        dump("CAS_COUNT", CAS_COUNT);
        dump("PIPE_CUTS", PIPE_CUTS);
        dump("LATENCY_CYCLES", LATENCY_CYCLES);

        -- --- wire protocol ----------------------------------------------------------
        dump("MAGIC_0", MAGIC_0);
        dump("MAGIC_1", MAGIC_1);
        dump("STATUS_OK", STATUS_OK);
        dump("STATUS_CRC_FAIL", STATUS_CRC_FAIL);
        dump("STATUS_BUSY", STATUS_BUSY);
        dump("STATUS_INTERNAL", STATUS_INTERNAL);
        dump("FRAME_BYTES_IN", FRAME_BYTES_IN);
        dump("REPLY_BYTES", REPLY_BYTES);
        dump("CLOCK_HZ", CLOCK_HZ);
        dump("BAUD", BAUD);
        dump("BAUD_DIV", BAUD_DIV);

        -- --- the subtypes must be cut from those same constants ---------------------
        --
        -- A subtype declared against the wrong constant would be invisible in the dump
        -- above, so the widths are checked rather than assumed.
        assert coord_t'length = COORD_W
            report "coord_t is " & integer'image(coord_t'length) & " bits, not COORD_W"
            severity error;
        assert score_t'length = SCORE_W
            report "score_t is " & integer'image(score_t'length) & " bits, not SCORE_W"
            severity error;
        assert index_t'length = INDEX_W
            report "index_t is " & integer'image(index_t'length) & " bits, not INDEX_W"
            severity error;
        assert key_t'length = KEY_W
            report "key_t is " & integer'image(key_t'length) & " bits, not KEY_W"
            severity error;
        assert area_t'length = AREA_W
            report "area_t is " & integer'image(area_t'length) & " bits, not AREA_W"
            severity error;
        assert record_t'length = RECORD_BITS
            report "record_t is " & integer'image(record_t'length) & " bits, not RECORD_BITS"
            severity error;
        assert mask_t'length = N
            report "mask_t is " & integer'image(mask_t'length) & " bits, not N"
            severity error;
        assert key_array_t'length = N and index_array_t'length = N
             and area_array_t'length = N and record_array_t'length = N
            report "an array type does not have N elements"
            severity error;

        -- --- derivations the package cannot state as derivations ---------------------
        assert 2 ** INDEX_W = N
            report "INDEX_W does not address exactly N slots"
            severity error;
        assert 4 * COORD_W + SCORE_W = RECORD_BITS
            report "the record has spare or missing bits"
            severity error;
        assert X_SHIFT + COORD_W = RECORD_BITS
            report "x does not sit at the top of the record"
            severity error;
        assert RECORD_BITS mod 8 = 0
            report "the record is not byte-aligned"
            severity error;

        -- The bounds that make the widths correct, evaluated rather than trusted. The
        -- LHS and RHS maxima are omitted here only because they overflow 32-bit integer.
        assert COORD_MAX * COORD_MAX < 2 ** AREA_W
            report "AREA_W cannot hold COORD_MAX squared"
            severity error;
        assert 2 * COORD_MAX * COORD_MAX < 2 ** UNION_W
            report "UNION_W cannot hold two areas"
            severity error;
        assert SCORE_MAX * N + N - 1 = 2 ** KEY_W - 1
            report "KEY_W is not exactly the width of score*N + (N-1)"
            severity error;
        assert RHS_W >= LHS_W and COMPARE_W = RHS_W
            report "the comparison is not done at the width of the wider operand"
            severity error;

        assert T_INT = 2 ** (K_SHIFT - 1)
            report "T_INT does not encode an IoU threshold of 0.5"
            severity error;
        assert T_INT <= 2 ** T_INT_W - 1
            report "T_INT does not fit its own field"
            severity error;

        assert LATENCY_CYCLES = 72
            report "latency at P_DEFAULT is " & integer'image(LATENCY_CYCLES)
                 & " cycles, not the 72 docs/architecture.md states"
            severity error;
        assert N mod P_DEFAULT = 0
            report "P_DEFAULT does not divide N, so a lane would own a partial column"
            severity error;
        assert SORT_SUBSTAGES = 15 and CAS_COUNT = 240
            report "the network is " & integer'image(SORT_SUBSTAGES) & " sub-stages and "
                 & integer'image(CAS_COUNT) & " CAS, not the 15 and 240 the area budget "
                 & "is built on"
            severity error;
        assert PIPE_CUTS <= SORT_SUBSTAGES
            report "PIPE_CUTS exceeds the number of sub-stages there are to cut after"
            severity error;
        assert FRAME_BYTES_IN = 264 and REPLY_BYTES = 6
            report "the frame is not 264 bytes in and 6 out"
            severity error;
        assert CLOCK_HZ mod BAUD = 0 and BAUD_DIV = 100
            report "the baud divider is not exactly 100, so there would be baud error"
            severity error;

        report "tb_params: " & integer'image(dumped) & " constants reported, "
             & "subtype widths and derivations checked";
        report "PASS";
        wait;
    end process checker;

end architecture sim;
