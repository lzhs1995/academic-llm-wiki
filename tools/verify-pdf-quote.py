#!/usr/bin/env python3
"""Render a single scanned PDF page to PNG for visual verbatim-quote verification.

Usage examples:
  python3 tools/verify-pdf-quote.py "/path/to/book.pdf" --pdf-page 172
  python3 tools/verify-pdf-quote.py "/path/to/book.pdf" --book-page 160 --pdf-offset 12
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

DEFAULT_VAULT = Path(__file__).resolve().parent.parent


def resolve_pdf_page(pdf_page: int | None, book_page: int | None, pdf_offset: int) -> int:
    if pdf_page is not None:
        if pdf_page < 1:
            raise ValueError("--pdf-page must be >= 1")
        return pdf_page
    if book_page is not None:
        computed = book_page + pdf_offset
        if computed < 1:
            raise ValueError(f"Computed PDF page ({computed}) must be >= 1")
        return computed
    raise ValueError("Provide either --pdf-page or --book-page")


def render_page(
    pdf_path: Path,
    *,
    pdf_page: int,
    dpi: int = 250,
    out_dir: Path | None = None,
    vault: Path = DEFAULT_VAULT,
) -> dict:
    pdf_real = Path(pdf_path).expanduser().resolve(strict=True)
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        raise RuntimeError("pdftoppm not found in PATH (install poppler via Homebrew: brew install poppler)")

    target_dir = (Path(out_dir).expanduser().resolve()
                  if out_dir is not None
                  else (Path(vault).resolve() / ".vault-meta" / "quote-renders"))
    target_dir.mkdir(parents=True, exist_ok=True)

    short_hash = hashlib.sha256(str(pdf_real).encode("utf-8")).hexdigest()[:10]
    safe_stem = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in pdf_real.stem)[:48].strip("_")
    prefix = target_dir / f"{safe_stem}_{short_hash}_pdfp{pdf_page}_dpi{dpi}"
    png_path = Path(f"{prefix}.png")

    subprocess.run(
        [
            pdftoppm,
            "-png",
            "-r",
            str(dpi),
            "-f",
            str(pdf_page),
            "-l",
            str(pdf_page),
            "-singlefile",
            str(pdf_real),
            str(prefix),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    if not png_path.is_file():
        raise RuntimeError(f"Failed to render page {pdf_page} from {pdf_real}")

    return {
        "ok": True,
        "pdf_path": str(pdf_real),
        "pdf_page": pdf_page,
        "dpi": dpi,
        "image_path": str(png_path),
        "bytes": png_path.stat().st_size,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="Path to the original scanned PDF (read-only)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pdf-page", type=int, help="1-based physical PDF page number")
    group.add_argument("--book-page", type=int, help="Printed book page number (combined with --pdf-offset)")
    parser.add_argument(
        "--pdf-offset",
        type=int,
        default=0,
        help="Offset such that pdf_page = book_page + pdf_offset (e.g. 12 if book p.1 is PDF p.13)",
    )
    parser.add_argument("--dpi", type=int, default=250, help="Render resolution in DPI (default: 250)")
    parser.add_argument("--out-dir", type=Path, default=None, help="Optional output directory")
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT, help="Research vault root")
    args = parser.parse_args(argv)

    try:
        target_page = resolve_pdf_page(args.pdf_page, args.book_page, args.pdf_offset)
        result = render_page(
            args.pdf,
            pdf_page=target_page,
            dpi=args.dpi,
            out_dir=args.out_dir,
            vault=args.vault,
        )
        if args.book_page is not None:
            result["book_page"] = args.book_page
            result["pdf_offset"] = args.pdf_offset
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
