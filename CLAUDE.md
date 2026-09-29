# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.


## Repository structure

- `src/components/` — individual datapath and link blocks (`cas`, `bitonic32`, `iou_lane`, `box_store`, `nms_ctrl`, UART, frame layer)
- `src/pipeline/` — integration: `nms_core` (the compute core) and `nms_top` (the Basys 3 top)
- `test/` — self-checking VHDL testbenches (`tb_*.vhd`); `test/stubs/` holds simulation-only stand-ins (the ILA IP)
- `models/nms/` — the Python golden model, frozen constants, vector generator, wire format and host program
- `models/data/` — committed test vectors consumed by the testbenches
- `benchmarks/` — the software benchmark harness and the on-chip latency checker; results under `benchmarks/results/`
- `scripts/` — the Makefile rules and Vivado Tcl (synthesis, implementation, programming, ILA)
- `deployment/` — Basys 3 constraints (`basys3.xdc`)
- `docs/` — `user_guide.md` (the workflow reference), `design/` (spec, FSM, primer), `results/` (hardware, benchmarks), `project/` (build log, future work)
- `explorations/` — side studies outside the NMS product (the early 3DGS measurements)
- `tests/` — repository checks (every Markdown link must resolve)

## Claude skills

This repo defines shared Claude Code skills under `.claude/skills/` so agentic changes follow consistent commit/PR practices:

- `/commit [message]` — reviews `git status`/`git diff HEAD`, creates a single commit in Conventional Commits format (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`); an argument is used verbatim as the message.
- `/pr <issue-number>` — stages changes, ensures a conventional commit exists, pulls the task list from the linked GitHub issue, and generates a reviewer-friendly summary (what changed, why, testing performed, potential impacts).

## Conventions

- Commit messages follow Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`), enforced via the `/commit` and `/pr` skills above.
- VHDL testbenches are self-checking (see the `report`/`assert` pattern in `test/tb_cas.vhd`) rather than relying solely on manual waveform inspection.
- For the golden model development, python is choosen with `uv` tooling. Follows `Google` code conventions and for formatting and linting `ruff` is used.

# Project Configuration
If the student is struck on project specific configurations guide them using these informations.

## Project Overview
- **Name**: Hardware Acclerated NMS Algorithm with Bitonic Sort
- **Tech Stack**: VHDL, GHDL, Vivado, Basys3
- **Team Size**: 2 developers
- **Status**: complete (2026-09-29): verified on silicon and benchmarked; see `docs/project/future_work.md` for what was not done

## Additional Docs
If the student needs more details, prompts them to read these documents
@docs/design/architecture.md
@docs/user_guide.md

## Development Standards

### Code Style
- Use `ruff` for formatting the Golden model related code
- Use google code convention for Python code
- Use `uv` for Python tooling

### Naming Conventions
- **Files**: snake_case (state_machine.vhd)
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