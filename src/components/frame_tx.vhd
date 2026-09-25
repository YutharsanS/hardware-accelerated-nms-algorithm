-- frame_tx -- sends the 6-byte reply, one per frame that arrived complete.
--
--   byte 0     status   0x00 OK, 0x01 CRC fail, 0x02 busy, 0x03 internal error
--   byte 1     seq      echoed from the frame, so a late reply is never mis-attributed
--   bytes 2..5 keep_mask, MSB first -- ZERO whenever status /= 0x00
--
-- Two sources, one rule. nms_core's `done` sends its status and keep_mask; frame_rx's
-- `rej_req` sends a reject status with no mask. The zero-mask rule is applied here, once,
-- for both (fsm_design.md section 1), so a core that finished with status 0x03 cannot leak
-- a partial mask onto the wire.
--
-- The two never collide in practice -- a reply takes 60 us, the next frame 2.64 ms -- but a
-- request that arrives while a reply is still going out is dropped, and flagged in
-- simulation, rather than corrupting the reply in flight.
--
-- VHDL-93 subset; combinational logic as concurrent assignments, one clocked process.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

use work.nms_pkg.all;

entity frame_tx is
    port (
        clk : in std_logic;
        rst : in std_logic;

        -- from nms_core
        done      : in std_logic;
        status    : in unsigned(7 downto 0);
        keep_mask : in mask_t;

        -- from frame_rx
        rej_req    : in std_logic;
        rej_status : in std_logic_vector(7 downto 0);
        seq        : in std_logic_vector(7 downto 0);

        -- to uart_tx
        tx_start : out std_logic;
        tx_data  : out std_logic_vector(7 downto 0);
        tx_busy  : in  std_logic;

        sending : out std_logic
    );
end entity frame_tx;

architecture rtl of frame_tx is

    constant NBYTES : natural := REPLY_BYTES;              -- 6

    signal reply   : std_logic_vector(8 * NBYTES - 1 downto 0) := (others => '0');
    signal left    : natural range 0 to NBYTES := 0;       -- bytes still to send
    signal start_r : std_logic := '0';
    signal wait_tx : std_logic := '0';                     -- a byte was just handed over

begin

    tx_start <= start_r;
    tx_data  <= reply(8 * NBYTES - 1 downto 8 * NBYTES - 8);
    sending  <= '0' when left = 0 else '1';

    regs : process (clk)
        variable st : std_logic_vector(7 downto 0);
    begin
        if rising_edge(clk) then
            start_r <= '0';

            if left = 0 then
                if done = '1' or rej_req = '1' then
                    if done = '1' then
                        st := std_logic_vector(status);
                    else
                        st := rej_status;
                    end if;
                    if done = '1' and status = to_unsigned(STATUS_OK, 8) then
                        reply <= st & seq & keep_mask;
                    else
                        reply <= st & seq & (keep_mask'range => '0');
                    end if;
                    left    <= NBYTES;
                    wait_tx <= '0';
                end if;
            elsif wait_tx = '0' and tx_busy = '0' and start_r = '0' then
                -- hand the head byte to uart_tx; its busy rises the cycle after
                start_r <= '1';
                wait_tx <= '1';
            elsif wait_tx = '1' and tx_busy = '1' then
                -- uart_tx has taken it: shift the next byte to the head
                reply   <= reply(8 * NBYTES - 9 downto 0) & x"00";
                left    <= left - 1;
                wait_tx <= '0';
            end if;

            assert not (left /= 0 and (done = '1' or rej_req = '1'))
                report "frame_tx: a reply was requested while one was still being sent; "
                     & "the new one is dropped"
                severity error;

            if rst = '1' then
                left    <= 0;
                start_r <= '0';
                wait_tx <= '0';
            end if;
        end if;
    end process regs;

end architecture rtl;
