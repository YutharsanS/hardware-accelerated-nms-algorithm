-- cas -- compare-and-swap, the single primitive the bitonic network is built from.
--
-- 240 of these make bitonic32. It is purely combinational: the register cuts that let the
-- network close 100 MHz live in bitonic32 between sub-stages, not inside the CAS, so that
-- PIPE_CUTS stays a property of the network rather than of its cells.
--
--   dir_desc = '0'  ascending   y0 = min(a, b),  y1 = max(a, b)
--   dir_desc = '1'  descending  y0 = max(a, b),  y1 = min(a, b)
--
-- House style (docs/architecture.md section 10): combinational logic is written as
-- concurrent assignments, never as a combinational process. There is no sensitivity list
-- to get wrong, so the usual sim/synth mismatch is unrepresentable rather than merely
-- avoided, and it synthesises identically to a process(all).
--
-- Cost model, which B2.2 measures against: a W-bit carry-chain compare (~8 LUT6) plus two
-- W-bit 2:1 swap muxes whose select is shared, so two mux bits pack into one LUT6 --
--   LUT = 8 + 2*ceil(W/2)
-- At W = KEY_W = 21 that is 30 LUT, and 240 of them is 7,200 LUT = 34.6% of an XC7A35T.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity cas is
    generic (
        W : positive := 21
    );
    port (
        a        : in  unsigned(W - 1 downto 0);
        b        : in  unsigned(W - 1 downto 0);
        dir_desc : in  std_logic;
        -- Initialised so the outputs are 0 rather than 'U' before the first delta cycle.
        -- Chaining 240 of these makes that visible: an undriven output feeds the next
        -- sub-stage's comparator, and numeric_std reports every such compare as
        -- "metavalue detected" -- 1,680 warning lines at time 0 for one bitonic32 run,
        -- which is noise that would hide a real one. Fixed here rather than by silencing
        -- the simulator, so it stays fixed under xsim at B6.1 too. Synthesis ignores an
        -- initial value on a combinational output.
        y0       : out unsigned(W - 1 downto 0) := (others => '0');
        y1       : out unsigned(W - 1 downto 0) := (others => '0')
    );
end entity cas;

architecture rtl of cas is

    -- One comparator feeding both muxes. Ascending wants the swap when a > b; descending
    -- wants it when a <= b, which the xor gives without a second comparator. The a = b
    -- case swaps under dir_desc = '1', which is unobservable because the two values are
    -- equal -- and irrelevant in this design regardless, since the sort key is a strict
    -- total order (docs/architecture.md section 6) so equal keys never reach a CAS.
    signal swap : boolean;

begin

    swap <= (a > b) xor (dir_desc = '1');

    y0 <= b when swap else a;
    y1 <= a when swap else b;

end architecture rtl;
