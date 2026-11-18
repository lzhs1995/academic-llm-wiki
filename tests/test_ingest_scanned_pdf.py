#!/usr/bin/env python3
"""Isolated unit tests for tools/ingest-scanned-pdf.py and tools/verify-pdf-quote.py."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ingest_mod = _load_module("ingest_scanned_pdf", "ingest-scanned-pdf.py")
verify_mod = _load_module("verify_pdf_quote", "verify-pdf-quote.py")


class TestIngestScannedPdfHelpers(unittest.TestCase):
    def test_slugify_chinese_and_ascii(self):
        self.assertEqual(
            ingest_mod.slugify("费孝通 2011: 乡土中国 / 生育制度"),
            "费孝通_2011_乡土中国_生育制度",
        )
        with self.assertRaises(ValueError):
            ingest_mod.slugify("   ///   ")

    def test_parse_page_selection_and_chunks(self):
        self.assertEqual(ingest_mod.parse_page_selection("all", 545), [(1, 545)])
        self.assertEqual(ingest_mod.parse_page_selection("1-5,10,20-22", 30), [(1, 5), (10, 10), (20, 22)])
        with self.assertRaises(ValueError):
            ingest_mod.parse_page_selection("0-5", 30)
        with self.assertRaises(ValueError):
            ingest_mod.parse_page_selection("25-35", 30)

        chunks = ingest_mod.split_chunks([(1, 52)], chunk_size=25)
        self.assertEqual(chunks, [(1, 25), (26, 50), (51, 52)])

    def test_compute_book_page_label_priority(self):
        # OCR-detected page number takes priority (handles Roman numerals & section jumps)
        self.assertEqual(ingest_mod.compute_book_page_label(36, "xxxiv", 42), "xxxiv")
        # Fallback to pdf_page - pdf_offset when OCR page number absent on chapter opener
        self.assertEqual(ingest_mod.compute_book_page_label(37, None, 12), "25")
        # Pre-offset front matter without OCR page number -> unnumbered
        self.assertEqual(ingest_mod.compute_book_page_label(5, None, 12), "unnumbered")

    def test_build_book_markdown_preserves_locators(self):
        md = ingest_mod.build_book_markdown(
            title="测试扫描书",
            source_pdf=Path("/tmp/demo.pdf"),
            source_sha256="a" * 64,
            total_pdf_pages=100,
            pdf_offset=12,
            tier="basic",
            pages=[
                {
                    "pdf_page": 37,
                    "book_page": "25",
                    "ocr_page": "25",
                    "header": "差序格局",
                    "markdown": "## 差序格局\n\n测试正文段落。",
                }
            ],
        )
        self.assertIn("<!-- pdf_page: 37 | book_page: 25 | ocr_page: 25 | header: 差序格局 -->", md)
        self.assertIn("### [PDF p.37 | 原书 p.25 · 差序格局]", md)
        self.assertIn("测试正文段落。", md)

    def test_verify_pdf_quote_page_resolution(self):
        self.assertEqual(verify_mod.resolve_pdf_page(172, None, 0), 172)
        self.assertEqual(verify_mod.resolve_pdf_page(None, 160, 12), 172)
        with self.assertRaises(ValueError):
            verify_mod.resolve_pdf_page(0, None, 0)
        with self.assertRaises(ValueError):
            verify_mod.resolve_pdf_page(None, -5, 2)


if __name__ == "__main__":
    unittest.main()
