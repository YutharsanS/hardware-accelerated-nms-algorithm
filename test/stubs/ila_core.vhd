-- ila_core -- simulation stub for Vivado's ILA IP.
--
-- nms_top instantiates ila_core only when its ILA generic is true, in debug builds that
-- scripts/impl.tcl synthesises against the real IP. GHDL and xsim still need an entity to
-- bind the component to at analysis, so this one stands in: it takes the same ports and
-- does nothing. It is listed only in scripts/Makefile's simulation sources, never in
-- impl.tcl or synth.tcl, so it can't reach a bitstream.

library ieee;
use ieee.std_logic_1164.all;

entity ila_core is
    port (
        clk    : in std_logic;
        probe0 : in std_logic_vector(0 downto 0);
        probe1 : in std_logic_vector(0 downto 0);
        probe2 : in std_logic_vector(0 downto 0);
        probe3 : in std_logic_vector(0 downto 0);
        probe4 : in std_logic_vector(0 downto 0)
    );
end entity ila_core;

architecture stub of ila_core is
begin
end architecture stub;
