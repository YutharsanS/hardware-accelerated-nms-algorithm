"""Live demo: YOLO finds boxes, the board runs NMS on them, both are drawn side by side.

The FPGA side is the shipped ``nms_top`` bitstream, unchanged. Per video frame::

    frame -> YOLOv8n raw head (8,400 anchors, before NMS) -> conf > 0.25, one batch per class,
    top 32 per class -> board over UART -> keep_mask -> draw kept / removed / not checked

The HUD shows where the time goes and checks every reply against the golden model. It is
there so the audience does not take the demo as a video speed-up: the board's part is
1.13 µs and fixed, and the UART and the network take milliseconds.

Usage (Ultralytics is AGPL-3.0, so it lives in its own extra; see pyproject.toml)::

    uv run --exact --extra demo python -m demo 0                  # webcam 0, board on ttyUSB1
    uv run --exact --extra demo python -m demo clip.mp4 --loop    # a recorded clip, repeated
    uv run --exact --extra demo python -m demo clip.mp4 --no-board  # golden model, no board

Keys: space pauses or resumes, n steps through one frame's NMS decisions box by box, q quits.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import torch
import torchvision

from benchmarks.feasibility.count_boxes import IMGSZ, raw_predictions
from benchmarks.targets.fpga import full_latency_cycles
from demo import batching, draw
from models.nms import host, model, wire
from models.nms import params as p

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
WINDOW = "NMS on the Basys 3"
IOU = p.T_INT / (1 << p.K_SHIFT)


class GoldenBoard:
    """Stands in for the board with the golden model, for running without hardware."""

    def transact_many(
        self, batches: list[tuple[list[model.Box], int]]
    ) -> list[wire.Reply]:
        """Answer each batch as the board would."""
        return [
            wire.Reply(p.STATUS_OK, 0, model.nms_sequential(boxes, mask))
            for boxes, mask in batches
        ]


@dataclass
class Stats:
    """Running totals and the latest frame's timings, for the HUD."""

    batches: int = 0
    agree: int = 0
    errors: int = 0
    torch_diff: int = 0
    t_yolo: float = 0.0
    t_uart: float = 0.0
    t_torch: float = 0.0
    n_batches: int = 0
    fps: float = 0.0
    log: list[str] = field(default_factory=list)

    def lines(self, *, simulated: bool) -> list[str]:
        """The HUD text."""
        core_us = full_latency_cycles() / p.CLOCK_HZ * 1e6
        link = "golden model in Python" if simulated else "UART round trip"
        errors = f" ({self.errors} error replies)" if self.errors else ""
        return [
            (
                f"Time per frame:  YOLOv8n {self.t_yolo * 1e3:.1f} ms  |  {link}"
                f" {self.t_uart * 1e3:.2f} ms for {self.n_batches} batch(es)"
                f"  |  {self.fps:.1f} fps"
            ),
            (
                f"Per batch:  FPGA core {core_us:.2f} us ({full_latency_cycles()} cycles,"
                f" the same for every input)  |  torchvision NMS on this frame's boxes"
                f" {self.t_torch * 1e6:.0f} us"
            ),
            (
                f"Check:  {'board' if not simulated else 'stand-in'} == golden model"
                f" {self.agree}/{self.batches} batches{errors}"
                f"  |  boxes decided differently by float torchvision: {self.torch_diff}"
            ),
            (
                f"Limits:  IoU threshold {IOU:.2f}, fixed  |  max {p.N} boxes per class"
                "  |  keys: space pause, n step through NMS, q quit"
            ),
        ]


def torchvision_masks(batches: list[batching.ClassBatch]) -> tuple[list[int], float]:
    """Run float NMS on the same batches, for comparison and timing.

    Args:
        batches: The batches sent to the board.

    Returns:
        ``(keep_mask per batch, seconds for one batched_nms call over all of them)``.
    """
    rows, scores, ids, owner = [], [], [], []
    for i, b in enumerate(batches):
        for slot in range(b.count):
            box = b.boxes[slot]
            rows.append([box.x, box.y, box.a, box.b])
            scores.append(box.score)
            ids.append(b.cls)
            owner.append((i, slot))
    masks = [0] * len(batches)
    if not rows:
        return masks, 0.0
    boxes_t = torch.tensor(rows, dtype=torch.float32)
    scores_t = torch.tensor(scores, dtype=torch.float32)
    ids_t = torch.tensor(ids)
    t0 = time.perf_counter()
    kept = torchvision.ops.batched_nms(boxes_t, scores_t, ids_t, IOU)
    elapsed = time.perf_counter() - t0
    for k in kept.tolist():
        i, slot = owner[k]
        masks[i] |= 1 << slot
    return masks, elapsed


def process(
    image: np.ndarray,
    detector: object,
    board: host.Board | GoldenBoard,
    stats: Stats,
    conf: float,
) -> draw.Frame:
    """Run one frame through the detector and the board, updating the stats.

    Args:
        image: The BGR frame.
        detector: A loaded Ultralytics YOLO model.
        board: The board, or the golden model standing in for it.
        stats: Updated in place.
        conf: Confidence threshold.

    Returns:
        What to draw.
    """
    from ultralytics.utils import ops

    t0 = time.perf_counter()
    xyxy, scores = raw_predictions(detector, image)
    xyxy = ops.scale_boxes((IMGSZ, IMGSZ), xyxy.copy(), image.shape[:2])
    stats.t_yolo = time.perf_counter() - t0

    cands = batching.candidates(xyxy, scores, conf)
    batches = batching.make_batches(cands)

    t0 = time.perf_counter()
    replies = board.transact_many([(b.boxes, b.present_mask) for b in batches])
    stats.t_uart = time.perf_counter() - t0
    stats.n_batches = len(batches)

    keep_masks = []
    for b, reply in zip(batches, replies, strict=True):
        stats.batches += 1
        if not reply.ok:
            stats.errors += 1
            stats.log.append(f"class {b.cls}: status {reply.status:#04x}")
        elif reply.keep_mask == model.nms_sequential(b.boxes, b.present_mask):
            stats.agree += 1
        else:
            stats.log.append(f"class {b.cls}: keep_mask {reply.keep_mask:#010x} wrong")
        keep_masks.append(reply.keep_mask)

    tv_masks, stats.t_torch = torchvision_masks(batches)
    stats.torch_diff += sum(
        (a ^ b).bit_count() for a, b in zip(tv_masks, keep_masks, strict=True)
    )
    return draw.Frame(image, cands, batches, keep_masks, detector.names)


def open_board(args: argparse.Namespace) -> host.Board | GoldenBoard | None:
    """Open the board, or the golden model with ``--no-board``; None on failure."""
    if args.no_board:
        return GoldenBoard()
    print(host.set_latency_timer(args.port)[1])
    try:
        return host.Board(args.port)
    except host.PortError as exc:
        print(
            f"{exc}\n  is the board programmed (make program)? or run with --no-board"
        )
        return None


def main(argv: list[str] | None = None) -> int:
    """Run the demo until the video ends or q is pressed.

    Args:
        argv: Command-line arguments, excluding the program name.

    Returns:
        The process exit status: 1 if any reply disagreed with the golden model.
    """
    parser = argparse.ArgumentParser(
        prog="python -m demo", description=__doc__.split("\n")[0]
    )
    parser.add_argument(
        "source", nargs="?", default="0", help="webcam index or video file"
    )
    parser.add_argument(
        "--no-board", action="store_true", help="golden model, no board"
    )
    parser.add_argument(
        "--port", default=host.DEFAULT_PORT, help="UART (default %(default)s)"
    )
    parser.add_argument("--weights", default="yolov8n.pt", help="Ultralytics weights")
    parser.add_argument("--conf", type=float, default=batching.CONF_THRESHOLD)
    parser.add_argument(
        "--loop", action="store_true", help="replay a video file forever"
    )
    parser.add_argument(
        "--save", type=Path, help="also write the shown video to this file"
    )
    parser.add_argument("--frames", type=int, help="stop after this many frames")
    parser.add_argument(
        "--no-window", action="store_true", help="no window (with --save)"
    )
    args = parser.parse_args(argv)

    from ultralytics import YOLO

    source = int(args.source) if args.source.isdigit() else args.source
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        print(f"cannot open video source {args.source!r}")
        return EXIT_USAGE
    board = open_board(args)
    if board is None:
        return EXIT_USAGE
    detector = YOLO(args.weights)
    stats = Stats()
    writer = None
    paused, frame, steps, step_i, shown = False, None, [], None, 0

    try:
        while args.frames is None or shown < args.frames:
            if not paused:
                ok, image = cap.read()
                if not ok:
                    if args.loop and isinstance(source, str):
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    break
                t0 = time.perf_counter()
                frame = process(image, detector, board, stats, args.conf)
                stats.fps = 0.9 * stats.fps + 0.1 / (time.perf_counter() - t0)
                for line in stats.log:
                    print(f"frame {shown}: {line}", file=sys.stderr)
                stats.log.clear()
                steps, step_i = draw.steps_of(frame), None
                shown += 1
            step = (steps[step_i], len(steps), step_i) if step_i is not None else None
            canvas = draw.compose(
                frame,
                stats.lines(simulated=args.no_board),
                simulated=args.no_board,
                step=step,
            )
            if args.save and not paused:
                if writer is None:
                    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                    h, w = canvas.shape[:2]
                    writer = cv2.VideoWriter(str(args.save), fourcc, 10, (w, h))
                writer.write(canvas)
            if args.no_window:
                continue
            cv2.imshow(WINDOW, canvas)
            key = cv2.waitKey(30 if paused else 1) & 0xFF
            if key in (ord("q"), 27):
                break
            if key == ord(" "):
                paused, step_i = not paused, None
            elif key == ord("n") and steps:
                paused = True
                step_i = 0 if step_i is None else (step_i + 1) % len(steps)
    finally:
        cap.release()
        if writer is not None:
            writer.release()
        if isinstance(board, host.Board):
            board.__exit__(None, None, None)
        cv2.destroyAllWindows()

    print(
        f"{shown} frames, {stats.batches} batches, {stats.agree} agree with the golden model,"
        f" {stats.errors} error replies"
    )
    return EXIT_OK if stats.agree == stats.batches else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
