# Out-of-context synthesis and implementation of one module, for the area and timing
# gates in docs/plan.md (B2.2, B3.2, B4.2).
#
#   vivado -mode batch -source scripts/synth.tcl -tclargs <module> [period_ns] [G=V ...]
#
# Invoked through `make synth MOD=<module>`. Numbers are taken after route_design, not
# after synth_design: a post-synthesis estimate omits routing delay, and P1 in the plan
# is a claim about real Fmax rather than about the logic depth alone.

set mod [lindex $argv 0]
if {$mod eq ""} {
    puts "usage: synth.tcl <module> \[period_ns\] \[GENERIC=VALUE ...\]"
    exit 1
}

set period 10.0
set generics {}
foreach arg [lrange $argv 1 end] {
    if {[string match "*=*" $arg]} {
        lappend generics $arg
    } else {
        set period $arg
    }
}

set part   xc7a35tcpg236-1
set outdir build/synth/$mod
file mkdir $outdir

# Dependency order, mirroring RTL in scripts/Makefile: a unit must be read before
# anything that instantiates it.
set rtl {
    src/components/nms_pkg.vhd
    src/components/cas.vhd
    src/components/bitonic32.vhd
    src/components/iou_lane.vhd
    src/components/nms_ctrl.vhd
}

create_project -in_memory -part $part
# No -vhdl2008: synthesisable RTL is held to the VHDL-93 subset (see scripts/Makefile).
read_vhdl $rtl

set synth_args [list -top $mod -part $part -mode out_of_context]
foreach g $generics {
    lappend synth_args -generic $g
}

# Out of context, because these are internal blocks rather than a top level: bitonic32
# alone presents 2 * 32 * 21 bits at its boundary, which no 236-pin package can carry.
# It also keeps the report free of IBUF/OBUF and pin-placement noise.
synth_design {*}$synth_args

# A module with no clock port is purely combinational (cas), so its constraint is a
# bound on input-to-output delay rather than a clock period. Both forms leave a single
# worst-case setup path for the slack query below.
set clk_ports [get_ports -quiet clk]
if {[llength $clk_ports] > 0} {
    create_clock -name clk -period $period $clk_ports
    # Port paths must be constrained too, or they are silently left out of the WNS below.
    # With only create_clock, input-port -> register and register -> output-port paths are
    # "unconstrained" -- which excluded iou_lane's entire stage 1 and bitonic32's first and
    # last segments from every figure taken before this line existed. A zero delay against
    # clk models each neighbour as a register at the boundary: the port path gets the full
    # period, as it would when the module sits between registers in the integrated design.
    set data_in [get_ports -quiet -filter {DIRECTION == IN && NAME != clk}]
    if {[llength $data_in] > 0} {
        set_input_delay 0 -clock clk $data_in
    }
    set data_out [get_ports -quiet -filter {DIRECTION == OUT}]
    if {[llength $data_out] > 0} {
        set_output_delay 0 -clock clk $data_out
    }
} else {
    set_max_delay $period -from [all_inputs] -to [all_outputs]
}

opt_design
place_design
route_design

report_utilization    -file $outdir/utilization.rpt
report_timing_summary -file $outdir/timing.rpt

# Counted from report_utilization rather than by counting cells. A
# `get_cells -filter {PRIMITIVE_GROUP == LUT}` query does not agree with the report --
# on bitonic32 it returns exactly twice the LUT count -- and the report is the figure
# every other tool and the datasheet quote, so it is the one the gates are judged on.
set util [report_utilization -return_string]

proc util_row {rpt name} {
    foreach line [split $rpt "\n"] {
        if {[regexp "^\\|\\s*${name}\\s*\\|\\s*(\\d+)\\s*\\|" $line -> v]} {
            return $v
        }
    }
    return 0
}

set luts  [util_row $util "Slice LUTs"]
set flops [util_row $util "Slice Registers"]
set carry [util_row $util "CARRY4"]
set dsps  [util_row $util "DSPs"]
set brams [util_row $util "Block RAM Tile"]

set path [get_timing_paths -quiet -max_paths 1 -nworst 1 -setup]
set wns  [expr {[llength $path] > 0 ? [get_property SLACK $path] : "n/a"}]

puts ""
puts "=========== synth $mod ==========="
puts "part        : $part"
puts "generics    : [expr {[llength $generics] ? $generics : {(defaults)}}]"
puts "constraint  : $period ns"
set lut_avail [get_property LUT_ELEMENTS [get_parts $part]]
puts "LUT         : $luts  ([format %.1f [expr {100.0 * $luts / $lut_avail}]]% of $lut_avail)"
puts "FF          : $flops"
puts "CARRY       : $carry"
puts "DSP         : $dsps"
puts "BRAM        : $brams"
puts "WNS         : $wns ns"
if {$wns ne "n/a"} {
    set delay [expr {$period - $wns}]
    puts "critical    : [format %.3f $delay] ns"
    if {$delay > 0} {
        puts "Fmax        : [format %.1f [expr {1000.0 / $delay}]] MHz"
    }
}
puts "reports     : $outdir/"
puts "=================================="
