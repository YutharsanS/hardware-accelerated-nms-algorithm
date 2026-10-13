"""Live demo: YOLOv8n finds boxes, the board runs NMS on them, the window shows the data.

The FPGA side is the shipped ``nms_top`` bitstream, unchanged. See docs/user_guide.md §11.

Usage (``make demo`` wraps it)::

    uv run --extra demo python -m demo                       # webcam 0, board on ttyUSB1
    uv run --extra demo python -m demo coco                  # COCO crowd images
    uv run --extra demo python -m demo a.mp4 b.mp4           # video files, looping
    uv run --extra demo python -m demo --no-board            # golden model, no board
    uv run --extra demo python -m demo --record run.jsonl    # keep every frame's data
    uv run python -m demo.audit run.jsonl                    # re-check it afterwards

The source, detector size and confidence can all be changed from the window.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from pathlib import Path

from demo import batching, coco
from demo.audit import Totals, audit
from demo.pipeline import BoardLink, Detector, GoldenBoard, Pipeline, SoftwareNMS
from demo.records import BOARD_FPGA, BOARD_MODEL, OFFLINE, FrameRecord, Settings
from demo.sources import ImageSet, Source, VideoFiles, Webcam
from models.nms import host

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
IMAGE_SECONDS = 5.0
"""How long each still image is shown before the next."""


def source_spec(args: list[str]) -> tuple[str, list[Path]]:
    """Turn the positional arguments into a source spec and the video file list.

    Args:
        args: Nothing (webcam 0), a camera index, ``coco``, or video files.

    Returns:
        ``(spec, video paths)``.
    """
    if not args:
        return "webcam:0", []
    if len(args) == 1 and args[0].isdigit():
        return f"webcam:{args[0]}", []
    if args == ["coco"]:
        return "coco", []
    return "video:0", [Path(a) for a in args]


def opener(video_paths: list[Path]) -> Callable[[str], Source]:
    """Return a function that opens a source spec; video specs read ``video_paths``."""

    def open_source(spec: str) -> Source:
        kind, _, arg = spec.partition(":")
        if kind == "webcam":
            return Webcam(int(arg))
        if kind == "video":
            return VideoFiles(list(video_paths))
        if kind == "coco":
            paths, errors = coco.fetch(coco.select())
            if not paths:
                msg = "no COCO images: " + (errors[0] if errors else "none selected")
                raise OSError(msg)
            return ImageSet(paths, IMAGE_SECONDS)
        msg = f"unknown source {spec!r}"
        raise OSError(msg)

    return open_source


def summary(records: list[FrameRecord]) -> tuple[str, Totals]:
    """Return the end-of-run line, from the same audit ``demo.audit`` runs."""
    t = audit(records)
    who = "golden model (no board)" if t.model_frames else "board"
    line = (
        f"{t.frames} frames, {t.batches} batches: {who} result equal to the golden model"
        f" {t.equal}, different {t.mismatched}, error replies {t.failed},"
        f" sent while offline {t.no_reply}"
    )
    return line, t


def run_headless(
    pipeline: Pipeline,
    source: Source,
    settings: Settings,
    frames: int,
    out: list[FrameRecord],
) -> None:
    """Run the pipeline without a window, appending each record to ``out``."""
    for index in range(frames):
        r0 = time.perf_counter()
        grab = source.read()
        r1 = time.perf_counter()
        if grab is None:
            break
        out.append(pipeline.step(grab, (r0, r1), settings, index, source.fps).record)


def main(argv: list[str] | None = None) -> int:
    """Run the demo.

    Args:
        argv: Command-line arguments, excluding the program name.

    Returns:
        The process exit status: 1 if any reply disagreed with the golden model.
    """
    parser = argparse.ArgumentParser(
        prog="python -m demo", description=__doc__.split("\n")[0]
    )
    parser.add_argument(
        "source", nargs="*", help="camera index (default 0), 'coco', or video files"
    )
    parser.add_argument(
        "--no-board", action="store_true", help="the golden model, no board"
    )
    parser.add_argument(
        "--port", default=host.DEFAULT_PORT, help="UART (default %(default)s)"
    )
    parser.add_argument("--weights", default="yolov8n.pt", help="Ultralytics weights")
    parser.add_argument("--imgsz", type=int, choices=(640, 320), default=640)
    parser.add_argument("--conf", type=float, default=batching.CONF_THRESHOLD)
    parser.add_argument("--record", type=Path, help="append every frame's data (JSONL)")
    parser.add_argument("--save", type=Path, help="record the window to a video file")
    parser.add_argument("--frames", type=int, help="stop after this many frames")
    parser.add_argument(
        "--headless", action="store_true", help="no window: run and print the totals"
    )
    parser.add_argument(
        "--no-software",
        action="store_true",
        help="skip the OpenCV/torchvision comparison",
    )
    args = parser.parse_args(argv)

    spec, video_paths = source_spec(args.source)
    settings = Settings(
        source=spec,
        imgsz=args.imgsz,
        conf=args.conf,
        board=BOARD_MODEL if args.no_board else BOARD_FPGA,
    )
    board = GoldenBoard() if args.no_board else BoardLink(args.port)
    if board.state == OFFLINE:
        print(f"board offline: {board.error} (retrying every 2 s; or use --no-board)")
    detector = Detector(args.weights)
    software = None if args.no_software else SoftwareNMS()
    pipeline = Pipeline(detector, detector.names, board, software)
    open_source = opener(video_paths)
    records: list[FrameRecord] = []

    if args.headless:
        try:
            source = open_source(spec)
        except OSError as exc:
            print(exc)
            return EXIT_USAGE
        try:
            run_headless(pipeline, source, settings, args.frames or 100, records)
        finally:
            source.close()
            board.close()
        if args.record:
            with args.record.open("a") as f:
                f.writelines(r.to_json() + "\n" for r in records)
    else:
        from PySide6.QtWidgets import QApplication

        from demo.app import MainWindow
        from demo.worker import Worker

        app = QApplication.instance() or QApplication(sys.argv)
        worker = Worker(pipeline, open_source, settings)
        window = MainWindow(
            worker,
            video_paths,
            record=args.record,
            save=args.save,
            frames=args.frames,
            on_record=records.append,
        )
        window.showMaximized()
        worker.start()
        app.exec()

    line, totals = summary(records)
    print(line)
    return EXIT_FAIL if totals.mismatched else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
