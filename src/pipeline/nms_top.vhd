-- nms_top -- the Basys 3 top level: UART in, NMS, UART out.
--
--   RsRx --> uart_rx --> frame_rx --we/start--> nms_core --done--> frame_tx --> uart_tx --> RsTx
--                           |                                         ^
--                           +---- rejects (CRC 0x01, busy 0x02), seq -+
--
-- One 100 MHz clock domain (pin W5). Two asynchronous inputs, each through a 2-flop
-- synchroniser before anything uses it: the UART RX pin (inside uart_rx) and the BTNC reset
-- button (here).
--
-- RESET is synchronous and active high, from two sources ORed together:
--   * power-on: a small counter holds reset for the first POR_CYCLES clocks after
--     configuration, so the design never starts from whatever the flops happened to hold;
--   * BTNC, debounced: it must read pressed for DEBOUNCE consecutive clocks (10 ms by
--     default) before it counts, so contact bounce cannot produce a train of resets.
--
-- LEDs (plan.md O4): the low 16 bits of the last keep_mask -- a visual check with no host
-- attached. keep_mask is held until the next batch starts, and cleared by reset.
--
-- VHDL-93 subset; combinational logic as concurrent assignments, processes clocked only.

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

use work.nms_pkg.all;

entity nms_top is
    generic (
        BAUD_DIV   : positive := work.nms_pkg.BAUD_DIV;
        DEBOUNCE   : positive := CLOCK_HZ / 100;         -- 10 ms
        POR_CYCLES : positive := 16;
        P          : positive := work.nms_pkg.P_DEFAULT;
        PIPE_CUTS  : natural  := work.nms_pkg.PIPE_CUTS;
        ISSUE_REGS : natural  := work.nms_pkg.ISSUE_REGS
    );
    port (
        clk  : in  std_logic;                         -- W5, 100 MHz
        btnC : in  std_logic;                         -- U18, reset
        RsRx : in  std_logic;                         -- B18, from the FT2232HQ
        RsTx : out std_logic;                         -- A18, to the FT2232HQ
        led  : out std_logic_vector(15 downto 0)
    );
end entity nms_top;

architecture rtl of nms_top is

    -- reset
    signal por_cnt  : natural range 0 to POR_CYCLES := 0;
    signal btn_meta : std_logic := '0';
    signal btn_s    : std_logic := '0';
    signal btn_cnt  : natural range 0 to DEBOUNCE := 0;
    signal btn_db   : std_logic := '0';
    signal rst      : std_logic := '1';

    attribute ASYNC_REG : string;
    attribute ASYNC_REG of btn_meta : signal is "TRUE";
    attribute ASYNC_REG of btn_s    : signal is "TRUE";

    -- uart_rx -> frame_rx
    signal rx_data  : std_logic_vector(7 downto 0);
    signal rx_valid : std_logic;
    signal rx_err   : std_logic;

    -- frame_rx <-> nms_core
    signal we           : std_logic;
    signal waddr        : index_t;
    signal wdata        : record_t;
    signal settled      : std_logic;
    signal busy         : std_logic;
    signal start        : std_logic;
    signal present_mask : mask_t;

    -- -> frame_tx
    signal rej_req    : std_logic;
    signal rej_status : std_logic_vector(7 downto 0);
    signal seq        : std_logic_vector(7 downto 0);
    signal done       : std_logic;
    signal status     : unsigned(7 downto 0);
    signal keep_mask  : mask_t;

    -- frame_tx -> uart_tx
    signal tx_start : std_logic;
    signal tx_data  : std_logic_vector(7 downto 0);
    signal tx_busy  : std_logic;

begin

    -- --- reset ----------------------------------------------------------------------

    reset_gen : process (clk)
    begin
        if rising_edge(clk) then
            btn_meta <= btnC;
            btn_s    <= btn_meta;

            -- debounce: count consecutive samples that disagree with the accepted level
            if btn_s = btn_db then
                btn_cnt <= 0;
            elsif btn_cnt = DEBOUNCE - 1 then
                btn_cnt <= 0;
                btn_db  <= btn_s;
            else
                btn_cnt <= btn_cnt + 1;
            end if;

            if por_cnt /= POR_CYCLES then
                por_cnt <= por_cnt + 1;
            end if;

            if por_cnt /= POR_CYCLES or btn_db = '1' then
                rst <= '1';
            else
                rst <= '0';
            end if;
        end if;
    end process reset_gen;

    -- --- the chain ------------------------------------------------------------------

    rx_i : entity work.uart_rx
        generic map (BAUD_DIV => BAUD_DIV)
        port map (clk => clk, rst => rst, rx => RsRx,
                  rx_data => rx_data, rx_valid => rx_valid, rx_err => rx_err);

    frx_i : entity work.frame_rx
        generic map (BAUD_DIV => BAUD_DIV)
        port map (
            clk => clk, rst => rst,
            rx_data => rx_data, rx_valid => rx_valid, rx_err => rx_err,
            we => we, waddr => waddr, wdata => wdata, settled => settled, busy => busy,
            start => start, present_mask => present_mask,
            rej_req => rej_req, rej_status => rej_status, seq => seq
        );

    core_i : entity work.nms_core
        generic map (P => P, PIPE_CUTS => PIPE_CUTS, ISSUE_REGS => ISSUE_REGS)
        port map (
            clk => clk, rst => rst,
            we => we, waddr => waddr, wdata => wdata, settled => settled,
            start => start, present_mask => present_mask, busy => busy,
            done => done, status => status, keep_mask => keep_mask
        );

    ftx_i : entity work.frame_tx
        port map (
            clk => clk, rst => rst,
            done => done, status => status, keep_mask => keep_mask,
            rej_req => rej_req, rej_status => rej_status, seq => seq,
            tx_start => tx_start, tx_data => tx_data, tx_busy => tx_busy,
            sending => open
        );

    tx_i : entity work.uart_tx
        generic map (BAUD_DIV => BAUD_DIV)
        port map (clk => clk, rst => rst, tx_start => tx_start, tx_data => tx_data,
                  tx_busy => tx_busy, tx => RsTx);

    led <= keep_mask(15 downto 0);

end architecture rtl;
