# Capture T on silicon with the ILA that `make impl ILA=1` inserts (docs/plan.md Phase E, E3).
#
#   vivado -mode batch -source scripts/ila.tcl -tclargs <outdir> <windows> <port>
#
# Invoked through `make ila`. Programs the board with build/impl_ila/nms_top.bit and its
# probe file, arms the ILA to capture <windows> windows -- one per batch, each triggered on
# the rising edge of `start` -- then runs the host to send that many random batches over the
# UART, checked against the golden model as usual. It writes every captured sample to
# <outdir>/ila.csv (and .vcd for GTKWave); benchmarks/onchip_latency.py then counts the
# cycles from `start` to `done` in each window.
#
# Each window is 128 samples, 8 of them before the trigger: T = 80 fits with margin. Batches
# arrive ~3 ms apart over the UART, so each window holds exactly one batch.

set outdir  [lindex $argv 0]
set windows [lindex $argv 1]
set port    [lindex $argv 2]
set bitfile build/impl_ila/nms_top.bit
set ltxfile build/impl_ila/nms_top.ltx
set depth   128
set pretrig 8

foreach f [list $bitfile $ltxfile] {
    if {![file exists $f]} {
        puts "no $f -- run `make impl ILA=1` first"
        exit 1
    }
}
file mkdir $outdir

open_hw_manager
connect_hw_server -allow_non_jtag
open_hw_target
set dev [lindex [get_hw_devices -quiet xc7a35t*] 0]
if {$dev eq ""} {
    puts "no xc7a35t on the JTAG chain -- is the board on, and are the cable drivers installed?"
    exit 1
}
current_hw_device $dev
set_property PROGRAM.FILE $bitfile $dev
set_property PROBES.FILE  $ltxfile $dev
set_property FULL_PROBES.FILE $ltxfile $dev
program_hw_devices $dev
refresh_hw_device $dev

set ila [lindex [get_hw_ilas -of_objects $dev] 0]
if {$ila eq ""} {
    puts "no ILA in the programmed design -- was it built with `make impl ILA=1`?"
    exit 1
}
set_property CONTROL.WINDOW_COUNT     $windows $ila
set_property CONTROL.DATA_DEPTH       $depth   $ila
set_property CONTROL.TRIGGER_POSITION $pretrig $ila
# No TRIGGER_MODE: the core is built without advanced triggering (C_ADV_TRIGGER false in
# impl.tcl), so it is fixed at basic mode and the property is read-only. With a single
# trigger probe the AND/OR condition makes no difference, so don't fail on it either.
catch {set_property CONTROL.TRIGGER_CONDITION AND $ila}
# `start` is probe0 in nms_top; Vivado names it after its net, or else probe0.
set trig [get_hw_probes -quiet -of_objects $ila -filter {NAME =~ *start*}]
if {$trig eq ""} {
    set trig [get_hw_probes -quiet -of_objects $ila -filter {NAME =~ *probe0*}]
}
if {$trig eq ""} {
    puts "no start probe; probes are: [get_hw_probes -of_objects $ila]"
    exit 1
}
set_property TRIGGER_COMPARE_VALUE eq1'bR [lindex $trig 0]
run_hw_ila $ila

# The host must not start before the ILA is armed; run_hw_ila returns once it is.
puts "ILA armed: $windows windows of $depth samples, trigger on start rising"
set host_rc [catch {
    # Vivado's launcher points LD_LIBRARY_PATH, PYTHONHOME and PYTHONPATH at its own bundled
    # libraries and Python 3.13; with those set, the project's Python can't find its own
    # standard library. Run the host without them.
    exec env -u LD_LIBRARY_PATH -u PYTHONHOME -u PYTHONPATH \
        uv run python -m models.nms.host --port $port \
        --random $windows >@ stdout 2>@ stderr
} host_msg]
if {$host_rc} {
    puts "host reported a failure: $host_msg"
}

if {[catch {wait_on_hw_ila -timeout 1 $ila} msg]} {
    puts "ILA did not fill all $windows windows within a minute: $msg"
    exit 1
}
set data [upload_hw_ila_data $ila]
write_hw_ila_data -force -csv_file $outdir/ila.csv $data
write_hw_ila_data -force -vcd_file $outdir/ila.vcd $data

puts ""
puts "=========== ila ==========="
puts "device  : $dev"
puts "windows : $windows x $depth samples, trigger at sample $pretrig"
puts "samples : $outdir/ila.csv, $outdir/ila.vcd"
puts "host    : [expr {$host_rc ? "FAILED" : "ok"}]"
puts "==========================="

close_hw_target
disconnect_hw_server
close_hw_manager
exit [expr {$host_rc ? 1 : 0}]
