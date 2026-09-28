-- uart_tx -- 8N1 transmitter.
--
-- Pulse tx_start with tx_data while tx_busy is low; the byte goes out start bit, 8 data bits
-- LSB first, stop bit, BAUD_DIV cycles each. tx_busy is high from the cycle after tx_start
-- until the stop bit has been shifted out, so a caller that starts the next byte as soon as
-- it falls sends back to back, with the stop bit one clock longer than nominal (the line
-- register lags the shift register by a cycle). A stop bit longer than one bit time is
-- legal 8N1. The line idles high and is driven straight from a register, so it never
-- glitches.
--
-- VHDL-93 subset; combinational logic as concurrent assignments, one clocked process.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

use work.nms_pkg.all;

entity uart_tx is
    generic (
        BAUD_DIV : positive := work.nms_pkg.BAUD_DIV
    );
    port (
        clk      : in  std_logic;
        rst      : in  std_logic;
        tx_start : in  std_logic;
        tx_data  : in  std_logic_vector(7 downto 0);
        tx_busy  : out std_logic;
        tx       : out std_logic
    );
end entity uart_tx;

architecture rtl of uart_tx is

    -- stop & data & start, shifted out LSB first
    signal shreg : std_logic_vector(9 downto 0) := (others => '1');
    signal cnt   : natural range 0 to BAUD_DIV - 1 := 0;
    signal bits  : natural range 0 to 10 := 0;          -- bits still to send
    signal tx_r  : std_logic := '1';

begin

    tx      <= tx_r;
    tx_busy <= '0' when bits = 0 else '1';

    regs : process (clk)
    begin
        if rising_edge(clk) then
            if bits = 0 then
                tx_r <= '1';
                if tx_start = '1' then
                    shreg <= '1' & tx_data & '0';
                    bits  <= 10;
                    cnt   <= 0;
                end if;
            else
                tx_r <= shreg(0);
                if cnt = BAUD_DIV - 1 then
                    cnt   <= 0;
                    shreg <= '1' & shreg(9 downto 1);
                    bits  <= bits - 1;
                else
                    cnt <= cnt + 1;
                end if;
            end if;

            if rst = '1' then
                bits <= 0;
                tx_r <= '1';
            end if;
        end if;
    end process regs;

end architecture rtl;
