# GHDL / pytest automation for the NMS accelerator.
#
# Included by the root Makefile, so all paths here are relative to the repository root
# and every target is invoked from there:
#
#   make test            everything fast: pytest + all VHDL testbenches
#   make tb_cas          one testbench (analyse, elaborate, run)
#   make tb_hello        toolchain smoke test on the pre-existing hello_tb.vhdl
#   make lint            ruff check + format check on the Python models
#   make synth MOD=cas   Vivado synthesis of one module (blocked, see docs/plan.md M1)
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
      src/components/bitonic32.vhd

# Any RTL file not named above. Adding a module means declaring where it sits in the
# dependency order, so an omission fails the build with a message instead of being
# analysed at whatever position a wildcard happened to choose.
RTL_UNLISTED = $(filter-out $(RTL), \
                 $(wildcard src/components/*.vhd) $(wildcard src/pipeline/*.vhd))

TB_SRC  = $(wildcard test/tb_*.vhd)
TB_ALL  = $(basename $(notdir $(TB_SRC)))

# Testbenches that must run in more than one configuration. Each word is one run's extra
# ghdl -r flags; a testbench with no SWEEP_ entry gets a single run with no overrides.
#
# bitonic32 is verified both purely combinational and with its two register cuts, from one
# testbench that takes the latency as a generic. Running only the shipped configuration
# would leave the combinational path -- the PIPE_CUTS=0 point in B3.2's timing sweep --
# unverified for correctness.
SWEEP_tb_bitonic32 = -gPIPE_CUTS=0 -gPIPE_CUTS=2

PYTHON = uv run
# Lint everything under models/, notebook included (ruff skips non-Python files, so the
# 267 MB .ply under models/data is not a concern). Formatting is enforced only on .py
# sources: reformatting notebook cells is churn that fights nbstripout for no benefit.
PYLINT_SRC   = models
PYFORMAT_SRC = $(wildcard models/__init__.py) $(wildcard models/nms) $(wildcard models/gs)

.PHONY: all test lint clean help $(TB_ALL) tb_hello synth
.DEFAULT_GOAL := help

help:
	@echo "targets: test | lint | tb_hello | $(if $(TB_ALL),$(TB_ALL),<no testbenches yet>) | synth MOD=<mod> | clean"

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
	@for cfg in $(if $(SWEEP_$@),$(SWEEP_$@),__default__); do \
	   if [ "$$cfg" = "__default__" ]; then cfg=""; fi; \
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

# Blocked: this install enumerates zero parts. See docs/plan.md M1 for the diagnosis.
synth:
	@test -n "$(MOD)" || { echo "usage: make synth MOD=<module>"; exit 1; }
	@command -v vivado >/dev/null 2>&1 || { \
	  echo "vivado not on PATH -- run: source ~/Vivado/2026.1/Vivado/settings64.sh"; exit 1; }
	vivado -mode batch -nolog -nojournal -source scripts/synth.tcl -tclargs $(MOD)

# --- Aggregate ---------------------------------------------------------------------

# The standing regression: fast, no Vivado, re-run after every step.
test: lint pytest tb_hello $(TB_ALL)
	@echo
	@echo "ALL GATES PASSED  (python + $(words $(TB_ALL)) vhdl testbench(es) + toolchain smoke test)"

all: test

clean:
	rm -rf $(WORKDIR) *.cf *.o e~*.o
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +
