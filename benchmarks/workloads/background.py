"""Start and stop the background loads, off the benchmark's own core.

``concurrent`` runs :mod:`benchmarks.workloads.detector` as a second process and ``stress``
runs ``stress-ng``'s memory workers. Both are pinned to every CPU except the one the
benchmark is pinned to, so they contend for the shared cache and memory, which is the
point, without simply stealing the benchmark's core.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import TracebackType
from typing import Self

from benchmarks.workloads import detector

STRESS_ARGS = ("--vm", "2", "--vm-bytes", "25%", "--timeout", "0", "--quiet")
"""Two memory workers over a quarter of RAM between them: a heavy but survivable load."""


def other_cpus(bench_cpu: int) -> list[int]:
    """Return every CPU the process may use except the benchmark's.

    Args:
        bench_cpu: The CPU the benchmark is pinned to.

    Returns:
        The rest; the benchmark's own CPU alone if there are no others.
    """
    rest = sorted(os.sched_getaffinity(0) | set(range(os.cpu_count() or 1)))
    others = [c for c in rest if c != bench_cpu]
    return others or [bench_cpu]


class Background:
    """A background load process, stopped on leaving the ``with`` block."""

    def __init__(
        self,
        load: str,
        bench_cpu: int,
        *,
        weights: str = detector.DEFAULT_WEIGHTS,
        images: Path | None = None,
    ) -> None:
        """Describe the load; nothing starts until the ``with`` block is entered.

        Args:
            load: ``concurrent`` or ``stress``.
            bench_cpu: The benchmark's CPU, kept free of the load.
            weights: Detector weights, for ``concurrent``.
            images: Detector images, for ``concurrent``.

        Raises:
            ValueError: For any other load name.
        """
        if load not in ("concurrent", "stress"):
            msg = f"not a background load: {load!r}"
            raise ValueError(msg)
        self.load = load
        self.cpus = other_cpus(bench_cpu)
        self.weights = weights
        self.images = images
        self.proc: subprocess.Popen[str] | None = None
        self.command: list[str] = []

    def __enter__(self) -> Self:
        """Start the load, and for ``concurrent`` wait for its first inference.

        Returns:
            Itself.

        Raises:
            RuntimeError: If ``stress-ng`` is missing or the detector fails to start.
        """
        cpus = ",".join(map(str, self.cpus))
        if self.load == "stress":
            exe = shutil.which("stress-ng")
            if exe is None:
                msg = "stress-ng is not installed: sudo apt install stress-ng"
                raise RuntimeError(msg)
            self.command = ["taskset", "-c", cpus, exe, *STRESS_ARGS]
            self.proc = subprocess.Popen(self.command, text=True)
            return self
        self.command = [
            sys.executable,
            "-m",
            "benchmarks.workloads.detector",
            "--loop",
            "--weights",
            self.weights,
            "--cpus",
            cpus,
        ]
        if self.images:
            self.command += ["--images", str(self.images)]
        self.proc = subprocess.Popen(self.command, stdout=subprocess.PIPE, text=True)
        line = self.proc.stdout.readline() if self.proc.stdout else ""
        if line.strip() != detector.READY:
            self.proc.kill()
            msg = "the concurrent detector did not start; run `python -m benchmarks.workloads.detector --loop` alone to see why"
            raise RuntimeError(msg)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop the load process."""
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def describe(self) -> dict[str, str]:
        """Return what the results metadata records about this load.

        Returns:
            The load's command line and CPUs.
        """
        return {
            "load_command": " ".join(self.command),
            "load_cpus": ",".join(map(str, self.cpus)),
        }
