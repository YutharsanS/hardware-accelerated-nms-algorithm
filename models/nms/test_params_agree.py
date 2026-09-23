"""B2.1 gate: the VHDL and Python constants must not drift apart.

``src/components/nms_pkg.vhd`` and ``models/nms/params.py`` both mirror
``docs/architecture.md``, and a silent disagreement between them would produce hardware
that the golden model declares correct. So the comparison is made against **GHDL's own
evaluation** of the package: ``test/tb_params.vhd`` reports every constant, this module
runs it and parses those lines. A Python re-implementation of VHDL constant folding was
the obvious alternative and is a worse one -- it could be wrong in the same direction as
the package, and then the two sides would agree about nothing in particular.

The check is deliberately **bidirectional and total**: every constant the package declares
must be reported, every reported constant must be compared, and every integer constant in
``params.py`` must have a counterpart here. Adding a constant to either side without the
other therefore fails rather than passing unnoticed.

A missing ``ghdl`` fails these tests rather than skipping them. GHDL is a hard project
requirement (``CLAUDE.md``), and an anti-drift check that quietly skips is worth nothing --
drift would pass on exactly the machine that could not detect it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from models.nms import params as p

REPO = Path(__file__).resolve().parents[2]
PKG = REPO / "src" / "components" / "nms_pkg.vhd"
TB = REPO / "test" / "tb_params.vhd"

# Every constant in nms_pkg.vhd, against the number params.py says it should be. The four
# that are not plain params.py attributes are the two magic bytes (a tuple on the Python
# side) and the two values params.py derives with an expression instead of a constant.
EXPECTED: dict[str, int] = {
    "N": p.N,
    "INDEX_W": p.INDEX_W,
    "COORD_W": p.COORD_W,
    "SCORE_W": p.SCORE_W,
    "RECORD_BITS": p.RECORD_BITS,
    "RECORD_BYTES": p.RECORD_BYTES,
    "COORD_MAX": p.COORD_MAX,
    "SCORE_MAX": p.SCORE_MAX,
    "SCORE_SHIFT": p.SCORE_SHIFT,
    "B_SHIFT": p.B_SHIFT,
    "A_SHIFT": p.A_SHIFT,
    "Y_SHIFT": p.Y_SHIFT,
    "X_SHIFT": p.X_SHIFT,
    "AREA_W": p.AREA_W,
    "UNION_W": p.UNION_W,
    "T_INTERMEDIATE_W": p.T_INTERMEDIATE_W,
    "K_SHIFT": p.K_SHIFT,
    "T_INT_W": p.T_INT_W,
    "T_INT": p.T_INT,
    "LHS_W": p.LHS_W,
    "RHS_W": p.RHS_W,
    "COMPARE_W": p.COMPARE_W,
    "KEY_W": p.KEY_W,
    "P_DEFAULT": p.P_DEFAULT,
    "LANE_LATENCY": p.LANE_LATENCY,
    "SORT_SUBSTAGES": p.SORT_SUBSTAGES,
    "CAS_COUNT": p.CAS_COUNT,
    "PIPE_CUTS": p.PIPE_CUTS,
    "LATENCY_CYCLES": p.latency_cycles(),
    "MAGIC_0": p.MAGIC[0],
    "MAGIC_1": p.MAGIC[1],
    "STATUS_OK": p.STATUS_OK,
    "STATUS_CRC_FAIL": p.STATUS_CRC_FAIL,
    "STATUS_BUSY": p.STATUS_BUSY,
    "STATUS_INTERNAL": p.STATUS_INTERNAL,
    "FRAME_BYTES_IN": p.FRAME_BYTES_IN,
    "REPLY_BYTES": p.REPLY_BYTES,
    "CLOCK_HZ": p.CLOCK_HZ,
    "BAUD": p.BAUD,
    "BAUD_DIV": p.CLOCK_HZ // p.BAUD,
}


def _ghdl() -> str:
    """Locate the GHDL executable.

    Returns:
        Path to ``ghdl``.
    """
    found = shutil.which("ghdl")
    assert found is not None, (
        "ghdl is not on PATH; it is required to check that nms_pkg.vhd and params.py agree"
    )
    return found


@pytest.fixture(scope="module")
def reported(tmp_path_factory: pytest.TempPathFactory) -> dict[str, int]:
    """Run ``tb_params`` and return the constants GHDL evaluated.

    Args:
        tmp_path_factory: Pytest factory, used for GHDL's work library.

    Returns:
        Mapping of constant name to the value GHDL computed.
    """
    ghdl = _ghdl()
    work = tmp_path_factory.mktemp("ghdl")
    # GHDL wants the command first and its options after it, so the shared flags cannot
    # simply be prepended.
    flags = ["--std=08", f"--workdir={work}"]

    for stage in (
        ["-a", *flags, "-Wall", "--warn-error", str(PKG), str(TB)],
        ["-e", *flags, "-Wall", "--warn-error", "tb_params"],
        ["-r", *flags, "tb_params", "--assert-level=error"],
    ):
        # Fixed argv, no shell: nothing here comes from outside the repository.
        run = subprocess.run(
            [ghdl, *stage],
            capture_output=True,
            text=True,
            cwd=work,
            check=False,
        )
        output = run.stdout + run.stderr
        assert run.returncode == 0, (
            f"ghdl {stage[0]} failed with {run.returncode}:\n{output}"
        )

    assert "PASS" in output, f"tb_params did not report PASS:\n{output}"
    values = {
        name: int(text) for name, text in re.findall(r"PARAM (\w+) (-?\d+)", output)
    }
    assert values, f"tb_params reported no PARAM lines:\n{output}"
    return values


def _declared_in_package() -> set[str]:
    """Read the constant names declared in ``nms_pkg.vhd``.

    Returns:
        The declared names.
    """
    text = PKG.read_text()
    return set(re.findall(r"^\s*constant\s+(\w+)\s*:", text, re.MULTILINE))


def _python_int_constants() -> dict[str, int]:
    """Collect the module-level integer constants in ``params.py``.

    Returns:
        Mapping of name to value, excluding booleans and non-integers.
    """
    return {
        name: value
        for name, value in vars(p).items()
        if name.isupper() and type(value) is int
    }


def test_every_constant_agrees(reported: dict[str, int]) -> None:
    mismatched = {
        name: (value, EXPECTED[name])
        for name, value in reported.items()
        if name in EXPECTED and value != EXPECTED[name]
    }
    assert not mismatched, (
        "nms_pkg.vhd and params.py disagree (vhdl, python): "
        + ", ".join(f"{n}=({v}, {e})" for n, (v, e) in sorted(mismatched.items()))
    )


def test_the_comparison_covers_the_whole_package(reported: dict[str, int]) -> None:
    # Three-way closure: what the package declares, what the testbench reports, and what
    # this module compares must be the same set of names. Any one of the three growing
    # alone is the drift this gate exists to catch.
    declared = _declared_in_package()
    assert declared == set(reported), (
        f"declared but not reported: {sorted(declared - set(reported))}; "
        f"reported but not declared: {sorted(set(reported) - declared)}"
    )
    assert set(reported) == set(EXPECTED), (
        f"reported but not compared: {sorted(set(reported) - set(EXPECTED))}; "
        f"compared but not reported: {sorted(set(EXPECTED) - set(reported))}"
    )


def test_every_python_constant_has_a_vhdl_counterpart() -> None:
    # The other direction: a constant added to params.py with no VHDL equivalent means the
    # RTL is about to hard-code a number the model believes it shares.
    uncovered = sorted(set(_python_int_constants()) - set(EXPECTED))
    assert not uncovered, (
        f"params.py constants with no counterpart in nms_pkg.vhd: {uncovered}"
    )


def test_python_constants_are_self_consistent() -> None:
    # params.validate() also checks the two bounds tb_params cannot: max LHS and max RHS
    # overflow GHDL's 32-bit integer, while Python's are unbounded.
    assert p.validate() == []
