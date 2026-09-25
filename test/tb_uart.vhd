-- tb_uart -- self-checking testbench for uart_tx and uart_rx.
--
--   1. LOOPBACK: uart_tx drives uart_rx; all 256 byte values, then 256 pseudo-random bytes,
--      sent back to back. Every byte must arrive once, in order, unchanged, with no rx_err.
--   2. BIT TIMING: every run uart_tx puts on the line is measured. Low runs must be exact
--      multiples of BAUD_DIV; high runs, which can end in a stop bit, may be up to 2 cycles
--      over (the line register's lag plus this testbench's own busy -> start handshake).
--   3. GLITCH: a low pulse shorter than half a bit, driven by the testbench, must be
--      rejected as a false start -- no rx_valid, no rx_err.
--   4. FRAMING ERROR: a byte whose stop bit is '0' must raise rx_err and not rx_valid, and
--      the receiver must recover and take the next good byte.
--   5. OFF-CENTRE: bytes driven by the testbench with every bit 3% long, and then 3% short,
--      must still arrive intact -- mid-bit sampling tolerates the drift of 10 bits.
--
-- Runs at the shipped BAUD_DIV = 100 and at a small odd divider, where the half-bit count
-- rounds down; see SWEEP_tb_uart in scripts/Makefile.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

use work.nms_pkg.all;

entity tb_uart is
    generic (
        BAUD_DIV : positive := work.nms_pkg.BAUD_DIV
    );
end entity tb_uart;

architecture sim of tb_uart is

    constant NBYTES : natural := 512;
    constant TCLK   : time    := 10 ns;

    type byte_array_t is array (0 to NBYTES - 1) of std_logic_vector(7 downto 0);

    -- 0..255, then a 16-bit LFSR's low byte: deterministic but not sequential
    function build_bytes return byte_array_t is
        variable b   : byte_array_t;
        variable lfs : unsigned(15 downto 0) := x"ACE1";
    begin
        for i in 0 to 255 loop
            b(i) := std_logic_vector(to_unsigned(i, 8));
        end loop;
        for i in 256 to NBYTES - 1 loop
            lfs  := lfs(14 downto 0) & (lfs(15) xor lfs(13) xor lfs(12) xor lfs(10));
            b(i) := std_logic_vector(lfs(7 downto 0));
        end loop;
        return b;
    end function build_bytes;

    constant BYTES : byte_array_t := build_bytes;

    signal clk     : std_logic := '0';
    signal running : boolean   := true;
    signal rst     : std_logic := '1';

    signal tx_start : std_logic := '0';
    signal tx_data  : std_logic_vector(7 downto 0) := (others => '0');
    signal tx_busy  : std_logic;
    signal tx_line  : std_logic;

    -- the receiver's input: either the transmitter or the testbench's own driver
    signal use_tb   : boolean   := false;
    signal tb_line  : std_logic := '1';
    signal rx_line  : std_logic;

    signal rx_data  : std_logic_vector(7 downto 0);
    signal rx_valid : std_logic;
    signal rx_err   : std_logic;

    signal rx_count  : natural := 0;
    signal err_count : natural := 0;
    signal last_byte : std_logic_vector(7 downto 0) := (others => '0');

    signal loopback_done : boolean := false;

begin

    tx_i : entity work.uart_tx
        generic map (BAUD_DIV => BAUD_DIV)
        port map (clk => clk, rst => rst, tx_start => tx_start, tx_data => tx_data,
                  tx_busy => tx_busy, tx => tx_line);

    rx_line <= tb_line when use_tb else tx_line;

    rx_i : entity work.uart_rx
        generic map (BAUD_DIV => BAUD_DIV)
        port map (clk => clk, rst => rst, rx => rx_line, rx_data => rx_data,
                  rx_valid => rx_valid, rx_err => rx_err);

    clock : process
    begin
        while running loop
            clk <= '0';
            wait for TCLK / 2;
            clk <= '1';
            wait for TCLK / 2;
        end loop;
        wait;
    end process clock;

    -- Every received byte is checked in order against what was sent (loopback phase only).
    monitor : process (clk)
    begin
        if rising_edge(clk) then
            if rx_valid = '1' then
                if not loopback_done then
                    assert rx_count < NBYTES
                        report "more bytes received than sent" severity error;
                    if rx_count < NBYTES then
                        assert rx_data = BYTES(rx_count)
                            report "byte " & integer'image(rx_count) & ": got "
                                 & to_hstring(rx_data) & ", sent "
                                 & to_hstring(BYTES(rx_count))
                            severity error;
                    end if;
                end if;
                rx_count  <= rx_count + 1;
                last_byte <= rx_data;
            end if;
            if rx_err = '1' then
                err_count <= err_count + 1;
            end if;
        end if;
    end process monitor;

    -- 2. Measure every bit on the transmitter's line during loopback.
    bit_timer : process
        variable t_edge : time;
        variable len    : natural;
    begin
        -- Start at the first start bit: the idle stretch before it is not a bit.
        wait until tx_line = '0';
        t_edge := now;
        while not loopback_done loop
            wait until tx_line'event or loopback_done;
            exit when loopback_done;
            len := (now - t_edge) / TCLK;
            -- A run of equal bits spans whole bit times. A LOW run never contains a stop bit,
            -- so it must be an exact multiple -- which is where a transmitter that stretched
            -- its bits would show, since every byte starts low. A HIGH run may end in a stop
            -- bit, lengthened by up to 2 cycles: one from the line register's lag, one from
            -- this testbench's own busy -> start handshake.
            if tx_line = '1' then                    -- the run that just ended was low
                assert len mod BAUD_DIV = 0
                    report "tx line held low for " & integer'image(len)
                         & " cycles, not a multiple of BAUD_DIV = " & integer'image(BAUD_DIV)
                    severity error;
            else
                assert len mod BAUD_DIV <= 2
                    report "tx line held high for " & integer'image(len)
                         & " cycles: more than 2 over a multiple of BAUD_DIV = "
                         & integer'image(BAUD_DIV)
                    severity error;
            end if;
            t_edge := now;
        end loop;
        wait;
    end process bit_timer;

    stimulus : process

        -- Drive one byte from the testbench, each bit `bit_cycles` long, stop bit `stop`.
        procedure drive_byte (b : std_logic_vector(7 downto 0); bit_cycles : positive;
                              stop : std_logic := '1') is
        begin
            tb_line <= '0';
            wait for bit_cycles * TCLK;
            for i in 0 to 7 loop
                tb_line <= b(i);
                wait for bit_cycles * TCLK;
            end loop;
            tb_line <= stop;
            wait for bit_cycles * TCLK;
            tb_line <= '1';
            wait for 2 * BAUD_DIV * TCLK;              -- idle between test bytes
        end procedure drive_byte;

        variable before_ok  : natural;
        variable before_err : natural;
    begin
        report "tb_uart: BAUD_DIV = " & integer'image(BAUD_DIV);
        rst <= '1';
        wait for 5 * TCLK;
        wait until rising_edge(clk);
        rst <= '0';

        -- --- 1. loopback, back to back -----------------------------------------------
        for i in 0 to NBYTES - 1 loop
            wait until rising_edge(clk) and tx_busy = '0';
            tx_data  <= BYTES(i);
            tx_start <= '1';
            wait until rising_edge(clk);
            tx_start <= '0';
        end loop;
        wait until rising_edge(clk) and tx_busy = '0';
        wait for 2 * BAUD_DIV * TCLK;
        assert rx_count = NBYTES
            report integer'image(rx_count) & " bytes received, " & integer'image(NBYTES)
                 & " sent"
            severity error;
        assert err_count = 0
            report integer'image(err_count) & " framing errors in loopback" severity error;
        loopback_done <= true;
        use_tb        <= true;
        wait for 2 * BAUD_DIV * TCLK;

        -- --- 3. a glitch shorter than half a bit -------------------------------------
        before_ok  := rx_count;
        before_err := err_count;
        tb_line <= '0';
        wait for maximum(1, BAUD_DIV / 2 - 3) * TCLK;  -- under half a bit, at least 1 clock
        tb_line <= '1';
        wait for 12 * BAUD_DIV * TCLK;
        assert rx_count = before_ok and err_count = before_err
            report "a sub-half-bit glitch was taken as a start bit" severity error;

        -- --- 4. framing error, then recovery -----------------------------------------
        before_ok  := rx_count;
        before_err := err_count;
        drive_byte(x"5A", BAUD_DIV, stop => '0');
        assert rx_count = before_ok and err_count = before_err + 1
            report "a '0' stop bit did not give exactly one rx_err and no rx_valid"
            severity error;
        drive_byte(x"C3", BAUD_DIV);
        assert rx_count = before_ok + 1 and last_byte = x"C3"
            report "the receiver did not recover after a framing error" severity error;

        -- --- 5. bits 3% long, then 3% short ------------------------------------------
        for pass in 0 to 1 loop
            for i in 0 to 15 loop
                before_ok := rx_count;
                if pass = 0 then
                    drive_byte(BYTES(300 + i), BAUD_DIV + (BAUD_DIV * 3) / 100);
                else
                    drive_byte(BYTES(300 + i), BAUD_DIV - (BAUD_DIV * 3) / 100);
                end if;
                assert rx_count = before_ok + 1 and last_byte = BYTES(300 + i)
                    report "off-centre byte " & integer'image(i) & " (pass "
                         & integer'image(pass) & ") lost or corrupted"
                    severity error;
            end loop;
        end loop;

        report "tb_uart: BAUD_DIV = " & integer'image(BAUD_DIV) & ", "
             & integer'image(NBYTES) & " bytes looped back, bit timing measured, "
             & "glitch, framing error and +/-3% drift checked";
        report "PASS";
        running <= false;
        wait;
    end process stimulus;

end architecture sim;
