"""CLI: regenerate the verification vectors and print the frozen constants.

uv run python -m models.nms            # write vectors, then summarise
uv run python -m models.nms params     # frozen constants only
uv run python -m models.nms vectors    # write vectors only
"""

from __future__ import annotations

import sys

from models.nms import params, vectors


def _write_vectors() -> None:
    """Regenerate every vector file and print a one-line summary per case."""
    written = vectors.write_all()
    cases = vectors.all_cases()
    print(
        f"wrote {sum(len(v) for v in written.values())} files for {len(written)} cases"
    )
    print(f"  -> {vectors.DEFAULT_DIR}")
    print()
    print(f"  {'case':<18} {'present':>10} {'keep':>10} {'survivors':>10}")
    for name in sorted(cases):
        case = cases[name]
        survivors = case.keep_mask.bit_count()
        print(
            f"  {name:<18} 0x{case.present_mask:08X} 0x{case.keep_mask:08X} {survivors:>10}",
        )


def main(argv: list[str]) -> int:
    """Run the requested action.

    Args:
        argv: Arguments after the module name.

    Returns:
        Process exit status.
    """
    action = argv[0] if argv else "all"
    if action in {"all", "params"}:
        print(params.summary())
        problems = params.validate()
        if problems:
            print("\nFAILED:")
            for problem in problems:
                print(f"  {problem}")
            return 1
        print()
    if action in {"all", "vectors"}:
        _write_vectors()
    if action not in {"all", "params", "vectors"}:
        print(f"unknown action {action!r}; expected params, vectors or all")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
