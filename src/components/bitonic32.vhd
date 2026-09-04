-- bitonic32 -- Batcher bitonic sorting network over N = 32 keys of KEY_W bits.
--
-- SORT_SUBSTAGES = 15 sub-stages of N/2 = 16 compare-and-swap units, CAS_COUNT = 240 in
-- total. The schedule is the textbook one, generated rather than written out:
--
--   for kk in 2, 4, 8, 16, 32
--     for jj = kk/2 down to 1, halving
--       for i in 0 .. N-1 where bit jj of i is clear
--         compare-exchange i with (i xor jj), descending when bit kk of i is set
--
-- OUTPUT IS ASCENDING. The rank table therefore reads it reversed --
-- index_table(r) = idx(keys_out(N-1-r)) -- because rank 0 is the highest key. Getting
-- that backwards is the wiring error B5.2 instantiates the real sorter to catch.
--
-- Only {score, index} traverses the network: KEY_W = 21 bits, not the 64-bit record.
-- Routing full payloads would cost 17,280 LUT (83% of the device) against 7,200, which is
-- the measured justification for keying the sort rather than moving the boxes.
--
-- PIPELINING. PIPE_CUTS register cuts are placed between sub-stages, so latency is exactly
-- PIPE_CUTS cycles and PIPE_CUTS = 0 is purely combinational. Cut positions are spread
-- evenly by sub-stage count, which is *not* the same as evenly by delay -- the jj = 16
-- sub-stages route across the whole array while the jj = 1 sub-stages are local. That is
-- the known weak point (docs/plan.md P1), so CUT_AFTER exists to override the placement
-- with positions taken from a real timing report without touching this file.
--
-- No reset port, deliberately. This is a pure feed-forward pipeline with no state that
-- outlives a batch: the controller waits PIPE_CUTS cycles and then reads, so stale
-- contents are never observed. A reset would add FF control routing and change nothing.
--
-- VHDL-93 subset: note the paired `if generate` blocks below rather than VHDL-2008's
-- `else generate`, and the direction bits driven from a constant through a signal because
-- VHDL-93 does not allow an expression as the actual of an `in` port.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

use work.nms_pkg.all;

entity bitonic32 is
    generic (
        -- Register cuts between sub-stages. Latency in cycles equals this value. The
        -- default is spelled with its full name because the generic shadows the package
        -- constant from the point of its own declaration onwards.
        PIPE_CUTS : natural := work.nms_pkg.PIPE_CUTS;
        -- Explicit cut placement: bit s set means "register after sub-stage s". All zeros
        -- (the default) means derive an even spread from PIPE_CUTS instead. When this is
        -- non-zero its population count must equal PIPE_CUTS.
        CUT_AFTER : std_logic_vector(1 to SORT_SUBSTAGES) := (others => '0')
    );
    port (
        clk      : in  std_logic;
        keys_in  : in  key_array_t;
        -- Initialised for the same reason as the cas outputs, and because it is the
        -- truthful power-on value: a Xilinx flip-flop comes up at 0 unless told
        -- otherwise, so 'U' models something the hardware never does.
        keys_out : out key_array_t := (others => (others => '0'))
    );
end entity bitonic32;

architecture rtl of bitonic32 is

    subtype substage_range is natural range 1 to SORT_SUBSTAGES;

    type net_t  is array (0 to SORT_SUBSTAGES) of key_array_t;
    type comb_t is array (substage_range) of key_array_t;
    type dir_t  is array (substage_range) of std_logic_vector(0 to N - 1);

    -- The (kk, jj) pair belonging to sub-stage s, by walking the schedule. Both are pure
    -- and called only to initialise constants, so they cost nothing at run time.
    function stage_kk (s : substage_range) return natural is
        variable idx : natural := 0;
        variable kk  : natural := 2;
        variable jj  : natural;
    begin
        while kk <= N loop
            jj := kk / 2;
            while jj >= 1 loop
                idx := idx + 1;
                if idx = s then
                    return kk;
                end if;
                jj := jj / 2;
            end loop;
            kk := kk * 2;
        end loop;
        return 0;
    end function stage_kk;

    function stage_jj (s : substage_range) return natural is
        variable idx : natural := 0;
        variable kk  : natural := 2;
        variable jj  : natural;
    begin
        while kk <= N loop
            jj := kk / 2;
            while jj >= 1 loop
                idx := idx + 1;
                if idx = s then
                    return jj;
                end if;
                jj := jj / 2;
            end loop;
            kk := kk * 2;
        end loop;
        return 0;
    end function stage_jj;

    -- Direction per (sub-stage, lane): descending when bit kk of the index is set. jj is
    -- a power of two, so "bit kk of i" is (i / kk) mod 2 -- VHDL-93 has no integer `and`.
    function build_dir return dir_t is
        variable m : dir_t := (others => (others => '0'));
    begin
        for s in substage_range loop
            for i in 0 to N - 1 loop
                if (i / stage_kk(s)) mod 2 = 1 then
                    m(s)(i) := '1';
                end if;
            end loop;
        end loop;
        return m;
    end function build_dir;

    -- Where the register cuts go. An explicit CUT_AFTER wins; otherwise spread PIPE_CUTS
    -- cuts evenly by sub-stage count, cut c landing after ceil((c+1)*SUBSTAGES/(CUTS+1)).
    -- At PIPE_CUTS = 2 that is after sub-stages 5 and 10, as docs/architecture.md states.
    function build_cuts return std_logic_vector is
        variable m : std_logic_vector(1 to SORT_SUBSTAGES) := (others => '0');
    begin
        if CUT_AFTER /= (CUT_AFTER'range => '0') then
            return CUT_AFTER;
        end if;
        for c in 0 to PIPE_CUTS - 1 loop
            m(((c + 1) * SORT_SUBSTAGES + PIPE_CUTS) / (PIPE_CUTS + 1)) := '1';
        end loop;
        return m;
    end function build_cuts;

    function popcount (v : std_logic_vector) return natural is
        variable total : natural := 0;
    begin
        for i in v'range loop
            if v(i) = '1' then
                total := total + 1;
            end if;
        end loop;
        return total;
    end function popcount;

    constant DIR  : dir_t                                := build_dir;
    constant CUTS : std_logic_vector(1 to SORT_SUBSTAGES) := build_cuts;

    -- net(s) is the value after s sub-stages; net(0) is the input. comb(s) is sub-stage
    -- s's combinational output, which net(s) either takes directly or registers.
    --
    -- net carries an initial value and comb deliberately does not. comb is driven by the
    -- CAS output ports, so an initialiser here would be overridden by the port driver and
    -- buy nothing -- that half is fixed in cas.vhd, where it originates. net is an
    -- ordinary signal, and without the initialiser every sub-stage reads 'U' during the
    -- first delta cycle, which is 240 metavalue warnings before the first batch arrives.
    -- For the cut sub-stages this is also the power-on state of the registers.
    signal net  : net_t := (others => (others => (others => '0')));
    signal comb : comb_t;

    -- VHDL-93 needs a signal, not an expression, as the actual of an `in` port. Given the
    -- initial value too, so the directions are right from time 0 rather than one delta in.
    signal dir_s : dir_t := DIR;

begin

    -- PIPE_CUTS cuts must be placeable, and an explicit CUT_AFTER must agree with the
    -- latency the controller and the testbenches assume. Checked at elaboration, so a bad
    -- configuration fails the build rather than producing a quietly mistimed sorter.
    assert PIPE_CUTS <= SORT_SUBSTAGES
        report "bitonic32: PIPE_CUTS = " & integer'image(PIPE_CUTS) & " exceeds the "
             & integer'image(SORT_SUBSTAGES) & " sub-stages there are to cut after"
        severity failure;
    assert popcount(CUTS) = PIPE_CUTS
        report "bitonic32: CUT_AFTER places " & integer'image(popcount(CUTS))
             & " registers but PIPE_CUTS says " & integer'image(PIPE_CUTS)
             & ", so the latency would not be PIPE_CUTS cycles"
        severity failure;

    dir_s <= DIR;

    net(0) <= keys_in;

    substages : for s in substage_range generate

        -- 16 CAS per sub-stage: one for each i whose jj bit is clear, paired with i xor jj.
        lanes : for i in 0 to N - 1 generate
            lower : if (i / stage_jj(s)) mod 2 = 0 generate
                unit : entity work.cas
                    generic map (W => KEY_W)
                    port map (
                        a        => net(s - 1)(i),
                        b        => net(s - 1)(i + stage_jj(s)),
                        dir_desc => dir_s(s)(i),
                        y0       => comb(s)(i),
                        y1       => comb(s)(i + stage_jj(s))
                    );
            end generate lower;
        end generate lanes;

        -- Exactly one of these two exists for each sub-stage. Written as a pair because
        -- `else generate` is VHDL-2008 and the RTL stays in the VHDL-93 subset.
        cut : if CUTS(s) = '1' generate
            reg : process (clk)
            begin
                if rising_edge(clk) then
                    net(s) <= comb(s);
                end if;
            end process reg;
        end generate cut;

        straight : if CUTS(s) = '0' generate
            net(s) <= comb(s);
        end generate straight;

    end generate substages;

    keys_out <= net(SORT_SUBSTAGES);

end architecture rtl;
