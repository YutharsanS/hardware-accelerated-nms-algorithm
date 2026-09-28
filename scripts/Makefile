# GHDL / pytest automation for the NMS accelerator.
#
# Included by the root Makefile, so all paths here are relative to the repository root
# and every target is invoked from there:
#
#   make test            what CI runs: every check, sized to ~1.5 min locally
#   make test-full       the same checks at full volume (~12 min): run before merging
#                        anything that touches the datapath
#   make tb_cas          one testbench (analyse, elaborate, run); add FULL=1 for its full sweep
#   make tb_hello        toolchain smoke test on the pre-existing hello_tb.vhdl
#   make lint            ruff check + format check on the Python models
#   make synth MOD=cas   Vivado out-of-context synthesis + implementation of one module
#   make impl            Vivado implementation of nms_top on the real pins, plus a bitstream
#   make program         load that bitstream onto a connected Basys 3 over JTAG
#   make host            talk to the programmed board: --selftest by default
#                        (PORT=/dev/ttyUSBn, ARGS="--crc-test --random 1000 --latency 500")
#   make xsim TB=tb_x    the same testbench under Vivado xsim, as a second simulator
#   make clean           remove build artefacts
#
# Language policy (docs/plan.md Part 2): synthesisable RTL is written in the VHDL-93
# subset so Vivado's partial VHDL-2008 support can never bite, while testbenches use
# VHDL-2008 for HREAD. Everything is *analysed* at --std=08, which accepts both.

GHDL      ?= ghdl
STD       ?= 08
WORKDIR   ?= build
GHDL_FLAGS = --std=$(STD) --workdir=$(WORKDIR)

# -Wall reports everything; -Wsensitivity in particular catches an incomplete
# combinational sensitivity list, which is a simulation/synthesis mismatch and is *not*
# enabled by default.
#
# --warn-error was held back until B2.1 because GHDL has no per-warning severity, so
# turning it on before a clean baseline exists fails the build on benign categories such
# as unbound components. As of B2.1 the whole tree analyses and elaborates with zero
# warnings, so a new warning now means new code, and it stops the build. If a future
# module trips a category that is genuinely benign, override for that run rather than
# weakening the default:  make test WARN=-Wall
WARN = -Wall --warn-error

# A correct testbench stops its own clock and terminates by itself. This limit is only a
# hang safety net -- if a run needs it to finish, that is a failure, not a pass.
STOP_TIME ?= 100ms
RUNFLAGS   = --assert-level=error --stop-time=$(STOP_TIME)

# RTL in dependency order. GHDL analyses in the order given and a unit must be analysed
# before anything that instantiates it, so this list is explicit rather than a wildcard:
# a wildcard sorts alphabetically, which put bitonic32 ahead of the cas it instantiates
# and broke the build the moment the second module landed.
RTL = src/components/nms_pkg.vhd \
      src/components/cas.vhd \
      src/components/bitonic32.vhd \
      src/components/iou_lane.vhd \
      src/components/box_store.vhd \
      src/components/nms_ctrl.vhd \
      src/components/uart_rx.vhd \
      src/components/uart_tx.vhd \
      src/components/frame_rx.vhd \
      src/components/frame_tx.vhd \
      src/pipeline/nms_core.vhd \
      src/pipeline/nms_top.vhd

# Any RTL file not named above. Adding a module means declaring where it sits in the
# dependency order, so an omission fails the build with a message instead of being
# analysed at whatever position a wildcard happened to choose.
RTL_UNLISTED = $(filter-out $(RTL), \
                 $(wildcard src/components/*.vhd) $(wildcard src/pipeline/*.vhd))

TB_SRC  = $(wildcard test/tb_*.vhd)
TB_ALL  = $(basename $(notdir $(TB_SRC)))

# Testbenches that must run in more than one configuration. Each word is one run's extra
# ghdl -r flags -- join several flags for one run with commas -- and a testbench with no
# SWEEP_ entry gets a single run with no overrides.
#
# TWO TIERS. `make test` is what CI runs on every pull request: every check, sized to finish
# in a few minutes. `make test-full` (FULL=1) swaps in SWEEP_FULL_<tb> where one is defined,
# and NMS_FULL=1 for pytest, restoring the full volumes: 1,000 random batches per core
# configuration, all 20 frames at the pins, the 20,000-batch model sweep. Run it before
# merging anything that touches the datapath.
#
# bitonic32 is verified purely combinational, at the old default of two cuts, and at the
# shipped eight (the lowest that meets 100 MHz), from one testbench that takes the latency
# as a generic. Running only the shipped configuration would leave the combinational path
# -- the PIPE_CUTS=0 point in B3.2's timing sweep -- unverified for correctness.
SWEEP_tb_bitonic32 = -gPIPE_CUTS=0 -gPIPE_CUTS=2 -gPIPE_CUTS=8

# The lane runs at the shipped threshold and at the maximum. Only T_INT=255 drives the
# RHS above 2**31; see docs/build_log.md B4.1 for why the 33rd bit stays unreachable.
SWEEP_tb_iou_lane  = -gT_INT=128 -gT_INT=255

# The control FSM runs at the shipped point, the old sorter default, the combinational
# sorter (the IDLE -> FILL path), and both extremes of the column-group count G = N/P.
SWEEP_tb_nms_ctrl  = -gPIPE_CUTS=8 -gPIPE_CUTS=2 -gPIPE_CUTS=0 -gP=1 -gP=32

# box_store's candidate striping depends only on P: the shipped 16 and both extremes.
SWEEP_tb_box_store = -gP=16 -gP=1 -gP=32

# The UART at the shipped divider and at a small odd one, where BAUD_DIV / 2 rounds down.
SWEEP_tb_uart      = -gBAUD_DIV=100 -gBAUD_DIV=7 -gBAUD_DIV=4

# The board top at the pins, bit by bit. CI sends 2 frames plus every directed scenario at
# BAUD_DIV 4 (tb_uart proves that divider too); the full tier sends all 20 frames.
SWEEP_tb_nms_top      = -gMAX_FRAMES=2
SWEEP_FULL_tb_nms_top = -gMAX_FRAMES=64

# The whole core at the shipped point, with fewer issue registers (functionally identical,
# only T changes), the old sorter default, and P = 32.
#
# CI: the shipped configuration gets the 20 curated cases plus 150 random batches, and every
# other configuration the 20 curated cases -- still bit-exact with the latency pinned, so a
# configuration that breaks cannot pass. The full tier runs all 1,000 random batches in every
# configuration, plus P = 1.
SWEEP_tb_nms_core      = -gRANDOM_COUNT=150 \
                         -gISSUE_REGS=1,-gRANDOM_COUNT=0 -gISSUE_REGS=0,-gRANDOM_COUNT=0 \
                         -gPIPE_CUTS=2,-gRANDOM_COUNT=0 -gP=32,-gRANDOM_COUNT=0
SWEEP_FULL_tb_nms_core = -gISSUE_REGS=2 -gISSUE_REGS=1 -gISSUE_REGS=0 -gPIPE_CUTS=2 -gP=32 -gP=1

# tb_nms_core also runs 1,000 random batches from a gitignored file, regenerated whenever
# the model that defines the expected answers changes.
RANDOM_BATCHES = models/data/random/random_batches.txt
$(RANDOM_BATCHES): models/nms/vectors.py models/nms/batches.py models/nms/model.py models/nms/params.py
	$(PYTHON) python -m models.nms random
tb_nms_core: $(RANDOM_BATCHES)

PYTHON = uv run
# Lint everything under models/, notebook included (ruff skips non-Python files, so the
# 267 MB .ply under models/data is not a concern). Formatting is enforced only on .py
# sources: reformatting notebook cells is churn that fights nbstripout for no benefit.
PYLINT_SRC   = models
PYFORMAT_SRC = $(wildcard models/__init__.py) $(wildcard models/nms) $(wildcard models/gs)

.PHONY: all test test-full lint clean help $(TB_ALL) tb_hello synth xsim impl program host
.DEFAULT_GOAL := help

help:
	@echo "targets: test | test-full | lint | tb_hello | $(if $(TB_ALL),$(TB_ALL),<no testbenches yet>) | synth MOD=<mod> | impl | program | host [PORT=..] [ARGS=..] | xsim TB=<tb> | clean"

$(WORKDIR):
	@mkdir -p $(WORKDIR)

# --- Python ------------------------------------------------------------------------

lint:
	$(PYTHON) ruff format --check $(PYFORMAT_SRC)
	$(PYTHON) ruff check $(PYLINT_SRC)

    # pytest exits 5 when it collects nothing, which is expected only until the first
    # test lands in B1.1. Once tests exist that code cannot occur, so this tolerance
    # never masks a real failure.
pytest:
	@$(PYTHON) pytest -q; rc=$$?; \
	 if [ $$rc -eq 5 ]; then echo "  (no tests collected yet)"; elif [ $$rc -ne 0 ]; then exit $$rc; fi

# --- VHDL --------------------------------------------------------------------------

# Analyse the RTL once; testbenches then only need their own file analysed.
.PHONY: analyse
analyse: | $(WORKDIR)
	@if [ -n "$(strip $(RTL_UNLISTED))" ]; then \
	  echo "RTL not listed in scripts/Makefile: $(strip $(RTL_UNLISTED))"; \
	  echo "add it to RTL, in dependency order -- GHDL analyses in the order given."; \
	  exit 1; \
	fi
	@if [ -n "$(strip $(RTL))" ]; then \
	  echo "  analyse RTL: $(strip $(RTL))"; \
	  $(GHDL) -a $(GHDL_FLAGS) $(WARN) $(RTL); \
	else \
	  echo "  (no RTL yet)"; \
	fi

# One target per testbench in test/, e.g. `make tb_cas`.
$(TB_ALL): analyse
	$(GHDL) -a $(GHDL_FLAGS) $(WARN) test/$@.vhd
	$(GHDL) -e $(GHDL_FLAGS) $(WARN) $@
	@for cfg in $(or $(if $(FULL),$(SWEEP_FULL_$@)),$(SWEEP_$@),__default__); do \
	   if [ "$$cfg" = "__default__" ]; then cfg=""; fi; \
	   cfg=$$(echo "$$cfg" | tr ',' ' '); \
	   echo "  $(GHDL) -r $(GHDL_FLAGS) $@ $$cfg $(RUNFLAGS)"; \
	   $(GHDL) -r $(GHDL_FLAGS) $@ $$cfg $(RUNFLAGS) || exit 1; \
	 done

# Toolchain smoke test. hello_tb.vhdl predates this project layout and lives at the
# repository root. It drives `clk <= not clk after 5 ns` as a free-running concurrent
# assignment and its process ends in `wait`, so it generates events forever and will
# NOT terminate on its own -- hence the explicit, short --stop-time here.
tb_hello: | $(WORKDIR)
	$(GHDL) -a $(GHDL_FLAGS) $(WARN) hello_tb.vhdl
	$(GHDL) -e $(GHDL_FLAGS) $(WARN) hello_tb
	$(GHDL) -r $(GHDL_FLAGS) hello_tb --stop-time=100ns

# --- Vivado ------------------------------------------------------------------------

# Area and timing are measured after route_design, not after synth_design: a post-synthesis
# number omits routing delay, and P1 is a claim about real Fmax. PERIOD sets the clock
# constraint the slack is measured against; GENERICS passes through to synth_design, which
# is how B3.2 sweeps PIPE_CUTS.
#
#   make synth MOD=bitonic32 PERIOD=20 GENERICS="PIPE_CUTS=0"
# Second-opinion simulation under Vivado xsim (docs/plan.md L6): two independent simulators
# agreeing is far stronger evidence than one. Same RTL, same testbench, same vectors. It runs
# in build/xsim, with a `models` symlink so the testbenches' repository-relative vector
# paths resolve unchanged. xsim reports assertion errors and carries on rather than
# stopping, so the verdict comes from the log: no "Error:" line, and a PASS line.
#   make xsim TB=tb_nms_core [XGENERICS="-generic_top ISSUE_REGS=1"]
XSIM_DIR = $(WORKDIR)/xsim
xsim: $(RANDOM_BATCHES)
	@test -n "$(TB)" || { echo "usage: make xsim TB=<testbench> [XGENERICS=...]"; exit 1; }
	@command -v xvhdl >/dev/null 2>&1 || { \
	  echo "xvhdl not on PATH -- run: source ~/Vivado/2026.1/Vivado/settings64.sh"; exit 1; }
	@mkdir -p $(XSIM_DIR) && ln -sfn ../../models $(XSIM_DIR)/models
	cd $(XSIM_DIR) && xvhdl -2008 -nolog $(addprefix ../../,$(RTL)) ../../test/$(TB).vhd >/dev/null
	cd $(XSIM_DIR) && xelab -nolog -debug off $(XGENERICS) $(TB) -s $(TB)_sim >/dev/null
	cd $(XSIM_DIR) && xsim $(TB)_sim -R -nolog | tee $(TB).log | grep -E "Note: (PASS|tb_)|Error:"
	@! grep -q "Error:" $(XSIM_DIR)/$(TB).log || { echo "xsim: $(TB) reported errors"; exit 1; }
	@grep -q "Note: PASS" $(XSIM_DIR)/$(TB).log || { echo "xsim: $(TB) did not reach PASS"; exit 1; }
	@echo "xsim: $(TB) PASS"

# Full implementation of nms_top on the real part with deployment/basys3.xdc: synthesis,
# place, route, timing and -- only if setup and hold both pass -- a bitstream.
#   make impl [GENERICS="PIPE_CUTS=9"]
impl:
	@command -v vivado >/dev/null 2>&1 || { \
	  echo "vivado not on PATH -- run: source ~/Vivado/2026.1/Vivado/settings64.sh"; exit 1; }
	vivado -mode batch -nolog -nojournal -source scripts/impl.tcl -tclargs $(GENERICS)

# Load build/impl/nms_top.bit onto a connected Basys 3 over JTAG (volatile: lost at power-off).
# Then talk to it with:  uv run python -m models.nms.host --selftest
program:
	@command -v vivado >/dev/null 2>&1 || { \
	  echo "vivado not on PATH -- run: source ~/Vivado/2026.1/Vivado/settings64.sh"; exit 1; }
	vivado -mode batch -nolog -nojournal -source scripts/program.tcl -tclargs $(BIT)

# Talk to the programmed board through models/nms/host.py. Runs --selftest unless ARGS says
# otherwise. `make` fails whenever the host does; for the host's own exit codes (1 failed,
# 2 port unopenable) run `uv run python -m models.nms.host` directly.
#   make host
#   make host ARGS="--crc-test --random 1000"
#   make host ARGS="--latency 500 --leave-latency-timer"     # at the 16 ms FTDI default
#   make host PORT=/dev/serial/by-id/<...-if01-port0>
PORT ?= /dev/ttyUSB1
ARGS ?= --selftest
host:
	$(PYTHON) python -m models.nms.host --port $(PORT) $(ARGS)

synth:
	@test -n "$(MOD)" || { echo "usage: make synth MOD=<module> [PERIOD=<ns>] [GENERICS=\"G=V ...\"]"; exit 1; }
	@command -v vivado >/dev/null 2>&1 || { \
	  echo "vivado not on PATH -- run: source ~/Vivado/2026.1/Vivado/settings64.sh"; exit 1; }
	vivado -mode batch -nolog -nojournal -source scripts/synth.tcl \
	  -tclargs $(MOD) $(PERIOD) $(GENERICS)

# --- Aggregate ---------------------------------------------------------------------

# The standing regression: fast, no Vivado, re-run after every step.
test: lint pytest tb_hello $(TB_ALL)
	@echo
	@echo "ALL GATES PASSED  (python + $(words $(TB_ALL)) vhdl testbench(es) + toolchain smoke test)$(if $(FULL),  [FULL tier])"

# The full volumes: see "TWO TIERS" above.
test-full:
	@NMS_FULL=1 $(MAKE) --no-print-directory test FULL=1

all: test

clean:
	rm -rf $(WORKDIR) *.cf *.o e~*.o
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +
