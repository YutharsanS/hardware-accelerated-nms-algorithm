"""Tests for the frame records, the audit, and the COCO selection (no extras needed)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from demo import audit, coco
from demo.records import (
    BOARD_FPGA,
    CONNECTED,
    BatchRecord,
    FrameRecord,
    Settings,
    read_jsonl,
)
from models.nms import model
from models.nms import params as p

BOXES = [[10, 10, 50, 50, 60000], [12, 12, 52, 52, 50000], [200, 200, 260, 260, 40000]]


def a_record(keep_mask: int | None, status: int | None = p.STATUS_OK) -> FrameRecord:
    batch = BatchRecord(cls=0, boxes=BOXES, present_mask=0b111)
    golden = model.nms_sequential(batch.model_boxes(), batch.present_mask)
    batch.status, batch.seq, batch.keep_mask = status, 5, keep_mask
    batch.golden_mask = golden
    batch.software_us = {"opencv": 9.5}
    return FrameRecord(
        index=3,
        settings=Settings("webcam:0", 640, 0.25, BOARD_FPGA),
        source_name="webcam 0",
        t_capture=1.0,
        t={"read": (1.0, 1.01), "detect": (1.01, 1.2), "paint": (1.25, 1.26)},
        dropped=2,
        source_fps=30.0,
        candidates=3,
        over_cap=0,
        board_state=CONNECTED,
        batches=[batch],
    )


def test_golden_answer_for_the_fixture() -> None:
    # boxes 0 and 1 overlap heavily, box 2 is apart: 0 and 2 survive
    assert model.nms_sequential(a_record(0).batches[0].model_boxes(), 0b111) == 0b101


def test_jsonl_round_trip(tmp_path: Path) -> None:
    rec = a_record(0b101)
    path = tmp_path / "run.jsonl"
    path.write_text(rec.to_json() + "\n" + rec.to_json() + "\n")
    back = list(read_jsonl(path))
    assert back == [rec, rec]
    assert back[0].capture_to_screen_ms() == (1.26 - 1.0) * 1e3
    assert back[0].ms("detect") == (1.2 - 1.01) * 1e3
    assert back[0].ms("board") is None
    assert back[0].batches[0].agrees


def test_audit_passes_a_true_run_and_catches_a_tampered_mask() -> None:
    good = audit.audit([a_record(0b101)])
    assert (good.equal, good.mismatched) == (1, 0)
    bad = audit.audit([a_record(0b111)])
    assert (bad.equal, bad.mismatched) == (0, 1)
    assert "0x00000007" in bad.mismatches[0]


def test_audit_counts_offline_and_error_replies_apart() -> None:
    t = audit.audit([a_record(None, None), a_record(0, p.STATUS_CRC_FAIL)])
    assert (t.no_reply, t.failed, t.equal, t.mismatched) == (1, 1, 0, 0)


def test_audit_cli_exit_status(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    path.write_text(a_record(0b101).to_json() + "\n")
    assert audit.main([str(path)]) == 0
    path.write_text(a_record(0b001).to_json() + "\n")
    assert audit.main([str(path)]) == 1
    assert audit.main([str(tmp_path / "missing.jsonl")]) == 2


def test_settings_are_part_of_each_record() -> None:
    a = a_record(0b101)
    b = replace(a, settings=replace(a.settings, imgsz=320))
    assert json.loads(b.to_json())["settings"]["imgsz"] == 320
    assert a.settings != b.settings


def test_coco_selection_rule() -> None:
    names = coco.select()
    rows = {
        r["image"]: max(r["cls@0.25"], default=0)
        for r in json.loads(coco.COUNTS.read_text())
    }
    busiest = [rows[n] for n in names]
    lo, hi = coco.FULL_RANGE
    assert len(names) == 24
    assert all(lo <= n <= hi for n in busiest[: -coco.OVER_CAP])
    assert all(n > hi for n in busiest[-coco.OVER_CAP :])
    assert busiest[: -coco.OVER_CAP] == sorted(busiest[: -coco.OVER_CAP], reverse=True)


def test_coco_fetch_reports_failures_instead_of_raising(tmp_path: Path) -> None:
    (tmp_path / "cached.jpg").write_bytes(b"x")
    real_url = coco.URL
    coco.URL = "http://127.0.0.1:9/{name}"  # nothing listens on the discard port
    try:
        paths, errors = coco.fetch(["cached.jpg", "absent.jpg"], tmp_path)
    finally:
        coco.URL = real_url
    assert paths == [tmp_path / "cached.jpg"]
    assert len(errors) == 1
    assert errors[0].startswith("absent.jpg")
