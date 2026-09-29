"""Check T on silicon from an ILA capture: ``make ila`` runs this after ``scripts/ila.tcl``.

The ILA samples the core's handshake -- ``we``, ``settled``, ``start``, ``busy``, ``done`` --
on every 100 MHz clock edge, one capture window per batch. A sample shows each signal's
value just before its edge, as a register sees it. The core registers ``start`` at edge 0
and ``done`` rises after edge T - 1, so ``done`` is first seen T samples after ``start``:
in every window, ``index(done) - index(start)`` must equal ``params.latency_cycles()``,
exactly as ``tb_nms_core`` pins it in simulation.

Every window holds a different random batch, so equal counts across all of them are the
determinism claim, measured on the chip rather than in simulation.

Usage::

    python -m benchmarks.onchip_latency --csv build/ila/ila.csv [--png docs/images/...]
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path

from models.nms import params as p

SIGNALS = ("start", "done", "busy", "settled", "we")


@dataclass(frozen=True)
class Window:
    """One capture window, one batch.

    Attributes:
        samples: Per signal, its value at each sample.
    """

    samples: dict[str, list[int]]

    def first(self, name: str, *, after: int = 0) -> int | None:
        """Return the first sample index at or after ``after`` where ``name`` is 1.

        Args:
            name: Signal.
            after: Where to start looking.

        Returns:
            The index, or None if the signal never rises in the window.
        """
        values = self.samples[name]
        for i in range(after, len(values)):
            if values[i]:
                return i
        return None

    def latency(self) -> int | None:
        """Return the samples from ``start`` to ``done``, i.e. T in cycles.

        Returns:
            The count, or None if the window lacks either edge.
        """
        s = self.first("start")
        if s is None:
            return None
        d = self.first("done", after=s)
        return None if d is None else d - s

    def busy_cycles(self) -> int:
        """Return how many samples ``busy`` is high: the FSM's time out of IDLE.

        Returns:
            The count.
        """
        return sum(self.samples["busy"])


def _column(header: list[str], name: str) -> int:
    """Find a probe's column by signal name, or by its fixed ILA probe number.

    Vivado names a probe after the net it watches, sometimes with a hierarchy prefix or a
    bus suffix, and otherwise ``probeK``; nms_top wires probe K to ``SIGNALS[K]``.
    """
    bases = [h.strip().split("/")[-1].split("[")[0] for h in header]
    for wanted in (name, f"probe{SIGNALS.index(name)}"):
        if wanted in bases:
            return bases.index(wanted)
    msg = f"no '{name}' column in the capture; columns are {header}"
    raise ValueError(msg)


def read_capture(path: Path) -> list[Window]:
    """Parse Vivado's ``write_hw_ila_data -csv_file`` output into windows.

    The first row names the columns and a ``Radix`` row follows. A new window starts wherever
    ``Sample in Window`` returns to 0 (or a ``Window`` column changes, where present).

    Args:
        path: The CSV.

    Returns:
        Windows in capture order.
    """
    with path.open() as f:
        rows = list(csv.reader(f))
    header = rows[0]
    data = [r for r in rows[1:] if r and not r[0].startswith("Radix")]
    cols = {name: _column(header, name) for name in SIGNALS}
    in_window = header.index("Sample in Window")
    window_col = header.index("Window") if "Window" in header else None

    windows: list[Window] = []
    current: dict[str, list[int]] = {n: [] for n in SIGNALS}
    last_key: str | None = None
    for r in data:
        key = r[window_col] if window_col is not None else None
        new = (int(r[in_window]) == 0) if window_col is None else (key != last_key)
        if new and current["start"]:
            windows.append(Window(current))
            current = {n: [] for n in SIGNALS}
        last_key = key
        for name, c in cols.items():
            current[name].append(int(r[c], 16))
    if current["start"]:
        windows.append(Window(current))
    return windows


def check(
    windows: list[Window], expected: int = p.latency_cycles()
) -> tuple[bool, list[str]]:
    """Check every window's ``start`` -> ``done`` count against T.

    Args:
        windows: From :func:`read_capture`.
        expected: T.

    Returns:
        ``(all equal to T, one report line per window)``.
    """
    lines, ok = [], bool(windows)
    for k, w in enumerate(windows):
        t = w.latency()
        good = t == expected
        ok &= good
        lines.append(
            f"window {k:2d}: start -> done = {t if t is not None else 'n/a'} cycles, "
            f"busy {w.busy_cycles()} cycles  {'ok' if good else 'MISMATCH'}"
        )
    return ok, lines


SIGNAL_NOTES = {
    "start": "frame_rx asks the core to begin (one-cycle pulse)",
    "done": "the core's result is ready (one-cycle pulse)",
    "busy": "the core is computing",
    "settled": "all 32 records and areas are in the store (high throughout: they "
    "arrived over the UART milliseconds earlier)",
    "we": "a record is being written (low throughout: no writes while computing)",
}


def plot(
    window: Window, png: Path, expected: int = p.latency_cycles(), windows: int = 1
) -> None:
    """Draw one window as a labelled digital waveform, marking ``start`` to ``done``.

    Every trace is named and explained on the figure; the two edges that define T are
    labelled where they happen, and ``busy``'s span is measured in place.

    Args:
        window: The window to draw.
        png: Output path.
        expected: T, for the annotation.
        windows: How many windows the capture held, for the subtitle.
    """
    import matplotlib as mpl

    mpl.use("Agg")
    import matplotlib.pyplot as plt

    from benchmarks import figstyle as fs

    names = list(SIGNALS)
    n = len(window.samples["start"])
    high, gap = 0.8, 1.6
    fig, ax = plt.subplots(figsize=(11, 5.2), dpi=150, facecolor=fs.SURFACE)
    fs.style_axes(ax, left_spine=False)
    ax.grid(False)
    rows = {name: (len(names) - 1 - k) * gap for k, name in enumerate(names)}
    for name, base in rows.items():
        emphasis = name in ("start", "done", "busy")
        color = fs.BLOCK if emphasis else fs.MUTED
        ax.axhline(
            base, color=fs.GRID, lw=0.8, zorder=0
        )  # the low level, for reference
        ax.step(
            range(n),
            [base + high * v for v in window.samples[name]],
            where="post",
            color=color,
            lw=2.2 if emphasis else 1.6,
        )
        ax.text(
            -2,
            base + high / 2,
            name,
            ha="right",
            va="center",
            color=fs.INK,
            fontsize=10,
            weight="bold",
            family="monospace",
        )
        ax.text(
            n + 1,
            base + high / 2,
            SIGNAL_NOTES[name],
            ha="left",
            va="center",
            color=fs.MUTED,
            fontsize=8,
            wrap=True,
        )
    s = window.first("start")
    d = window.first("done", after=s or 0)
    if s is not None and d is not None:
        top = rows["start"] + high + 0.9
        for x, text in (
            (s, f"start sampled\n(sample {s}: cycle 0)"),
            (d, f"done first seen\n(sample {d}: {d - s} cycles later)"),
        ):
            ax.vlines(x, -0.5, top - 0.05, color=fs.MUTED, lw=0.9, ls=":", zorder=1)
            ax.text(
                x, top + 0.35, text, ha="center", va="bottom", color=fs.INK, fontsize=8
            )
        ax.annotate(
            "",
            xy=(d, top),
            xytext=(s, top),
            arrowprops={"arrowstyle": "<->", "color": fs.INK, "lw": 1.2},
        )
        ax.text(
            (s + d) / 2,
            top + 0.12,
            f"T = {d - s} cycles = {(d - s) * 10} ns",
            ha="center",
            va="bottom",
            color=fs.INK,
            fontsize=10,
            weight="bold",
            bbox={"boxstyle": "round,pad=0.3", "fc": fs.SURFACE, "ec": fs.BLOCK},
        )
        busy_y = rows["busy"] + high + 0.18
        ax.text(
            (s + d) / 2,
            busy_y,
            f"busy for {window.busy_cycles()} cycles",
            ha="center",
            va="bottom",
            color=fs.INK,
            fontsize=8,
        )
    ax.set_xlim(-1, n + 1)
    ax.set_ylim(-0.5, rows["start"] + high + 2.2)
    ax.set_yticks([])
    ax.set_xlabel(
        "ILA sample number: one sample per 100 MHz clock edge, so one sample = "
        "one cycle = 10 ns",
        color=fs.INK,
        fontsize=9,
    )
    fs.titles(
        fig,
        f"T measured on silicon: start to done in {expected} cycles, every batch",
        f"Basys 3 (Artix-7 XC7A35T), 100 MHz, debug build with an on-chip logic analyser "
        f"(ILA). One of {windows} captured windows, each a different random batch; "
        f"all {windows} measure {expected}.",
    )
    fig.tight_layout(rect=(0, 0, 0.74, 0.9))
    png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png, facecolor=fs.SURFACE)
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    """Check a capture, print one line per window, and optionally draw the first.

    Args:
        argv: Arguments, excluding the program name.

    Returns:
        0 when every window measures T, 1 otherwise.
    """
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--csv", type=Path, required=True)
    ap.add_argument("--png", type=Path, help="draw the first window here")
    args = ap.parse_args(argv)
    windows = read_capture(args.csv)
    ok, lines = check(windows)
    print("\n".join(lines))
    t = p.latency_cycles()
    print(
        f"{len(windows)} windows: "
        + (
            f"every one measures T = {t} cycles on silicon"
            if ok
            else f"NOT all equal to T = {t}"
        )
    )
    if args.png and windows:
        plot(windows[0], args.png, windows=len(windows))
        print(f"wrote {args.png}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
