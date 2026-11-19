#!/usr/bin/env python3
"""Unit tests for the cross-platform scanned-pdf-to-markdown skill CLI (pdf2md.py v2.0.0)."""
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
        ww2, cw2, _ = pdf2md.recommend_concurrency(5.4, 16, sysname="Windows")
        self.assertEqual((ww2, cw2), (2, 25))

    def test_infer_pagination_rescues_misreads_and_romans(self):
        # Simulate Toynbee-style pages: Roman preface (offset=2), body (offset=42) with 'A', 'B', '12I', 'III' misreads,
        # and publisher catalogue backmatter (offset=362).
        pages = [
            {"pdf_page": 1, "ocr_page": None, "page_num_candidates": [], "header": None},
            {"pdf_page": 8, "ocr_page": "vi", "page_num_candidates": ["vi"], "header": "Memoir."},
            {"pdf_page": 9, "ocr_page": "vii", "page_num_candidates": ["vii"], "header": "Memoir."},
            {"pdf_page": 10, "ocr_page": None, "page_num_candidates": [], "header": "viii | Memoir."},
            {"pdf_page": 36, "ocr_page": None, "page_num_candidates": [], "header": "xxxiv | Contents."},
            {"pdf_page": 43, "ocr_page": "A", "page_num_candidates": ["A"], "header": None},
            {"pdf_page": 44, "ocr_page": "2", "page_num_candidates": ["2"], "header": "Chapter 1"},
            {"pdf_page": 45, "ocr_page": "3", "page_num_candidates": ["3"], "header": "Chapter 1"},
            {"pdf_page": 59, "ocr_page": "B", "page_num_candidates": ["B"], "header": "Chapter 2"},
            {"pdf_page": 153, "ocr_page": "III", "page_num_candidates": ["III"], "header": "Chapter 8"},
            {"pdf_page": 163, "ocr_page": "12I", "page_num_candidates": ["12I"], "header": "Chapter 9"},
            {"pdf_page": 361, "ocr_page": "319", "page_num_candidates": ["319"], "header": "Appendix"},
            {"pdf_page": 365, "ocr_page": "3", "page_num_candidates": ["3"], "header": "Catalogue"},
            {"pdf_page": 366, "ocr_page": "4", "page_num_candidates": ["4"], "header": "Catalogue"},
            {"pdf_page": 373, "ocr_page": "II", "page_num_candidates": ["II"], "header": "Catalogue"},
            {"pdf_page": 374, "ocr_page": "12", "page_num_candidates": ["12"], "header": "Catalogue"},
            {"pdf_page": 390, "ocr_page": None, "page_num_candidates": [], "header": None},
        ]
        # Pad enough pages >30 so full-book mode activates
        for p in range(46, 59):
            pages.append({"pdf_page": p, "ocr_page": str(p - 42), "page_num_candidates": [str(p - 42)], "header": "Body"})
        for p in range(60, 70):
            pages.append({"pdf_page": p, "ocr_page": str(p - 42), "page_num_candidates": [str(p - 42)], "header": "Body"})
        pages.sort(key=lambda x: x["pdf_page"])

        info = pdf2md.infer_pagination(pages)
        self.assertEqual(info["body_offset"], 42)
        self.assertEqual(info["roman_offset"], 2)
        self.assertEqual(info["back_offset"], 362)
        by_pdf = {pg["pdf_page"]: pg["book_page"] for pg in pages}
        self.assertEqual(by_pdf[1], "unnumbered")
        self.assertEqual(by_pdf[8], "vi")
        self.assertEqual(by_pdf[10], "viii")
        self.assertEqual(by_pdf[36], "xxxiv")
        self.assertEqual(by_pdf[43], "1")
        self.assertEqual(by_pdf[59], "17")
        self.assertEqual(by_pdf[153], "111")
        self.assertEqual(by_pdf[163], "121")
        self.assertEqual(by_pdf[361], "319")
        self.assertEqual(by_pdf[373], "back-11")
        self.assertEqual(by_pdf[390], "unnumbered")

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

    def test_format_readonly_and_verify_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            md_path = Path(td) / "book.md"
            md_text = pdf2md.format_markdown(
                title="Demo Book",
                source_pdf=Path("/tmp/demo.pdf"),
                source_sha="f" * 64,
                total_pdf_pages=2,
                pdf_offset=1,
                tier="basic",
                pages=[
                    {"pdf_page": 1, "book_page": "unnumbered", "ocr_page": "福", "header": None, "markdown": ""},
                    {"pdf_page": 2, "book_page": "1", "ocr_page": "I", "header": "Chapter 1", "markdown": "Body text long enough to count as non-empty content on page two."},
                ],
            )
            md_path.write_text(md_text, encoding="utf-8")
            pdf2md.set_file_readonly(md_path, readonly=True)
            ver = pdf2md.cmd_verify(md_path, expected_pages=2)
            self.assertTrue(ver["ok"])
            self.assertTrue(ver["read_only_protected"])
            self.assertEqual(ver["verified_pages"], 2)
            self.assertEqual(ver["missing_pages"], [])
            self.assertEqual(ver["invalid_book_page_labels"], [])
            self.assertEqual(ver["ocr_misreads_corrected_count"], 1)

            # Verify bogus book_page is caught as invalid
            pdf2md.set_file_readonly(md_path, readonly=False)
            bad_text = md_text.replace("book_page: 1", "book_page: 12I")
            md_path.write_text(bad_text, encoding="utf-8")
            ver_bad_lbl = pdf2md.cmd_verify(md_path, expected_pages=2)
            self.assertFalse(ver_bad_lbl["ok"])
            self.assertEqual(len(ver_bad_lbl["invalid_book_page_labels"]), 1)


if __name__ == "__main__":
    unittest.main()
