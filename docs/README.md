## Repository Structure

The project is organized to separate hardware description (VHDL), software algorithmic modeling (Python), and automated verification.

```text
bitonic-sorting-network-3dgs/
├── deployment/       # Hardware constraints (e.g., .xdc files) and synthesis scripts for physical FPGA deployment.
├── docs/             # Technical documentation, architecture diagrams, and project reports.
│   ├── development_guide.md  # Guidelines for branching, commit standards, and workflow.
│   └── README.md             # This file.
├── models/           # Software "golden models" (Python) for algorithmic verification.
│   └── data/         # Datasets and generated test vectors (I/O payloads) for the testbenches.
├── scripts/          # Makefiles and utility scripts (e.g., GHDL automation, Python formatters like Ruff/Black).
├── src/              # Synthesizable RTL source code (VHDL).
│   ├── components/   # Individual, modular datapath blocks (e.g., compare-and-swap units, FSMs).
│   └── pipeline/     # Top-level integration wiring the components into the final sorting network.
├── test/             # Automated self-checking VHDL testbenches for unit and pipeline-level verification.
└── LICENSE           # Open-source license documentation.

```

## Claude Skills

This repository defines shared Claude Code skills under `.claude/skills/` so that agentic developers follow consistent commit and PR practices.

- **`/commit`** — Reviews the current git status/diff and creates a single commit, following the [Conventional Commits](https://www.conventionalcommits.org/) format (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`). Accepts an optional message argument to use verbatim instead of generating one.
- **`/pr`** — Prepares a pull request: reviews and stages changes, ensures a conventional commit exists, fetches the task list from a linked GitHub issue number (passed as an argument), and generates a reviewer-friendly PR summary covering what changed, why, testing performed, and potential impacts.

Usage: run `/commit` or `/commit <message>` to create a commit, and `/pr <issue-number>` to prepare a pull request linked to that issue.
