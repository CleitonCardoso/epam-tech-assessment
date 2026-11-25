"""Fail if any text file in the repository is not valid UTF-8.

This exists because a Windows-1252 em dash (0x97) slipped into a couple of files during
development: it renders as garbage in editors, breaks `mypy` and any tool that decodes strictly, and
is invisible in most diffs. A one-line gate is cheaper than discovering it in review.

    python scripts/check_encoding.py
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".yaml",
    ".yml",
    ".toml",
    ".txt",
    ".json",
    ".sh",
    ".conf",
    ".tf",
    ".tftpl",
    ".css",
    ".ts",
    ".tsx",
    ".html",
}
SKIP_DIRS = {".git", "node_modules", ".terraform", ".tmp", "dist", "__pycache__", ".pytest_cache"}


def main() -> int:
    bad: list[tuple[str, int, str]] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        data = path.read_bytes()
        # A UTF-8 BOM is tolerated (Windows editors add it); anything else invalid is not.
        if data.startswith(b"\xef\xbb\xbf"):
            data = data[3:]
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            bad.append((str(path.relative_to(ROOT)), exc.start, exc.reason))

    for name, offset, reason in bad:
        print(f"NOT UTF-8: {name} at byte {offset}: {reason}")
    if bad:
        print(f"\n{len(bad)} file(s) are not valid UTF-8", file=sys.stderr)
        return 1
    print("encoding check: all text files are valid UTF-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
