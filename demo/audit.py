"""Re-check a recorded demo run against the golden model, offline.

``python -m demo --record run.jsonl`` writes every batch that crossed the wire and every
reply. This recomputes ``model.nms_sequential`` on each recorded batch, independently of
what the demo computed live, and compares::

    uv run python -m demo.audit run.jsonl

Exit status: 0 if every reply that arrived equals the golden model, 1 on any mismatch,
2 if the file cannot be read.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from demo.records import BOARD_MODEL, FrameRecord, read_jsonl
from models.nms import model
from models.nms import params as p


@dataclass
class Totals:
    """What an audit found.

    Attributes:
        frames: Frames in the file.
        batches: Batches sent.
        equal: Replies OK and equal to the golden model.
        mismatched: Replies OK but different from the golden model.
        failed: Replies with a non-OK status.
        no_reply: Batches sent while the board was offline.
        model_frames: Frames run with ``--no-board``, where the model answered itself.
        mismatches: ``frame/class: board vs model`` for each mismatch.
    """

    frames: int = 0
    batches: int = 0
    equal: int = 0
    mismatched: int = 0
    failed: int = 0
    no_reply: int = 0
    model_frames: int = 0
    mismatches: list[str] | None = None


def audit(records: list[FrameRecord]) -> Totals:
    """Recompute the golden model for every recorded batch and compare.

    Args:
        records: A run's frame records.

    Returns:
        The totals.
    """
    totals = Totals(mismatches=[])
    for rec in records:
        totals.frames += 1
        if rec.settings.board == BOARD_MODEL:
            totals.model_frames += 1
        for b in rec.batches:
            totals.batches += 1
            if b.status is None:
                totals.no_reply += 1
                continue
            if b.status != p.STATUS_OK:
                totals.failed += 1
                continue
            golden = model.nms_sequential(b.model_boxes(), b.present_mask)
            if b.keep_mask == golden:
                totals.equal += 1
            else:
                totals.mismatched += 1
                totals.mismatches.append(
                    f"frame {rec.index} class {b.cls}:"
                    f" reply {b.keep_mask:#010x} vs model {golden:#010x}",
                )
    return totals


def main(argv: list[str] | None = None) -> int:
    """Audit one recorded run and print the totals.

    Args:
        argv: Command-line arguments, excluding the program name.

    Returns:
        The process exit status.
    """
    parser = argparse.ArgumentParser(prog="python -m demo.audit", description=__doc__)
    parser.add_argument("record", type=Path, help="a file written by --record")
    args = parser.parse_args(argv)
    try:
        records = list(read_jsonl(args.record))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"cannot read {args.record}: {exc}")
        return 2
    t = audit(records)
    print(
        f"{t.frames} frames, {t.batches} batches: {t.equal} equal to the golden model,"
        f" {t.mismatched} different, {t.failed} error replies, {t.no_reply} sent while"
        " the board was offline",
    )
    if t.model_frames:
        print(
            f"note: {t.model_frames} frames were run with --no-board; their replies came"
            " from the golden model, not the board",
        )
    for line in t.mismatches:
        print(f"  {line}")
    return 1 if t.mismatched else 0


if __name__ == "__main__":
    sys.exit(main())
