library ieee;
use ieee.std_logic_1164.all;

entity hello_tb is
end entity;

architecture sim of hello_tb is
    signal clk : std_logic := '0';
begin
    -- Simple clock generator with a 10ns period
    clk <= not clk after 5 ns;

    process
    begin
        report "Simulation started!";
        wait for 50 ns;
        report "Simulation finished!";
        wait;
    end process;
end architecture;