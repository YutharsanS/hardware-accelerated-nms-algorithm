library ieee;
use ieee.std_logic_1164.all;

-- A testbench entity is always completely empty
entity tb_Switches_To_LEDs is
end entity tb_Switches_To_LEDs;

architecture sim of tb_Switches_To_LEDs is

    -- 1. Create signals to wire into our module
    -- We initialize the inputs to '0' so they have a known starting state
    signal r_Switch_1 : std_logic := '0';
    signal r_Switch_2 : std_logic := '0';
    signal r_Switch_3 : std_logic := '0';
    signal r_Switch_4 : std_logic := '0';
    
    signal w_LED_1 : std_logic;
    signal w_LED_2 : std_logic;
    signal w_LED_3 : std_logic;
    signal w_LED_4 : std_logic;

begin

    -- 2. Instantiate the Device Under Test (DUT)
    -- This connects our testbench signals to your actual hardware design
    DUT: entity work.Switches_To_LEDs
        port map (
            i_Switch_1 => r_Switch_1,
            i_Switch_2 => r_Switch_2,
            i_Switch_3 => r_Switch_3,
            i_Switch_4 => r_Switch_4,
            o_LED_1    => w_LED_1,
            o_LED_2    => w_LED_2,
            o_LED_3    => w_LED_3,
            o_LED_4    => w_LED_4
        );

    -- 3. The Stimulus Process
    -- This runs sequentially and changes the input signals over time
    p_Stimulus: process
    begin
        -- Start with everything off for 20 nanoseconds
        wait for 20 ns;
        
        -- Turn on Switch 1
        r_Switch_1 <= '1';
        wait for 20 ns;
        
        -- Turn off Switch 1, turn on Switch 2 and 3
        r_Switch_1 <= '0';
        r_Switch_2 <= '1';
        r_Switch_3 <= '1';
        wait for 20 ns;
        
        -- Turn on all switches
        r_Switch_1 <= '1';
        r_Switch_4 <= '1';
        wait for 20 ns;
        
        -- Turn everything off
        r_Switch_1 <= '0';
        r_Switch_2 <= '0';
        r_Switch_3 <= '0';
        r_Switch_4 <= '0';
        wait for 20 ns;
        
        -- The 'wait' statement with no time stops the process from looping infinitely
        wait;
    end process p_Stimulus;

end architecture sim;