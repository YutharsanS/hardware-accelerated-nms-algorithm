"""Benchmark NMS against its competitors: ``python -m benchmarks`` (``make bench``).

``--target cpu`` times every software implementation on this machine; ``--target fpga``
times the Basys 3 over the UART, end to end. The target names *what* is measured and the
results name *which machine*, so the same command runs on the laptop or on the Pi 5
(plan.md Phase E, E.4).

``--load`` sets what else the machine is doing: ``none`` (idle), ``pipeline`` (a detector inference
before every timed call, in this process), ``concurrent`` (a detector in another process)
or ``stress`` (``stress-ng`` memory workers). Loads run on the other cores; the benchmark
pins itself to ``--cpu``.

Each run writes three files to ``--out`` (default ``benchmarks/results/``), named
``<target>-<host>-<date>-<load>``:

* ``.csv`` -- one row per implementation and input group: min / median / p99 / max in
  microseconds, and whether every answer agreed with its reference;
* ``.json`` -- the machine, clocks, versions, and ``valid`` (false if a Pi throttled);
* ``-samples.csv.gz`` -- every sample, for the under-load histograms.

Exit status: 0 when every answer agreed and the run is valid, 1 otherwise, 2 when the run
could not start (a missing tool or library, or a serial port that won't open).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import sys
from pathlib import Path

from benchmarks import inputs
from benchmarks import meta as machine
from benchmarks.inputs import Batch
from benchmarks.targets import cpu as cpu_target
from benchmarks.targets import fpga as fpga_target
from models.nms import bench, host

RESULTS_DIR = Path(__file__).resolve().parent / "results"
LOADS = ("none", "pipeline", "concurrent", "stress")
FIELDS = (
    "target",
    "load",
    "host",
    "impl",
    "kind",
    "case",
    "batches",
    "samples",
    "min_us",
    "median_us",
    "p99_us",
    "max_us",
    "agreement",
    "mismatch_causes",
    "valid",
)


def default_cpu() -> int:
    """Return CPU 2 when the process may use it, else its lowest allowed CPU.

    CPU 2 is a P-core on the i5-13500H, which mixes P- and E-cores (E.3); on the Pi 5 all
    four cores are alike.

    Returns:
        A CPU number.
    """
    allowed = os.sched_getaffinity(0)
    return 2 if 2 in allowed else min(allowed)


def check_agreement(impl: cpu_target.Impl, group: list[Batch]) -> tuple[str, dict]:
    """Run every batch once, untimed, and compare with the implementation's reference.

    Args:
        impl: The implementation.
        group: Its inputs.

    Returns:
        ``(verdict, causes)``. The verdict is ``ok``, ``explained`` (a library differs only
        where a known cause applies) or ``FAIL``; causes counts mismatches by cause.
    """
    causes: dict[str, int] = {}
    for b in group:
        keep, _ = impl.timed(impl.prepare(b))
        want = b.keep_mask if impl.kind == "ours" else b.keep_strict
        if keep == want:
            continue
        cause = "wrong" if impl.kind == "ours" else inputs.explain_mismatch(b)
        causes[cause] = causes.get(cause, 0) + 1
    if not causes:
        return "ok", causes
    if impl.kind == "library" and set(causes) <= {"ties", "inverted", "on-threshold"}:
        return "explained", causes
    return "FAIL", causes


def time_cpu(
    impls: list[cpu_target.Impl],
    groups: dict[str, list[Batch]],
    args: argparse.Namespace,
) -> dict[tuple[str, str], list[float]]:
    """Time every implementation on every group, with no load or a background load.

    Args:
        impls: Implementations.
        groups: Input groups.
        args: The command line.

    Returns:
        Samples in microseconds, keyed by ``(impl, case)``.
    """
    out = {}
    for impl in impls:
        for case, group in groups.items():
            prepared = [impl.prepare(b) for b in group]
            for x in prepared[:3]:
                impl.timed(x)  # warm-up
            out[impl.name, case] = bench.time_samples(
                lambda k, pr=prepared, im=impl: im.timed(pr[k % len(pr)])[1],
                budget_s=args.budget,
                max_samples=args.reps,
            )
    return out


def time_pipeline(
    impls: list[cpu_target.Impl],
    groups: dict[str, list[Batch]],
    args: argparse.Namespace,
) -> tuple[dict[tuple[str, str], list[float]], dict]:
    """Time one NMS call after each detector inference, round-robin over the pairs.

    Args:
        impls: Implementations.
        groups: Input groups.
        args: The command line.

    Returns:
        Samples keyed by ``(impl, case)``, and the load's metadata.
    """
    from benchmarks.workloads.detector import Detector

    detector = Detector(args.weights, args.images)
    pairs = [(impl, case) for impl in impls for case in groups]
    prepared = {
        (impl.name, case): [impl.prepare(b) for b in groups[case]]
        for impl, case in pairs
    }
    out: dict[tuple[str, str], list[float]] = {(i.name, c): [] for i, c in pairs}
    for k in range(args.frames * len(pairs)):
        impl, case = pairs[k % len(pairs)]
        detector.step()
        inputs_ = prepared[impl.name, case]
        n = len(out[impl.name, case])
        out[impl.name, case].append(impl.timed(inputs_[n % len(inputs_)])[1])
    return out, {"load_weights": args.weights, "load_frames": detector.k}


def run_cpu(
    args: argparse.Namespace, groups: dict[str, list[Batch]]
) -> tuple[list, dict, dict]:
    """Run the CPU target.

    Args:
        args: The command line.
        groups: Input groups.

    Returns:
        ``(rows, samples, extra metadata)``.
    """
    impls, skipped = cpu_target.all_impls(args.impls)
    for line in skipped:
        print(f"skipped {line}")
    agreement = {
        (i.name, c): check_agreement(i, g) for i in impls for c, g in groups.items()
    }
    extra: dict = {
        "skipped": skipped,
        "impl_notes": {i.name: i.notes for i in impls},
        "spec_vs_strict": {
            c: sum(b.keep_mask != b.keep_strict for b in g) for c, g in groups.items()
        },
    }
    if args.load == "pipeline":
        samples, load_meta = time_pipeline(impls, groups, args)
        extra.update(load_meta)
    elif args.load in ("concurrent", "stress"):
        from benchmarks.workloads.background import Background

        with Background(
            args.load, args.cpu, weights=args.weights, images=args.images
        ) as bg:
            samples = time_cpu(impls, groups, args)
            extra.update(bg.describe())
    else:
        samples = time_cpu(impls, groups, args)
    kinds = {i.name: i.kind for i in impls}
    rows = []
    for (name, case), s in samples.items():
        verdict, causes = agreement[name, case]
        rows.append(
            _row(args, name, kinds[name], case, len(groups[case]), s, verdict, causes)
        )
    return rows, samples, extra


def run_fpga(
    args: argparse.Namespace, groups: dict[str, list[Batch]]
) -> tuple[list, dict, dict]:
    """Run the FPGA target: the board over the UART, every reply checked.

    Args:
        args: The command line.
        groups: Input groups.

    Returns:
        ``(rows, samples, extra metadata)``.
    """
    if args.leave_latency_timer:
        timer, line = None, "latency timer: left unchanged"
    else:
        timer, line = host.set_latency_timer(args.port)
    print(line)
    samples, rows = {}, []
    with host.Board(args.port) as board:
        for case, group in groups.items():
            try:
                s = fpga_target.measure(board, group, args.reps)
                verdict = "ok"
            except host.LatencyError as exc:
                print(f"{case}: FAIL -- {exc}")
                s, verdict = [], "FAIL"
            samples["uart_system", case] = s
            if s:
                rows.append(
                    _row(
                        args, "uart_system", "system", case, len(group), s, verdict, {}
                    )
                )
            else:
                rows.append(
                    {
                        "target": args.target,
                        "impl": "uart_system",
                        "case": case,
                        "agreement": verdict,
                    }
                )
    return (
        rows,
        samples,
        {"port": args.port, "latency_timer_ms": timer, "core": fpga_target.core_row()},
    )


def _row(
    args: argparse.Namespace,
    impl: str,
    kind: str,
    case: str,
    batches: int,
    samples: list[float],
    verdict: str,
    causes: dict,
) -> dict:
    s = bench.summarise(samples)
    return {
        "target": args.target,
        "load": args.load,
        "host": machine.host_label(),
        "impl": impl,
        "kind": kind,
        "case": case,
        "batches": batches,
        "samples": len(samples),
        "min_us": round(s["min"], 4),
        "median_us": round(s["median"], 4),
        "p99_us": round(s["p99"], 4),
        "max_us": round(s["max"], 4),
        "agreement": verdict,
        "mismatch_causes": ";".join(f"{k}={v}" for k, v in sorted(causes.items())),
    }


def write_results(
    out_dir: Path, stem: str, rows: list[dict], samples: dict, meta: dict
) -> Path:
    """Write the CSV, the metadata JSON and the raw samples.

    Args:
        out_dir: Results directory.
        stem: File name without extension.
        rows: Summary rows.
        samples: Raw samples keyed by ``(impl, case)``.
        meta: Metadata.

    Returns:
        Path of the CSV.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{stem}.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, restval="")
        w.writeheader()
        for r in rows:
            w.writerow({**r, "valid": meta["valid"]})
    (out_dir / f"{stem}.json").write_text(
        json.dumps(meta, indent=2, default=str) + "\n"
    )
    with gzip.open(out_dir / f"{stem}-samples.csv.gz", "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(("impl", "case", "us"))
        for (impl, case), s in samples.items():
            w.writerows((impl, case, round(v, 4)) for v in s)
    return path


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments, excluding the program name.

    Returns:
        The parsed arguments.
    """
    ap = argparse.ArgumentParser(
        prog="python -m benchmarks", description=__doc__.split("\n")[0]
    )
    ap.add_argument("--target", choices=("cpu", "fpga"), required=True)
    ap.add_argument("--load", choices=LOADS, default="none")
    ap.add_argument(
        "--cpu", type=int, default=default_cpu(), help="CPU to pin to (default 2)"
    )
    ap.add_argument(
        "--impls", nargs="+", help="only these implementations (cpu target)"
    )
    ap.add_argument("--cases", nargs="+", help="only these input groups")
    ap.add_argument("--hostile-count", type=int, default=inputs.HOSTILE_COUNT)
    ap.add_argument("--budget", type=float, default=0.5, help="seconds per group (cpu)")
    ap.add_argument(
        "--reps",
        type=int,
        default=5000,
        help="most samples per group (cpu), round trips per group (fpga)",
    )
    ap.add_argument(
        "--frames",
        type=int,
        default=200,
        help="samples per implementation and group under --load pipeline",
    )
    ap.add_argument(
        "--weights", default="yolov8n.pt", help="detector for pipeline/concurrent"
    )
    ap.add_argument("--images", type=Path, help="JPEG directory for the detector")
    ap.add_argument("--port", default=host.DEFAULT_PORT)
    ap.add_argument("--leave-latency-timer", action="store_true")
    ap.add_argument("--out", type=Path, default=RESULTS_DIR)
    ap.add_argument("--tag", default="", help="suffix for the result file names")
    args = ap.parse_args(argv)
    if args.target == "fpga" and args.load != "none":
        ap.error("--load applies to --target cpu only")
    if args.target == "fpga" and args.reps == 5000:
        args.reps = 500
    return args


def main(argv: list[str] | None = None) -> int:
    """Run one benchmark and write its results.

    Args:
        argv: Arguments, excluding the program name.

    Returns:
        The process exit status.
    """
    args = parse_args(argv)
    os.sched_setaffinity(0, {args.cpu})
    groups = inputs.suite(hostile_count=args.hostile_count)
    if args.cases:
        groups = {c: groups[c] for c in args.cases}
    before = machine.machine_metadata()
    try:
        if args.target == "cpu":
            rows, samples, extra = run_cpu(args, groups)
        else:
            rows, samples, extra = run_fpga(args, groups)
    except (RuntimeError, host.PortError, ValueError) as exc:
        print(f"cannot run: {exc}")
        return 2
    after = machine.pi_readings()
    valid = not (machine.throttled(before) or machine.throttled(after))
    meta = {
        **before,
        **{f"{k}_after": v for k, v in after.items()},
        "valid": valid,
        "target": args.target,
        "load": args.load,
        "argv": sys.argv[1:] if argv is None else argv,
        **extra,
    }
    stem = "-".join(
        filter(None, [args.target, before["host"], before["date"], args.load, args.tag])
    )
    path = write_results(args.out, stem, rows, samples, meta)
    for r in rows:
        if "median_us" in r:
            print(
                f"{r['impl']:18s} {r['case']:12s} median {r['median_us']:10.3f} us  "
                f"p99 {r['p99_us']:10.3f}  {r['agreement']} {r['mismatch_causes']}"
            )
    if not valid:
        print("INVALID: the Pi reported throttling; fix the cooling and rerun")
    print(f"wrote {path}")
    ok = valid and all(r["agreement"] in ("ok", "explained") for r in rows)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
