-- frame_rx -- turns received bytes into box_store writes and a start pulse.
--
-- The host -> FPGA frame (docs/architecture.md section 3), every field MSB first:
--
--   bytes 0..1     magic A5 5A
--   bytes 2..257   32 records, 8 bytes each; record i -> box_store slot i
--   bytes 258..261 present_mask
--   byte  262      seq
--   byte  263      crc8 over bytes 2..262 (CRC-8/SMBUS)
--
-- FRAMING. A plain byte counter would desynchronise for good after one lost byte, so:
--   * HUNT for A5 5A. A second A5 keeps the half-match, so "A5 A5 5A" still syncs.
--   * IDLE TIMEOUT: no byte for more than two byte-times mid-frame returns to HUNT.
--   * a FRAMING ERROR from uart_rx mid-frame also returns to HUNT.
-- A frame dropped by a timeout or a framing error gets no reply; the host's read timeout
-- covers it (plan.md O7). A frame that arrives complete always gets exactly one outcome.
--
-- OUTCOME, decided on the CRC byte (fsm_design.md section 1, the start/busy rule):
--   * CRC mismatch                                        -> reject, status 0x01
--   * core busy at the end, or ANY record write blocked    -> reject, status 0x02
--   * otherwise                                           -> start (present_mask loaded)
-- Writes are gated by `busy`, and a blocked write is remembered, because the CRC covers the
-- bytes received, not the bytes stored: a frame whose early records were blocked would pass
-- its CRC and start a batch on a store holding parts of two frames. `start` also waits for
-- box_store's area pipeline to settle; it always has, since the last record lands five bytes
-- (tens of microseconds) before the CRC byte, but the wait makes it a rule rather than luck.
--
-- A CRC failure may leave some slots overwritten. That is harmless: no batch starts on them,
-- and the next good frame rewrites all 32 slots.
--
-- VHDL-93 subset; combinational logic as concurrent assignments, one clocked process.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

use work.nms_pkg.all;

entity frame_rx is
    generic (
        BAUD_DIV : positive := work.nms_pkg.BAUD_DIV
    );
    port (
        clk : in std_logic;
        rst : in std_logic;

        -- from uart_rx
        rx_data  : in std_logic_vector(7 downto 0);
        rx_valid : in std_logic;
        rx_err   : in std_logic;

        -- to nms_core
        we           : out std_logic;
        waddr        : out index_t;
        wdata        : out record_t;
        settled      : in  std_logic;
        busy         : in  std_logic;
        start        : out std_logic;
        present_mask : out mask_t;

        -- to frame_tx: a reject reply, and the seq every reply echoes
        rej_req    : out std_logic;
        rej_status : out std_logic_vector(7 downto 0);
        seq        : out std_logic_vector(7 downto 0)
    );
end entity frame_rx;

architecture rtl of frame_rx is

    -- Payload bytes are counted from 0 at frame byte 2.
    constant REC_BYTES   : natural := N * RECORD_BYTES;       -- 256
    constant MASK_FIRST  : natural := REC_BYTES;              -- 256..259
    constant SEQ_BYTE    : natural := REC_BYTES + 4;          -- 260
    constant CRC_BYTE    : natural := REC_BYTES + 5;          -- 261
    constant TIMEOUT     : natural := 2 * 10 * BAUD_DIV;      -- two byte-times, in clocks

    type state_t is (S_HUNT, S_MAGIC1, S_BODY);

    -- CRC-8/SMBUS, one byte, MSB first: identical to models/nms/params.py::crc8.
    function crc8_byte (crc : std_logic_vector(7 downto 0);
                        b   : std_logic_vector(7 downto 0)) return std_logic_vector is
        variable c : std_logic_vector(7 downto 0) := crc xor b;
    begin
        for i in 0 to 7 loop
            if c(7) = '1' then
                c := (c(6 downto 0) & '0') xor std_logic_vector(to_unsigned(CRC8_POLY, 8));
            else
                c := c(6 downto 0) & '0';
            end if;
        end loop;
        return c;
    end function crc8_byte;

    signal state   : state_t := S_HUNT;
    signal pos     : natural range 0 to CRC_BYTE := 0;
    signal idle    : natural range 0 to TIMEOUT := 0;
    signal crc     : std_logic_vector(7 downto 0) := std_logic_vector(to_unsigned(CRC8_INIT, 8));
    signal shreg   : std_logic_vector(RECORD_BITS - 1 downto 0) := (others => '0');
    signal mask_sh : mask_t := (others => '0');
    signal seq_sh  : std_logic_vector(7 downto 0) := (others => '0');
    signal blocked : std_logic := '0';

    signal we_r      : std_logic := '0';
    signal waddr_r   : index_t   := (others => '0');
    signal wdata_r   : record_t  := (others => '0');
    signal start_r   : std_logic := '0';
    signal pmask_r   : mask_t    := (others => '0');
    signal rej_r     : std_logic := '0';
    signal rej_st_r  : std_logic_vector(7 downto 0) := (others => '0');
    signal seq_r     : std_logic_vector(7 downto 0) := (others => '0');

    -- the record being assembled, including the byte arriving now
    signal rec_next : std_logic_vector(RECORD_BITS - 1 downto 0);

begin

    we           <= we_r;
    waddr        <= waddr_r;
    wdata        <= wdata_r;
    start        <= start_r;
    present_mask <= pmask_r;
    rej_req      <= rej_r;
    rej_status   <= rej_st_r;
    seq          <= seq_r;

    rec_next <= shreg(RECORD_BITS - 9 downto 0) & rx_data;

    regs : process (clk)
    begin
        if rising_edge(clk) then
            we_r    <= '0';
            start_r <= '0';
            rej_r   <= '0';

            -- Idle timeout: counts clocks since the last byte, only while inside a frame.
            if state = S_HUNT or rx_valid = '1' then
                idle <= 0;
            elsif idle /= TIMEOUT then
                idle <= idle + 1;
            end if;

            case state is
                when S_HUNT =>
                    if rx_valid = '1' and rx_data = std_logic_vector(to_unsigned(MAGIC_0, 8)) then
                        state <= S_MAGIC1;
                    end if;

                when S_MAGIC1 =>
                    if rx_valid = '1' then
                        if rx_data = std_logic_vector(to_unsigned(MAGIC_1, 8)) then
                            state   <= S_BODY;
                            pos     <= 0;
                            crc     <= std_logic_vector(to_unsigned(CRC8_INIT, 8));
                            blocked <= '0';
                        elsif rx_data /= std_logic_vector(to_unsigned(MAGIC_0, 8)) then
                            state <= S_HUNT;
                        end if;                         -- another A5: stay half-matched
                    elsif rx_err = '1' or idle = TIMEOUT then
                        state <= S_HUNT;
                    end if;

                when S_BODY =>
                    if rx_valid = '1' then
                        if pos /= CRC_BYTE then
                            crc <= crc8_byte(crc, rx_data);
                            pos <= pos + 1;
                        end if;

                        if pos < REC_BYTES then
                            shreg <= rec_next;
                            if pos mod RECORD_BYTES = RECORD_BYTES - 1 then
                                if busy = '0' then
                                    we_r    <= '1';
                                    waddr_r <= to_unsigned(pos / RECORD_BYTES, INDEX_W);
                                    wdata_r <= unsigned(rec_next);
                                else
                                    blocked <= '1';
                                end if;
                            end if;
                        elsif pos < SEQ_BYTE then
                            mask_sh <= mask_sh(N - 9 downto 0) & rx_data;
                        elsif pos = SEQ_BYTE then
                            seq_sh <= rx_data;
                        else                            -- the CRC byte: decide
                            state <= S_HUNT;
                            seq_r <= seq_sh;
                            if rx_data /= crc then
                                rej_r    <= '1';
                                rej_st_r <= std_logic_vector(to_unsigned(STATUS_CRC_FAIL, 8));
                            elsif busy = '1' or blocked = '1' or settled = '0' then
                                rej_r    <= '1';
                                rej_st_r <= std_logic_vector(to_unsigned(STATUS_BUSY, 8));
                            else
                                start_r <= '1';
                                pmask_r <= mask_sh;
                            end if;
                        end if;
                    elsif rx_err = '1' or idle = TIMEOUT then
                        state <= S_HUNT;                -- lost sync: drop, no reply
                    end if;
            end case;

            if rst = '1' then
                state   <= S_HUNT;
                we_r    <= '0';
                start_r <= '0';
                rej_r   <= '0';
                blocked <= '0';
            end if;
        end if;
    end process regs;

end architecture rtl;
