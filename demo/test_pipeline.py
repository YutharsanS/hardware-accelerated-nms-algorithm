"""Tests for the sources and the pipeline, with fakes for the camera, detector and board."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from demo import sources
from demo.pipeline import BoardLink, GoldenBoard, Pipeline, with_paint
from demo.records import (
    BOARD_FPGA,
    BOARD_MODEL,
    CONNECTED,
    OFFLINE,
    STAGES,
    Settings,
)
from models.nms import host, model, wire
from models.nms import params as p

# --- sources ------------------------------------------------------------------------


class FakeCamera:
    """Delivers numbered frames as fast as it is read, like a driver with a buffer."""

    def __init__(self, _index: int) -> None:
        self.n = 0
        self.lock = threading.Lock()

    def read(self) -> tuple[bool, np.ndarray]:
        time.sleep(0.002)
        with self.lock:
            self.n += 1
            return True, np.full((4, 4, 3), self.n % 256, np.uint8)

    def get(self, _prop: int) -> float:
        return 30.0

    def release(self) -> None:
        pass


def test_webcam_returns_the_newest_frame_and_counts_the_rest() -> None:
    cam = sources.Webcam(0, open_capture=FakeCamera)
    try:
        first = cam.read()
        time.sleep(0.1)  # the camera keeps delivering while "the detector runs"
        grab = cam.read()
    finally:
        cam.close()
    assert first is not None and grab is not None
    assert grab.dropped > 5
    assert grab.image[0, 0, 0] > first.image[0, 0, 0]
    assert cam.fps == 30.0
    assert grab.name == "webcam 0"


def test_webcam_that_cannot_open_raises() -> None:
    class Dead(FakeCamera):
        def read(self) -> tuple[bool, None]:
            return False, None

    with pytest.raises(OSError, match="cannot open webcam"):
        sources.Webcam(3, open_capture=Dead)


def write_clip(path: Path, frames: int, value: int) -> None:
    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48))
    for _ in range(frames):
        out.write(np.full((48, 64, 3), value, np.uint8))
    out.release()


def test_video_files_loop_and_step(tmp_path: Path) -> None:
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    write_clip(a, 3, 40)
    write_clip(b, 3, 200)
    vid = sources.VideoFiles([a, b])
    names = [vid.read().name for _ in range(5)]  # past the end of a: it rewinds
    assert names == ["a.mp4"] * 5
    assert vid.fps == 10.0
    vid.next()
    grab = vid.read()
    assert grab.name == "b.mp4"
    assert grab.dropped == 0
    assert abs(int(grab.image.mean()) - 200) < 10
    vid.prev()
    assert vid.read().name == "a.mp4"
    vid.close()


def test_image_set_advances_on_its_clock(tmp_path: Path) -> None:
    for i in range(3):
        cv2.imwrite(str(tmp_path / f"{i}.png"), np.full((8, 8, 3), i * 50, np.uint8))
    now = [0.0]
    imgs = sources.ImageSet(
        sorted(tmp_path.glob("*.png")), seconds=5.0, clock=lambda: now[0]
    )
    assert imgs.read().name == "0.png (1/3)"
    now[0] = 4.9
    assert imgs.read().name == "0.png (1/3)"
    now[0] = 5.0
    assert imgs.read().name == "1.png (2/3)"
    imgs.prev()
    assert imgs.read().name == "0.png (1/3)"
    assert imgs.fps is None


# --- pipeline -----------------------------------------------------------------------


def fake_detect(image: np.ndarray, imgsz: int) -> tuple[np.ndarray, np.ndarray]:
    """Two overlapping class-0 boxes and one class-1 box, as raw head output."""
    xyxy = np.array([[10, 10, 50, 50], [12, 12, 52, 52], [100, 100, 140, 150.0]])
    scores = np.array([[0.9, 0.0], [0.8, 0.0], [0.0, 0.7]])
    return xyxy, scores


def a_grab() -> sources.Grab:
    return sources.Grab(np.zeros((200, 200, 3), np.uint8), time.perf_counter(), 1, "x")


def step(board: object, settings: Settings) -> object:
    pipe = Pipeline(fake_detect, {0: "a", 1: "b"}, board, software=None)
    t = time.perf_counter()
    return pipe.step(a_grab(), (t, t), settings, 7, 25.0).record


def test_model_run_is_recorded_as_the_model() -> None:
    rec = step(GoldenBoard(), Settings("webcam:0", 640, 0.25, BOARD_MODEL))
    assert rec.board_state == BOARD_MODEL
    assert [b.cls for b in rec.batches] == [0, 1]
    assert rec.batches[0].keep_mask == rec.batches[0].golden_mask == 0b01
    assert all(b.seq is None for b in rec.batches)  # the model has no wire
    assert rec.candidates == 3
    assert rec.dropped == 1
    assert rec.source_fps == 25.0


def test_stage_timestamps_are_ordered() -> None:
    rec = step(GoldenBoard(), Settings("webcam:0", 640, 0.25, BOARD_MODEL))
    rec = with_paint(rec, time.perf_counter(), time.perf_counter())
    order = [s for s in STAGES if s in rec.t]
    assert order == ["read", "detect", "batch", "board", "check", "paint"]
    ends = [rec.t[s][1] for s in order]
    assert ends == sorted(ends)
    assert rec.capture_to_screen_ms() > 0


class FakeBoard:
    """A board that answers like the real one, or fails on demand."""

    fail = False

    def __init__(self, _port: str) -> None:
        if FakeBoard.fail:
            msg = "cannot open /dev/fake"
            raise host.PortError(msg)
        self.seq = 0

    def transact_many(self, batches: list) -> list[wire.Reply]:
        if FakeBoard.fail:
            msg = "only 0 of 6 reply bytes"
            raise host.ReplyTimeout(msg)
        out = []
        for boxes, mask in batches:
            out.append(
                wire.Reply(p.STATUS_OK, self.seq, model.nms_sequential(boxes, mask))
            )
            self.seq += 1
        return out

    def __exit__(self, *_: object) -> None:
        pass


def test_offline_board_gives_no_result_never_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host, "set_latency_timer", lambda _port: (None, ""))
    now = [0.0]
    FakeBoard.fail = False
    link = BoardLink("/dev/fake", open_board=FakeBoard, clock=lambda: now[0])
    settings = Settings("webcam:0", 640, 0.25, BOARD_FPGA)

    rec = step(link, settings)
    assert rec.board_state == CONNECTED
    assert [b.seq for b in rec.batches] == [0, 1]
    assert all(b.agrees for b in rec.batches)

    FakeBoard.fail = True  # unplugged mid-run
    rec = step(link, settings)
    assert rec.board_state == OFFLINE
    assert all(b.status is None and b.keep_mask is None for b in rec.batches)
    assert all(b.golden_mask is not None for b in rec.batches)  # the check still ran
    assert "reply bytes" in link.error

    FakeBoard.fail = False
    now[0] = 1.0  # before the retry interval: still offline
    assert step(link, settings).board_state == OFFLINE
    now[0] = 2.5  # after it: reconnected
    assert step(link, settings).board_state == CONNECTED
