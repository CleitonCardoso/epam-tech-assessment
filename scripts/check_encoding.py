"""Fail if any text file is not valid UTF-8, or has been double-encoded.

This exists because two separate encoding accidents reached the repository:

1. a Windows-1252 em dash (0x97) made files invalid UTF-8, which broke `mypy` with
   "cannot decode file" and renders as garbage in editors;
2. UTF-8 bytes decoded as cp1252 and re-encoded, so "—" became "â€”" — *valid* UTF-8, invisible to a
   validity check. It hit em dashes and ellipses first, then box-drawing characters (─ → "â”€",
   ┌ → "â”Œ", ┤ → "â”¤"), and finally a section sign (Â§G1) — each time a new variant that a
   fixed list of sequences missed.

The detector therefore does not list sequences: it matches the *signature* of the accident — a
Latin-1 lead byte (À-ï) immediately followed by a cp1252 punctuation byte. Both are cheap to detect
and expensive to notice in review.

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
# This file's own docstring uses the mojibake strings as examples, so it cannot scan itself.
SKIP_FILES = {"scripts/check_encoding.py"}

# The double-encoding signature: a UTF-8 lead byte mis-decoded as Latin-1 (À-ï = U+00C0-U+00EF),
# followed by a cp1252 punctuation/continuation byte. This catches every variant in one rule
# instead of a hand-maintained list: "â€”" (em dash), "Â·" (middot), "â”€" (box-drawing), "Â§"
# (section sign), "Ã©" (é), and so on.
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
