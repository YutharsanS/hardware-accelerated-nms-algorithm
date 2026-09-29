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
            f"— median / p99 µs per NMS call (`{run['stem']}`)"
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


LOAD_COLORS = {
    "none": "#2a78d6",
    "pipeline": "#eb6834",
    "concurrent": "#1baf7a",
    "stress": "#eda100",
}
"""Fixed categorical order, so a load keeps its colour whichever loads a figure shows."""
LOAD_LABELS = {
    "none": "idle",
    "pipeline": "pipeline (YOLO before each call)",
    "concurrent": "concurrent (YOLO in another process)",
    "stress": "stress-ng memory load",
}
COMPARE_IMPLS = ("c_scalar", "opencv", "torchvision")


def load_comparison(
    runs: list[dict], directory: Path, host: str, png: Path
) -> Path | None:
    """Draw how each implementation's time shifts with load on one machine, for E2.

    One panel per implementation, one cumulative distribution per load over the hostile
    stream, and the block's fixed full latency as a vertical line: a curve that sits right
    of the line, or leans further right under load, is time and variance the block removes.

    Args:
        runs: From :func:`load_runs`.
        directory: The results directory holding the samples files.
        host: A substring of the machine label, e.g. ``Raspberry Pi 4``.
        png: Output path.

    Returns:
        The PNG path, or None when the machine has no valid CPU runs.
    """
    import matplotlib as mpl

    mpl.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    chosen: dict[str, dict] = {}
    for run in sorted(runs, key=lambda r: "--tag" in r["meta"].get("argv", [])):
        m = run["meta"]
        if (
            host in m.get("label", "")
            and m.get("target") == "cpu"
            and m.get("valid", True)
        ):
            chosen.setdefault(m.get("load", "none"), run)
    if not chosen:
        return None
    samples: dict[tuple[str, str], list[float]] = defaultdict(list)
    for load, run in chosen.items():
        with gzip.open(directory / f"{run['stem']}-samples.csv.gz", "rt") as f:
            for r in csv.DictReader(f):
                if r["case"] == "hostile" and r["impl"] in COMPARE_IMPLS:
                    samples[r["impl"], load].append(float(r["us"]))
    core = fpga_target.core_row()
    ink, muted, surface, block = "#0b0b0b", "#52514e", "#fcfcfb", "#4a3aa7"
    fig, axes = plt.subplots(
        1,
        len(COMPARE_IMPLS),
        figsize=(11, 3.8),
        dpi=150,
        facecolor=surface,
        sharey=True,
    )
    for ax, impl in zip(axes, COMPARE_IMPLS, strict=True):
        ax.set_facecolor(surface)
        for load, color in LOAD_COLORS.items():
            values = np.sort(samples.get((impl, load), []))
            if len(values) == 0:
                continue
            y = np.arange(1, len(values) + 1) / len(values)
            ax.step(
                values,
                y,
                where="post",
                color=color,
                lw=2,
                label=f"{LOAD_LABELS[load]} (n={len(values)})",
            )
        ax.axvline(core["full_us"], color=block, lw=2, ls=(0, (4, 2)))
        ax.text(
            core["full_us"] * 1.08,
            0.04,
            f"block {core['full_us']:.2f} µs",
            color=ink,
            fontsize=8,
            rotation=90,
            va="bottom",
        )
        ax.set_xscale("log")
        ax.set_title(impl, color=ink, loc="left", fontsize=10)
        ax.set_xlabel("µs per NMS call (log)", color=ink, fontsize=9)
        ax.grid(True, which="major", color="#e4e3df", lw=0.8)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.tick_params(colors=muted, labelsize=8)
    axes[0].set_ylabel("fraction of calls at or below", color=ink, fontsize=9)
    label = next(iter(chosen.values()))["meta"].get("label", host)
    fig.suptitle(
        f"{label}, hostile stream: software NMS by load, against the block",
        color=ink,
        x=0.01,
        ha="left",
        fontsize=11,
    )
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(
        handles,
        [lb.split(" (n=")[0] for lb in labels],
        loc="lower center",
        ncol=len(labels),
        frameon=False,
        fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.08, 1, 0.95))
    png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png, facecolor=surface)
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
    ap.add_argument(
        "--compare-loads",
        nargs=2,
        metavar=("HOST", "PNG"),
        help="draw one machine's runs by load, e.g. 'Raspberry Pi 4' docs/images/x.png",
    )
    args = ap.parse_args()
    runs = load_runs(args.dir)
    if not runs:
        print(f"no results in {args.dir}; run make bench first")
        return
    print(render(runs))
    if args.compare_loads:
        png = load_comparison(
            runs, args.dir, args.compare_loads[0], Path(args.compare_loads[1])
        )
        print(
            f"wrote {png}" if png else f"no valid runs match {args.compare_loads[0]!r}"
        )
    if args.hist:
        # One figure per machine, target and load. A repeat run (``--tag run2``) is there
        # for the within-10% check on its numbers; drawing it again duplicates the figure.
        drawn: set[tuple[str, str, str]] = set()
        untagged_first = sorted(
            runs, key=lambda r: "--tag" in r["meta"].get("argv", [])
        )
        for run in untagged_first:
            m = run["meta"]
            key = (m.get("host", ""), m.get("target", ""), m.get("load", ""))
            if key in drawn or not m.get("valid", True):
                continue
            png = histograms(run, args.dir, args.hist)
            if png:
                drawn.add(key)
                print(f"wrote {png}")


if __name__ == "__main__":
    main()
