# Full implementation of the board top, nms_top, on the real part with the real pins:
# synthesis, placement, routing, timing, utilisation, and a bitstream.
#
#   vivado -mode batch -source scripts/impl.tcl -tclargs [G=V ...]
#
# Invoked through `make impl`. Unlike synth.tcl this is NOT out of context: the top-level
# ports are real I/O, constrained by deployment/basys3.xdc (clock on W5 at 100 MHz, the UART
# and button as false-path asynchronous inputs). The timing figure it prints is therefore
# the one the board will actually run at.

set generics {}
foreach arg $argv {
    if {[string match "*=*" $arg]} {
        lappend generics $arg
    }
}

set part   xc7a35tcpg236-1
set top    nms_top
set outdir build/impl
file mkdir $outdir

# Dependency order, mirroring RTL in scripts/Makefile.
set rtl {
    src/components/nms_pkg.vhd
    src/components/cas.vhd
    src/components/bitonic32.vhd
    src/components/iou_lane.vhd
    src/components/box_store.vhd
    src/components/nms_ctrl.vhd
    src/components/uart_rx.vhd
    src/components/uart_tx.vhd
    src/components/frame_rx.vhd
    src/components/frame_tx.vhd
    src/pipeline/nms_core.vhd
    src/pipeline/nms_top.vhd
}

create_project -in_memory -part $part
# No -vhdl2008: synthesisable RTL is held to the VHDL-93 subset (see scripts/Makefile).
read_vhdl $rtl
read_xdc deployment/basys3.xdc

set synth_args [list -top $top -part $part]
foreach g $generics {
    lappend synth_args -generic $g
}
synth_design {*}$synth_args

opt_design
place_design
phys_opt_design
route_design

report_utilization       -file $outdir/utilization.rpt
report_timing_summary    -file $outdir/timing.rpt
report_methodology       -file $outdir/methodology.rpt
report_drc               -file $outdir/drc.rpt

set util  [report_utilization -return_string]
proc util_row {rpt name} {
    foreach line [split $rpt "\n"] {
        if {[regexp "^\\|\\s*${name}\\s*\\|\\s*(\\d+)\\s*\\|" $line -> v]} {
            return $v
        }
    }
    return 0
}

set path [get_timing_paths -quiet -max_paths 1 -nworst 1 -setup]
set wns  [expr {[llength $path] > 0 ? [get_property SLACK $path] : "n/a"}]
set hold [get_timing_paths -quiet -max_paths 1 -nworst 1 -hold]
set whs  [expr {[llength $hold] > 0 ? [get_property SLACK $hold] : "n/a"}]

set bit_ok 0
if {$wns ne "n/a" && $wns >= 0 && $whs >= 0} {
    write_bitstream -force $outdir/$top.bit
    set bit_ok 1
}

puts ""
puts "=========== impl $top ==========="
puts "part        : $part"
puts "generics    : [expr {[llength $generics] ? $generics : {(defaults)}}]"
puts "LUT         : [util_row $util {Slice LUTs}]"
puts "FF          : [util_row $util {Slice Registers}]"
puts "DSP         : [util_row $util DSPs]"
puts "BRAM        : [util_row $util {Block RAM Tile}]"
puts "IO          : [util_row $util {Bonded IOB}]"
puts "WNS (setup) : $wns ns"
puts "WHS (hold)  : $whs ns"
if {[llength $path] > 0} {
    puts "critical    : [get_property STARTPOINT_PIN $path] -> [get_property ENDPOINT_PIN $path]"
}
puts "bitstream   : [expr {$bit_ok ? "$outdir/$top.bit" : "NOT written (timing failed)"}]"
puts "reports     : $outdir/"
puts "=================================="
