#!/usr/bin/env python3
"""Convert a scanned/image PDF into page-anchored Markdown in raw/books/ and register its snapshot.

Engine: MinerU 4.2+ (ONNX basic tier + middle_json per-page assembly), preserving:
  1. 1-based physical PDF page numbers (`pdf_page`)
  2. OCR-detected printed book page numbers (`ocr_page`) + optional `--pdf-offset` fallback (`book_page`)
  3. Running headers (`header`), structured borderless/bordered tables (`<table>`), and footnotes
  4. Crash-safe per-chunk caching under `.vault-meta/ocr-cache/` + multi-worker CPU parallelism
  5. Immutable provenance registration via `tools/source_registry.py` (original PDF stays untouched)
"""
from __future__ import annotations

import argparse
import concurrent.futures
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

DEFAULT_VAULT = Path(__file__).resolve().parent.parent
DEFAULT_MINERU_VENV = Path.home() / ".local" / "share" / "research-vault" / "mineru-venv"
EXTRACTOR_ID = "ingest-scanned-pdf/v1"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def slugify(name: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", name.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        raise ValueError("Slug cannot be empty")
    return cleaned


def pdf_page_count(pdf_path: Path, mineru_python: Path | None = None) -> int:
    pdfinfo = shutil.which("pdfinfo")
    if pdfinfo:
        proc = subprocess.run([pdfinfo, str(pdf_path)], capture_output=True, text=True)
        if proc.returncode == 0:
            m = re.search(r"^Pages:\s+(\d+)\s*$", proc.stdout, flags=re.MULTILINE)
            if m:
                return int(m.group(1))
    py = str(mineru_python) if (mineru_python and mineru_python.is_file()) else sys.executable
    out = subprocess.check_output(
        [py, "-c", "import sys; from pypdf import PdfReader; print(len(PdfReader(sys.argv[1]).pages))", str(pdf_path)],
        text=True,
    )
    return int(out.strip())


def parse_page_selection(spec: str, total_pages: int) -> list[tuple[int, int]]:
    spec = spec.strip().lower()
    if spec in ("", "all"):
        return [(1, total_pages)]
    ranges: list[tuple[int, int]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a_str, b_str = part.split("-", 1)
            a, b = int(a_str), int(b_str)
        else:
            a = b = int(part)
        if not (1 <= a <= b <= total_pages):
            raise ValueError(f"Page range {part!r} out of bounds (1..{total_pages})")
        ranges.append((a, b))
    return ranges


def split_chunks(ranges: list[tuple[int, int]], chunk_size: int) -> list[tuple[int, int]]:
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    chunks: list[tuple[int, int]] = []
    for start, end in ranges:
        cur = start
        while cur <= end:
            nxt = min(end, cur + chunk_size - 1)
            chunks.append((cur, nxt))
            cur = nxt + 1
    return chunks


def extract_inline_text(block: dict) -> str:
    parts: list[str] = []
    content = block.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                txt = item.get("content")
                if isinstance(txt, str):
                    parts.append(txt)
    return " ".join(" ".join(parts).split()).strip()


def compute_book_page_label(pdf_page: int, ocr_page: str | None, pdf_offset: int | None) -> str:
    if ocr_page:
        cleaned = ocr_page.strip()
        if cleaned:
            return cleaned
    if pdf_offset is not None:
        calc = pdf_page - pdf_offset
        if calc >= 1:
            return str(calc)
    return "unnumbered"


def render_pages_via_docvortex(
    chunk_files: list[tuple[int, int, Path]],
    *,
    mineru_python: Path,
    pdf_offset: int | None = None,
) -> list[dict]:
    """Render each page's blocks into clean Markdown using MinerU's docvortex renderer."""
    helper_code = r"""
import json, pathlib, sys
import docvortex

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
out_path = pathlib.Path(sys.argv[2])
results = []

for start_p, end_p, cpath in manifest["chunks"]:
    raw = json.loads(pathlib.Path(cpath).read_text(encoding="utf-8"))
    pages = raw.get("pages", [])
    expected_len = end_p - start_p + 1
    if len(pages) != expected_len:
        raise RuntimeError(f"Chunk {cpath} expected {expected_len} pages, got {len(pages)}")
    for idx, pg in enumerate(pages):
        pdf_page = start_p + idx
        ocr_nums = []
        headers = []
        for blk in pg.get("blocks", []):
            btype = blk.get("type")
            if btype in ("page_number", "number", "footer"):
                val = " ".join(
                    (c.get("content", "") if isinstance(c, dict) else str(c))
                    for c in (blk.get("content") or [])
                ).strip()
                if val:
                    ocr_nums.append(val)
            elif btype == "header":
                val = " ".join(
                    (c.get("content", "") if isinstance(c, dict) else str(c))
                    for c in (blk.get("content") or [])
                ).strip()
                if val:
                    headers.append(val)
        single_doc = {
            "schema": raw.get("schema", "docvortex.middle"),
            "schema_version": raw.get("schema_version", "2.0"),
            "is_full_document": False,
            "metadata": raw.get("metadata", {}),
            "extensions": raw.get("extensions", {}),
            "pages": [pg],
        }
        mj = docvortex.schema.MiddleJson.model_validate(single_doc)
        md = docvortex.render_artifact(mj, "markdown").content.decode("utf-8").strip()
        results.append({
            "pdf_page": pdf_page,
            "ocr_page": ocr_nums[0] if ocr_nums else None,
            "header": " | ".join(headers) if headers else None,
            "markdown": md,
        })

out_path.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
"""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="render-mineru-") as td:
        td_path = Path(td)
        in_json = td_path / "chunks.json"
        out_json = td_path / "rendered.json"
        in_json.write_text(
            json.dumps(
                {"chunks": [[s, e, str(p)] for s, e, p in chunk_files]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        subprocess.run([str(mineru_python), "-c", helper_code, str(in_json), str(out_json)], check=True)
        pages = json.loads(out_json.read_text(encoding="utf-8"))

    for item in pages:
        item["book_page"] = compute_book_page_label(item["pdf_page"], item.get("ocr_page"), pdf_offset)
    return pages


def build_book_markdown(
    *,
    title: str,
    source_pdf: Path,
    source_sha256: str,
    total_pdf_pages: int,
    pdf_offset: int | None,
    tier: str,
    pages: list[dict],
) -> str:
    lines = [
        "---",
        "type: raw_book_ocr",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"source_pdf: {json.dumps(str(source_pdf), ensure_ascii=False)}",
        f"source_sha256: {source_sha256}",
        f"total_pdf_pages: {total_pdf_pages}",
        f"converted_pages: {len(pages)}",
        f"pdf_offset: {pdf_offset if pdf_offset is not None else 'null'}",
        f"extractor: {json.dumps(f'{EXTRACTOR_ID} (mineru-{tier}-middle-json)', ensure_ascii=False)}",
        f"generated_at: {datetime.now(timezone.utc).isoformat()}",
        "---",
        "",
        f"# {title}",
        "",
        f"- **原始扫描文件**：`{source_pdf}`",
        f"- **原始文件 SHA-256**：`{source_sha256}`",
        f"- **PDF 总页数**：`{total_pdf_pages}`（本文件已转写 `{len(pages)}` 页）",
        "- **直引核验规范**：凡将本书句子加双引号直接写入论文，须先运行 `python3 tools/verify-pdf-quote.py <原PDF路径> --pdf-page <N>` 导出原扫描单页图片做人工/多模态字形终核，防止同音或形近字误识（如“权利/权力”“己/已”“间/问”）。",
        "",
        "---",
        "",
    ]

    for pg in pages:
        pdf_p = pg["pdf_page"]
        book_p = pg["book_page"]
        ocr_p = pg.get("ocr_page") or ""
        hdr = (pg.get("header") or "").replace("-->", "—>")
        meta_parts = [f"pdf_page: {pdf_p}", f"book_page: {book_p}"]
        if ocr_p:
            meta_parts.append(f"ocr_page: {ocr_p}")
        if hdr:
            meta_parts.append(f"header: {hdr}")
        lines.append(f"<!-- {' | '.join(meta_parts)} -->")
        book_disp = f"原书 p.{book_p}" if book_p != "unnumbered" else "原书未标页码"
        hdr_disp = f" · {hdr}" if hdr else ""
        lines.append(f"### [PDF p.{pdf_p} | {book_disp}{hdr_disp}]")
        lines.append("")
        body = (pg.get("markdown") or "").strip()
        lines.append(body if body else "*[本页为空白页或纯图像页]*")
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def run_chunk_ocr(
    *,
    pdf_path: Path,
    start_page: int,
    end_page: int,
    out_json: Path,
    mineru_kit: Path,
    tier: str,
    threads_per_worker: int,
) -> dict:
    expected = end_page - start_page + 1
    if out_json.is_file():
        try:
            existing = json.loads(out_json.read_text(encoding="utf-8"))
            if len(existing.get("pages", [])) == expected:
                return {"start": start_page, "end": end_page, "cached": True, "sec": 0.0}
        except Exception:
            pass

    env = dict(os.environ)
    if (Path.home() / ".mineru" / "models" / "MinerU-4_models_onnx").is_dir():
        env["HF_HUB_OFFLINE"] = "1"
    thr = str(max(1, threads_per_worker))
    env["OMP_NUM_THREADS"] = thr
    env["MKL_NUM_THREADS"] = thr
    env["OPENBLAS_NUM_THREADS"] = thr

    tmp_json = out_json.with_suffix(f".tmp.{os.getpid()}.json")
    t0 = time.perf_counter()
    subprocess.run(
        [
            str(mineru_kit),
            "parse",
            str(pdf_path),
            "-o",
            str(tmp_json),
            "--pages",
            f"{start_page}-{end_page}",
            "--format",
            "middle_json",
            "--tier",
            tier,
            "--ocr-mode",
            "ocr",
            "--disable-image-analysis",
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = json.loads(tmp_json.read_text(encoding="utf-8"))
    if len(loaded.get("pages", [])) != expected:
        raise RuntimeError(f"Chunk {start_page}-{end_page} returned {len(loaded.get('pages', []))} pages")
    os.replace(tmp_json, out_json)
    return {"start": start_page, "end": end_page, "cached": False, "sec": round(time.perf_counter() - t0, 2)}


def detect_available_ram_gb() -> float | None:
    """Estimate truly free + speculative + purgeable RAM on macOS via vm_stat."""
    vm_stat = shutil.which("vm_stat")
    if not vm_stat:
        return None
    try:
        out = subprocess.check_output([vm_stat], text=True, timeout=3)
        m_ps = re.search(r"page size of (\d+) bytes", out)
        page_size = int(m_ps.group(1)) if m_ps else 16384
        vals = {}
        for key in ("Pages free", "Pages speculative", "Pages purgeable"):
            m = re.search(rf"^{re.escape(key)}:\s+(\d+)\.", out, flags=re.MULTILINE)
            if m:
                vals[key] = int(m.group(1))
        avail_bytes = sum(vals.values()) * page_size
        return round(avail_bytes / (1024 ** 3), 2)
    except Exception:
        return None


def ingest_pdf(
    pdf_path: Path,
    *,
    vault: Path = DEFAULT_VAULT,
    slug: str | None = None,
    title: str | None = None,
    pdf_offset: int | None = None,
    pages_spec: str = "all",
    tier: str = "basic",
    workers: int = 2,
    chunk_size: int = 32,
    threads_per_worker: int = 4,
    mineru_venv: Path = DEFAULT_MINERU_VENV,
    register: bool = True,
) -> dict:
    vault = Path(vault).resolve()
    pdf_real = Path(pdf_path).expanduser().resolve(strict=True)
    mineru_kit = Path(mineru_venv) / "bin" / "mineru-kit"
    mineru_py = Path(mineru_venv) / "bin" / "python"
    if not mineru_kit.is_file() or not mineru_py.is_file():
        raise RuntimeError(f"MinerU runtime not found at {mineru_venv}")

    avail_gb = detect_available_ram_gb()
    if avail_gb is not None and avail_gb < 5.0 and workers > 2:
        print(
            f"[ram-guard] Free RAM is {avail_gb} GiB (<5 GiB); auto-throttling workers {workers}->2, chunk_size {chunk_size}->35 to prevent swap thrashing.",
            file=sys.stderr,
            flush=True,
        )
        workers = 2
        chunk_size = min(chunk_size, 35)
        threads_per_worker = max(threads_per_worker, 4)

    sha_before = sha256_file(pdf_real)
    total_pages = pdf_page_count(pdf_real, mineru_py)
    ranges = parse_page_selection(pages_spec, total_pages)
    chunks = split_chunks(ranges, chunk_size)

    book_slug = slugify(slug or pdf_real.stem)
    book_title = (title or pdf_real.stem).strip()

    cache_dir = vault / ".vault-meta" / "ocr-cache" / f"{book_slug}_{sha_before[:12]}_{tier}"
    cache_dir.mkdir(parents=True, exist_ok=True)

    t_start = time.perf_counter()
    chunk_targets = [(s, e, cache_dir / f"chunk_{s:04d}_{e:04d}.json") for s, e in chunks]

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        future_map = {
            pool.submit(
                run_chunk_ocr,
                pdf_path=pdf_real,
                start_page=s,
                end_page=e,
                out_json=cpath,
                mineru_kit=mineru_kit,
                tier=tier,
                threads_per_worker=threads_per_worker,
            ): (s, e)
            for s, e, cpath in chunk_targets
        }
        done_cnt = 0
        for fut in concurrent.futures.as_completed(future_map):
            info = fut.result()
            done_cnt += 1
            print(
                f"[{done_cnt}/{len(chunk_targets)}] pages {info['start']}-{info['end']} "
                f"({'cached' if info['cached'] else str(info['sec']) + 's'})",
                file=sys.stderr,
                flush=True,
            )

    rendered_pages = render_pages_via_docvortex(
        chunk_targets,
        mineru_python=mineru_py,
        pdf_offset=pdf_offset,
    )
    md_text = build_book_markdown(
        title=book_title,
        source_pdf=pdf_real,
        source_sha256=sha_before,
        total_pdf_pages=total_pages,
        pdf_offset=pdf_offset,
        tier=tier,
        pages=rendered_pages,
    )

    out_rel = f"raw/books/{book_slug}.md"
    out_path = vault / out_rel
    out_path.parent.mkdir(parents=True, exist_ok=True)
    md_bytes = md_text.encode("utf-8")
    out_path.write_bytes(md_bytes)

    sha_after = sha256_file(pdf_real)
    if sha_after != sha_before:
        raise RuntimeError(f"Original PDF changed during OCR: {pdf_real}")

    reg_info = None
    if register:
        sys.path.insert(0, str(vault / "tools"))
        import source_registry as sr

        logical_id = f"book:{book_slug}"
        existing_rec = sr.lookup(out_rel, vault=vault)
        aliases = [] if existing_rec else [out_rel]
        spec = sr.SourceSpec(
            logical_id=logical_id,
            source_path=pdf_real,
            source_type="scanned_book_pdf_markdown",
            adoption_status="source_only",
            locator="document",
            content=md_bytes,
            suffix=".md",
            aliases=aliases,
            extractor=EXTRACTOR_ID,
            metadata={
                "title": book_title,
                "total_pdf_pages": total_pages,
                "converted_pages": len(rendered_pages),
                "pdf_offset": pdf_offset,
                "tier": tier,
                "output_relpath": out_rel,
            },
            expected_hashes={str(pdf_real): sha_before},
        )
        pub = sr.publish_batch([spec], vault=vault, write=True)
        entry = pub["manifest"]["sources"]["src-" + sr.sha256(logical_id.encode())[:20]]
        latest_ver = entry["latest_version"]
        ver_rec = next(v for v in entry["versions"] if v["version"] == latest_ver)
        reg_info = {
            "source_id": ver_rec["source_id"],
            "version": latest_ver,
            "snapshot_path": ver_rec["snapshot_path"],
            "aliases": ver_rec["aliases"],
        }

    elapsed = round(time.perf_counter() - t_start, 2)
    return {
        "ok": True,
        "title": book_title,
        "source_pdf": str(pdf_real),
        "source_sha256": sha_before,
        "output_markdown": str(out_path),
        "output_relpath": out_rel,
        "total_pdf_pages": total_pages,
        "converted_pages": len(rendered_pages),
        "elapsed_sec": elapsed,
        "sec_per_page": round(elapsed / max(1, len(rendered_pages)), 3),
        "registry": reg_info,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="Path to scanned PDF file (read-only)")
    parser.add_argument("--slug", type=str, default=None, help="Output filename stem under raw/books/<slug>.md")
    parser.add_argument("--title", type=str, default=None, help="Display title of the book")
    parser.add_argument("--pdf-offset", type=int, default=None, help="Fallback offset: book_page = pdf_page - offset")
    parser.add_argument("--pages", type=str, default="all", help="Pages to convert, e.g. 'all' or '1-50'")
    parser.add_argument("--tier", choices=("basic", "standard", "flash", "advanced"), default="basic")
    parser.add_argument("--workers", type=int, default=2, help="Parallel chunk workers (default: 2; auto-throttled if RAM < 14 GiB)")
    parser.add_argument("--chunk-size", type=int, default=32, help="Pages per chunk (default: 32)")
    parser.add_argument("--threads-per-worker", type=int, default=4, help="OMP threads per worker (default: 4)")
    parser.add_argument("--mineru-venv", type=Path, default=DEFAULT_MINERU_VENV)
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    parser.add_argument("--no-register", action="store_true", help="Skip source_registry.py registration")
    args = parser.parse_args(argv)

    res = ingest_pdf(
        args.pdf,
        vault=args.vault,
        slug=args.slug,
        title=args.title,
        pdf_offset=args.pdf_offset,
        pages_spec=args.pages,
        tier=args.tier,
        workers=args.workers,
        chunk_size=args.chunk_size,
        threads_per_worker=args.threads_per_worker,
        mineru_venv=args.mineru_venv,
        register=not args.no_register,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
