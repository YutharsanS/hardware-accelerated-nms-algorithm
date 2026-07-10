library ieee; -- ieee library
use ieee.std_logic_1164.all; --to use std_logic data type (binary values)

entity Switches_To_LEDs is --entity declaration
    port ( --specifying inputs and outputs
        i_Switch_1 : in std_logic;
        i_Switch_2 : in std_logic;
        i_Switch_3 : in std_logic;
        i_Switch_4 : in std_logic;
        o_LED_1 : out std_logic;
        o_LED_2 : out std_logic;
        o_LED_3 : out std_logic;
        o_LED_4 : out std_logic);
end entity Switches_To_LEDs;
architecture RTL of Switches_To_LEDs is
    
begin
    
    o_LED_1 <= i_Switch_1;
    o_LED_2 <= i_Switch_2;
    o_LED_3 <= i_Switch_3;
    o_LED_4 <= i_Switch_4;
    
end architecture RTL;