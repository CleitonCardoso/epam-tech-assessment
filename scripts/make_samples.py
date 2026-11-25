#!/usr/bin/env python3
"""Generate the deterministic PDF fixtures used by ``scripts/smoke.sh`` and the integration tests.

Stdlib only. Running this twice produces byte-identical files, so ``git status`` stays clean
after a regeneration and reviewers can diff the samples confidently.

Outputs (relative to the repository root, not the current directory):

  samples/sample-text.pdf     2 pages of real, distinct, extractable Helvetica text
  samples/sample-blank.pdf    1 page whose content stream is empty (no text operators)
  samples/sample-corrupt.pdf  not a PDF: no ``%PDF-`` header, deterministic filler bytes

The PDFs are assembled by hand the old-fashioned way: a Catalog -> Pages -> Page tree, one
base-14 Helvetica font resource, one content stream per page, an ``xref`` table holding the real
byte offset of every object, and a trailer. A PDF whose xref offsets are wrong often still
opens in forgiving readers but makes pypdf complain, so ``_build_pdf`` asserts every offset
lands on its own ``N 0 obj`` header before returning.

Usage:
    python scripts/make_samples.py [--out DIR]
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "samples"

# Characters outside printable ASCII that the report needs. The content stream is byte-oriented,
# so each character is written as its WinAnsi code and the font resource declares
# /Encoding /WinAnsiEncoding. Extend this table if the sample text grows more Unicode.
_WINANSI = {
    "\u20ac": 0x80,  # euro
    "\u201a": 0x82,
    "\u0192": 0x83,
    "\u201e": 0x84,
    "\u2026": 0x85,  # ellipsis
    "\u2018": 0x91,  # left single quote
    "\u2019": 0x92,  # right single quote
    "\u201c": 0x93,  # left double quote
    "\u201d": 0x94,  # right double quote
    "\u2022": 0x95,  # bullet
    "\u2013": 0x96,  # en dash
    "\u2014": 0x97,  # em dash
}


def _pdf_literal_string(text: str) -> bytes:
    """Encode `text` as the body of a PDF literal string (without the surrounding parentheses).

    Parentheses and backslashes are escaped as required by ISO 32000; WinAnsi bytes outside
    printable ASCII are written as three-digit octal escapes so the file stays pure ASCII.
    """
    out = bytearray()
    for char in text:
        code = _WINANSI.get(char)
        if code is None:
            code = ord(char)
            if not 32 <= code <= 126:
                raise ValueError(
                    f"make_samples: character {char!r} is not representable in WinAnsi; "
                    "keep the sample text ASCII or extend _WINANSI"
                )
        if code in (0x28, 0x29, 0x5C):  # ( ) \
            out += b"\\" + bytes([code])
        elif 32 <= code <= 126:
            out.append(code)
        else:
            out += f"\\{code:03o}".encode("ascii")
    return bytes(out)


def _content_stream(lines: list[str]) -> bytes:
    """A minimal text content stream: 16 pt Helvetica on 16 pt leading, one Tj per line."""
    if not lines:
        return b""
    ops = [b"BT", b"/F1 16 Tf", b"72 780 Td", b"16 TL"]
    for line in lines:
        ops.append(b"(" + _pdf_literal_string(line) + b") Tj")
        ops.append(b"T*")
    ops.append(b"ET")
    return b"\n".join(ops) + b"\n"


def _build_pdf(pages: list[list[str]]) -> bytes:
    """Assemble a single-PDF with one Helvetica text page per entry of `pages`.

    Object layout (1-based): 1 = Catalog, 2 = Pages, then two objects per page
    (page dictionary, content stream), then one shared Font object. All offsets are measured
    while serializing, so the xref is exact by construction.
    """
    if not pages:
        raise ValueError("a PDF needs at least one page")

    page_count = len(pages)
    page_ids = [3 + 2 * i for i in range(page_count)]
    content_ids = [4 + 2 * i for i in range(page_count)]
    font_id = 3 + 2 * page_count
    objects: dict[int, bytes] = {}

    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode("ascii")

    for index, lines in enumerate(pages):
        objects[page_ids[index]] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {content_ids[index]} 0 R >>"
        ).encode("ascii")
        stream = _content_stream(lines)
        objects[content_ids[index]] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream"
        )

    objects[font_id] = (
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )

    highest_id = font_id
    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")  # binary marker comment, as readers expect
    offsets: dict[int, int] = {}
    for object_id in range(1, highest_id + 1):
        offsets[object_id] = len(pdf)
        pdf += f"{object_id} 0 obj\n".encode("ascii")
        pdf += objects[object_id]
        pdf += b"\nendobj\n"

    xref_offset = len(pdf)
    pdf += f"xref\n0 {highest_id + 1}\n".encode("ascii")
    pdf += b"0000000000 65535 f \n"  # 20-byte free entry for object 0
    for object_id in range(1, highest_id + 1):
        pdf += f"{offsets[object_id]:010d} 00000 n \n".encode("ascii")
    pdf += (
        f"trailer\n<< /Size {highest_id + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")

    # Self-check: every xref offset must point at that object's header, otherwise pypdf would
    # complain even though some readers silently recover.
    for object_id, offset in offsets.items():
        header = f"{object_id} 0 obj".encode("ascii")
        if not bytes(pdf[offset : offset + len(header)]) == header:
            raise AssertionError(
                f"make_samples: xref offset for object {object_id} is wrong "
                f"({offset} -> {bytes(pdf[offset : offset + 16])!r})"
            )
    return bytes(pdf)


def _corrupt_bytes() -> bytes:
    """Not a PDF at all: no ``%PDF-`` header, just deterministic pseudo-random filler."""
    filler = bytearray()
    block = hashlib.sha256(b"qa-corrupt-sample-v1").digest()
    while len(filler) < 2048:
        block = hashlib.sha256(block).digest()
        filler += block
    data = b"not a pdf\n" + bytes(filler)
    assert b"%PDF-" not in data
    return data


SAMPLE_TEXT = _build_pdf(
    [
        [
            "Piscina maintenance report \u2014 page 1",
            "Site: residential pool, 12 m x 5 m, 48 m3 of water.",
            "Filter pressure 1.4 bar; backwash performed at 09:10.",
            "Free chlorine 1.2 mg/L, pH 7.4; water temperature 27.3 C.",
        ],
        [
            "Piscina maintenance report \u2014 page 2",
            "Skimmer baskets cleaned; pump run time 6 hours.",
            "Next service visit scheduled in 14 days.",
        ],
    ]
)


def render_samples() -> dict[str, bytes]:
    """Return ``{filename: bytes}`` for all three fixtures."""
    return {
        "sample-text.pdf": SAMPLE_TEXT,
        "sample-blank.pdf": _build_pdf([[]]),
        "sample-corrupt.pdf": _corrupt_bytes(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic PDF sample fixtures.")
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help=f"output directory (default: {DEFAULT_OUT})",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for filename, data in render_samples().items():
        path = out_dir / filename
        path.write_bytes(data)
        print(f"wrote {path} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
