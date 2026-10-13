"""The window builds and shows frames, offscreen; skipped without the demo extra."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("cv2")
pytest.importorskip("pyqtgraph")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from demo.app import MainWindow
from demo.pipeline import GoldenBoard, Pipeline
from demo.records import BOARD_MODEL, Settings
from demo.test_pipeline import a_grab, fake_detect
from demo.worker import Worker


@pytest.fixture(scope="module")
def app() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_window_shows_frames_records_and_the_replay(
    app: QtWidgets.QApplication,
) -> None:
    settings = Settings("webcam:0", 640, 0.25, BOARD_MODEL)
    pipe = Pipeline(fake_detect, {0: "a", 1: "b"}, GoldenBoard(), software=None)
    worker = Worker(pipe, lambda _spec: None, settings)  # never started
    seen = []
    win = MainWindow(worker, [], on_record=seen.append)
    win.show()
    for i in range(3):
        result = pipe.step(a_grab(), (0.0, 0.0), settings, i, None)
        win.on_frame(result)
        app.processEvents()

    assert [r.index for r in seen] == [0, 1, 2]
    assert all("paint" in r.t for r in seen)
    # the dashboard shows the previous completed frame, whose paint time is known
    assert win.bar.rec is seen[1]
    assert "golden model (no board)" in win.titles[id(win.right)].text()
    assert "golden model's own" in win.status.text()

    win.step_replay()  # pauses and shows the first step of the software replay
    assert win.panel.pause.isChecked()
    assert win.titles[id(win.right)].text().startswith("Software replay (golden model)")
    win.panel.pause.setChecked(False)
    assert win.step_i is None

    win.panel.s320.setChecked(True)
    assert win.settings.imgsz == 320
    win.clear_stats()
    assert win.totals["replies"] == 0
    win.closing = True  # skip worker.stop(): the worker was never started
    win.close()
