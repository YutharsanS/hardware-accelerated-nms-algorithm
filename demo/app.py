"""The demo window: two video panels, a data dashboard, and a settings panel.

Every number on screen is computed from frame records (``demo/records.py``). The video
panels show the newest frame; the dashboard shows the newest *completed* record, whose
paint time is known, so it is one frame behind the video and says which frame it is.

The one figure not measured in this run is the FPGA's compute time, which the UART cannot
resolve: [C] draws it as a dashed reference line, labelled as measured on silicon (ILA).
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QKeySequence,
    QPainter,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from benchmarks.targets.fpga import full_latency_cycles
from demo import draw
from demo.pipeline import FrameResult, with_paint
from demo.records import BOARD_MODEL, OFFLINE, FrameRecord, Settings
from demo.worker import Worker
from models.nms import params as p

HISTORY = 100
"""Frames in [B]."""
SCATTER = 2000
"""Samples per implementation kept in [C]."""
TABLE_ROWS = 10
LEGEND_ROOM = 1.0
"""Decades of empty space above the data, so the one-row legend never covers a point."""
IOU = p.T_INT / (1 << p.K_SHIFT)

# Stage segments of [A]: (label, colour), between consecutive timestamps of a record.
SEGMENTS = (
    ("frame age at read", "#8a8f98"),
    ("YOLOv8n", "#4e79a7"),
    ("batch", "#76b7b2"),
    ("board round trip (UART + FPGA)", "#e15759"),
    ("golden check", "#b07aa1"),
    ("software NMS (comparison)", "#f28e2b"),
    ("to window", "#bab0ac"),
    ("paint", "#59a14f"),
)
SOFTWARE_COLOURS = {"opencv": "#f28e2b", "torchvision": "#4e79a7"}
BOARD_STAGE = {
    "fpga": "board round trip (UART + FPGA)",
    "model": "golden model in Python (no board)",
}
"""What the ``board`` stage was: the real board, or the model chosen with --no-board."""


def stage_name(label: str, rec: FrameRecord) -> str:
    """Return a segment's on-screen name for this record."""
    if label == "YOLOv8n":
        return f"YOLOv8n @{rec.settings.imgsz}"
    if label == BOARD_STAGE["fpga"]:
        return BOARD_STAGE[rec.settings.board]
    return label


def boundaries(rec: FrameRecord) -> list[float | None]:
    """Return a record's stage boundaries, capture to paint end, for the [A] bar.

    Consecutive differences are the :data:`SEGMENTS`; together they sum exactly to the
    capture-to-screen latency. A stage that did not run has zero width.
    """
    t = rec.t
    points = [rec.t_capture, t["read"][1], t["detect"][1], t["batch"][1]]
    points.append(t["board"][1])
    points.append(t["check"][1])
    points.append(t["software"][1] if "software" in t else points[-1])
    points += [t["paint"][0], t["paint"][1]]
    return points


def log_range(values: list[float]) -> tuple[float, float]:
    """Return a log10 axis range covering ``values`` to the nearest half decade."""
    lo, hi = np.log10(min(values)), np.log10(max(values))
    return np.floor(lo * 2) / 2, max(np.ceil(hi * 2) / 2, np.floor(lo * 2) / 2 + 0.5)


def decade_ticks(plot: pg.PlotWidget, lo: float, hi: float) -> None:
    """Label a log axis at each decade from ``10**lo`` to ``10**hi``, as plain numbers."""
    ticks = [
        (k, f"{10.0**k:g}") for k in range(int(np.floor(lo)), int(np.ceil(hi)) + 1)
    ]
    plot.getAxis("left").setTicks([ticks, []])


def fixed_axes(plot: pg.PlotWidget) -> None:
    """Take a plot's ranges out of pyqtgraph's hands: they are set from the data.

    pyqtgraph applies auto-range lazily, so a frame painted (or recorded) right after new
    data could show a stale range; and its SI prefixes turn "ms" into scaled units.
    """
    plot.plotItem.vb.disableAutoRange()
    for side in ("left", "bottom"):
        plot.getAxis(side).enableAutoSIPrefix(False)


def to_qimage(bgr: np.ndarray) -> QImage:
    """Wrap a BGR image for Qt, copying so the numpy buffer can go."""
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    return QImage(rgb.data, w, h, rgb.strides[0], QImage.Format.Format_RGB888).copy()


class StageBar(QWidget):
    """[A]: one frame's time from capture to screen, as a to-scale stacked bar."""

    def __init__(self) -> None:
        """An empty bar until the first completed frame."""
        super().__init__()
        self.setMinimumHeight(92)
        self.rec: FrameRecord | None = None

    def show_record(self, rec: FrameRecord) -> None:
        """Draw this record."""
        self.rec = rec
        self.update()

    def paintEvent(self, _event: object) -> None:
        """Draw the bar, its labels and the budget line."""
        qp = QPainter(self)
        qp.fillRect(self.rect(), QColor("#1e1f22"))
        qp.setPen(QColor("#e8e8e8"))
        qp.setFont(QFont("DejaVu Sans", 9, QFont.Weight.Bold))
        if self.rec is None:
            qp.drawText(10, 18, "[A] waiting for the first frame")
            return
        rec = self.rec
        total = rec.capture_to_screen_ms() or 0.0
        qp.drawText(
            10,
            18,
            f"[A] LAST COMPLETED FRAME #{rec.index}: capture to screen {total:.1f} ms"
            f"   |   camera frames dropped since the previous: {rec.dropped}",
        )
        qp.setFont(QFont("DejaVu Sans", 8))
        points = boundaries(rec)
        left, width, y, h = 10, self.width() - 20, 28, 22
        scale = width / max(total, 1e-9)
        x = float(left)
        for (label, colour), a, b in zip(SEGMENTS, points, points[1:], strict=False):
            ms = (b - a) * 1e3
            w = ms * scale
            qp.fillRect(QRectF(x, y, w, h), QColor(colour))
            text = f"{stage_name(label, rec)} {ms:.1f} ms"
            if w > qp.fontMetrics().horizontalAdvance(text) + 6:
                qp.setPen(QColor("#ffffff"))
                qp.drawText(QRectF(x, y, w, h), Qt.AlignmentFlag.AlignCenter, text)
            x += w
        # The legend under the bar names every segment, including those too thin to label.
        qp.setPen(QColor("#e8e8e8"))
        lx, ly = left, y + h + 16
        for (label, colour), a, b in zip(SEGMENTS, points, points[1:], strict=False):
            text = f"{stage_name(label, rec)} {(b - a) * 1e3:.2f} ms"
            tw = qp.fontMetrics().horizontalAdvance(text)
            if lx + tw + 24 > left + width:
                lx, ly = left, ly + 14
            qp.fillRect(lx, ly - 9, 10, 10, QColor(colour))
            qp.drawText(lx + 14, ly, text)
            lx += tw + 28
        fps = rec.source_fps
        budget = (
            f"source frame rate {fps:.0f} fps: a new frame every {1000 / fps:.1f} ms"
            if fps
            else "still image: no frame rate"
        )
        qp.drawText(left, ly + 16, budget)


class Dashboard(QWidget):
    """[B] the last frames, [C] NMS time per batch, [D] the latest batches."""

    def __init__(self, board: str) -> None:
        """Build the plots and the table; ``board`` is ``Settings.board``."""
        super().__init__()
        pg.setConfigOptions(antialias=True, background="#1e1f22", foreground="#e8e8e8")
        row = QHBoxLayout(self)

        self.history = pg.PlotWidget(title="[B] last 100 completed frames")
        self.history.setLogMode(y=True)
        self.history.setLabel("left", "time (ms, log scale)")
        self.history.setLabel("bottom", "frame #")
        self.history.addLegend(offset=(5, 2), brush=pg.mkBrush("#1e1f22dd"), colCount=3)
        fixed_axes(self.history)
        self.lines = {
            "capture to screen": self.history.plot(pen=pg.mkPen("#e8e8e8", width=2)),
            "YOLOv8n": self.history.plot(pen=pg.mkPen("#4e79a7", width=2)),
            "board": self.history.plot(pen=pg.mkPen("#e15759", width=2)),
        }
        for name, item in self.lines.items():
            label = BOARD_STAGE[board] if name == "board" else name
            self.history.plotItem.legend.addItem(item, label)
        self.markers: list[pg.InfiniteLine] = []
        row.addWidget(self.history, 5)

        mid = QVBoxLayout()
        self.scatter = pg.PlotWidget(
            title="[C] NMS time per batch: software on this PC (dots), FPGA (dashed)"
        )
        self.scatter.setLogMode(y=True)
        self.scatter.setLabel("left", "time per batch (µs, log scale)")
        self.scatter.setLabel("bottom", "boxes in the batch")
        self.scatter.addLegend(offset=(5, 2), brush=pg.mkBrush("#1e1f22dd"), colCount=3)
        fixed_axes(self.scatter)
        self.dots = {
            name: pg.ScatterPlotItem(
                size=5, pen=None, brush=pg.mkBrush(colour), name=name
            )
            for name, colour in SOFTWARE_COLOURS.items()
        }
        for item in self.dots.values():
            self.scatter.addItem(item)
        # The FPGA's time, for comparison: a dashed line, not dots, because it is the one
        # figure not measured in this run: the ILA measured it on silicon, 16 of 16 batches.
        # It is the same for every batch size.
        core_us = full_latency_cycles() / p.CLOCK_HZ * 1e6
        self.core_us = core_us
        self.scatter.plot(
            [1, p.N],
            [core_us, core_us],
            pen=pg.mkPen("#f0c040", width=2, style=Qt.PenStyle.DashLine),
            name=f"FPGA {core_us:.2f} µs, any batch (ILA, not this run)",
        )
        self.count = QLabel()
        mid.addWidget(self.scatter, 5)
        mid.addWidget(self.count)
        row.addLayout(mid, 5)

        self.table = QTableWidget(TABLE_ROWS, 6)
        self.table.setHorizontalHeaderLabels(
            ["frame", "class", "boxes", "kept", "status", "seq"]
        )
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        box = QGroupBox("[D] latest batches, newest first")
        lay = QVBoxLayout(box)
        lay.addWidget(self.table)
        row.addWidget(box, 4)
        self.clear()

    def clear(self) -> None:
        """Forget every sample."""
        self.records: deque[FrameRecord] = deque(maxlen=HISTORY)
        self.samples = {name: deque(maxlen=SCATTER) for name in SOFTWARE_COLOURS}
        self.rows: deque[list[str]] = deque(maxlen=TABLE_ROWS)
        for marker in self.markers:
            self.history.removeItem(marker)
        self.markers = []
        self._redraw()

    def add(self, rec: FrameRecord, names: dict[int, str]) -> None:
        """Take one completed record; ``names`` maps class ids to names."""
        if self.records and self.records[-1].settings != rec.settings:
            marker = pg.InfiniteLine(
                pos=rec.index, angle=90, pen=pg.mkPen("#aaaaaa", style=Qt.DashLine)
            )
            self.history.addItem(marker)
            self.markers.append(marker)
        self.records.append(rec)
        for b in rec.batches:
            n = b.present_mask.bit_count()
            for name, us in b.software_us.items():
                if name in self.samples:
                    self.samples[name].append((n, us))
            self.rows.appendleft(
                [
                    str(rec.index),
                    names.get(b.cls, str(b.cls)),
                    str(n),
                    "-" if b.keep_mask is None else str(b.keep_mask.bit_count()),
                    "no reply"
                    if b.status is None
                    else ("OK" if b.status == p.STATUS_OK else f"{b.status:#04x}"),
                    "-" if b.seq is None else f"{b.seq:#04x}",
                ],
            )
        self._redraw()

    def _redraw(self) -> None:
        recs = list(self.records)
        x = [r.index for r in recs]
        series = {
            "capture to screen": [r.capture_to_screen_ms() for r in recs],
            "YOLOv8n": [r.ms("detect") for r in recs],
            "board": [r.ms("board") for r in recs],
        }
        values = []
        for name, ys in series.items():
            pts = [(a, b) for a, b in zip(x, ys, strict=True) if b and b > 0]
            self.lines[name].setData([a for a, _ in pts], [b for _, b in pts])
            values += [b for _, b in pts]
        if x:
            self.history.setXRange(x[0], max(x[-1], x[0] + 1), padding=0.02)
            lo, hi = log_range(values)
            self.history.setYRange(lo, hi + LEGEND_ROOM, padding=0)
            decade_ticks(self.history, lo, hi)
        total, values = 0, [self.core_us]
        for name, item in self.dots.items():
            pts = list(self.samples[name])
            item.setData([a for a, _ in pts], [np.log10(b) for _, b in pts])
            total = max(total, len(pts))
            values += [b for _, b in pts]
        self.scatter.setXRange(0, p.N + 1, padding=0)
        if values:
            lo, hi = log_range(values)
            self.scatter.setYRange(lo, hi + LEGEND_ROOM, padding=0)
            decade_ticks(self.scatter, lo, hi)
        self.count.setText(f"{total} batches timed per implementation")
        for i in range(TABLE_ROWS):
            values = self.rows[i] if i < len(self.rows) else [""] * 6
            for j, v in enumerate(values):
                self.table.setItem(i, j, QTableWidgetItem(v))


class SettingsPanel(QWidget):
    """The settings panel; every change becomes a new :class:`Settings` for the worker."""

    def __init__(self, window: MainWindow) -> None:
        """Build the controls from the window's current settings."""
        super().__init__()
        self.w = window
        s = window.settings
        lay = QVBoxLayout(self)

        src = QGroupBox("Input")
        form = QVBoxLayout(src)
        self.group = QButtonGroup(self)
        self.webcam = QRadioButton("Webcam")
        self.cam_index = QSpinBox()
        self.cam_index.setRange(0, 9)
        cam_row = QHBoxLayout()
        cam_row.addWidget(self.webcam)
        cam_row.addWidget(self.cam_index)
        form.addLayout(cam_row)
        self.video = QRadioButton("Video files")
        self.choose = QPushButton("Choose…")
        vid_row = QHBoxLayout()
        vid_row.addWidget(self.video)
        vid_row.addWidget(self.choose)
        form.addLayout(vid_row)
        self.video_label = QLabel()
        self.video_label.setWordWrap(True)
        form.addWidget(self.video_label)
        self.coco = QRadioButton("COCO crowd images")
        form.addWidget(self.coco)
        nav = QHBoxLayout()
        self.prev = QPushButton("◀ previous")
        self.next = QPushButton("next ▶")
        nav.addWidget(self.prev)
        nav.addWidget(self.next)
        form.addLayout(nav)
        for b in (self.webcam, self.video, self.coco):
            self.group.addButton(b)
        lay.addWidget(src)

        det = QGroupBox("Detector")
        df = QFormLayout(det)
        size_row = QHBoxLayout()
        self.s640 = QRadioButton("640 px")
        self.s320 = QRadioButton("320 px")
        size_row.addWidget(self.s640)
        size_row.addWidget(self.s320)
        df.addRow("YOLOv8n input", size_row)
        self.conf = QSlider(Qt.Orientation.Horizontal)
        self.conf.setRange(5, 90)
        self.conf_label = QLabel()
        df.addRow("Confidence", self.conf)
        df.addRow("", self.conf_label)
        lay.addWidget(det)

        run = QGroupBox("Run")
        rl = QVBoxLayout(run)
        self.pause = QPushButton("Pause  (space)")
        self.pause.setCheckable(True)
        self.step = QPushButton("Replay NMS step  (n)")
        self.clear = QPushButton("Clear stats")
        for b in (self.pause, self.step, self.clear):
            rl.addWidget(b)
        lay.addWidget(run)

        board = QGroupBox("Board")
        bl = QVBoxLayout(board)
        self.board_label = QLabel()
        self.board_label.setWordWrap(True)
        bl.addWidget(self.board_label)
        lay.addWidget(board)
        lay.addStretch(1)

        # initial state, then wire the signals
        kind = s.source.split(":")[0]
        {"webcam": self.webcam, "video": self.video, "coco": self.coco}[
            kind
        ].setChecked(True)
        if kind == "webcam":
            self.cam_index.setValue(int(s.source.split(":")[1]))
        (self.s640 if s.imgsz == 640 else self.s320).setChecked(True)
        self.conf.setValue(round(s.conf * 100))
        self._conf_text()
        self._video_text()
        self.group.buttonClicked.connect(self._changed)
        self.cam_index.valueChanged.connect(self._changed)
        self.s640.toggled.connect(self._changed)
        self.conf.valueChanged.connect(self._conf_text)
        self.conf.sliderReleased.connect(self._changed)
        self.choose.clicked.connect(self._choose)
        self.prev.clicked.connect(lambda: window.worker.step_source(-1))
        self.next.clicked.connect(lambda: window.worker.step_source(+1))
        self.pause.toggled.connect(window.set_paused)
        self.step.clicked.connect(window.step_replay)
        self.clear.clicked.connect(window.clear_stats)

    def _conf_text(self) -> None:
        self.conf_label.setText(
            f"{self.conf.value() / 100:.2f}  (boxes above this reach NMS)"
        )

    def _video_text(self) -> None:
        names = ", ".join(path.name for path in self.w.video_paths) or "none chosen"
        self.video_label.setText(f"files: {names}")

    def _choose(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Video files", str(Path.cwd()), "Video (*.mp4 *.avi *.mov *.mkv)"
        )
        if paths:
            self.w.video_paths[:] = [Path(x) for x in paths]
            self._video_text()
            self.video.setChecked(True)
            self.w.video_generation += 1
            self._changed()

    def _changed(self) -> None:
        if self.webcam.isChecked():
            source = f"webcam:{self.cam_index.value()}"
        elif self.video.isChecked():
            if not self.w.video_paths:
                self._choose()
                return
            source = f"video:{self.w.video_generation}"
        else:
            source = "coco"
        self.w.apply(
            replace(
                self.w.settings,
                source=source,
                imgsz=640 if self.s640.isChecked() else 320,
                conf=self.conf.value() / 100,
            ),
        )


class MainWindow(QMainWindow):
    """The demo window."""

    def __init__(
        self,
        worker: Worker,
        video_paths: list[Path],
        *,
        record: Path | None = None,
        save: Path | None = None,
        frames: int | None = None,
        on_record: Callable[[FrameRecord], None] | None = None,
    ) -> None:
        """Lay out the window and connect it to the worker.

        Args:
            worker: The work loop, not yet started.
            video_paths: Files for the video source; the settings panel can change them.
            record: Append every completed record here as JSON lines.
            save: Record the window to this video file.
            frames: Close after this many frames.
            on_record: Also called with every completed record (the run summary).
        """
        super().__init__()
        self.worker = worker
        self.settings = worker.settings
        self.video_paths = video_paths
        self.video_generation = 0
        self.frames = frames
        self.on_record = on_record
        self.current: FrameResult | None = None
        self.pending: FrameRecord | None = None
        self.closing = False
        self.steps: list[draw.Step] = []
        self.step_i: int | None = None
        self.shown = 0
        self.record_file = record.open("a") if record else None
        self.save = save
        self.writer: cv2.VideoWriter | None = None
        self.totals = {"agree": 0, "replies": 0, "errors": 0, "no_reply": 0}
        self.setWindowTitle("NMS on the Basys 3: live")

        central = QWidget()
        col = QVBoxLayout(central)
        panels = QHBoxLayout()
        self.left, self.right = QLabel(), QLabel()
        self.titles = {}
        for lbl in (self.left, self.right):
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
            lbl.setMinimumSize(draw.PANEL_W, 480)
            title = QLabel()
            title.setWordWrap(True)
            title.setStyleSheet("font-size:13pt; font-weight:600;")
            box = QVBoxLayout()
            box.addWidget(title)
            box.addWidget(lbl, 1)
            panels.addLayout(box)
            self.titles[id(lbl)] = title
        col.addLayout(panels, 3)
        self.bar = StageBar()
        col.addWidget(self.bar)
        self.dash = Dashboard(self.settings.board)
        self.dash.setMinimumHeight(230)
        col.addWidget(self.dash, 1)
        self.setCentralWidget(central)

        self.panel = SettingsPanel(self)
        dock = QDockWidget("Settings")
        dock.setWidget(self.panel)
        dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self.status = QLabel()
        self.message = QLabel()
        self.statusBar().addWidget(self.status, 1)
        self.statusBar().addPermanentWidget(self.message)

        QShortcut(QKeySequence(Qt.Key.Key_Space), self, self.panel.pause.toggle)
        QShortcut(QKeySequence(Qt.Key.Key_N), self, self.step_replay)
        QShortcut(QKeySequence(Qt.Key.Key_Q), self, self.close)

        worker.frame_ready.connect(self.on_frame)
        worker.message.connect(self.message.setText)
        self._status_text(None)

    # --- settings -------------------------------------------------------------------

    def apply(self, settings: Settings) -> None:
        """Send new settings to the worker; they apply from its next frame."""
        self.settings = settings
        self.worker.set_settings(settings)

    def set_paused(self, paused: bool) -> None:
        """Pause or resume the worker; resuming leaves the replay."""
        self.worker.set_paused(paused)
        self.panel.pause.setText("Resume  (space)" if paused else "Pause  (space)")
        if not paused:
            self.step_i = None
        self._show_panels()

    def step_replay(self) -> None:
        """Pause and show the next step of the golden-model replay of this frame."""
        if self.current is None:
            return
        if not self.panel.pause.isChecked():
            self.panel.pause.setChecked(True)
        if not self.steps:
            return
        self.step_i = 0 if self.step_i is None else (self.step_i + 1) % len(self.steps)
        self._show_panels()

    def clear_stats(self) -> None:
        """Forget the dashboard's samples and the status totals (not the record file)."""
        self.dash.clear()
        self.totals = dict.fromkeys(self.totals, 0)
        self._status_text(None)

    # --- frames ---------------------------------------------------------------------

    def _show_panels(self) -> None:
        if self.current is None:
            return
        box = (self.left.width(), self.left.height())
        frame = draw.frame_of(self.current, box)
        left = draw.left_panel(frame)
        if self.step_i is not None and self.steps:
            right = draw.step_panel(
                frame, self.steps[self.step_i], len(self.steps), self.step_i
            )
        else:
            right = draw.right_panel(frame)
        for label, panel in ((self.left, left), (self.right, right)):
            label.setPixmap(QPixmap.fromImage(to_qimage(panel.image)))
            legend = (
                f"<br><span style='font-size:10pt; font-weight:400;'>{panel.legend}</span>"
                if panel.legend
                else ""
            )
            self.titles[id(label)].setText(panel.title + legend)

    def on_frame(self, result: FrameResult) -> None:
        """Paint a new frame, complete its record, and release the worker.

        The dashboard shows the previous frame's record, the newest whose paint time is
        known, so updating it is part of this frame's measured paint.
        """
        if self.closing:
            return  # a frame the worker emitted before it was told to stop
        t0 = time.perf_counter()
        if self.pending is not None:
            self._show_record(self.pending)
        self.current = result
        self.steps = draw.steps_of(draw.frame_of(result))
        self.step_i = None
        self._show_panels()
        self.repaint()
        if self.save:
            self._save_frame()
        rec = with_paint(result.record, t0, time.perf_counter())
        self.pending = rec
        if self.record_file:
            self.record_file.write(rec.to_json() + "\n")
            self.record_file.flush()
        if self.on_record:
            self.on_record(rec)
        self.shown += 1
        if self.frames is not None and self.shown >= self.frames:
            self.close()
            return
        self.worker.painted.set()

    def _show_record(self, rec: FrameRecord) -> None:
        """Feed a completed record to the dashboard and the status line."""
        self.bar.show_record(rec)
        self.dash.add(rec, self.current.names if self.current else {})
        for b in rec.batches:
            if b.status is None:
                self.totals["no_reply"] += 1
            elif b.status != p.STATUS_OK:
                self.totals["errors"] += 1
            else:
                self.totals["replies"] += 1
                self.totals["agree"] += b.agrees
        self._status_text(rec)

    def _status_text(self, rec: FrameRecord | None) -> None:
        t = self.totals
        if self.settings.board == BOARD_MODEL:
            check = "No board (--no-board): results are the golden model's own"
        else:
            check = f"Board result = golden model: {t['agree']}/{t['replies']} batches"
            if t["errors"]:
                check += f", {t['errors']} error replies"
            if t["no_reply"]:
                check += f", {t['no_reply']} sent while offline"
        if rec is None:
            board = ""
        elif rec.board_state == OFFLINE:
            board = f"board OFFLINE ({self.worker.pipeline.board.error}); retrying every 2 s"
        elif rec.board_state == BOARD_MODEL:
            board = "golden model"
        else:
            board = f"board connected on {self.worker.pipeline.board.port}"
        where = f"frame #{rec.index}: {rec.source_name}" if rec else ""
        self.status.setText(f"{check}   |   {board}   |   IoU {IOU:.1f}   |   {where}")
        self.panel.board_label.setText(board or "waiting for the first frame")

    def _save_frame(self) -> None:
        img = self.grab().toImage().convertToFormat(QImage.Format.Format_RGB888)
        w, h = img.width(), img.height()
        arr = np.frombuffer(img.constBits(), np.uint8).reshape(h, img.bytesPerLine())
        bgr = cv2.cvtColor(arr[:, : w * 3].reshape(h, w, 3), cv2.COLOR_RGB2BGR)
        if self.writer is None:
            self.size = (w, h)
            self.writer = cv2.VideoWriter(
                str(self.save), cv2.VideoWriter_fourcc(*"mp4v"), 5, self.size
            )
        if (w, h) != self.size:
            bgr = cv2.resize(bgr, self.size)
        self.writer.write(bgr)

    def closeEvent(self, event: object) -> None:
        """Stop the worker and close the files."""
        self.closing = True
        self.worker.stop()
        if self.record_file:
            self.record_file.close()
        if self.writer is not None:
            self.writer.release()
        super().closeEvent(event)
