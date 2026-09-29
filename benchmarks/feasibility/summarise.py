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

COLORS = {"torchvision": "#2a78d6", "opencv": "#eb6834", "numpy_allpairs": "#1baf7a"}
BLOCK_COLOR = "#4a3aa7"
INK, MUTED, SURFACE = "#0b0b0b", "#52514e", "#fcfcfb"


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
    """Read every timing CSV and tag each row with its host and CPU.

    Args:
        directory: Where ``time_vs_boxes.py`` wrote its output.

    Returns:
        All rows, numeric fields converted.
    """
    rows = []
    for f in sorted(directory.glob("time_*.csv")):
        meta = json.loads(f.with_suffix(".json").read_text())
        label = (meta["board"] or meta["cpu"]).replace(
            "13th Gen Intel(R) Core(TM) ", ""
        )
        for r in csv.DictReader(f.open()):
            rows.append(
                {
                    **{k: float(v) if k != "impl" else v for k, v in r.items()},
                    "host": label,
                    "source": f.stem,
                }
            )
    return rows


def plot(timings: list[dict], png: Path) -> None:
    """Draw software time against N, with the block's fixed latency.

    Args:
        timings: Rows from :func:`load_timings`, already limited to the plotted N.
        png: Output path.
    """
    fig, ax = plt.subplots(figsize=(8, 6), dpi=150, facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    hosts = sorted({r["host"] for r in timings})
    styles = ["-", "--", ":", "-."]
    for h, style in zip(hosts, styles, strict=False):
        for impl, color in COLORS.items():
            rs = sorted(
                (r for r in timings if r["host"] == h and r["impl"] == impl),
                key=lambda r: r["n"],
            )
            if not rs:
                continue
            n = [r["n"] for r in rs]
            ax.plot(
                n,
                [r["median_us"] for r in rs],
                style,
                color=color,
                lw=2,
                marker="o",
                ms=5,
                label=f"{impl} — {h}",
            )
            ax.fill_between(
                n,
                [r["median_us"] for r in rs],
                [r["p99_us"] for r in rs],
                color=color,
                alpha=0.12,
                lw=0,
            )
    ns = sorted({r["n"] for r in timings})
    ax.plot(
        ns,
        [block_us()] * len(ns),
        color=BLOCK_COLOR,
        lw=2.5,
        label="this block at 100 MHz, load included (same at every N ≤ 32)",
    )
    ax.annotate(
        f"{block_us():.2f} µs, every batch",
        (ns[-1], block_us()),
        xytext=(-4, 6),
        textcoords="offset points",
        ha="right",
        color=INK,
        fontsize=8,
    )
    ax.set_xscale("log", base=2)
    ax.set_yscale("log")
    ax.set_xticks(ns, [str(int(n)) for n in ns])
    ax.minorticks_off()
    ax.set_ylim(bottom=0.5)
    ax.set_xlabel("boxes entering NMS, N", color=INK)
    ax.set_ylabel("time per NMS call, µs (line median, band to p99)", color=INK)
    ax.set_title("Software NMS against the block, N ≤ 32", color=INK, loc="left")
    ax.grid(True, which="major", color="#e4e3df", lw=0.8)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(colors=MUTED)
    ax.legend(
        fontsize=7,
        frameon=False,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.14),
        ncol=2,
    )
    fig.tight_layout()
    png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png, facecolor=SURFACE)


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
