# Load the nms_top bitstream onto a connected Basys 3 over JTAG.
#
#   vivado -mode batch -source scripts/program.tcl -tclargs [bitfile]
#
# Invoked through `make program`, after `make impl` has written build/impl/nms_top.bit.
# Needs the Xilinx cable drivers (README section 6) and the board plugged in and switched on.
# This programs the FPGA's configuration RAM only: the design is lost at power-off, and the
# board's flash is left untouched.
#
# UNTESTED until a board is attached -- the first run is part of hardware bring-up.

set bitfile [expr {[llength $argv] > 0 ? [lindex $argv 0] : "build/impl/nms_top.bit"}]
if {![file exists $bitfile]} {
    puts "no bitstream at $bitfile -- run `make impl` first"
    exit 1
}

open_hw_manager
connect_hw_server -allow_non_jtag
open_hw_target

# The Basys 3 has exactly one device on its JTAG chain, the XC7A35T.
set dev [lindex [get_hw_devices -quiet xc7a35t*] 0]
if {$dev eq ""} {
    puts "no xc7a35t on the JTAG chain -- is the board on, and are the cable drivers installed?"
    exit 1
}
current_hw_device $dev
set_property PROGRAM.FILE $bitfile $dev
program_hw_devices $dev
refresh_hw_device $dev

puts ""
puts "=========== program ==========="
puts "device    : $dev"
puts "bitstream : $bitfile"
puts "done      : [get_property REGISTER.CONFIG_STATUS.BIT14_DONE_PIN $dev]"
puts "==============================="

close_hw_target
disconnect_hw_server
close_hw_manager
