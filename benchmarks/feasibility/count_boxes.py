"""Count how many boxes a real detector sends to NMS.

Runs YOLOv8n and YOLO11n at 640 on COCO val2017 images, takes the raw head output before
NMS, and counts candidates above each confidence threshold per image and per
(image, class). Per (image, class) is the number that matters for the block: multi-class
NMS runs one N = 32 batch per class (architecture.md §11).

Also counts survivors after NMS, for the duplicate ratio, and exports real candidate sets
for ``time_vs_boxes.py``: all 8,400 anchors of the first ``--export`` images, class-agnostic
and sorted by score, as integer corners in the 640 x 640 letterboxed frame (so 12-bit
coordinates hold them). The top N of one image is a real candidate set of size N.

Usage (scratch venv with ultralytics, torch, torchvision)::

    python -m benchmarks.feasibility.count_boxes --images <coco>/val2017 --n 500 --out <dir>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
import torch
import torchvision
from ultralytics import YOLO
from ultralytics.data.augment import LetterBox

CONF_THRESHOLDS = (0.10, 0.25, 0.50)
IOU_THRESHOLDS = (0.45, 0.50, 0.70)
"""0.45 is ``non_max_suppression``'s default, 0.50 the block's, 0.70 what ``predict`` passes."""
MODELS = ("yolov8n.pt", "yolo11n.pt")
IMGSZ = 640


def raw_predictions(model: YOLO, image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Run the network and return its head output before any filtering.

    Letterboxes to a fixed 640 x 640 (``auto=False``) so every image yields the full 8,400
    anchors, as the published "8,400 candidates" figure assumes.

    Args:
        model: A loaded detector.
        image: BGR image as read by OpenCV.

    Returns:
        ``(boxes_xyxy, class_scores)`` with shapes ``(8400, 4)`` and ``(8400, 80)``.
    """
    letterboxed = LetterBox((IMGSZ, IMGSZ), auto=False)(image=image)
    x = (
        torch.from_numpy(letterboxed[:, :, ::-1].transpose(2, 0, 1).copy()).float()
        / 255
    )
    with torch.inference_mode():
        out = model.model(x[None])
    pred = out[0] if isinstance(out, (tuple, list)) else out
    pred = pred[0].T.numpy()  # (8400, 4 + nc)
    xywh, scores = pred[:, :4], pred[:, 4:]
    xyxy = np.concatenate(
        [xywh[:, :2] - xywh[:, 2:] / 2, xywh[:, :2] + xywh[:, 2:] / 2], 1
    )
    return xyxy, scores


def count_one(xyxy: np.ndarray, scores: np.ndarray) -> dict:
    """Count candidates and NMS survivors for one image.

    Single-label, as ``YOLO.predict`` does by default: each anchor keeps its best class.

    Args:
        xyxy: Anchor boxes.
        scores: Per-class scores.

    Returns:
        Counts keyed by threshold.
    """
    conf = scores.max(1)
    cls = scores.argmax(1)
    row: dict = {}
    for c in CONF_THRESHOLDS:
        sel = conf > c
        _, per_class = np.unique(cls[sel], return_counts=True)
        row[f"img@{c}"] = int(sel.sum())
        row[f"cls@{c}"] = per_class.tolist()
        for iou in IOU_THRESHOLDS:
            kept = torchvision.ops.batched_nms(
                torch.from_numpy(xyxy[sel]),
                torch.from_numpy(conf[sel]),
                torch.from_numpy(cls[sel]),
                iou,
            )
            row[f"kept@{c}/{iou}"] = len(kept)
        row[f"lost@{c}"] = keepers_beyond_top(xyxy[sel], conf[sel], cls[sel])
    return row


def keepers_beyond_top(
    xyxy: np.ndarray,
    conf: np.ndarray,
    cls: np.ndarray,
    top: int = 32,
    iou: float = 0.5,
) -> list[int]:
    """Count, per class, the NMS keepers that a top-``top`` truncation would drop.

    Greedy NMS decides each box from higher-scoring boxes only, so running it on the top
    ``top`` gives exactly the full result's keepers among those ranks. The loss from
    truncating is therefore the number of full-result keepers ranked below ``top``.

    Args:
        xyxy: Candidate boxes.
        conf: Candidate scores.
        cls: Candidate classes.
        top: Batch size of the block.
        iou: The block's threshold, 0.5.

    Returns:
        One count per class present, in class order.
    """
    lost = []
    for k in np.unique(cls):
        idx = np.flatnonzero(cls == k)
        order = idx[np.argsort(-conf[idx], kind="stable")]
        kept = torchvision.ops.nms(
            torch.from_numpy(xyxy[order]),
            torch.from_numpy(conf[order]),
            iou,
        )
        lost.append(int((kept >= top).sum()))
    return lost


def export_sets(xyxy: np.ndarray, scores: np.ndarray) -> np.ndarray:
    """Quantise one image's anchors to the frozen record format, sorted by score.

    Args:
        xyxy: Anchor boxes in the 640 x 640 frame.
        scores: Per-class scores.

    Returns:
        ``(8400, 5)`` int32 array of ``x, y, a, b, score`` with score in ``0..65535``,
        highest score first.
    """
    conf = scores.max(1)
    order = np.argsort(-conf, kind="stable")
    corners = np.clip(np.rint(xyxy[order]), 0, 4095).astype(np.int32)
    q = np.rint(conf[order] * 65535).astype(np.int32)
    return np.concatenate([corners, q[:, None]], 1)


def main() -> None:
    """Parse arguments, run both models, and write counts and candidate sets."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--images", type=Path, required=True)
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--export", type=int, default=50)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(8)

    files = sorted(args.images.glob("*.jpg"))[: args.n]
    for weights in MODELS:
        model = YOLO(weights)
        model.model.eval()
        rows, raw = [], []
        for k, f in enumerate(files):
            xyxy, scores = raw_predictions(model, cv2.imread(str(f)))
            rows.append({"image": f.name, **count_one(xyxy, scores)})
            if k < args.export:
                raw.append(export_sets(xyxy, scores))
        stem = Path(weights).stem
        (args.out / f"counts_{stem}.json").write_text(json.dumps(rows))
        np.savez_compressed(args.out / f"raw_{stem}.npz", raw=np.stack(raw))
        print(f"{stem}: {len(rows)} images")


if __name__ == "__main__":
    main()
