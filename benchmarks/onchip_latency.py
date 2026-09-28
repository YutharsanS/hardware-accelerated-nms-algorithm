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


def plot(window: Window, png: Path, expected: int = p.latency_cycles()) -> None:
    """Draw one window as a digital waveform, marking ``start`` to ``done``.

    Args:
        window: The window to draw.
        png: Output path.
        expected: T, for the annotation.
    """
    import matplotlib as mpl

    mpl.use("Agg")
    import matplotlib.pyplot as plt

    ink, muted, surface, accent = "#0b0b0b", "#52514e", "#fcfcfb", "#2a78d6"
    names = list(SIGNALS)
    n = len(window.samples["start"])
    fig, ax = plt.subplots(figsize=(9, 3.4), dpi=150, facecolor=surface)
    ax.set_facecolor(surface)
    for row, name in enumerate(reversed(names)):
        y = [row * 1.5 + 0.9 * v for v in window.samples[name]]
        ax.step(
            range(n),
            y,
            where="post",
            color=accent if name in ("start", "done") else ink,
            lw=1.6,
        )
        ax.text(
            -2, row * 1.5 + 0.35, name, ha="right", va="center", color=ink, fontsize=9
        )
    s = window.first("start")
    d = window.first("done", after=s or 0)
    if s is not None and d is not None:
        top = len(names) * 1.5
        for x in (s, d):
            ax.axvline(x, color=muted, lw=0.8, ls=(0, (3, 3)))
        ax.annotate(
            "", (d, top), (s, top), arrowprops={"arrowstyle": "<->", "color": ink}
        )
        ax.text(
            (s + d) / 2,
            top + 0.15,
            f"{d - s} cycles = {(d - s) * 10} ns (expected {expected})",
            ha="center",
            color=ink,
            fontsize=9,
        )
    ax.set_xlim(-1, n)
    ax.set_ylim(-0.4, len(names) * 1.5 + 0.8)
    ax.set_yticks([])
    ax.set_xlabel("ILA sample (one per 100 MHz clock edge)", color=ink)
    ax.set_title(
        "nms_top on the Basys 3: start to done, captured by the ILA",
        color=ink,
        loc="left",
        fontsize=10,
    )
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.tick_params(colors=muted)
    fig.tight_layout()
    png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png, facecolor=surface)
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
        plot(windows[0], args.png)
        print(f"wrote {args.png}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
