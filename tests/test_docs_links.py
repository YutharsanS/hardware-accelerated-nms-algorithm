"""Every relative link and image in the repository's Markdown must resolve.

The docs cross-reference each other, the RTL and the result files heavily, and they were
reorganised into docs/design, docs/results and docs/project. A broken link fails here, in
`make test` and CI, rather than on a reader's screen.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")


def _markdown_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [REPO / f for f in out if (REPO / f).exists()]


def _links(path: Path) -> list[tuple[int, str]]:
    """Relative link targets in a Markdown file, outside fenced code blocks."""
    found, in_code = [], False
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_code = not in_code
        if in_code:
            continue
        for target in LINK.findall(line):
            if re.match(r"^[a-z]+:", target) or target.startswith("#"):
                continue
            found.append((n, target))
    return found


@pytest.mark.parametrize(
    "path", _markdown_files(), ids=lambda p: str(p.relative_to(REPO))
)
def test_relative_links_resolve(path: Path) -> None:
    broken = [
        f"line {n}: {target}"
        for n, target in _links(path)
        if not (path.parent / target.split("#")[0]).exists()
    ]
    assert not broken, "broken links:\n" + "\n".join(broken)
