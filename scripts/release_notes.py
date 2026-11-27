"""Print one version's section of CHANGELOG.md (the GitHub Release notes for that tag).

    python3 scripts/release_notes.py 0.2.0

Exits non-zero when CHANGELOG.md has no section for the version, so a release cannot ship
without one.
"""

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"


def notes(version: str, text: str) -> str | None:
    match = re.search(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|^\[[^\]]+\]: |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else None


if __name__ == "__main__":
    body = notes(sys.argv[1], CHANGELOG.read_text())
    if body is None:
        sys.exit(f"CHANGELOG.md has no section for {sys.argv[1]}")
    print(body)
