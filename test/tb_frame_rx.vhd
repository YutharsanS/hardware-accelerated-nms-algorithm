-- tb_frame_rx -- byte-level testbench for the frame parser.
--
-- Drives frame_rx with bytes directly (no UART), so it is fast and can force `busy` and
-- `settled` -- the start/busy rule's reject paths are unreachable through the real top level
-- at 1 Mbaud, and this is the only place they are exercised. Frames come from the committed
-- models/data/vectors/frames.txt, built in Python with the reference CRC, so this testbench
-- never computes a CRC of its own.
--
-- Every scenario checks the complete outcome: the number and content of box_store writes,
-- the number of start pulses and rejects, the reject status, present_mask, and the echoed seq.
--
--   1. every case's frame: 32 writes to the right slots, one start, the right mask and seq
--   2. a corrupted payload byte                  -> reject 0x01, no start
--   3. busy for the whole frame                  -> no writes, reject 0x02
--   4. busy only at the CRC byte                 -> 32 writes, reject 0x02
--   5. busy only during record 10                -> reject 0x02, although idle at the end
--   6. area pipeline not settled at the CRC byte -> reject 0x02
--   7. garbage and false magic first ("00 A5 11 A5 A5 5A ...") -> still syncs, one start
--   8. truncated frame, then silence past the timeout, then a good frame -> one start only
--   9. framing error mid-frame, then a good frame                        -> one start only

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use std.textio.all;

use work.nms_pkg.all;

entity tb_frame_rx is
    generic (
        BAUD_DIV   : positive := 10;          -- small: sets the idle timeout to 200 clocks
        GAP        : natural  := 3;           -- idle clocks between bytes, well under it
        VECTOR_DIR : string   := "models/data/vectors/"
    );
end entity tb_frame_rx;

architecture sim of tb_frame_rx is

    constant FRAME_BITS : natural := 8 * FRAME_BYTES_IN;
    constant REPLY_BITS : natural := 8 * REPLY_BYTES;
    constant TIMEOUT    : natural := 2 * 10 * BAUD_DIV;

    subtype frame_t is std_logic_vector(FRAME_BITS - 1 downto 0);

    signal clk     : std_logic := '0';
    signal running : boolean   := true;
    signal rst     : std_logic := '1';

    signal rx_data  : std_logic_vector(7 downto 0) := (others => '0');
    signal rx_valid : std_logic := '0';
    signal rx_err   : std_logic := '0';

    signal we           : std_logic;
    signal waddr        : index_t;
    signal wdata        : record_t;
    signal settled      : std_logic := '1';
    signal busy         : std_logic := '0';
    signal start        : std_logic;
    signal present_mask : mask_t;
    signal rej_req      : std_logic;
    signal rej_status   : std_logic_vector(7 downto 0);
    signal seq          : std_logic_vector(7 downto 0);

    -- what the monitor saw since the last clear
    signal clear      : boolean := false;
    signal n_writes   : natural := 0;
    signal n_starts   : natural := 0;
    signal n_rejects  : natural := 0;
    signal last_rej   : std_logic_vector(7 downto 0) := (others => '0');
    signal last_mask  : mask_t := (others => '0');
    signal store      : record_array_t := (others => (others => '0'));

    function byte_of (f : frame_t; k : natural) return std_logic_vector is
    begin
        return f(FRAME_BITS - 1 - 8 * k downto FRAME_BITS - 8 - 8 * k);
    end function byte_of;

begin

    dut : entity work.frame_rx
        generic map (BAUD_DIV => BAUD_DIV)
        port map (
            clk => clk, rst => rst,
            rx_data => rx_data, rx_valid => rx_valid, rx_err => rx_err,
            we => we, waddr => waddr, wdata => wdata, settled => settled, busy => busy,
            start => start, present_mask => present_mask,
            rej_req => rej_req, rej_status => rej_status, seq => seq
        );

    clock : process
    begin
        while running loop
            clk <= '0';
            wait for 5 ns;
            clk <= '1';
            wait for 5 ns;
        end loop;
        wait;
    end process clock;

    monitor : process (clk)
    begin
        if rising_edge(clk) then
            if clear then
                n_writes  <= 0;
                n_starts  <= 0;
                n_rejects <= 0;
                store     <= (others => (others => '0'));
            else
                if we = '1' then
                    n_writes <= n_writes + 1;
                    store(to_integer(waddr)) <= wdata;
                end if;
                if start = '1' then
                    n_starts  <= n_starts + 1;
                    last_mask <= present_mask;
                end if;
                if rej_req = '1' then
                    n_rejects <= n_rejects + 1;
                    last_rej  <= rej_status;
                end if;
            end if;
        end if;
    end process monitor;

    stimulus : process
        type frame_list_t is array (0 to 63) of frame_t;
        variable frames  : frame_list_t;
        variable nframes : natural := 0;
        variable f       : frame_t;

        procedure send (b : std_logic_vector(7 downto 0)) is
        begin
            rx_data  <= b;
            rx_valid <= '1';
            wait until rising_edge(clk);
            rx_valid <= '0';
            for i in 1 to GAP loop
                wait until rising_edge(clk);
            end loop;
        end procedure send;

        -- Send bytes first..last of a frame; `busy_at` forces busy on while byte k goes out.
        procedure send_frame (fr : frame_t; first : natural := 0;
                              last : natural := FRAME_BYTES_IN - 1;
                              busy_from : integer := -1; busy_to : integer := -1) is
        begin
            for k in first to last loop
                if k >= busy_from and k <= busy_to then
                    busy <= '1';
                else
                    busy <= '0';
                end if;
                send(byte_of(fr, k));
            end loop;
            busy <= '0';
        end procedure send_frame;

        procedure reset_counts is
        begin
            clear <= true;
            wait until rising_edge(clk);
            clear <= false;
            wait until rising_edge(clk);
        end procedure reset_counts;

        procedure settle is
        begin
            for i in 1 to 4 loop
                wait until rising_edge(clk);
            end loop;
            wait for 1 ns;
        end procedure settle;

        procedure expect (tag : string; writes, starts, rejects : natural;
                          rej_st : natural := 0) is
        begin
            settle;
            assert n_writes = writes
                report tag & ": " & integer'image(n_writes) & " writes, expected "
                     & integer'image(writes) severity error;
            assert n_starts = starts
                report tag & ": " & integer'image(n_starts) & " starts, expected "
                     & integer'image(starts) severity error;
            assert n_rejects = rejects
                report tag & ": " & integer'image(n_rejects) & " rejects, expected "
                     & integer'image(rejects) severity error;
            if rejects > 0 then
                assert to_integer(unsigned(last_rej)) = rej_st
                    report tag & ": reject status " & to_hstring(last_rej) & ", expected "
                         & integer'image(rej_st) severity error;
            end if;
        end procedure expect;

        -- the record, mask and seq a frame carries, straight from its bytes
        procedure check_contents (tag : string; fr : frame_t) is
            variable rec : std_logic_vector(RECORD_BITS - 1 downto 0);
        begin
            for s in 0 to N - 1 loop
                for b in 0 to RECORD_BYTES - 1 loop
                    rec(RECORD_BITS - 1 - 8 * b downto RECORD_BITS - 8 - 8 * b)
                        := byte_of(fr, 2 + RECORD_BYTES * s + b);
                end loop;
                assert store(s) = unsigned(rec)
                    report tag & ": slot " & integer'image(s) & " holds the wrong record"
                    severity error;
            end loop;
            assert last_mask = byte_of(fr, 258) & byte_of(fr, 259) & byte_of(fr, 260)
                               & byte_of(fr, 261)
                report tag & ": present_mask " & to_hstring(last_mask) severity error;
            assert seq = byte_of(fr, 262)
                report tag & ": seq " & to_hstring(seq) & ", frame carries "
                     & to_hstring(byte_of(fr, 262)) severity error;
        end procedure check_contents;

        file ffile     : text;
        variable fstat : file_open_status;
        variable buf   : line;
        variable reply : std_logic_vector(REPLY_BITS - 1 downto 0);
    begin
        file_open(fstat, ffile, VECTOR_DIR & "frames.txt", read_mode);
        assert fstat = open_ok
            report "cannot open " & VECTOR_DIR & "frames.txt" severity failure;
        while not endfile(ffile) loop
            readline(ffile, buf);
            exit when buf'length = 0;
            hread(buf, frames(nframes));
            readline(ffile, buf);
            hread(buf, reply);
            nframes := nframes + 1;
        end loop;
        file_close(ffile);
        assert nframes >= 20
            report "only " & integer'image(nframes) & " frames in frames.txt" severity error;

        rst <= '1';
        wait until rising_edge(clk);
        wait until rising_edge(clk);
        rst <= '0';
        wait until rising_edge(clk);

        -- --- 1. every case --------------------------------------------------------------
        for i in 0 to nframes - 1 loop
            reset_counts;
            send_frame(frames(i));
            expect("frame " & integer'image(i), N, 1, 0);
            check_contents("frame " & integer'image(i), frames(i));
        end loop;

        f := frames(0);

        -- --- 2. corrupted payload byte ---------------------------------------------------
        reset_counts;
        for k in 0 to FRAME_BYTES_IN - 1 loop
            if k = 100 then
                send(byte_of(f, k) xor x"10");
            else
                send(byte_of(f, k));
            end if;
        end loop;
        expect("corrupted byte", N, 0, 1, STATUS_CRC_FAIL);
        assert seq = byte_of(f, 262)
            report "a CRC reject must still echo the frame's seq" severity error;

        -- --- 3. busy for the whole frame ---------------------------------------------------
        reset_counts;
        send_frame(f, busy_from => 0, busy_to => FRAME_BYTES_IN - 1);
        expect("busy throughout", 0, 0, 1, STATUS_BUSY);

        -- --- 4. busy only at the CRC byte --------------------------------------------------
        reset_counts;
        send_frame(f, busy_from => FRAME_BYTES_IN - 1, busy_to => FRAME_BYTES_IN - 1);
        expect("busy at the CRC byte", N, 0, 1, STATUS_BUSY);

        -- --- 5. busy only while record 10's last byte lands -------------------------------
        reset_counts;
        send_frame(f, busy_from => 2 + 8 * 10 + 7, busy_to => 2 + 8 * 10 + 7);
        expect("busy during record 10", N - 1, 0, 1, STATUS_BUSY);

        -- --- 6. not settled at the CRC byte ------------------------------------------------
        reset_counts;
        send_frame(f, last => FRAME_BYTES_IN - 2);
        settled <= '0';
        send(byte_of(f, FRAME_BYTES_IN - 1));
        settled <= '1';
        expect("not settled", N, 0, 1, STATUS_BUSY);

        -- --- 7. garbage and false magic, then the frame -----------------------------------
        reset_counts;
        send(x"00");
        send(x"A5");
        send(x"11");                            -- A5 then not 5A: back to hunting
        send(x"A5");                            -- A5 A5 5A: the second A5 keeps the match
        send_frame(f);
        expect("resync after garbage", N, 1, 0);
        check_contents("resync after garbage", f);

        -- --- 8. truncated frame, timeout, then a good frame -------------------------------
        reset_counts;
        send_frame(f, last => 50);
        for i in 1 to TIMEOUT + 10 loop
            wait until rising_edge(clk);
        end loop;
        send_frame(frames(1));
        expect("timeout then frame", N + 6, 1, 0);     -- 6 records of the truncated one
        check_contents("timeout then frame", frames(1));

        -- --- 9. framing error mid-frame, then a good frame --------------------------------
        reset_counts;
        send_frame(f, last => 30);
        rx_err <= '1';
        wait until rising_edge(clk);
        rx_err <= '0';
        send_frame(frames(2));
        expect("framing error then frame", N + 3, 1, 0);
        check_contents("framing error then frame", frames(2));

        report "tb_frame_rx: " & integer'image(nframes) & " frames, CRC reject, 4 busy/settle "
             & "rejects, resync, timeout and framing-error recovery checked";
        report "PASS";
        running <= false;
        wait;
    end process stimulus;

end architecture sim;
