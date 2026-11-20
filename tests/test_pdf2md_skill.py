#!/usr/bin/env python3
"""Unit tests for the cross-platform scanned-pdf-to-markdown skill CLI (pdf2md.py v2.0.0)."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

SKILL_SCRIPT = Path(os.environ.get("PDF2MD_SCRIPT") or
                    Path(__file__).resolve().parent.parent / "skills" / "scanned-pdf-to-markdown" / "scripts" / "pdf2md.py")


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

    @staticmethod
    def _pages(spec):
        """spec: {pdf_page: ocr page-number text or None}"""
        return [{"pdf_page": p, "ocr_page": t, "page_num_candidates": [t] if t else [], "header": None}
                for p, t in sorted(spec.items())]

    def test_excerpt_whose_first_pdf_page_is_printed_231(self):
        # Journal article / book chapter excerpt: printed numbers are larger than PDF page numbers.
        pages = self._pages({1: "231", 2: "232", 3: None, 4: "234", 5: "235"})
        info = pdf2md.infer_pagination(pages)
        self.assertEqual(info["body_offset"], -230)
        self.assertEqual([pg["book_page"] for pg in pages], ["231", "232", "233", "234", "235"])
        self.assertEqual(pages[2]["book_page_basis"], "inferred")

    def test_single_page_excerpt_keeps_its_printed_number(self):
        pages = self._pages({1: "25"})
        pdf2md.infer_pagination(pages)
        self.assertEqual(pages[0]["book_page"], "25")
        self.assertEqual(pages[0]["book_page_basis"], "printed")

    def test_unnumbered_plates_mid_book_shift_the_offset(self):
        # pdf 5..24 = p.1..20, pdf 25..28 = 4 unnumbered plates, pdf 29..60 = p.21..52
        spec = {p: str(p - 4) for p in range(5, 25)}
        spec.update({p: None for p in range(25, 29)})
        spec.update({p: str(p - 8) for p in range(29, 61)})
        pages = self._pages(spec)
        pdf2md.infer_pagination(pages)
        by = {pg["pdf_page"]: pg["book_page"] for pg in pages}
        self.assertEqual(by[24], "20")
        self.assertEqual([by[p] for p in range(25, 29)], ["unnumbered"] * 4)
        self.assertEqual(by[29], "21")
        self.assertEqual(by[60], "52")

    def test_isolated_misread_is_corrected_not_trusted(self):
        # Fei pdf 112: OCR read '1' on a section opening; neighbours say offset 12 -> p.100
        spec = {p: str(p - 12) for p in range(100, 125)}
        spec[112] = "1"
        pages = self._pages(spec)
        pdf2md.infer_pagination(pages)
        pg = next(x for x in pages if x["pdf_page"] == 112)
        self.assertEqual((pg["book_page"], pg["book_page_basis"]), ("100", "corrected"))

    def test_edge_pages_marked_extrapolated_and_verify_reports_breaks(self):
        spec = {1: None, 2: None, 3: None, 4: None}
        spec.update({p: str(p - 2) for p in range(5, 40)})  # pdf 3,4 = p.1,2 without printed numbers
        pages = self._pages(spec)
        pdf2md.infer_pagination(pages)
        by = {pg["pdf_page"]: (pg["book_page"], pg["book_page_basis"]) for pg in pages}
        self.assertEqual(by[2], ("unnumbered", None))
        self.assertEqual(by[3], ("1", "extrapolated"))
        for pg in pages:
            pg["markdown"] = "text " * 10
        with tempfile.TemporaryDirectory() as td:
            md = Path(td) / "b.md"
            md.write_text(pdf2md.format_markdown(title="T", source_pdf=Path("/x.pdf"), source_sha="0" * 64,
                                                 total_pdf_pages=39, pdf_offset=2, tier="basic", pages=pages), encoding="utf-8")
            self.assertIn("原书 p.1（无印刷页码，推算）", md.read_text(encoding="utf-8"))
            ver = pdf2md.cmd_verify(md, expected_pages=39)
            self.assertTrue(ver["ok"])
            self.assertEqual(ver["extrapolated_pages"], [3, 4])
            self.assertEqual(ver["numbering_breaks"], [])
            broken = md.read_text(encoding="utf-8").replace("pdf_page: 20 | book_page: 18", "pdf_page: 20 | book_page: 19")
            md.write_text(broken, encoding="utf-8")
            self.assertEqual(len(pdf2md.cmd_verify(md, expected_pages=39)["numbering_breaks"]), 2)

    def test_status_of_dead_process_is_interrupted(self):
        st = {"state": "running", "host": pdf2md.socket.gethostname(), "pid": 2 ** 22 + 12345}
        self.assertEqual(pdf2md.with_liveness(st)["state"], "interrupted")
        self.assertEqual(pdf2md.with_liveness(dict(st, pid=pdf2md.os.getpid()))["state"], "running")

    def test_status_file_is_per_vault(self):
        a = pdf2md.status_file_for_slug("book", Path("/tmp/vault_a"))
        b = pdf2md.status_file_for_slug("book", Path("/tmp/vault_b"))
        self.assertNotEqual(a, b)

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


            # Verify missing local image asset is caught by cmd_verify, and present asset passes
            pdf2md.set_file_readonly(md_path, readonly=False)
            asset_rel = "assets/demo/p0002_image_1.png"
            with_img = md_text + f"\n![]({asset_rel})\n"
            md_path.write_text(with_img, encoding="utf-8")
            ver_missing_img = pdf2md.cmd_verify(md_path, expected_pages=2)
            self.assertFalse(ver_missing_img["ok"])
            self.assertEqual(ver_missing_img["missing_visual_assets"], [asset_rel])

            asset_file = Path(td) / asset_rel
            asset_file.parent.mkdir(parents=True, exist_ok=True)
            asset_file.write_bytes(b"PNG-DATA")
            ver_ok_img = pdf2md.cmd_verify(md_path, expected_pages=2)
            self.assertTrue(ver_ok_img["ok"])
            self.assertEqual(ver_ok_img["visual_assets_count"], 1)
            self.assertEqual(ver_ok_img["missing_visual_assets"], [])
            self.assertEqual(ver_ok_img["blank_pages"], [1])

    def test_dark_scan_fallback_anchor_and_blank_separation(self):
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            snap_rel = "assets/book/p0002_scan_fallback.png"
            (td_p / snap_rel).parent.mkdir(parents=True, exist_ok=True)
            (td_p / snap_rel).write_bytes(b"PNG-SNAPSHOT")
            md_path = td_p / "book.md"
            md_text = pdf2md.format_markdown(
                title="Dark Scan Demo",
                source_pdf=Path("/tmp/dark.pdf"),
                source_sha="a" * 64,
                total_pdf_pages=3,
                pdf_offset=None,
                tier="basic",
                pages=[
                    {"pdf_page": 1, "book_page": "unnumbered", "ocr_page": None, "header": None, "markdown": ""},
                    {
                        "pdf_page": 2,
                        "book_page": "unnumbered",
                        "ocr_page": None,
                        "header": "viii | England in 1760.",
                        "fallback": "dark_scan_recovered",
                        "markdown": f"> *[深色或低对比度扫描页]*\n\nRecovered text from dark scan.\n\n![]({snap_rel})",
                    },
                    {"pdf_page": 3, "book_page": "1", "book_page_basis": "printed", "ocr_page": "1", "header": "viii | Memoir.", "markdown": "Normal body text exceeding thirty characters."},
                ],
            )
            md_path.write_text(md_text, encoding="utf-8")
            ver = pdf2md.cmd_verify(md_path, expected_pages=3)
            self.assertTrue(ver["ok"])
            self.assertEqual(ver["verified_pages"], 3)
            self.assertEqual(ver["blank_pages"], [1])
            self.assertEqual(ver["fallback_scan_pages"], [2])
            self.assertEqual(ver["non_empty_text_pages"], 2)
            self.assertEqual(ver["visual_assets_count"], 1)
            self.assertEqual(ver["missing_visual_assets"], [])


if __name__ == "__main__":
    unittest.main()
