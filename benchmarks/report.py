"""Merge the benchmark results into Markdown tables: ``make bench-report``.

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
from models.nms import bench

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


COMPARE_IMPLS = ("c_scalar", "opencv", "torchvision")


def _hostile_samples(
    run: dict, directory: Path, impls: tuple[str, ...]
) -> dict[str, list[float]]:
    """Read one run's hostile-stream samples for some implementations.

    Args:
        run: From :func:`load_runs`.
        directory: The results directory holding the samples file.
        impls: Implementations to keep.

    Returns:
        Samples in microseconds, keyed by implementation.
    """
    out: dict[str, list[float]] = defaultdict(list)
    with gzip.open(directory / f"{run['stem']}-samples.csv.gz", "rt") as f:
        for r in csv.DictReader(f):
            if r["case"] == "hostile" and r["impl"] in impls:
                out[r["impl"]].append(float(r["us"]))
    return out


def _runs_for(runs: list[dict], host: str) -> dict[str, dict]:
    """Pick one valid CPU run per load for a machine, preferring the untagged run.

    Args:
        runs: From :func:`load_runs`.
        host: A substring of the machine label.

    Returns:
        Runs keyed by load.
    """
    chosen: dict[str, dict] = {}
    for run in sorted(runs, key=lambda r: "--tag" in r["meta"].get("argv", [])):
        m = run["meta"]
        if (
            host in m.get("label", "")
            and m.get("target") == "cpu"
            and m.get("valid", True)
        ):
            chosen.setdefault(m.get("load", "none"), run)
    return chosen


def _us(v: float) -> str:
    """Format microseconds without scientific notation: 8.89, 75.8, 1,372."""
    return f"{v:,.0f}" if v >= 100 else f"{v:.3g}"


def load_comparison(
    runs: list[dict], directory: Path, host: str, png: Path
) -> Path | None:
    """Draw how each implementation's time shifts with load on one machine.

    One panel per implementation. Each curve is a cumulative distribution over the hostile
    stream: at time x, the share of calls that had finished. The 50% and 99% guides read
    off the median and p99; each panel's key gives them in numbers. The block is the
    labelled violet line.

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
    from matplotlib.lines import Line2D

    from benchmarks import figstyle as fs

    chosen = _runs_for(runs, host)
    if not chosen:
        return None
    loads = [ld for ld in fs.LOAD_COLORS if ld in chosen]
    samples = {
        ld: _hostile_samples(chosen[ld], directory, COMPARE_IMPLS) for ld in loads
    }
    fig, axes = plt.subplots(
        1,
        len(COMPARE_IMPLS),
        figsize=(13, 6.6),
        dpi=150,
        facecolor=fs.SURFACE,
        sharey=True,
    )
    for ax, impl in zip(axes, COMPARE_IMPLS, strict=True):
        fs.style_axes(ax)
        handles, stats = [], {}
        for ld in loads:
            values = np.sort(samples[ld].get(impl, []))
            if len(values) == 0:
                continue
            y = np.arange(1, len(values) + 1) / len(values)
            summary = bench.summarise(values.tolist())  # the tables' own p99
            med, p99 = summary["median"], summary["p99"]
            stats[ld] = (med, p99)
            (line,) = ax.step(
                values,
                y,
                where="post",
                color=fs.LOAD_COLORS[ld],
                lw=2,
                label=f"{fs.LOAD_NAMES[ld].split(':')[0]:<10s} "
                f"{_us(med):>6s} / {_us(p99):<6s}",
            )
            handles.append(line)
        for level, name in ((0.5, "median"), (0.99, "p99")):
            ax.axhline(level, color=fs.MUTED, lw=0.9, ls=":", zorder=1)
            ax.annotate(
                name,
                xy=(1, level),
                xycoords=("axes fraction", "data"),
                xytext=(-3, 3),
                textcoords="offset points",
                ha="right",
                color=fs.MUTED,
                fontsize=7.5,
            )
        fs.block_vline(ax, y_text=0.08, text="FPGA block\n1.13 µs, every batch")
        ax.set_xscale("log")
        lo = min(min(v.get(impl, [fs.BLOCK_US])) for v in samples.values())
        hi = max(max(v.get(impl, [fs.BLOCK_US])) for v in samples.values())
        ax.set_xlim(min(lo, fs.BLOCK_US) / 1.5, hi * 1.6)
        ax.set_ylim(0, 1.04)
        ax.set_title(
            fs.IMPL_NAMES[impl],
            color=fs.INK,
            loc="left",
            fontsize=10,
            weight="bold",
            pad=30,
        )
        ax.set_xlabel("time per NMS call, µs (log scale)", color=fs.INK, fontsize=9)
        note = ""
        if "none" in stats and len(stats) > 1:
            worst_med = max(
                (v[0] / stats["none"][0], k) for k, v in stats.items() if k != "none"
            )
            worst_p99 = max(
                (v[1] / stats["none"][1], k) for k, v in stats.items() if k != "none"
            )
            note = (
                f"worst median under load: {worst_med[0]:.1f}× idle ({worst_med[1]})\n"
                f"worst p99 under load: {worst_p99[0]:.1f}× idle ({worst_p99[1]})"
            )
        ax.text(0, 1.02, note, transform=ax.transAxes, color=fs.MUTED, fontsize=8)
        leg = ax.legend(
            handles=handles,
            title="median / p99, µs",
            loc="upper center",
            bbox_to_anchor=(0.5, -0.17),
            fontsize=7.5,
            title_fontsize=7.5,
            frameon=True,
            prop={"family": "monospace", "size": 7.5},
        )
        leg.get_frame().set_edgecolor(fs.GRID)
        leg.get_frame().set_facecolor(fs.SURFACE)
    axes[0].set_ylabel("share of calls finished within x µs", color=fs.INK, fontsize=9)
    label = next(iter(chosen.values()))["meta"].get("label", host)
    n_pipe = len(samples.get("pipeline", {}).get("c_scalar", []))
    fs.titles(
        fig,
        f"{label}: what load does to software NMS, and does not do to the block",
        "Hostile stream (1,000 varied batches), NMS pinned to one core, one thread. "
        "A curve further right is slower; a curve that stretches right is less predictable."
        + (f" Pipeline: {n_pipe} samples per implementation." if n_pipe else ""),
    )
    fig.legend(
        handles=[
            Line2D([], [], color=fs.LOAD_COLORS[ld], lw=2, label=fs.LOAD_NAMES[ld])
            for ld in loads
        ]
        + [Line2D([], [], color=fs.BLOCK, lw=2.5, label=fs.BLOCK_LABEL)],
        loc="lower center",
        ncol=len(loads) + 1,
        frameon=False,
        fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.04, 1, 0.9))
    png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png, facecolor=fs.SURFACE)
    plt.close(fig)
    return png


def headline(runs: list[dict], png: Path) -> Path | None:
    """Draw the README's headline: every software NMS's median and tail against the block.

    One row per implementation and machine, on the hostile stream: a dot at the median, a
    ring at the idle p99, and on the Pi 4 a cross at the worst p99 under any load, each
    labelled with its value. The block is the labelled violet line.

    Args:
        runs: From :func:`load_runs`.
        png: Output path.

    Returns:
        The PNG path, or None if the laptop or Pi 4 idle run is missing.
    """
    import matplotlib as mpl

    mpl.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    from benchmarks import figstyle as fs

    machines = (
        ("i5-13500H", "laptop, Intel i5-13500H at 4.7 GHz"),
        ("Raspberry Pi 4", "Raspberry Pi 4, Cortex-A72 at 1.8 GHz"),
    )
    chosen = {key: _runs_for(runs, key) for key, _ in machines}
    if any("none" not in c for c in chosen.values()):
        return None

    def hostile(run: dict, impl: str) -> tuple[float, float]:
        row = next(
            r for r in run["rows"] if r["impl"] == impl and r["case"] == "hostile"
        )
        return float(row["median_us"]), float(row["p99_us"])

    rows = []
    for impl in COMPARE_IMPLS:
        for key, name in machines:
            med, p99 = hostile(chosen[key]["none"], impl)
            loaded = [
                hostile(r, impl)[1] for ld, r in chosen[key].items() if ld != "none"
            ]
            rows.append((impl, name, med, p99, max(loaded) if loaded else None))
    fig, ax = plt.subplots(figsize=(11, 5.6), dpi=150, facecolor=fs.SURFACE)
    fs.style_axes(ax)
    ax.grid(True, axis="x", which="major", color=fs.GRID, lw=0.8)
    ax.grid(False, axis="y")
    for k, (impl, _name, med, p99, worst) in enumerate(rows):
        y = len(rows) - 1 - k
        color = fs.IMPL_COLORS[impl]
        right = worst if worst else p99
        ax.plot([med, right], [y, y], color=color, lw=2, alpha=0.5, zorder=2)
        ax.plot(med, y, "o", color=color, ms=9, zorder=4)
        ax.plot(p99, y, "o", mfc=fs.SURFACE, mec=color, mew=2, ms=9, zorder=4)
        ax.annotate(
            f"{_us(med)}",
            (med, y),
            xytext=(0, 9),
            textcoords="offset points",
            ha="center",
            color=fs.INK,
            fontsize=7.5,
        )
        ax.annotate(
            f"{_us(p99)}",
            (p99, y),
            xytext=(0, -14),
            textcoords="offset points",
            ha="center",
            color=fs.INK,
            fontsize=7.5,
        )
        if worst:
            ax.plot(worst, y, "X", color=color, ms=9, zorder=4)
        ratio = right / fs.BLOCK_US
        times = f"{ratio:,.0f}×" if ratio >= 100 else f"{ratio:.1f}×"
        text = (
            f"worst {_us(worst)} under load: {times} the block"
            if worst
            else f"p99 {times} the block"
        )
        ax.annotate(
            text,
            (right, y),
            xytext=(10, 0),
            textcoords="offset points",
            va="center",
            color=fs.INK,
            fontsize=7.5,
        )
    ax.set_yticks(
        range(len(rows)),
        [f"{fs.IMPL_NAMES[i]}\n{n}" for i, n, *_ in reversed(rows)],
        fontsize=8,
    )
    ax.tick_params(axis="y", colors=fs.INK, length=0)
    fs.block_vline(ax, y_text=1.02, text=fs.BLOCK_LABEL)
    ax.set_xscale("log")
    ax.set_xlim(0.6, 30000)
    ax.set_ylim(-0.7, len(rows) - 0.3)
    ax.set_xlabel("time per NMS call, µs (log scale)", color=fs.INK, fontsize=9)
    fs.titles(
        fig,
        "Worst case: the FPGA block against every software NMS tested",
        "Hostile stream (1,000 varied batches of 32 boxes). One core, one thread per call. "
        "Software's tail is observed; the block's 1.13 µs is exact for every batch.",
    )
    fig.legend(
        handles=[
            Line2D(
                [], [], ls="", marker="o", color=fs.MUTED, ms=8, label="median, idle"
            ),
            Line2D(
                [],
                [],
                ls="",
                marker="o",
                mfc=fs.SURFACE,
                mec=fs.MUTED,
                mew=2,
                ms=8,
                label="p99, idle",
            ),
            Line2D(
                [],
                [],
                ls="",
                marker="X",
                color=fs.MUTED,
                ms=8,
                label="worst p99 under load (Pi 4: pipeline, concurrent or stress)",
            ),
            Line2D([], [], color=fs.BLOCK, lw=2.5, label=fs.BLOCK_LABEL),
        ],
        loc="lower center",
        ncol=4,
        frameon=False,
        fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.05, 1, 0.9))
    png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png, facecolor=fs.SURFACE)
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
        "--headline", type=Path, help="draw the README headline figure here"
    )
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
    if args.headline:
        png = headline(runs, args.headline)
        print(f"wrote {png}" if png else "headline needs the laptop and Pi 4 idle runs")
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
