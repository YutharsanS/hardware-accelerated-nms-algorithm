-- uart_rx -- 8N1 receiver, one byte per rx_valid pulse.
--
-- The RX pin is the design's ONLY asynchronous input (docs/architecture.md section 10), so
-- the 2-flop synchroniser lives here, in front of everything, rather than being left to
-- whoever instantiates this. Nothing downstream ever sees the raw pin.
--
-- Timing: BAUD_DIV clock cycles per bit, exactly 100 at 1 Mbaud from 100 MHz -- an integer
-- divider, so there is no accumulated baud error to budget for. A falling edge starts a
-- count of BAUD_DIV/2 to the middle of the start bit; if the line is high again there, it
-- was a glitch and is ignored. Otherwise each data bit and the stop bit are sampled one
-- BAUD_DIV later, in the middle of the bit, LSB first. A stop bit that reads '0' is a
-- framing error: the byte is dropped and rx_err pulses instead of rx_valid.
--
-- No oversampling-and-vote: at an exact divider the mid-bit sample is already centred, and
-- the frame layer's CRC-8 rejects the rare corrupted byte that gets through.
--
-- VHDL-93 subset; combinational logic as concurrent assignments, one clocked process.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

use work.nms_pkg.all;

entity uart_rx is
    generic (
        -- Clock cycles per bit. Testbenches shrink it to keep bit-level simulation fast.
        BAUD_DIV : positive := work.nms_pkg.BAUD_DIV
    );
    port (
        clk      : in  std_logic;
        rst      : in  std_logic;
        rx       : in  std_logic;                       -- raw pin, asynchronous
        rx_data  : out std_logic_vector(7 downto 0);
        rx_valid : out std_logic;                       -- one cycle per good byte
        rx_err   : out std_logic                        -- one cycle per framing error
    );
end entity uart_rx;

architecture rtl of uart_rx is

    type state_t is (S_IDLE, S_START, S_DATA, S_STOP);

    -- Idle-high line: both synchroniser flops power up at '1' so reset does not look like
    -- a start bit.
    signal rx_meta : std_logic := '1';
    signal rx_s    : std_logic := '1';

    signal state : state_t := S_IDLE;
    signal cnt   : natural range 0 to BAUD_DIV - 1 := 0;
    signal bitn  : natural range 0 to 7 := 0;
    signal shreg : std_logic_vector(7 downto 0) := (others => '0');

    signal valid_r : std_logic := '0';
    signal err_r   : std_logic := '0';

    -- The synchroniser flops must not be packed into logic or retimed apart.
    attribute ASYNC_REG : string;
    attribute ASYNC_REG of rx_meta : signal is "TRUE";
    attribute ASYNC_REG of rx_s    : signal is "TRUE";

begin

    assert BAUD_DIV >= 4
        report "uart_rx: BAUD_DIV must be at least 4 to find the middle of a bit"
        severity failure;

    rx_data  <= shreg;
    rx_valid <= valid_r;
    rx_err   <= err_r;

    regs : process (clk)
    begin
        if rising_edge(clk) then
            rx_meta <= rx;
            rx_s    <= rx_meta;

            valid_r <= '0';
            err_r   <= '0';

            case state is
                when S_IDLE =>
                    if rx_s = '0' then
                        cnt   <= 0;
                        state <= S_START;
                    end if;

                when S_START =>
                    if cnt = BAUD_DIV / 2 - 1 then
                        cnt <= 0;
                        if rx_s = '0' then
                            bitn  <= 0;
                            state <= S_DATA;
                        else
                            state <= S_IDLE;            -- glitch, not a start bit
                        end if;
                    else
                        cnt <= cnt + 1;
                    end if;

                when S_DATA =>
                    if cnt = BAUD_DIV - 1 then
                        cnt   <= 0;
                        shreg <= rx_s & shreg(7 downto 1);
                        if bitn = 7 then
                            state <= S_STOP;
                        else
                            bitn <= bitn + 1;
                        end if;
                    else
                        cnt <= cnt + 1;
                    end if;

                when S_STOP =>
                    if cnt = BAUD_DIV - 1 then
                        cnt   <= 0;
                        state <= S_IDLE;
                        if rx_s = '1' then
                            valid_r <= '1';
                        else
                            err_r <= '1';
                        end if;
                    else
                        cnt <= cnt + 1;
                    end if;
            end case;

            if rst = '1' then
                state   <= S_IDLE;
                valid_r <= '0';
                err_r   <= '0';
            end if;
        end if;
    end process regs;

end architecture rtl;
