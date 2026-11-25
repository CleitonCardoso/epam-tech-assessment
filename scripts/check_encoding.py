"""Fail if any text file is not valid UTF-8, or has been double-encoded.

This exists because two separate encoding accidents reached the repository:

1. a Windows-1252 em dash (0x97) made files invalid UTF-8, which broke `mypy` with
   "cannot decode file" and renders as garbage in editors;
2. UTF-8 bytes decoded as cp1252 and re-encoded, so an em dash (U+2014) became a three-character
   sequence (U+00E2 U+20AC U+201D) that is *valid* UTF-8 and invisible to a validity check. It hit
   em dashes and ellipses first, then box-drawing characters (U+2500, U+250C, U+2524), and finally
   a section sign (U+00A7) - each time a new variant that a fixed list of sequences missed.

The detector therefore does not list sequences: it matches the *signature* of the accident - a
Latin-1 lead byte (U+00C0-U+00EF) immediately followed by a cp1252 punctuation byte. Both are cheap
to detect and expensive to notice in review.

    python scripts/check_encoding.py
"""

from __future__ import annotations

import pathlib
import re
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

# The double-encoding signature: a UTF-8 lead byte (U+00C0-U+00EF) mis-decoded as Latin-1, followed
# by a cp1252 punctuation/continuation byte. One rule catches every variant instead of a list:
# em dash (U+00E2 U+20AC U+201D), middot (U+00C2 U+00B7), box-drawing (U+00E2 U+201D U+20AC),
# section sign (U+00C2 U+00A7), accented letters (U+00C3 U+00A9), and so on.
_DOUBLE_ENCODED_RE = re.compile(
    r"[\u00c0-\u00ef]"
    r"[\u20ac\u201a\u0192\u201e\u2026\u2020\u2021\u02c6\u2030\u0160\u2039\u0152\u017d"
    r"\u2018\u2019\u201c\u201d\u2022\u2013\u2014\u02dc\u2122\u0161\u203a\u0153\u017e\u0178"
    r"\u00a0-\u00bf]"
)


def main() -> int:
    bad: list[tuple[str, int, str]] = []
    mojibake: list[tuple[str, int]] = []
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
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            bad.append((str(path.relative_to(ROOT)), exc.start, exc.reason))
            continue
        hits = len(_DOUBLE_ENCODED_RE.findall(text))
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
