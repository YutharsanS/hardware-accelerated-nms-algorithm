# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.


## Repository structure

- `src/components/` — individual, modular datapath blocks (e.g., compare-and-swap units, FSMs)
- `src/pipeline/` — top-level integration wiring components into the final sorting network
- `test/` — automated self-checking VHDL testbenches (unit and pipeline-level); does not exist yet, create it when adding the first testbench
- `models/data/` — datasets and generated test vectors (I/O payloads) consumed by testbenches
- `scripts/` — Makefiles and utility scripts (GHDL automation, Python formatters like Ruff/Black)
- `deployment/` — hardware constraints (e.g., `.xdc` files) and synthesis scripts for physical FPGA deployment
- `docs/development_guide.md` — the canonical GHDL/GTKWave workflow reference (see below)


## Claude skills

This repo defines shared Claude Code skills under `.claude/skills/` so agentic changes follow consistent commit/PR practices:

- `/commit [message]` — reviews `git status`/`git diff HEAD`, creates a single commit in Conventional Commits format (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`); an argument is used verbatim as the message.
- `/pr <issue-number>` — stages changes, ensures a conventional commit exists, pulls the task list from the linked GitHub issue, and generates a reviewer-friendly summary (what changed, why, testing performed, potential impacts).

## Conventions

- Commit messages follow Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`), enforced via the `/commit` and `/pr` skills above.
- VHDL testbenches are self-checking (see the `report`/`assert` pattern in `hello_tb.vhdl`) rather than relying solely on manual waveform inspection.
- For the golden model development, python is choosen with `uv` tooling. Follows `Google` code conventions and for formatting and linting `ruff` is used.

# Project Configuration
If the student is struck on project specific configurations guide them using these informations.

## Project Overview
- **Name**: Hardware Acclerated NMS Algorithm with Bitonic Sort
- **Tech Stack**: VHDL, GHDL, Vivado, Basys3
- **Team Size**: 2 developers
- **Deadline**: 28 August 2026 (Behavioral simulation of major components)

## Additional Docs
If the student needs more details, prompts them to read these documents
@docs/architecture.md
@docs/developement_guide.md

## Development Standards

### Code Style
- Use `ruff` for formatting the Golden model related code
- Use google code convention for Python code
- Use `uv` for Python tooling

### Naming Conventions
- **Files**: snake_case (state_machine.vhdl)
- **Classes**: PascalCase (NMS)
- **Functions/Variables**: snake_case (calculate_iou)
- **Constants**: UPPER_SNAKE_CASE (API_BASE_URL)
- Append `tb_` before the test bench files for VHDL

### Git Workflow
- Branch names: follow github branching convention if the issue exists otherwise stick to conventional commit standards  i.e. `feature/description` or `fix/description`
- Commit messages: Follow conventional commits
- PR required before merge
- All CI/CD checks must pass (only if exist)
- Minimum 1 approval required

### Testing Requirements
- No code coverage is needed
- Use self checking testbenches
- Ensure all the test benches pass with `GHDL`

---
**Sources**:
- https://code.claude.com/docs/en/memory
**Compatible Models**: Claude Fable 5, Claude Opus 5, Claude Sonnet 5, Claude Sonnet 4.6, Claude Opus 4.8, Claude Haiku 4.5