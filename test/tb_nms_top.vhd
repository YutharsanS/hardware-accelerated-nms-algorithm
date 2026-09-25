-- tb_nms_top -- the whole board design, driven at the pins.
--
-- A host model serialises complete frames onto RsRx, bit by bit, and a receiver model
-- decodes the reply straight off RsTx. Nothing inside nms_top is touched: this is what the
-- FT2232HQ will see. Frames and expected replies come from models/data/vectors/frames.txt,
-- built in Python with the reference CRC-8.
--
-- BAUD_DIV is shrunk to 4, the smallest uart_rx accepts (and the button debounce to 20
-- clocks), so a 264-byte frame costs ~11k clocks instead of ~264k. The logic under test is identical; tb_uart covers the UART
-- at the real divider of 100.
--
--   1. every case's frame -> reply 00, seq echoed, keep_mask; the LEDs show keep_mask(15:0)
--   2. a corrupted payload byte -> reply 01, seq echoed, zero mask
--   3. garbage and a false magic first -> still syncs, correct reply
--   4. a truncated frame, silence past the timeout -> no reply; the next frame is answered
--   5. a byte with a '0' stop bit mid-frame -> frame dropped, no reply; next one answered
--   6. BTNC pressed mid-frame -> no reply, LEDs cleared; the next frame is answered
--
-- A frame that gets no reply is checked by listening for a full reply window and hearing
-- nothing, and every reply is checked to be exactly 6 bytes -- no more.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use std.textio.all;

use work.nms_pkg.all;

entity tb_nms_top is
    generic (
        BAUD_DIV   : positive := 4;
        DEBOUNCE   : positive := 20;
        -- How many frames.txt cases scenario 1 sends. All of them by default; the standing
        -- regression sends 5 (scripts/Makefile), since tb_nms_core already checks every case
        -- bit-exact through the core and this testbench's job is the transport around it.
        MAX_FRAMES : positive := 64;
        VECTOR_DIR : string   := "models/data/vectors/"
    );
end entity tb_nms_top;

architecture sim of tb_nms_top is

    constant TCLK       : time := 10 ns;
    constant TBIT       : time := BAUD_DIV * TCLK;
    constant FRAME_BITS : natural := 8 * FRAME_BYTES_IN;
    constant REPLY_BITS : natural := 8 * REPLY_BYTES;

    -- Long enough for the core (80 cycles) plus 6 reply bytes, with room to spare.
    constant REPLY_WINDOW : time := (LATENCY_CYCLES + 200) * TCLK + 8 * 10 * TBIT;

    subtype frame_t is std_logic_vector(FRAME_BITS - 1 downto 0);
    subtype reply_t is std_logic_vector(REPLY_BITS - 1 downto 0);

    type byte_list_t is array (0 to 63) of std_logic_vector(7 downto 0);

    signal clk     : std_logic := '0';
    signal running : boolean   := true;
    signal btnC    : std_logic := '0';
    signal RsRx    : std_logic := '1';
    signal RsTx    : std_logic;
    signal led     : std_logic_vector(15 downto 0);

    -- bytes decoded off RsTx
    signal got       : byte_list_t := (others => (others => '0'));
    signal got_count : natural := 0;
    signal got_err   : natural := 0;

    function byte_of (f : frame_t; k : natural) return std_logic_vector is
    begin
        return f(FRAME_BITS - 1 - 8 * k downto FRAME_BITS - 8 - 8 * k);
    end function byte_of;

begin

    dut : entity work.nms_top
        generic map (BAUD_DIV => BAUD_DIV, DEBOUNCE => DEBOUNCE)
        port map (clk => clk, btnC => btnC, RsRx => RsRx, RsTx => RsTx, led => led);

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

    -- The host's receiver: decode RsTx, sampling mid-bit, independent of uart_rx.
    host_rx : process
        variable b : std_logic_vector(7 downto 0);
    begin
        wait until RsTx = '0' or not running;
        if not running then
            wait;
        end if;
        wait for TBIT + TBIT / 2;
        for i in 0 to 7 loop
            b(i) := RsTx;
            wait for TBIT;
        end loop;
        if RsTx = '1' then
            got(got_count mod (got'high + 1)) <= b;     -- circular: only this process writes
            got_count <= got_count + 1;
        else
            got_err <= got_err + 1;
            -- a framing error: resynchronise on the line returning to idle
            if RsTx /= '1' then
                wait until RsTx = '1';
            end if;
        end if;
        -- Now mid-stop-bit with the line high. No `wait until RsTx = '1'` here: `wait until`
        -- waits for an EVENT that makes it true, which would sleep through the next byte.
    end process host_rx;

    host_tx : process
        type frame_list_t is array (0 to 63) of frame_t;
        type reply_list_t is array (0 to 63) of reply_t;
        variable frames  : frame_list_t;
        variable replies : reply_list_t;
        variable nframes : natural := 0;
        variable f       : frame_t;
        variable want    : reply_t;
        variable base    : natural;

        procedure send_byte (b : std_logic_vector(7 downto 0); stop : std_logic := '1') is
        begin
            RsRx <= '0';
            wait for TBIT;
            for i in 0 to 7 loop
                RsRx <= b(i);
                wait for TBIT;
            end loop;
            RsRx <= stop;
            wait for TBIT;
            RsRx <= '1';
        end procedure send_byte;

        procedure send_frame (fr : frame_t; last : natural := FRAME_BYTES_IN - 1) is
        begin
            for k in 0 to last loop
                send_byte(byte_of(fr, k));
            end loop;
        end procedure send_frame;

        -- Listen for one reply window, then check what arrived.
        procedure expect_reply (tag : string; r : reply_t) is
        begin
            base := got_count;
            wait for REPLY_WINDOW;
            assert got_count - base = REPLY_BYTES
                report tag & ": " & integer'image(got_count - base) & " reply bytes, expected "
                     & integer'image(REPLY_BYTES) severity error;
            if got_count - base = REPLY_BYTES then
                for k in 0 to REPLY_BYTES - 1 loop
                    assert got((base + k) mod (got'high + 1))
                           = r(REPLY_BITS - 1 - 8 * k downto REPLY_BITS - 8 - 8 * k)
                        report tag & ": reply byte " & integer'image(k) & " = "
                             & to_hstring(got((base + k) mod (got'high + 1))) & ", expected "
                             & to_hstring(r(REPLY_BITS - 1 - 8 * k downto REPLY_BITS - 8 - 8 * k))
                        severity error;
                end loop;
            end if;
            assert got_err = 0 report tag & ": framing error on RsTx" severity error;
        end procedure expect_reply;

        procedure expect_silence (tag : string) is
        begin
            base := got_count;
            wait for REPLY_WINDOW;
            assert got_count = base
                report tag & ": " & integer'image(got_count - base)
                     & " reply bytes for a frame that should have been dropped"
                severity error;
        end procedure expect_silence;

        -- A frame's expected reply with its status byte replaced and the mask zeroed.
        function reject_of (fr : frame_t; st : natural) return reply_t is
        begin
            return std_logic_vector(to_unsigned(st, 8)) & byte_of(fr, 262) & x"00000000";
        end function reject_of;

        file ffile     : text;
        variable fstat : file_open_status;
        variable buf   : line;
    begin
        file_open(fstat, ffile, VECTOR_DIR & "frames.txt", read_mode);
        assert fstat = open_ok
            report "cannot open " & VECTOR_DIR & "frames.txt" severity failure;
        while not endfile(ffile) loop
            readline(ffile, buf);
            exit when buf'length = 0;
            hread(buf, frames(nframes));
            readline(ffile, buf);
            hread(buf, replies(nframes));
            nframes := nframes + 1;
        end loop;
        file_close(ffile);

        report "tb_nms_top: BAUD_DIV = " & integer'image(BAUD_DIV) & ", "
             & integer'image(nframes) & " frames";

        wait for 50 * TCLK;                     -- power-on reset runs out

        -- --- 1. every case ----------------------------------------------------------------
        for i in 0 to nframes - 1 loop
            exit when i = MAX_FRAMES;
            send_frame(frames(i));
            expect_reply("frame " & integer'image(i), replies(i));
            assert led = replies(i)(15 downto 0)
                report "frame " & integer'image(i) & ": LEDs " & to_hstring(led)
                     & ", keep_mask low half " & to_hstring(replies(i)(15 downto 0))
                severity error;
        end loop;

        f    := frames(0);
        want := replies(0);

        -- --- 2. corrupted payload byte -------------------------------------------------------
        for k in 0 to FRAME_BYTES_IN - 1 loop
            if k = 150 then
                send_byte(byte_of(f, k) xor x"01");
            else
                send_byte(byte_of(f, k));
            end if;
        end loop;
        expect_reply("corrupted byte", reject_of(f, STATUS_CRC_FAIL));

        -- --- 3. garbage and false magic -----------------------------------------------------
        send_byte(x"5A");
        send_byte(x"A5");
        send_byte(x"00");
        send_byte(x"A5");
        send_frame(f);
        expect_reply("resync after garbage", want);

        -- --- 4. truncated frame, then the timeout ---------------------------------------------
        send_frame(f, last => 77);
        expect_silence("truncated frame");
        send_frame(frames(1));
        expect_reply("frame after timeout", replies(1));

        -- --- 5. framing error mid-frame ---------------------------------------------------------
        for k in 0 to 40 loop
            if k = 40 then
                send_byte(byte_of(f, k), stop => '0');
            else
                send_byte(byte_of(f, k));
            end if;
        end loop;
        expect_silence("framing error");
        send_frame(frames(2));
        expect_reply("frame after framing error", replies(2));

        -- --- 6. BTNC mid-frame ------------------------------------------------------------------
        send_frame(f, last => 120);
        btnC <= '1';
        wait for (DEBOUNCE + 20) * TCLK;
        btnC <= '0';
        wait for (DEBOUNCE + 20) * TCLK;
        assert led = x"0000" report "BTNC reset did not clear the LEDs" severity error;
        expect_silence("BTNC mid-frame");
        send_frame(frames(3));
        expect_reply("frame after BTNC", replies(3));

        report "tb_nms_top: " & integer'image(minimum(nframes, MAX_FRAMES)) & " frames answered bit-exact at the "
             & "pins, CRC reject, resync, timeout, framing error and BTNC reset checked";
        report "PASS";
        running <= false;
        wait;
    end process host_tx;

end architecture sim;
