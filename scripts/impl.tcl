# Full implementation of the board top, nms_top, on the real part with the real pins:
# synthesis, placement, routing, timing, utilisation, and a bitstream.
#
#   vivado -mode batch -source scripts/impl.tcl -tclargs [G=V ...] [ILA]
#
# Invoked through `make impl`. Unlike synth.tcl this is NOT out of context: the top-level
# ports are real I/O, constrained by deployment/basys3.xdc (clock on W5 at 100 MHz, the UART
# and button as false-path asynchronous inputs). The timing figure it prints is therefore
# the one the board will actually run at.
#
# ILA (`make impl ILA=1`): also build an Integrated Logic Analyzer on the core's handshake
# -- start, done, busy, settled, we -- to capture T on silicon (docs/user_guide.md §8).
# The ILA is Vivado's catalogue IP, generated here as `ila_core` and instantiated by
# nms_top's ILA generic: the BASIC licence refuses create_debug_core (post-synthesis
# insertion) but allows the IP. It builds into build/impl_ila/ and writes the probe file
# nms_top.ltx beside the bitstream, so the verified production bitstream in build/impl/ is
# never overwritten by a debug build. The datapath and the wire protocol are unchanged; the
# ILA only watches, and its sample buffer lives in block RAM, which the design otherwise
# leaves unused.

set generics {}
set ila 0
foreach arg $argv {
    if {[string match "*=*" $arg]} {
        lappend generics $arg
    } elseif {$arg eq "ILA"} {
        set ila 1
    }
}

set part   xc7a35tcpg236-1
set top    nms_top
set outdir [expr {$ila ? "build/impl_ila" : "build/impl"}]
file mkdir $outdir

# What the ILA watches, in nms_top's probe order (probe0..4): frame_rx <-> nms_core.
set ila_nets {start done busy settled we}
set ila_depth 2048

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

if {$ila} {
    # -dir keeps the generated IP under build/; without it an in-memory project writes
    # .gen/ and .srcs/ into the repository root.
    file mkdir $outdir/ip
    create_ip -name ila -vendor xilinx.com -library ip -module_name ila_core -dir $outdir/ip
    set_property -dict [list \
        CONFIG.C_NUM_OF_PROBES     [llength $ila_nets] \
        CONFIG.C_DATA_DEPTH        $ila_depth \
        CONFIG.C_INPUT_PIPE_STAGES 0 \
        CONFIG.C_ADV_TRIGGER       false \
        CONFIG.C_EN_STRG_QUAL      0 \
        CONFIG.C_TRIGIN_EN         false \
        CONFIG.C_TRIGOUT_EN        false \
    ] [get_ips ila_core]
    generate_target all [get_ips ila_core]
    synth_ip [get_ips ila_core]
    lappend generics ILA=true
}

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
        if {[regexp "^\\|\\s*${name}\\s*\\|\\s*([0-9.]+)\\s*\\|" $line -> v]} {
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
    if {$ila} {
        write_debug_probes -force $outdir/$top.ltx
    }
    set bit_ok 1
}

puts ""
puts "=========== impl $top ==========="
puts "part        : $part"
puts "generics    : [expr {[llength $generics] ? $generics : {(defaults)}}]"
puts "ILA         : [expr {$ila ? "on: $ila_nets, $ila_depth samples" : "off"}]"
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
