"""Fail if any text file is not valid UTF-8, or has been double-encoded.

This exists because two separate encoding accidents reached the repository:

1. a Windows-1252 em dash (0x97) made files invalid UTF-8, which broke `mypy` with
   "cannot decode file" and renders as garbage in editors;
2. UTF-8 bytes decoded as cp1252 and re-encoded, so "—" became "â€”" — *valid* UTF-8, invisible to a
   validity check, and it silently degraded 127 characters across three documents.

Both are cheap to detect and expensive to notice in review.

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
# This file necessarily contains the sequences it looks for, so it cannot scan itself.
SKIP_FILES = {"scripts/check_encoding.py"}

# UTF-8 read back as cp1252: the tell-tale byte pairs for the characters this repo actually uses.
DOUBLE_ENCODED = ("â€”", "â€“", "â€¦", "Â·", "â†’", "â€™", "â€œ")  # noqa: RUF001 - literals by design


def main() -> int:
    bad: list[tuple[str, int, str]] = []
    mojibake: list[tuple[str, int]] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        relative = str(path.relative_to(ROOT)).replace("\\", "/")
        if relative in SKIP_FILES:
            continue
        data = path.read_bytes()
        # A UTF-8 BOM is tolerated (Windows editors add it); anything else invalid is not.
        if data.startswith(b"\xef\xbb\xbf"):
            data = data[3:]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            bad.append((str(path.relative_to(ROOT)), exc.start, exc.reason))
            continue
        hits = sum(text.count(seq) for seq in DOUBLE_ENCODED)
        if hits:
            mojibake.append((str(path.relative_to(ROOT)), hits))

    for name, offset, reason in bad:
        print(f"NOT UTF-8: {name} at byte {offset}: {reason}")
    for name, hits in mojibake:
        print(f"DOUBLE-ENCODED: {name} ({hits} occurrence(s) of e.g. 'a-euro-quote')")
    if bad or mojibake:
        print(
            f"\n{len(bad)} file(s) are not valid UTF-8, {len(mojibake)} file(s) are double-encoded",
            file=sys.stderr,
        )
        return 1
    print("encoding check: all text files are valid UTF-8 and not double-encoded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
