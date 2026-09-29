"""One visual language for every benchmark figure.

Rules the figures follow, so a reader never has to guess what a mark means:

* **No meaning carried by line style alone.** Hosts get separate panels, not dashed lines.
* **Every mark is named on the figure itself**: lines are labelled at their ends, reference
  lines carry their own text, and a legend is kept only as a second route to the same names.
* **The block is always drawn the same way**: one violet line, labelled
  :data:`BLOCK_LABEL`, because its latency is the same for every batch.
* **A subtitle states the conditions**: machine, clock, pinning, input.

Colours are a fixed categorical order (never cycled), so an implementation keeps its colour
in every figure; text is always drawn in ink, never in a series colour.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from matplotlib.axes import Axes
    from matplotlib.figure import Figure

INK = "#0b0b0b"
MUTED = "#52514e"
GRID = "#e4e3df"
SURFACE = "#fcfcfb"
BLOCK = "#4a3aa7"

IMPL_COLORS = {
    "c_scalar": "#eda100",
    "opencv": "#eb6834",
    "torchvision": "#2a78d6",
    "numpy_allpairs": "#1baf7a",
}
IMPL_NAMES = {
    "c_scalar": "C (compiler-optimised, -O3)",
    "opencv": "OpenCV NMSBoxes",
    "torchvision": "torchvision ops.nms",
    "numpy_allpairs": "numpy (golden model)",
}
LOAD_COLORS = {
    "none": "#2a78d6",
    "pipeline": "#eb6834",
    "concurrent": "#1baf7a",
    "stress": "#e87ba4",
}
LOAD_NAMES = {
    "none": "idle",
    "pipeline": "pipeline: a YOLO inference before each call",
    "concurrent": "concurrent: YOLO in another process",
    "stress": "stress: stress-ng memory load",
}

BLOCK_US = 1.13
BLOCK_LABEL = "FPGA block: 1.13 µs, every batch"


def style_axes(ax: Axes, *, left_spine: bool = True) -> None:
    """Apply the recessive grid, spines and tick colours.

    Args:
        ax: A matplotlib axes.
        left_spine: Whether to keep the left spine.
    """
    ax.set_facecolor(SURFACE)
    ax.grid(True, which="major", color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right") + (() if left_spine else ("left",)):
        ax.spines[side].set_visible(False)
    for side in ("bottom", "left"):
        ax.spines[side].set_color(MUTED)
    ax.tick_params(colors=MUTED, labelsize=8)


def titles(fig: Figure, title: str, subtitle: str) -> None:
    """Put a left-aligned title and a one-line conditions subtitle on a figure.

    Args:
        fig: A matplotlib figure.
        title: What the figure shows.
        subtitle: The conditions it was measured under.
    """
    fig.text(0.012, 0.975, title, color=INK, fontsize=12, weight="bold", va="top")
    fig.text(0.012, 0.935, subtitle, color=MUTED, fontsize=8.5, va="top")


def block_vline(ax: Axes, *, y_text: float, text: str = BLOCK_LABEL) -> None:
    """Draw the block as a labelled vertical line at 1.13 µs (x axis in µs).

    Args:
        ax: A matplotlib axes.
        y_text: Where the label sits, in axes-fraction y.
        text: The label.
    """
    ax.axvline(BLOCK_US, color=BLOCK, lw=2.5, zorder=3)
    ax.annotate(
        text,
        xy=(BLOCK_US, y_text),
        xycoords=("data", "axes fraction"),
        xytext=(6, 0),
        textcoords="offset points",
        color=INK,
        fontsize=8,
        va="center",
        bbox={"boxstyle": "round,pad=0.25", "fc": SURFACE, "ec": BLOCK, "lw": 1},
    )


def block_hline(ax: Axes, *, x_text: float, text: str = BLOCK_LABEL) -> None:
    """Draw the block as a labelled horizontal line at 1.13 µs (y axis in µs).

    Args:
        ax: A matplotlib axes.
        x_text: Where the label sits, in data x.
        text: The label.
    """
    ax.axhline(BLOCK_US, color=BLOCK, lw=2.5, zorder=3)
    ax.annotate(
        text,
        xy=(x_text, BLOCK_US),
        xytext=(0, 7),
        textcoords="offset points",
        color=INK,
        fontsize=8,
        ha="center",
        bbox={"boxstyle": "round,pad=0.25", "fc": SURFACE, "ec": BLOCK, "lw": 1},
    )
