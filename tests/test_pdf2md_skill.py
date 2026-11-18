#!/usr/bin/env python3
"""Unit tests for the cross-platform scanned-pdf-to-markdown skill CLI (pdf2md.py)."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest

SKILL_SCRIPT = Path(__file__).resolve().parent.parent / "skills" / "scanned-pdf-to-markdown" / "scripts" / "pdf2md.py"


def _load_pdf2md():
    spec = importlib.util.spec_from_file_location("pdf2md", SKILL_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pdf2md = _load_pdf2md()


class TestPdf2MdSkill(unittest.TestCase):
    def test_recommend_concurrency_tiers(self):
        w1, c1, _ = pdf2md.recommend_concurrency(6.0, 12, sysname="Darwin")
        self.assertEqual((w1, c1), (1, 25))
        w2, c2, _ = pdf2md.recommend_concurrency(10.0, 12, sysname="Darwin")
        self.assertEqual((w2, c2), (2, 25))
        w3, c3, _ = pdf2md.recommend_concurrency(16.0, 15, sysname="Darwin")
        self.assertEqual((w3, c3), (3, 25))
        ww2, cw2, _ = pdf2md.recommend_concurrency(6.1, 16, sysname="Windows")
        self.assertEqual((ww2, cw2), (2, 25))

    def test_import_models_from_tar(self):
        import tarfile

        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            src_models = td_p / "src" / "MinerU-4_models_onnx" / "OCR"
            src_models.mkdir(parents=True)
            (src_models / "dummy.onnx").write_bytes(b"onnx-weights")
            tar_p = td_p / "bundle.tar"
            with tarfile.open(tar_p, "w") as tf:
                tf.add(td_p / "src" / "MinerU-4_models_onnx", arcname="MinerU-4_models_onnx")
            dest_p = td_p / "dest" / "MinerU-4_models_onnx"
            pdf2md.import_models_from_path(tar_p, target_model_dir=dest_p)
            self.assertTrue((dest_p / "OCR" / "dummy.onnx").is_file())

    def test_format_and_verify_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            md_path = Path(td) / "book.md"
            md_text = pdf2md.format_markdown(
                title="Demo Book",
                source_pdf=Path("/tmp/demo.pdf"),
                source_sha="f" * 64,
                total_pdf_pages=2,
                pdf_offset=10,
                tier="basic",
                pages=[
                    {"pdf_page": 1, "book_page": "unnumbered", "ocr_page": None, "header": None, "markdown": ""},
                    {"pdf_page": 2, "book_page": "1", "ocr_page": "1", "header": "Chapter 1", "markdown": "Body text long enough to count as non-empty content on page two."},
                ],
            )
            md_path.write_text(md_text, encoding="utf-8")
            ver = pdf2md.cmd_verify(md_path, expected_pages=2)
            self.assertTrue(ver["ok"])
            self.assertEqual(ver["verified_pages"], 2)
            self.assertEqual(ver["missing_pages"], [])
            self.assertEqual(ver["non_empty_text_pages"], 1)
            self.assertEqual(ver["book_page_labeled_count"], 1)

            ver_bad = pdf2md.cmd_verify(md_path, expected_pages=3)
            self.assertFalse(ver_bad["ok"])
            self.assertEqual(ver_bad["missing_pages"], [3])


if __name__ == "__main__":
    unittest.main()
