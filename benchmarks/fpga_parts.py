"""The same RTL on other FPGA parts, post-route: ``make parts`` (plan.md Phase E, E7).

Answers "is it just the Basys 3?". ``nms_core`` is implemented out of context, unchanged, on
each part and at each lane count P, through ``scripts/synth.tcl`` (``make synth``), and
the maximum clock, area and resulting latency are tabulated.

**Two passes per configuration.** Vivado only works as hard as the constraint asks, so a
single run at a loose 10 ns understates what a faster part can do. Pass 1 runs at 10 ns and
measures the critical path; pass 2 re-runs at a period just under it. Fmax is
``1000 / (period - WNS)`` from whichever pass gives the higher figure.

Every figure is post-route static timing on a part the team does not own: **not run on
silicon**. Only the Basys 3 row (xc7a35t, -1) has been, at 100 MHz.

Usage::

    python -m benchmarks.fpga_parts [--parts ...] [--lanes 16 32] [--jobs 3]
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from benchmarks.targets import fpga as fpga_target
from models.nms import params as p

REPO = Path(__file__).resolve().parents[1]
BUILD = REPO / "build" / "parts"
RESULTS = Path(__file__).resolve().parent / "results" / "fpga-parts.csv"

PARTS = (
    ("xc7a35tcpg236-1", "Artix-7 35T, -1: the Basys 3"),
    ("xc7a35tcpg236-2", "Artix-7 35T, -2: same die, faster grade"),
    ("xc7a35tcpg236-3", "Artix-7 35T, -3: same die, fastest grade"),
    ("xc7z020clg400-1", "Zynq-7020, -1: PYNQ-Z2 class, A9 + DDR beside the fabric"),
    ("xc7k325tffg900-2", "Kintex-7 325T, -2: more resources, faster fabric"),
)
LANES = (16, 32)
FIRST_PERIOD_NS = 10.0
TIGHTEN = 0.97
"""Pass 2 asks for 3% under pass 1's critical path, so the tool has to try harder."""


@dataclass(frozen=True)
class Run:
    """One Vivado run.

    Attributes:
        part: Device.
        lanes: P.
        period: Clock constraint, ns.
        summary: ``summary.json``, or None if the run failed.
        error: The first ERROR line of a failed run.
    """

    part: str
    lanes: int
    period: float
    summary: dict | None
    error: str = ""

    @property
    def fmax_mhz(self) -> float | None:
        """Return ``1000 / (period - WNS)``, the routed critical path as a clock."""
        if not self.summary or self.summary.get("wns_ns") is None:
            return None
        delay = self.period - float(self.summary["wns_ns"])
        return 1000.0 / delay if delay > 0 else None


def run_vivado(part: str, lanes: int, period: float, tag: str) -> Run:
    """Implement ``nms_core`` once, through ``scripts/synth.tcl``.

    Args:
        part: Device.
        lanes: P.
        period: Clock constraint, ns.
        tag: Pass name, for the build directory.

    Returns:
        The run, with its summary or its first error.
    """
    out = BUILD / f"{part}-P{lanes}-{tag}"
    out.mkdir(parents=True, exist_ok=True)
    summary_path = out / "summary.json"
    summary_path.unlink(missing_ok=True)
    cmd = [
        "vivado", "-mode", "batch", "-nolog", "-nojournal",
        "-source", "scripts/synth.tcl",
        "-tclargs", "nms_core", f"{period:.3f}", f"P={lanes}", f"PART={part}", f"OUT={out}",
    ]  # fmt: skip
    log = out / "vivado.log"
    with log.open("w") as f:
        subprocess.run(cmd, cwd=REPO, stdout=f, stderr=subprocess.STDOUT, check=False)
    if summary_path.exists():
        return Run(part, lanes, period, json.loads(summary_path.read_text()))
    errors = [line for line in log.read_text().splitlines() if line.startswith("ERROR")]
    return Run(part, lanes, period, None, errors[0] if errors else "no summary written")


def sweep_one(part: str, lanes: int) -> tuple[Run, Run | None]:
    """Run both passes for one configuration.

    Args:
        part: Device.
        lanes: P.

    Returns:
        ``(pass 1, pass 2)``; pass 2 is None when pass 1 failed.
    """
    first = run_vivado(part, lanes, FIRST_PERIOD_NS, "pass1")
    if first.fmax_mhz is None:
        return first, None
    period = round(1000.0 / first.fmax_mhz * TIGHTEN, 3)
    return first, run_vivado(part, lanes, period, "pass2")


def row(part: str, label: str, lanes: int, runs: tuple[Run, Run | None]) -> dict:
    """Turn one configuration's runs into a results row.

    Args:
        part: Device.
        label: Human-readable description.
        lanes: P.
        runs: From :func:`sweep_one`.

    Returns:
        The row: area from the pass that set Fmax, and T in cycles and ns.
    """
    first, second = runs
    if first.summary is None:
        return {
            "part": part,
            "label": label,
            "P": lanes,
            "status": f"failed: {first.error}",
        }
    best = max((r for r in runs if r and r.fmax_mhz), key=lambda r: r.fmax_mhz)
    s = best.summary
    t = p.latency_cycles(lanes)
    full = t + fpga_target.SETTLE_CYCLES + p.N
    fmax = best.fmax_mhz
    return {
        "part": part,
        "label": label,
        "speed": s["speed"],
        "P": lanes,
        "lut": s["lut"],
        "lut_pct": round(100 * s["lut"] / s["lut_avail"], 1),
        "ff": s["ff"],
        "dsp": s["dsp"],
        "dsp_pct": round(100 * s["dsp"] / s["dsp_avail"], 1),
        "pass1_period_ns": first.period,
        "pass1_wns_ns": first.summary["wns_ns"],
        "pass2_period_ns": second.period if second else "",
        "pass2_wns_ns": second.summary["wns_ns"] if second and second.summary else "",
        "fmax_mhz": round(fmax, 1),
        "t_cycles": t,
        "t_ns": round(t * 1000 / fmax, 1),
        "full_cycles": full,
        "full_ns": round(full * 1000 / fmax, 1),
        "status": "post-route, not run on silicon",
    }


def markdown(rows: list[dict]) -> str:
    """Render the rows as the table for results.md.

    Args:
        rows: From :func:`row`.

    Returns:
        A Markdown table.
    """
    lines = [
        "| part | P | LUT | FF | DSP | Fmax | T | first record to `done` |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if "fmax_mhz" not in r:
            lines.append(f"| `{r['part']}` | {r['P']} | {r['status']} | | | | | |")
            continue
        lines.append(
            f"| `{r['part']}` | {r['P']} | {r['lut']:,} ({r['lut_pct']}%) | {r['ff']:,} | "
            f"{r['dsp']} ({r['dsp_pct']}%) | {r['fmax_mhz']} MHz | "
            f"{r['t_cycles']} cyc = {r['t_ns']} ns | {r['full_cycles']} cyc = {r['full_ns']} ns |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Sweep the parts and lane counts, write the CSV and print the table.

    Args:
        argv: Arguments, excluding the program name.

    Returns:
        0 when every configuration produced a figure or failed only by not fitting.
    """
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--parts", nargs="+", help="only these parts")
    ap.add_argument("--lanes", nargs="+", type=int, default=list(LANES))
    ap.add_argument("--jobs", type=int, default=3, help="Vivado runs in parallel")
    ap.add_argument("--out", type=Path, default=RESULTS)
    args = ap.parse_args(argv)
    if shutil.which("vivado") is None:
        print("vivado not on PATH -- run: source ~/Vivado/2026.1/Vivado/settings64.sh")
        return 2
    parts = [(pt, lb) for pt, lb in PARTS if not args.parts or pt in args.parts]
    configs = [(pt, lb, lanes) for pt, lb in parts for lanes in args.lanes]
    print(f"{len(configs)} configurations, two passes each, {args.jobs} at a time")
    with ThreadPoolExecutor(args.jobs) as pool:
        futures = [pool.submit(sweep_one, pt, lanes) for pt, _, lanes in configs]
        rows = []
        for (pt, lb, lanes), fut in zip(configs, futures, strict=True):
            r = row(pt, lb, lanes, fut.result())
            rows.append(r)
            print(
                f"  {pt:18s} P={lanes:2d}  {r.get('fmax_mhz', r['status'])}", flush=True
            )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fields = list(
        dict.fromkeys(k for r in rows for k in r)
    )  # union, in first-seen order
    with args.out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, restval="")
        w.writeheader()
        w.writerows(rows)
    print()
    print(markdown(rows))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
