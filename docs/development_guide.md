# VHDL Development Workflow: GHDL & GTKWave

This guide outlines the standard workflow for compiling, simulating, and viewing VHDL designs using open-source tools. 

## Prerequisites
Ensure all team members have the following installed and added to their system `PATH`:
* **[GHDL](https://github.com/ghdl/ghdl):** The open-source VHDL compiler and simulator.
* **[GTKWave](https://gtkwave.sourceforge.net/):** The waveform viewer.

## Standard Directory Structure
To keep the repository organized, please adhere to the following structure:
```text
├── src/           # VHDL design files (.vhd)
├── test/          # Testbench files (.vhd)
├── Makefile       # Automation script
```

## Step-by-Step Manual Workflow

If you are running the tools manually from the command line, the workflow consists of four steps: Analyze, Elaborate, Run, and View.

### 1. Analyze (Compile)

First, analyze the design files and then the testbench. Order matters: dependencies must be analyzed before the files that instantiate them.

```bash
ghdl -a src/my_design.vhd
ghdl -a tb/my_design_tb.vhd

```

### 2. Elaborate (Build)

Elaborate the top-level entity (usually your testbench). This creates the executable for the simulation. *Note: Use the entity name, not the file name.*

```bash
ghdl -e my_design_tb

```

### 3. Run (Simulate)

Run the simulation and generate a waveform file. We recommend using the `.ghw` (GHDL Waveform) format instead of `.vcd`, as it perfectly supports VHDL's complex data types (like records and multi-dimensional arrays).

```bash
ghdl -r my_design_tb --wave=wave.ghw

```

*Optional: To stop a continuous simulation at a specific time, append `--stop-time=100ns`.*

### 4. View (GTKWave)

Open the generated waveform file in GTKWave to verify your signals.

```bash
gtkwave wave.ghw

```

---

## 🚀 Automated Workflow (Using Make)

To avoid typing the commands above repeatedly, use the provided `Makefile`. Just open your terminal in the project root and run:

* **`make`** or **`make all`**: Compiles everything, runs the simulation, and opens GTKWave automatically.
* **`make clean`**: Removes all compiled object files and waveform data to keep the workspace clean before committing to Git.

### Template Makefile

*(Create a file named `Makefile` in the root directory and paste this)*

```makefile
# --- Configuration ---
# Top level entity (your testbench name)
TOP = my_design_tb

# Source and Testbench directories
SRC_DIR = src
TB_DIR = tb

# Files to compile (order matters: design first, then tb)
FILES = $(SRC_DIR)/my_design.vhd $(TB_DIR)/my_design_tb.vhd

# Waveform output file
WAVE = wave.ghw

# --- Targets ---
.PHONY: all clean

all: $(WAVE)
	gtkwave $(WAVE) &

$(WAVE): $(FILES)
	@echo "==> Analyzing files..."
	ghdl -a $(FILES)
	@echo "==> Elaborating top entity..."
	ghdl -e $(TOP)
	@echo "==> Running simulation..."
	ghdl -r $(TOP) --wave=$(WAVE)

clean:
	@echo "==> Cleaning up..."
	ghdl --clean
	rm -f $(WAVE) work-obj93.cf

```

---

## Python tooling (uv)

Python dev tooling (currently just `nbstripout`; formatters like Ruff/Black will join it here per `scripts/`) is managed at the repo root with [`uv`](https://docs.astral.sh/uv/). `pyproject.toml` and `uv.lock` are committed; the `.venv/` uv creates is not (see `.gitignore`).

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then from the repo root:

```bash
uv sync
```

This creates `.venv/` and installs everything in the `dev` dependency group. Run any tool through it with `uv run <tool>` (e.g. `uv run nbstripout --status`), or activate `.venv` directly.

## Notebook output stripping (nbstripout)

The `models/` directory holds Python "golden model" notebooks (see `docs/README.md`). Executed notebooks embed cell outputs (plots, large data dumps) directly in the `.ipynb` JSON, which bloats the git history with binary-ish diffs every time a notebook is re-run. This repo uses [`nbstripout`](https://github.com/kynan/nbstripout) to strip outputs and execution metadata from `.ipynb` files at commit time, via the `filter=nbstripout` rule in `.gitattributes`.

The filter is a per-clone git config, so each contributor must install it once after cloning:

```bash
uv sync
uv run nbstripout --install --attributes .gitattributes
```

This registers the filter in your local `.git/config`, pointing at the `.venv` python; it is not committed, which is why every contributor needs to run it themselves. After that, `git add`/`git commit` on any `.ipynb` file automatically strips outputs before they reach the commit — your working copy in Jupyter still shows outputs normally.

To check the filter is active:

```bash
uv run nbstripout --status
```
