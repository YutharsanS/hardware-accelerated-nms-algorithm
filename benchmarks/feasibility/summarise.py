"""Summarise the feasibility study: the box counts, the timings, and the plot.

Reads the ``counts_*.json`` from ``count_boxes.py`` and every ``time_*.csv`` from
``time_vs_boxes.py`` in ``--dir``, prints the figures the feasibility decision needs, and writes
``nms_time_vs_boxes.png``.

The application guarantees N <= 32 (build_log.md, the feasibility decision), so timings are
shown for N <= ``--max-n`` only. The block always runs a full 32-slot batch, with absent
slots masked by ``present_mask``, so its latency is the same at every N <= 32: 32 load
cycles (one record per cycle), 1 settle cycle and T = 80, i.e. 113 cycles at 100 MHz.

Usage::

    python -m benchmarks.feasibility.summarise --dir <dir> [--png docs/images/nms_time_vs_boxes.png]
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import matplotlib as mpl
import numpy as np

mpl.use("Agg")
import matplotlib.pyplot as plt

from benchmarks.targets import fpga
from models.nms import params as p

CLOCK_MHZ = 100.0


def block_us() -> float:
    """Return the block's latency at 100 MHz, from the first record in, in microseconds.

    The same for every N <= 32: the batch is always 32 slots. Load, settle and T are each
    pinned as equalities in tb_nms_core.

    Returns:
        ``fpga.full_latency_cycles() / f``: 113 cycles, 1.13 us.
    """
    return fpga.full_latency_cycles() / CLOCK_MHZ


def truncation_loss(rows: list[dict], c: str) -> dict:
    """Summarise what a top-32 truncation costs, supplementary to the ≤ 32 criterion.

    Args:
        rows: Per-image counts.
        c: Confidence threshold key.

    Returns:
        Share of (image, class) batches whose NMS result is unchanged by truncation, and
        the share of all keepers that truncation drops. Empty if the counts predate it.
    """
    key = f"lost@{c}"
    if key not in rows[0]:
        return {}
    lost = np.array([n for r in rows for n in r[key]])
    kept = sum(r[f"kept@{c}/0.5"] for r in rows)
    return {
        "top32_unchanged": float((lost == 0).mean()),
        "top32_lost_keepers": int(lost.sum()),
        "top32_lost_share": float(lost.sum() / max(1, kept)),
    }


def summarise_counts(path: Path) -> dict:
    """Return the distribution figures the decision table needs for one model.

    Args:
        path: A ``counts_<model>.json``.

    Returns:
        Per threshold: per-image and per-(image, class) percentiles and the share of
        (image, class) batches that fit N = 32, plus duplicate ratios.
    """
    rows = json.loads(path.read_text())
    out: dict = {"images": len(rows)}
    for c in ("0.1", "0.25", "0.5"):
        img = np.array([r[f"img@{c}"] for r in rows])
        cls = np.array([n for r in rows for n in r[f"cls@{c}"]])
        kept = {
            iou: np.array([r[f"kept@{c}/{iou}"] for r in rows])
            for iou in ("0.45", "0.7")
        }
        out[c] = {
            "img_median": float(np.median(img)),
            "img_p90": float(np.percentile(img, 90)),
            "img_max": int(img.max()),
            "img_le32": float((img <= p.N).mean()),
            "batches": len(cls),
            "cls_median": float(np.median(cls)) if len(cls) else 0.0,
            "cls_p90": float(np.percentile(cls, 90)) if len(cls) else 0.0,
            "cls_p99": float(np.percentile(cls, 99)) if len(cls) else 0.0,
            "cls_max": int(cls.max()) if len(cls) else 0,
            "cls_le32": float((cls <= p.N).mean()) if len(cls) else 1.0,
            "classes_per_image": len(cls) / len(rows),
            **truncation_loss(rows, c),
            **{
                f"dup_ratio@{iou}": float(img.sum() / max(1, k.sum()))
                for iou, k in kept.items()
            },
        }
    return out


def load_timings(directory: Path) -> list[dict]:
    """Read every timing CSV and tag each row with its machine and run conditions.

    Args:
        directory: Where ``time_vs_boxes.py`` wrote its output.

    Returns:
        All rows, numeric fields converted, with ``host`` and ``conditions`` added.
    """
    rows = []
    for f in sorted(directory.glob("time_*.csv")):
        meta = json.loads(f.with_suffix(".json").read_text())
        label = (meta["board"] or meta["cpu"]).replace(
            "13th Gen Intel(R) Core(TM) ", ""
        )
        pinned = len(meta.get("affinity", [])) == 1
        conditions = f"{meta.get('governor') or 'unknown'} governor, " + (
            "pinned to one core" if pinned else "not pinned"
        )
        for r in csv.DictReader(f.open()):
            rows.append(
                {
                    **{k: float(v) if k != "impl" else v for k, v in r.items()},
                    "host": label,
                    "conditions": conditions,
                    "source": f.stem,
                }
            )
    return rows


def _spread(ys: list[float], min_decades: float = 0.34) -> list[float]:
    """Move label heights apart on a log axis so two-line labels don't overlap.

    Args:
        ys: The values the labels belong to.
        min_decades: The smallest gap between labels, in powers of ten.

    Returns:
        Label heights, in the input order, each within reach of its value.
    """
    order = sorted(range(len(ys)), key=lambda i: ys[i])
    logs = [float(np.log10(ys[i])) for i in order]
    for k in range(1, len(logs)):
        logs[k] = max(logs[k], logs[k - 1] + min_decades)
    out = [0.0] * len(ys)
    for k, i in enumerate(order):
        out[i] = 10 ** logs[k]
    return out


def _panel_title(host: str) -> str:
    if "Raspberry Pi 4" in host:
        return "Raspberry Pi 4 (Cortex-A72, 1.8 GHz)"
    return f"Laptop ({host})"


def plot(timings: list[dict], png: Path) -> None:
    """Draw software time against N, one panel per machine, against the block.

    Every line is labelled at its end with its N = 32 median; the shaded band runs from
    each library's median to its p99; the block is the labelled violet line.

    Args:
        timings: Rows from :func:`load_timings`, already limited to the plotted N.
        png: Output path.
    """
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    from benchmarks import figstyle as fs

    hosts = sorted({r["host"] for r in timings}, key=lambda h: "Raspberry" in h)
    ns = sorted({r["n"] for r in timings})
    fig, axes = plt.subplots(
        1,
        len(hosts),
        figsize=(4.6 * len(hosts) + 1.2, 5.2),
        dpi=150,
        facecolor=fs.SURFACE,
        sharey=True,
        squeeze=False,
    )
    for ax, host in zip(axes[0], hosts, strict=True):
        fs.style_axes(ax)
        conditions = next(r["conditions"] for r in timings if r["host"] == host)
        ends: list[tuple[float, float, str]] = []
        for impl in ("torchvision", "numpy_allpairs", "opencv"):
            rs = sorted(
                (r for r in timings if r["host"] == host and r["impl"] == impl),
                key=lambda r: r["n"],
            )
            if not rs:
                continue
            color = fs.IMPL_COLORS[impl]
            x = [r["n"] for r in rs]
            med = [r["median_us"] for r in rs]
            ax.fill_between(
                x, med, [r["p99_us"] for r in rs], color=color, alpha=0.15, lw=0
            )
            ax.plot(
                x,
                med,
                color=color,
                lw=2,
                marker="o",
                ms=6,
                markeredgecolor=fs.SURFACE,
                markeredgewidth=1.5,
            )
            ends.append(
                (
                    med[-1],
                    x[-1],
                    f"{fs.IMPL_NAMES[impl]}\n{med[-1]:.3g} µs at N = {int(x[-1])}",
                )
            )
        for y_label, (y, x_end, text) in zip(
            _spread([e[0] for e in ends]), ends, strict=True
        ):
            ax.annotate(
                text,
                xy=(x_end, y),
                xytext=(x_end * 1.18, y_label),
                color=fs.INK,
                fontsize=7.5,
                va="center",
                arrowprops={"arrowstyle": "-", "color": fs.MUTED, "lw": 0.6},
            )
        fs.block_hline(ax, x_text=ns[-1] * 2.3)
        ax.set_xscale("log", base=2)
        ax.set_yscale("log")
        ax.set_xticks(ns, [str(int(n)) for n in ns])
        ax.minorticks_off()
        ax.set_xlim(ns[0] / 1.25, ns[-1] * 4.2)
        ax.set_ylim(0.6, 1000)
        ax.set_xlabel("boxes in the batch, N", color=fs.INK, fontsize=9)
        ax.set_title(
            f"{_panel_title(host)}\n{conditions}", color=fs.INK, loc="left", fontsize=9
        )
    axes[0][0].set_ylabel("time per NMS call, µs (log scale)", color=fs.INK, fontsize=9)
    fs.titles(
        fig,
        "Software NMS gets slower as N grows; the FPGA block does not",
        "Real candidate sets: the top N YOLOv8n detections of 50 COCO images. "
        "One thread per call. Line = median, shaded band = median to p99.",
    )
    handles = [
        Line2D(
            [], [], color=fs.IMPL_COLORS[i], lw=2, marker="o", label=fs.IMPL_NAMES[i]
        )
        for i in ("torchvision", "numpy_allpairs", "opencv")
    ] + [
        Patch(color="#999999", alpha=0.3, label="shaded: median to p99"),
        Line2D([], [], color=fs.BLOCK, lw=2.5, label=fs.BLOCK_LABEL),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=5, frameon=False, fontsize=7.5)
    fig.tight_layout(rect=(0, 0.06, 1, 0.9))
    png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png, facecolor=fs.SURFACE)
    plt.close(fig)


def main() -> None:
    """Print the decision-table inputs and write the plot."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--png", type=Path)
    ap.add_argument("--max-n", type=int, default=p.N)
    args = ap.parse_args()

    for f in sorted(args.dir.glob("counts_*.json")):
        s = summarise_counts(f)
        print(f"\n== {f.stem}  ({s['images']} images)")
        for c in ("0.1", "0.25", "0.5"):
            print(
                f"  conf > {c}: "
                + ", ".join(
                    f"{k} {v:.3g}" if isinstance(v, float) else f"{k} {v}"
                    for k, v in s[c].items()
                )
            )

    timings = [r for r in load_timings(args.dir) if r["n"] <= args.max_n]
    print("\n== timings (median / p99 us)")
    for r in timings:
        print(
            f"  {r['host'][:32]:32s} {r['impl']:15s} N={r['n']:6.0f}  "
            f"{r['median_us']:10.1f} / {r['p99_us']:10.1f}"
        )
    print(f"  block, every N <= {p.N}: {block_us():.2f} us")
    if args.png and timings:
        plot(timings, args.png)
        print(f"\nwrote {args.png}")


if __name__ == "__main__":
    main()
