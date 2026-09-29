# Documentation

Start with the [project README](../README.md) for what the block is and what it achieves. The
documents below are grouped by what you want from them.

## Using it

| document | read it to |
|---|---|
| [user_guide.md](user_guide.md) | install the toolchain, simulate, synthesise, program the Basys 3, talk to it, capture T on silicon, and run the benchmarks |

## Design

| document | read it to |
|---|---|
| [design/nms_primer.md](design/nms_primer.md) | understand, in plain terms, exactly which NMS the block computes and how the hardware restructures it |
| [design/architecture.md](design/architecture.md) | **the normative spec**: record format, wire protocol, datapath widths, suppression predicate, sort key, latency. `models/nms/params.py` and `src/components/nms_pkg.vhd` mirror it |
| [design/fsm_design.md](design/fsm_design.md) | follow the control FSM cycle by cycle, and see how the latency equality is derived |

## Results

| document | read it to |
|---|---|
| [results/benchmarks.md](results/benchmarks.md) | **the evaluation**: the block against software NMS on a laptop and a Raspberry Pi 4, idle and under load, with method, figures and limitations |
| [results/hardware.md](results/hardware.md) | every measured area and timing figure, the board run, and T captured on silicon |

## Project record

| document | read it to |
|---|---|
| [project/plan.md](project/plan.md) | the design rationale, the build sequence and every decision with its evidence |
| [project/build_log.md](project/build_log.md) | one entry per step, in order: what was built and measured, and what failed on the way |
| [project/future_work.md](project/future_work.md) | what was not done, why it matters, and how to start |

Images used by these documents are in [images/](images/). The Phase 0 study that preceded the
NMS work — 3D Gaussian Splatting's depth sort — is in
[explorations/gaussian_splatting/](../explorations/gaussian_splatting/README.md).
