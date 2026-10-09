"""What one demo frame did, as data: the single source of truth for every view.

The pipeline produces one :class:`FrameRecord` per frame. Every number the window shows is
computed from these records, and ``--record run.jsonl`` writes them one per line, so the
demo can be audited afterwards (``python -m demo.audit run.jsonl``). Images are never
recorded; the boxes and masks that crossed the wire are.

Timestamps are ``time.perf_counter()`` seconds, one clock for every stage, so stages can be
laid end to end and the capture-to-screen latency is a plain difference.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

from models.nms import model
from models.nms import params as p

BOARD_FPGA = "fpga"
BOARD_MODEL = "model"
"""``Settings.board``: the real board, or the golden model chosen with ``--no-board``."""

CONNECTED, OFFLINE = "connected", "offline"
"""``FrameRecord.board_state`` when ``Settings.board`` is ``fpga``. With ``model`` the state is
``model``: there is no board, and nothing is ever presented as if it came from one."""

STAGES = ("read", "detect", "batch", "board", "check", "software", "paint")
"""Stage names, in pipeline order. ``check`` runs the golden model on every batch; ``paint``
is filled in by the window."""


@dataclass(frozen=True)
class Settings:
    """The settings a frame was produced with; immutable, so each record keeps its own.

    Attributes:
        source: ``webcam:<index>``, ``video:<path>`` or ``coco``.
        imgsz: Detector input size in pixels, 640 or 320.
        conf: Confidence threshold for a candidate to reach NMS.
        board: :data:`BOARD_FPGA` or :data:`BOARD_MODEL`.
    """

    source: str
    imgsz: int
    conf: float
    board: str


@dataclass
class BatchRecord:
    """One class's batch in one frame: what was sent, what came back, what software took.

    Attributes:
        cls: Class id.
        boxes: The present boxes as ``[x, y, a, b, score]``, in slot order.
        present_mask: Which of the 32 slots hold those boxes.
        status: Reply status byte, or None if no reply arrived (board offline).
        seq: The reply's echoed sequence number, or None.
        keep_mask: The reply's ``keep_mask``, or None if no reply arrived.
        golden_mask: ``model.nms_sequential`` on the same batch, computed live.
        software_us: Microseconds per software NMS implementation on this same batch.
    """

    cls: int
    boxes: list[list[int]]
    present_mask: int
    status: int | None = None
    seq: int | None = None
    keep_mask: int | None = None
    golden_mask: int | None = None
    software_us: dict[str, float] = field(default_factory=dict)

    @property
    def agrees(self) -> bool:
        """Whether a reply arrived, was OK, and equals the golden model."""
        return self.status == p.STATUS_OK and self.keep_mask == self.golden_mask

    def model_boxes(self) -> list[model.Box]:
        """Return the 32 slots as the golden model and the wire take them."""
        boxes = [model.Box(*b) for b in self.boxes]
        return boxes + [model.Box(0, 0, 0, 0, 0)] * (p.N - len(boxes))


@dataclass
class FrameRecord:
    """One processed frame.

    Attributes:
        index: Frame number since the demo started.
        settings: The settings it was produced with.
        source_name: What was shown, e.g. ``webcam 0`` or a COCO file name.
        t_capture: When the camera delivered the frame (or the file was read).
        t: Stage name to ``(start, end)``, see :data:`STAGES`.
        dropped: Camera frames overwritten unseen since the previous frame.
        source_fps: The source's own frame rate, if it reports one.
        candidates: Boxes above the confidence threshold.
        over_cap: Candidates beyond 32 in their class, never sent.
        board_state: :data:`CONNECTED`, :data:`OFFLINE` or ``model``.
        batches: One per class present.
    """

    index: int
    settings: Settings
    source_name: str
    t_capture: float
    t: dict[str, tuple[float, float]]
    dropped: int
    source_fps: float | None
    candidates: int
    over_cap: int
    board_state: str
    batches: list[BatchRecord]

    def ms(self, stage: str) -> float | None:
        """Return a stage's duration in milliseconds, or None if it did not run."""
        if stage not in self.t:
            return None
        start, end = self.t[stage]
        return (end - start) * 1e3

    def capture_to_screen_ms(self) -> float | None:
        """Return the time from capture to the end of painting, once painted."""
        if "paint" not in self.t:
            return None
        return (self.t["paint"][1] - self.t_capture) * 1e3

    def to_json(self) -> str:
        """Serialise to one JSON line."""
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> FrameRecord:
        """Parse one line written by :meth:`to_json`."""
        d = json.loads(line)
        d["settings"] = Settings(**d["settings"])
        d["t"] = {k: (v[0], v[1]) for k, v in d["t"].items()}
        d["batches"] = [BatchRecord(**b) for b in d["batches"]]
        return cls(**d)


def read_jsonl(path: Path) -> Iterator[FrameRecord]:
    """Yield the records of a ``--record`` file, in order."""
    with path.open() as f:
        for line in f:
            if line.strip():
                yield FrameRecord.from_json(line)
