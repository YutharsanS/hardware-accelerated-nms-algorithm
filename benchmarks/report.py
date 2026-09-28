"""Merge the benchmark results into the tables for results.md: ``make bench-report``.

Reads every ``<target>-<host>-<date>-<load>.csv`` in ``benchmarks/results/`` (the feasibility
study's files live in ``results/feasibility/`` and are not merged) and prints Markdown:

* one table per machine and load, with a row per implementation and a column per input
  group, each cell ``median / p99`` in microseconds;
* the end-to-end UART figures in a table of their own, beside the core's latency, never
  in the same column as a compute time (plan.md Phase E, E.4).

A run whose metadata says ``valid: false`` (a Pi that throttled) is listed but not
tabulated. ``--hist DIR`` also draws one histogram per machine and load from the raw
samples, for the under-load runs; that needs matplotlib (the ``bench`` extra).

Usage::

    python -m benchmarks.report [--dir benchmarks/results] [--hist docs/images]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
from collections import defaultdict
from pathlib import Path

from benchmarks.targets import fpga as fpga_target

RESULTS_DIR = Path(__file__).resolve().parent / "results"
CASE_ORDER = ("notebook32", "all_survive", "all_equal", "rand_seed0", "hostile")


def load_runs(directory: Path) -> list[dict]:
    """Read every run's rows and metadata.

    Args:
        directory: The results directory.

    Returns:
        One dictionary per run: ``stem``, ``meta`` and ``rows``.
    """
    runs = []
    for path in sorted(directory.glob("*.csv")):
        meta_path = path.with_suffix(".json")
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        with path.open() as f:
            rows = list(csv.DictReader(f))
        runs.append({"stem": path.stem, "meta": meta, "rows": rows})
    return runs


def _num(v: float) -> str:
    return f"{v:,.0f}" if v >= 100 else f"{v:.3g}"


def _cell(row: dict) -> str:
    if not row.get("median_us"):
        return row.get("agreement", "—")
    cell = f"{_num(float(row['median_us']))} / {_num(float(row['p99_us']))}"
    if row.get("agreement") not in ("ok", ""):
        cell += f" ({row['agreement']})"
    return cell


def _machine(meta: dict) -> str:
    label = meta.get("label") or meta.get("cpu", "?")
    extra = [meta.get("governor", ""), meta.get("vc_clock", "")]
    return f"{label}" + (f" ({', '.join(e for e in extra if e)})" if any(extra) else "")


def cpu_table(run: dict) -> str:
    """Render one CPU run as a Markdown table.

    Args:
        run: From :func:`load_runs`.

    Returns:
        The table, headed by the machine and load.
    """
    meta, rows = run["meta"], run["rows"]
    cases = [c for c in CASE_ORDER if any(r["case"] == c for r in rows)]
    cases += sorted({r["case"] for r in rows} - set(cases))
    by_impl: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        by_impl[r["impl"]][r["case"]] = r
    lines = [
        (
            f"**{_machine(meta)}, load `{meta.get('load', '?')}`**, "
            f"{meta.get('date', '?')}, commit `{meta.get('commit', '?')}` "
            "— median / p99 µs per NMS call"
        ),
        "",
        "| implementation | " + " | ".join(cases) + " |",
        "|---|" + "---|" * len(cases),
    ]
    for impl, cells in by_impl.items():
        lines.append(
            f"| {impl} | "
            + " | ".join(_cell(cells[c]) if c in cells else "—" for c in cases)
            + " |"
        )
    return "\n".join(lines)


def system_table(runs: list[dict]) -> str:
    """Render the UART runs beside the core's own latency.

    Args:
        runs: The FPGA-target runs.

    Returns:
        The table, or an empty string if there are none.
    """
    if not runs:
        return ""
    core = fpga_target.core_row()
    lines = [
        (
            "**End to end over the UART, from the host** — median / p99 **ms**. The core's "
            f"own latency is T = {core['cycles']} cycles = {core['t_us']:.2f} µs, and "
            f"{core['full_cycles']} cycles = {core['full_us']:.2f} µs from the first record "
            f"in ({core['note']})."
        ),
        "",
        "| host | timer | " + " | ".join(CASE_ORDER) + " |",
        "|---|---|" + "---|" * len(CASE_ORDER),
    ]
    for run in runs:
        cells = {r["case"]: r for r in run["rows"]}
        row = []
        for c in CASE_ORDER:
            r = cells.get(c)
            if r and r.get("median_us"):
                row.append(
                    f"{float(r['median_us']) / 1e3:.3f} / {float(r['p99_us']) / 1e3:.3f}"
                )
            else:
                row.append(r.get("agreement", "—") if r else "—")
        timer = run["meta"].get("latency_timer_ms")
        lines.append(
            f"| {_machine(run['meta'])} | {timer if timer is not None else '?'} ms | "
            + " | ".join(row)
            + " |"
        )
    return "\n".join(lines)


def histograms(run: dict, directory: Path, out: Path) -> Path | None:
    """Draw one run's sample distribution per implementation, hostile group, log x.

    Args:
        run: From :func:`load_runs`.
        directory: The results directory holding the samples file.
        out: Where the PNG goes.

    Returns:
        The PNG path, or None when the run has no samples file.
    """
    samples_path = directory / f"{run['stem']}-samples.csv.gz"
    if not samples_path.exists():
        return None
    import matplotlib as mpl

    mpl.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    data: dict[str, list[float]] = defaultdict(list)
    with gzip.open(samples_path, "rt") as f:
        for r in csv.DictReader(f):
            if r["case"] == "hostile":
                data[r["impl"]].append(float(r["us"]))
    if not data:
        return None
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150, facecolor="#fcfcfb")
    ax.set_facecolor("#fcfcfb")
    colors = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
    lo = min(min(v) for v in data.values())
    hi = max(max(v) for v in data.values())
    bins = np.geomspace(max(lo, 1e-2), hi, 80)
    for (impl, values), color in zip(data.items(), colors, strict=False):
        ax.hist(values, bins=bins, histtype="step", lw=2, color=color, label=impl)
    ax.set_xscale("log")
    ax.set_xlabel("time per NMS call, µs (hostile stream)", color="#0b0b0b")
    ax.set_ylabel("calls", color="#0b0b0b")
    ax.set_title(
        f"{_machine(run['meta'])}, load {run['meta'].get('load')}",
        color="#0b0b0b",
        loc="left",
    )
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(fontsize=8, frameon=False)
    fig.tight_layout()
    out.mkdir(parents=True, exist_ok=True)
    png = out / f"bench-{run['stem']}.png"
    fig.savefig(png, facecolor="#fcfcfb")
    plt.close(fig)
    return png


def render(runs: list[dict]) -> str:
    """Render every run as Markdown.

    Args:
        runs: From :func:`load_runs`.

    Returns:
        The report.
    """
    valid = [r for r in runs if r["meta"].get("valid", True)]
    invalid = [r["stem"] for r in runs if not r["meta"].get("valid", True)]
    parts = [cpu_table(r) for r in valid if r["meta"].get("target") == "cpu"]
    parts.append(system_table([r for r in valid if r["meta"].get("target") == "fpga"]))
    if invalid:
        parts.append(
            "Not tabulated, throttled: " + ", ".join(f"`{s}`" for s in invalid)
        )
    return "\n\n".join(p for p in parts if p) + "\n"


def main() -> None:
    """Print the merged tables, and optionally draw the histograms."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", type=Path, default=RESULTS_DIR)
    ap.add_argument("--hist", type=Path, help="write per-run histograms here")
    args = ap.parse_args()
    runs = load_runs(args.dir)
    if not runs:
        print(f"no results in {args.dir}; run make bench first")
        return
    print(render(runs))
    if args.hist:
        for run in runs:
            png = histograms(run, args.dir, args.hist)
            if png:
                print(f"wrote {png}")


if __name__ == "__main__":
    main()
