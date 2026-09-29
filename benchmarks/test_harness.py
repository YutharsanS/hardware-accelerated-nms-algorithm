"""The benchmark harness: its references, its C variant, its targets and its files.

Nothing here times anything that matters. The tests check that every implementation is
checked against the right reference, that the C variant is bit-exact, and that a run
writes results the report can read. Tests needing the ``bench`` extra or a C compiler skip
when it's absent.
"""

from __future__ import annotations

import csv
import gzip
import json
import os
import random
import shutil
from pathlib import Path

import pytest

from benchmarks import __main__ as cli
from benchmarks import inputs, meta, report
from benchmarks.targets import cpu, fpga
from models.nms import batches, bench, host, model, wire
from models.nms import params as p


@pytest.fixture(scope="module")
def groups() -> dict[str, list[inputs.Batch]]:
    return inputs.suite(hostile_count=200)


@pytest.fixture(scope="module")
def c_lib(tmp_path_factory: pytest.TempPathFactory) -> object:
    if not (shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")):
        pytest.skip("no C compiler")
    return cpu.load_c_library(cpu.build_c_library(tmp_path_factory.mktemp("c")))


# --- references -------------------------------------------------------------------------


def test_suite_has_the_e3_groups(groups: dict[str, list[inputs.Batch]]) -> None:
    assert list(groups) == [*inputs.SUITE_CASES, inputs.HOSTILE]
    assert len(groups[inputs.HOSTILE]) == 200
    for group in groups.values():
        for b in group:
            assert b.keep_mask == model.nms_sequential(b.boxes, b.present_mask)


def test_strict_differs_from_the_spec_only_on_threshold(
    groups: dict[str, list[inputs.Batch]],
) -> None:
    """``>`` and ``>=`` can only disagree where some pair has IoU exactly 0.5."""
    differ = [b for g in groups.values() for b in g if b.keep_strict != b.keep_mask]
    for b in differ:
        n = len(b.boxes)
        assert any(
            (model.intersection_area(b.boxes[i], b.boxes[j]) << p.K_SHIFT)
            == p.T_INT
            * (
                model.box_area(b.boxes[i])
                + model.box_area(b.boxes[j])
                - model.intersection_area(b.boxes[i], b.boxes[j])
            )
            for i in range(n)
            for j in range(i + 1, n)
        )


def test_boundary_case_separates_the_predicates() -> None:
    """The committed boundary case is exactly where the libraries' ``>`` differs."""
    b = inputs.make_batch(batches.named_cases()["boundary"])
    assert b.keep_strict != b.keep_mask
    assert inputs.explain_mismatch(b) in ("ties", "on-threshold")


def test_explain_mismatch_names_ties() -> None:
    b = inputs.make_batch(batches.case_all_equal())
    assert inputs.explain_mismatch(b) == "ties"


# --- implementations --------------------------------------------------------------------


@pytest.mark.parametrize("impl", cpu.golden_impls(), ids=lambda i: i.name)
def test_golden_impls_agree(
    impl: cpu.Impl, groups: dict[str, list[inputs.Batch]]
) -> None:
    for group in groups.values():
        assert cli.check_agreement(impl, group) == ("ok", {})


def test_c_matches_the_golden_model(
    c_lib: object, groups: dict[str, list[inputs.Batch]]
) -> None:
    impl = cpu.c_impl(c_lib)
    for group in groups.values():
        assert cli.check_agreement(impl, group) == ("ok", {})
    assert int(impl.notes["timer_overhead_ns"]) >= 0


def test_c_honours_present_mask(c_lib: object) -> None:
    impl = cpu.c_impl(c_lib)
    rng = random.Random(7)
    for boxes in batches.hostile_stream(200, seed=3):
        mask = rng.getrandbits(p.N)
        b = inputs.make_batch(boxes, mask)
        keep, us = impl.timed(impl.prepare(b))
        assert keep == b.keep_mask
        assert keep & ~mask == 0
        assert us >= 0


@pytest.mark.parametrize("name", ["torchvision", "opencv"])
def test_libraries_agree_or_explain(
    name: str, groups: dict[str, list[inputs.Batch]]
) -> None:
    factory = {"torchvision": cpu.torchvision_impl, "opencv": cpu.opencv_impl}[name]
    impl = factory()
    if impl is None:
        pytest.skip(f"{name} not installed (uv run --extra bench)")
    for group in groups.values():
        verdict, _ = cli.check_agreement(impl, group)
        assert verdict in ("ok", "explained")


def test_unknown_impl_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown"):
        cpu.all_impls(["no_such_impl"])


# --- timing helpers ---------------------------------------------------------------------


def test_summarise_percentiles() -> None:
    s = bench.summarise([float(v) for v in range(1, 101)])
    assert (s["min"], s["median"], s["max"]) == (1.0, 50.5, 100.0)
    assert s["p99"] == 99.0


def test_time_samples_respects_bounds() -> None:
    few = bench.time_samples(
        lambda k: float(k), budget_s=0.0, min_samples=7, max_samples=50
    )
    assert few == [float(k) for k in range(7)]
    capped = bench.time_samples(
        lambda k: 1.0, budget_s=10.0, min_samples=1, max_samples=25
    )
    assert len(capped) == 25


def test_throttled_parsing() -> None:
    assert not meta.throttled({"vc_throttled": "throttled=0x0"})
    assert meta.throttled({"vc_throttled": "throttled=0x50005"})
    assert not meta.throttled({"vc_throttled": ""})


# --- the fpga target, against a fake board ----------------------------------------------


class FakeBoard:
    """Answers like the board: the golden model's keep_mask, or a wrong one on request."""

    def __init__(self, *, wrong_after: int | None = None) -> None:
        self.calls = 0
        self.wrong_after = wrong_after

    def transact(self, boxes: list[model.Box], present_mask: int) -> wire.Reply:
        self.calls += 1
        keep = model.nms_sequential(boxes, present_mask)
        if self.wrong_after is not None and self.calls > self.wrong_after:
            keep ^= 1
        return wire.Reply(p.STATUS_OK, 0, keep)


def test_fpga_measure_checks_every_reply(groups: dict[str, list[inputs.Batch]]) -> None:
    board = FakeBoard()
    samples = fpga.measure(board, groups[inputs.HOSTILE], 30)  # type: ignore[arg-type]
    assert len(samples) == 30
    assert board.calls == 30
    with pytest.raises(host.LatencyError, match="wrong keep_mask"):
        fpga.measure(FakeBoard(wrong_after=5), groups[inputs.HOSTILE], 30)  # type: ignore[arg-type]


def test_run_latency_still_reports(capsys: pytest.CaptureFixture[str]) -> None:
    assert host.run_latency(FakeBoard(), 5, 1)  # type: ignore[arg-type]
    assert "latency: 5 round trips, latency timer 1 ms" in capsys.readouterr().out
    assert not host.run_latency(FakeBoard(wrong_after=2), 5, 1)  # type: ignore[arg-type]
    assert "latency: FAIL -- wrong keep_mask" in capsys.readouterr().out


def test_core_row_is_the_frozen_latency() -> None:
    row = fpga.core_row()
    assert row["cycles"] == p.latency_cycles() == 80
    assert row["full_cycles"] == fpga.full_latency_cycles() == 113
    assert row["full_us"] == pytest.approx(1.13)


# --- a whole run, and the report that reads it ------------------------------------------


def test_cpu_run_writes_results_the_report_reads(tmp_path: Path) -> None:
    argv = [
        "--target", "cpu",
        "--impls", "integer_sequential", "numpy_allpairs",
        "--hostile-count", "20",
        "--budget", "0.0",
        "--reps", "40",
        "--out", str(tmp_path),
        "--tag", "test",
    ]  # fmt: skip
    affinity = os.sched_getaffinity(0)
    try:
        assert cli.main(argv) == 0
    finally:
        os.sched_setaffinity(0, affinity)  # main pins the process to one CPU
    csv_path = next(tmp_path.glob("cpu-*-none-test.csv"))
    rows = list(csv.DictReader(csv_path.open()))
    assert {r["impl"] for r in rows} == {"integer_sequential", "numpy_allpairs"}
    assert {r["case"] for r in rows} == {*inputs.SUITE_CASES, inputs.HOSTILE}
    assert all(r["agreement"] == "ok" and r["valid"] == "True" for r in rows)
    info = json.loads(csv_path.with_suffix(".json").read_text())
    assert info["target"] == "cpu"
    assert info["load"] == "none"
    assert info["valid"] is True
    with gzip.open(tmp_path / f"{csv_path.stem}-samples.csv.gz", "rt") as f:
        assert sum(1 for _ in f) > len(rows)
    text = report.render(report.load_runs(tmp_path))
    assert "| integer_sequential |" in text
    assert "hostile" in text


def test_fpga_rejects_a_load() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["--target", "fpga", "--load", "stress"])
