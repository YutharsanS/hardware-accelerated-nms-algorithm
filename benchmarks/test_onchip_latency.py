"""The ILA capture checker, on synthetic captures in Vivado's CSV layout."""

from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks import onchip_latency as ila
from models.nms import params as p

T = p.latency_cycles()
PRETRIG = 8
DEPTH = 128


def _window(t: int) -> list[dict[str, int]]:
    """One window as the ILA would see it: start at the trigger, done T samples later."""
    rows = []
    for k in range(DEPTH):
        rows.append(
            {
                "start": int(k == PRETRIG),
                "busy": int(PRETRIG < k <= PRETRIG + t),
                "done": int(k == PRETRIG + t),
                "settled": 1,
                "we": 0,
            }
        )
    return rows


def _write(
    path: Path, windows: list[list[dict[str, int]]], *, prefix: str = ""
) -> Path:
    names = ["start", "done", "busy", "settled", "we"]
    lines = [
        ",".join(
            [
                "Sample in Buffer",
                "Sample in Window",
                "TRIGGER",
                *[f"{prefix}{n}" for n in names],
            ]
        ),
        ",".join(["Radix - UNSIGNED", "UNSIGNED", "UNSIGNED", *["HEX"] * len(names)]),
    ]
    buf = 0
    for w in windows:
        for k, row in enumerate(w):
            trig = int(k == PRETRIG)
            lines.append(",".join(map(str, [buf, k, trig, *[row[n] for n in names]])))
            buf += 1
    path.write_text("\n".join(lines) + "\n")
    return path


def test_every_window_at_t_passes(tmp_path: Path) -> None:
    csv = _write(tmp_path / "ila.csv", [_window(T) for _ in range(16)])
    windows = ila.read_capture(csv)
    assert len(windows) == 16
    ok, lines = ila.check(windows)
    assert ok
    assert all(f"= {T} cycles" in line for line in lines)
    assert windows[0].busy_cycles() == T


def test_one_slow_window_fails(tmp_path: Path) -> None:
    csv = _write(tmp_path / "ila.csv", [_window(T), _window(T + 1), _window(T)])
    ok, lines = ila.check(ila.read_capture(csv))
    assert not ok
    assert "MISMATCH" in lines[1]


def test_hierarchical_probe_names_are_found(tmp_path: Path) -> None:
    csv = _write(tmp_path / "ila.csv", [_window(T)], prefix="u_ila/")
    assert ila.check(ila.read_capture(csv))[0]


def test_probe_numbers_stand_in_for_names(tmp_path: Path) -> None:
    csv = _write(tmp_path / "ila.csv", [_window(T)])
    text = csv.read_text().splitlines()
    for k, name in enumerate(ila.SIGNALS):
        text[0] = text[0].replace(f",{name}", f",u_ila/probe{k}[0:0]")
    csv.write_text("\n".join(text) + "\n")
    assert ila.check(ila.read_capture(csv))[0]


def test_empty_capture_fails(tmp_path: Path) -> None:
    csv = _write(tmp_path / "ila.csv", [])
    assert ila.check(ila.read_capture(csv)) == (False, [])


def test_main_exit_status(tmp_path: Path) -> None:
    good = _write(tmp_path / "good.csv", [_window(T)] * 4)
    bad = _write(tmp_path / "bad.csv", [_window(T - 1)])
    assert ila.main(["--csv", str(good)]) == 0
    assert ila.main(["--csv", str(bad)]) == 1


def test_missing_probe_is_named(tmp_path: Path) -> None:
    path = tmp_path / "ila.csv"
    path.write_text("Sample in Buffer,Sample in Window,TRIGGER,start\n0,0,0,1\n")
    with pytest.raises(ValueError, match="no 'done' column"):
        ila.read_capture(path)
