"""The parts sweep's bookkeeping, on made-up Vivado summaries: no Vivado needed."""

from __future__ import annotations

import pytest

from benchmarks import fpga_parts as fp


def _summary(**over: object) -> dict:
    base = {
        "module": "nms_core",
        "part": "xc7a35tcpg236-1",
        "speed": "-1",
        "generics": "P=16",
        "period_ns": 10.0,
        "lut": 12384,
        "ff": 9616,
        "dsp": 33,
        "bram": 0,
        "lut_avail": 20800,
        "ff_avail": 41600,
        "dsp_avail": 90,
        "wns_ns": 0.2,
    }
    return {**base, **over}


def test_fmax_is_the_routed_critical_path() -> None:
    run = fp.Run("xc7a35tcpg236-1", 16, 10.0, _summary(wns_ns=0.2))
    assert run.fmax_mhz == pytest.approx(1000 / 9.8)
    # a failing constraint still reports the path it routed
    assert fp.Run("p", 16, 8.0, _summary(wns_ns=-0.5)).fmax_mhz == pytest.approx(
        1000 / 8.5
    )
    assert fp.Run("p", 16, 10.0, None, "ERROR: does not fit").fmax_mhz is None


def test_row_takes_the_faster_pass_and_converts_t() -> None:
    first = fp.Run("xc7a35tcpg236-1", 16, 10.0, _summary(wns_ns=0.2))
    second = fp.Run("xc7a35tcpg236-1", 16, 9.5, _summary(wns_ns=-0.1, lut=12400))
    r = fp.row("xc7a35tcpg236-1", "Basys 3", 16, (first, second))
    assert r["fmax_mhz"] == pytest.approx(1000 / 9.6, abs=0.05)
    assert r["lut"] == 12400  # area comes from the pass that set Fmax
    assert r["t_cycles"] == 80
    assert r["full_cycles"] == 113
    assert r["t_ns"] == pytest.approx(80 * 9.6, abs=0.1)
    assert "not run on silicon" in r["status"]


def test_p32_latency() -> None:
    first = fp.Run("xc7k325tffg900-2", 32, 10.0, _summary(wns_ns=5.0, dsp_avail=840))
    r = fp.row("xc7k325tffg900-2", "Kintex", 32, (first, None))
    assert (r["t_cycles"], r["full_cycles"]) == (48, 81)


def test_failed_run_is_reported_not_raised() -> None:
    failed = fp.Run(
        "xc7a35tcpg236-1", 32, 10.0, None, "ERROR: [Place 30-640] overutilised"
    )
    r = fp.row("xc7a35tcpg236-1", "Basys 3", 32, (failed, None))
    assert r["status"].startswith("failed: ERROR: [Place 30-640]")
    assert "overutilised" in fp.markdown([r])


def test_markdown_table_shape() -> None:
    first = fp.Run("xc7a35tcpg236-1", 16, 10.0, _summary())
    text = fp.markdown([fp.row("xc7a35tcpg236-1", "Basys 3", 16, (first, None))])
    header, rule, body = text.splitlines()
    assert header.count("|") == rule.count("|") == body.count("|")
    assert "113 cyc" in body
