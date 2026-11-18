#!/usr/bin/env python3
"""Cross-platform (macOS & Windows) Scanned PDF -> Page-Anchored Markdown engine for LLM-Wiki.

Subcommands (designed so even lightweight/cheap Agent models execute 100% deterministically):
  doctor    Check OS, CPU, free RAM, MinerU venv & local ONNX models
  setup     One-shot isolated installation of MinerU 4.2 + basic ONNX models (auto ModelScope/HF)
  estimate  1-second preflight: page count, cached resume progress, hardware-calibrated ETA
  convert   Convert scanned PDF -> <vault>/raw/books/<slug>.md with auto RAM guard & chunk resume
  verify    Deterministic check of page completeness, dual page markers, and non-empty text ratio
"""
from __future__ import annotations

import argparse
import concurrent.futures
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

SKILL_VERSION = "1.1.0"


def default_mineru_venv() -> Path:
    if platform.system() == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "research-vault" / "mineru-venv"
    return Path.home() / ".local" / "share" / "research-vault" / "mineru-venv"


def default_vault_dir() -> Path:
    return Path.home() / "Documents" / "brain"


def venv_paths(venv_dir: Path) -> tuple[Path, Path]:
    if platform.system() == "Windows":
        return venv_dir / "Scripts" / "python.exe", venv_dir / "Scripts" / "mineru-kit.exe"
    return venv_dir / "bin" / "python", venv_dir / "bin" / "mineru-kit"


def onnx_model_dir() -> Path:
    return Path.home() / ".mineru" / "models" / "MinerU-4_models_onnx"


def models_ready() -> bool:
    mdir = onnx_model_dir()
    if not mdir.is_dir():
        return False
    return any(mdir.rglob("*.onnx"))


def find_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    candidates = [
        Path.home() / ".local" / "bin" / ("uv.exe" if platform.system() == "Windows" else "uv"),
        Path.home() / ".cargo" / "bin" / ("uv.exe" if platform.system() == "Windows" else "uv"),
        Path("/opt/homebrew/bin/uv"),
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def get_memory_gb() -> tuple[float, float]:
    """Return (total_gb, available_gb) across macOS, Windows, and Linux."""
    sysname = platform.system()
    if sysname == "Windows":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return round(stat.ullTotalPhys / (1024 ** 3), 2), round(stat.ullAvailPhys / (1024 ** 3), 2)
    elif sysname == "Darwin":
        try:
            tot = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True, timeout=3).strip())
            vm = subprocess.check_output(["vm_stat"], text=True, timeout=3)
            m_ps = re.search(r"page size of (\d+) bytes", vm)
            ps = int(m_ps.group(1)) if m_ps else 16384
            free_pages = 0
            for k in ("Pages free", "Pages speculative", "Pages purgeable"):
                m = re.search(rf"^{re.escape(k)}:\s+(\d+)\.", vm, flags=re.MULTILINE)
                if m:
                    free_pages += int(m.group(1))
            # Also count half of inactive pages as reclaimable cushion
            m_inact = re.search(r"^Pages inactive:\s+(\d+)\.", vm, flags=re.MULTILINE)
            reclaim_pages = free_pages + (int(m_inact.group(1)) // 2 if m_inact else 0)
            return round(tot / (1024 ** 3), 2), round((reclaim_pages * ps) / (1024 ** 3), 2)
        except Exception:
            pass
    return 16.0, 6.0


def recommend_concurrency(avail_ram_gb: float, cpu_count: int, sysname: str | None = None) -> tuple[int, int, int]:
    """Return safe (workers, chunk_size, threads_per_worker) without swap thrashing."""
    os_name = sysname or platform.system()
    if os_name == "Windows":
        # Windows uses lightweight onnxruntime-directml (~0.8 GB/worker, no PyTorch overhead).
        if avail_ram_gb >= 10.0 and cpu_count >= 12:
            return 3, 25, max(2, min(4, cpu_count // 3))
        if avail_ram_gb >= 5.5 and cpu_count >= 8:
            return 2, 25, max(2, min(4, cpu_count // 2))
        return 1, 25, max(2, min(6, cpu_count))
    # macOS / Linux
    if avail_ram_gb >= 14.0 and cpu_count >= 12:
        return 3, 25, max(2, min(4, cpu_count // 3))
    if avail_ram_gb >= 8.0 and cpu_count >= 8:
        return 2, 25, max(2, min(4, cpu_count // 2))
    return 1, 25, max(2, min(6, cpu_count))


def estimate_sec_per_page(workers: int) -> float:
    """Empirical calibrated per-page wall-clock rate by OS/arch and worker count."""
    is_apple_silicon = platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64")
    if is_apple_silicon:
        rates = {1: 6.8, 2: 3.8, 3: 2.6}
        return rates.get(workers, round(6.8 / min(3.2, workers * 0.8), 2))
    # Windows x64 (i7-1260P ONNX DirectML empirical: w1=11.7s/p, w2=5.8s/p)
    rates_x64 = {1: 11.7, 2: 5.9, 3: 4.3}
    return rates_x64.get(workers, round(11.7 / min(2.9, workers * 0.85), 2))


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


def pdf_page_count_and_text_probe(pdf_path: Path, mineru_py: Path | None) -> tuple[int, int]:
    """Return (total_pages, embedded_chars_in_first_5_pages)."""
    py = str(mineru_py) if (mineru_py and mineru_py.is_file()) else sys.executable
    code = (
        "import json, sys\n"
        "from pypdf import PdfReader\n"
        "r = PdfReader(sys.argv[1])\n"
        "n = len(r.pages)\n"
        "chars = sum(len((r.pages[i].extract_text() or '').strip()) for i in range(min(5, n)))\n"
        "print(json.dumps({'pages': n, 'chars': chars}))\n"
    )
    try:
        out = subprocess.check_output(
            [py, "-c", code, str(pdf_path)],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        d = json.loads(out.strip().splitlines()[-1])
        return int(d["pages"]), int(d["chars"])
    except Exception:
        pdfinfo = shutil.which("pdfinfo")
        if pdfinfo:
            proc = subprocess.run(
                [pdfinfo, str(pdf_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            m = re.search(r"^Pages:\s+(\d+)\s*$", proc.stdout, flags=re.MULTILINE)
            if m:
                return int(m.group(1)), 0
        raise RuntimeError("Cannot read PDF page count; run `pdf2md.py setup` first.")


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
    chunks: list[tuple[int, int]] = []
    for start, end in ranges:
        cur = start
        while cur <= end:
            nxt = min(end, cur + chunk_size - 1)
            chunks.append((cur, nxt))
            cur = nxt + 1
    return chunks


def cache_dir_for(vault: Path, book_slug: str, pdf_sha: str, tier: str) -> Path:
    return vault / ".vault-meta" / "ocr-cache" / f"{book_slug}_{pdf_sha[:12]}_{tier}"


def chunk_is_valid(cpath: Path, expected_pages: int) -> bool:
    if not cpath.is_file():
        return False
    try:
        d = json.loads(cpath.read_text(encoding="utf-8"))
        return len(d.get("pages", [])) == expected_pages
    except Exception:
        return False


def cmd_doctor(venv_dir: Path) -> dict:
    py, kit = venv_paths(venv_dir)
    tot_gb, avail_gb = get_memory_gb()
    cpus = os.cpu_count() or 4
    w, csz, thr = recommend_concurrency(avail_gb, cpus)
    venv_ok = py.is_file() and kit.is_file()
    mod_ok = models_ready()
    return {
        "ok": bool(venv_ok and mod_ok),
        "skill_version": SKILL_VERSION,
        "os": platform.system(),
        "arch": platform.machine(),
        "cpu_logical_cores": cpus,
        "ram_total_gb": tot_gb,
        "ram_available_gb": avail_gb,
        "uv_path": find_uv(),
        "mineru_venv": str(venv_dir),
        "venv_installed": venv_ok,
        "onnx_models_dir": str(onnx_model_dir()),
        "onnx_models_ready": mod_ok,
        "recommended": {
            "workers": w,
            "chunk_size": csz,
            "threads_per_worker": thr,
            "est_sec_per_page": estimate_sec_per_page(w),
        },
    }


def choose_model_source() -> str:
    """Pick modelscope on direct domestic network; huggingface when proxy is active."""
    has_proxy = any(os.environ.get(k) for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"))
    if has_proxy:
        return "huggingface"
    try:
        req = urllib.request.Request("https://modelscope.cn/api/v1/models/OpenDataLab/MinerU-4_models_onnx", method="HEAD")
        with urllib.request.urlopen(req, timeout=4):
            return "modelscope"
    except Exception:
        return "huggingface"


def choose_pypi_index() -> str | None:
    """Return None if pypi.org is reachable quickly, else Tsinghua/Aliyun domestic mirror."""
    has_proxy = any(os.environ.get(k) for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"))
    if has_proxy:
        return None
    try:
        req = urllib.request.Request("https://pypi.org/simple/pip/", method="HEAD")
        with urllib.request.urlopen(req, timeout=3):
            return None
    except Exception:
        for mirror in ("https://pypi.tuna.tsinghua.edu.cn/simple", "https://mirrors.aliyun.com/pypi/simple"):
            try:
                req = urllib.request.Request(f"{mirror}/pip/", method="HEAD")
                with urllib.request.urlopen(req, timeout=4):
                    return mirror
            except Exception:
                continue
        return "https://pypi.tuna.tsinghua.edu.cn/simple"


def detect_windows_registry_proxy() -> str | None:
    """Detect local proxy address configured in Windows Internet Settings (even when TUN mode sets ProxyEnable=0)."""
    if platform.system() != "Windows":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            srv, _ = winreg.QueryValueEx(key, "ProxyServer")
            srv_str = str(srv).strip()
            if srv_str:
                if "://" not in srv_str:
                    srv_str = f"http://{srv_str}"
                return srv_str
    except Exception:
        pass
    return None


def import_models_from_path(src_path: Path, target_model_dir: Path | None = None) -> None:
    """Import MinerU-4_models_onnx from a local directory or .tar archive."""
    src = Path(src_path).expanduser().resolve(strict=True)
    dest = target_model_dir or onnx_model_dir()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        cand = src / "MinerU-4_models_onnx" if (src / "MinerU-4_models_onnx").is_dir() else src
        if not any(cand.rglob("*.onnx")):
            raise ValueError(f"No .onnx files found under {cand}")
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(cand, dest)
        return
    if tarfile.is_tarfile(src):
        with tempfile.TemporaryDirectory(prefix="mineru-onnx-unpack-") as td:
            td_path = Path(td)
            with tarfile.open(src, "r:*") as tf:
                try:
                    tf.extractall(td_path, filter="data")
                except TypeError:
                    tf.extractall(td_path)
            cand = td_path / "MinerU-4_models_onnx" if (td_path / "MinerU-4_models_onnx").is_dir() else td_path
            if not any(cand.rglob("*.onnx")):
                raise ValueError(f"No .onnx files found inside archive {src}")
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(cand, dest)
        return
    raise ValueError(f"Unsupported models source: {src} (expected directory or tar archive)")


def cmd_setup(venv_dir: Path, source: str = "auto", models_from: Path | None = None) -> dict:
    t0 = time.perf_counter()
    uv = find_uv()
    if not uv:
        raise RuntimeError("`uv` not found. Install uv first (`brew install uv` or `winget install astral-sh.uv`).")
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    py, kit = venv_paths(venv_dir)
    pypi_index = choose_pypi_index()
    if not (py.is_file() and kit.is_file()):
        subprocess.run([uv, "venv", str(venv_dir), "--python", "3.12"], check=True)
        pip_cmd = [uv, "pip", "install", "--no-offline", "--python", str(py), "-U", "mineru>=4.2,<5"]
        if pypi_index:
            pip_cmd.extend(["--index-url", pypi_index])
        res = subprocess.run(pip_cmd)
        if res.returncode != 0 and not pypi_index:
            # Automatic fallback to Tsinghua mirror on TLS/GFW error
            pypi_index = "https://pypi.tuna.tsinghua.edu.cn/simple"
            subprocess.run(pip_cmd + ["--index-url", pypi_index], check=True)
        elif res.returncode != 0:
            raise RuntimeError(f"uv pip install mineru failed with code {res.returncode}")
    # Remove hf-xet to avoid proxy CAS Client Error stalls
    subprocess.run([uv, "pip", "uninstall", "--python", str(py), "hf-xet"], capture_output=True)

    chosen_source = "already_cached"
    if models_from is not None:
        import_models_from_path(models_from)
        chosen_source = f"local:{Path(models_from).name}"
    elif not models_ready():
        attempts: list[tuple[str, dict[str, str]]] = []
        win_proxy = detect_windows_registry_proxy()
        if source in ("auto", "modelscope"):
            no_proxy_env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
            no_proxy_env["NO_PROXY"] = "*"
            no_proxy_env["no_proxy"] = "*"
            attempts.append(("modelscope-direct", {"src": "modelscope", "env": no_proxy_env}))
            if win_proxy:
                wp_ms = dict(os.environ)
                wp_ms["HTTP_PROXY"] = win_proxy
                wp_ms["HTTPS_PROXY"] = win_proxy
                attempts.append(("modelscope-winproxy", {"src": "modelscope", "env": wp_ms}))
        if source in ("auto", "huggingface"):
            attempts.append(("huggingface", {"src": "huggingface", "env": dict(os.environ)}))
            if win_proxy:
                wp_hf = dict(os.environ)
                wp_hf["HTTP_PROXY"] = win_proxy
                wp_hf["HTTPS_PROXY"] = win_proxy
                attempts.append(("huggingface-winproxy", {"src": "huggingface", "env": wp_hf}))
            mirror_env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
            mirror_env["NO_PROXY"] = "*"
            mirror_env["HF_ENDPOINT"] = "https://hf-mirror.com"
            attempts.append(("hf-mirror-direct", {"src": "huggingface", "env": mirror_env}))

        for label, cfg in attempts:
            dl = subprocess.run(
                [str(kit), "models", "download", "--tier", "basic", "--source", cfg["src"], "-v"],
                env=cfg["env"],
            )
            if dl.returncode == 0 and models_ready():
                chosen_source = label
                break
        if not models_ready():
            raise RuntimeError(
                "mineru-kit models download failed across all domestic and proxy routes "
                "(tip: check VPN/Clash TUN Fake-IP node or pass `--models-from <dir_or_tar>`)."
            )

    status = cmd_doctor(venv_dir)
    status["setup_elapsed_sec"] = round(time.perf_counter() - t0, 2)
    status["pypi_index_used"] = pypi_index or "https://pypi.org/simple"
    status["model_source_used"] = chosen_source
    return status


def cmd_estimate(
    pdf_path: Path,
    *,
    vault: Path,
    venv_dir: Path,
    slug: str | None = None,
    pages_spec: str = "all",
    tier: str = "basic",
    workers: int | None = None,
    chunk_size: int = 25,
) -> dict:
    pdf_real = Path(pdf_path).expanduser().resolve(strict=True)
    py, _ = venv_paths(venv_dir)
    doc = cmd_doctor(venv_dir)
    total_pages, sample_chars = pdf_page_count_and_text_probe(pdf_real, py if py.is_file() else None)
    ranges = parse_page_selection(pages_spec, total_pages)
    chunks = split_chunks(ranges, chunk_size)

    book_slug = slugify(slug or pdf_real.stem)
    pdf_sha = sha256_file(pdf_real)
    cdir = cache_dir_for(vault, book_slug, pdf_sha, tier)

    cached_pages = 0
    selected_pages = 0
    for s, e in chunks:
        cnt = e - s + 1
        selected_pages += cnt
        if chunk_is_valid(cdir / f"chunk_{s:04d}_{e:04d}.json", cnt):
            cached_pages += cnt

    eff_workers = workers if workers is not None else doc["recommended"]["workers"]
    spp = estimate_sec_per_page(eff_workers)
    rem_pages = max(0, selected_pages - cached_pages)
    est_sec = round(rem_pages * spp + (15 if rem_pages > 0 else 0), 1)
    est_min = round(est_sec / 60.0, 1)
    out_md = Path(vault).expanduser().resolve() / "raw" / "books" / f"{book_slug}.md"

    return {
        "ok": True,
        "runtime_ready": doc["ok"],
        "pdf_path": str(pdf_real),
        "pdf_size_mb": round(pdf_real.stat().st_size / (1024 * 1024), 2),
        "total_pdf_pages": total_pages,
        "selected_pages": selected_pages,
        "cached_pages": cached_pages,
        "remaining_pages": rem_pages,
        "is_pure_scan": sample_chars < 20,
        "workers": eff_workers,
        "chunk_size": chunk_size,
        "est_sec_per_page": spp,
        "estimated_seconds": est_sec,
        "estimated_minutes": est_min,
        "requires_user_confirmation": est_sec > 60.0,
        "output_markdown": str(out_md),
        "cost_rmb": 0.0,
        "llm_tokens_used": 0,
        "human_message": (
            f"检测到《{pdf_real.stem}》共 {total_pages} 页（已完成断点缓存 {cached_pages} 页，待转 {rem_pages} 页），"
            f"使用本机免费 MinerU ONNX 模型（{eff_workers} 并发），预计耗时约 {est_min} 分钟（0 元 / 0 Token），"
            f"完成后自动存入 {out_md}。"
        ),
    }


def compute_book_page_label(pdf_page: int, ocr_page: str | None, pdf_offset: int | None) -> str:
    if ocr_page and ocr_page.strip():
        return ocr_page.strip()
    if pdf_offset is not None:
        calc = pdf_page - pdf_offset
        if calc >= 1:
            return str(calc)
    return "unnumbered"


def run_one_chunk(
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
    if chunk_is_valid(out_json, expected):
        return {"start": start_page, "end": end_page, "cached": True, "sec": 0.0}

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if models_ready():
        env["HF_HUB_OFFLINE"] = "1"
    thr = str(max(1, threads_per_worker))
    env["OMP_NUM_THREADS"] = thr
    env["MKL_NUM_THREADS"] = thr
    env["OPENBLAS_NUM_THREADS"] = thr

    tmp_json = out_json.with_suffix(f".tmp.{os.getpid()}.{start_page}.json")
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
        encoding="utf-8",
        errors="replace",
    )
    if not chunk_is_valid(tmp_json, expected):
        raise RuntimeError(f"Chunk {start_page}-{end_page} output invalid")
    os.replace(tmp_json, out_json)
    return {"start": start_page, "end": end_page, "cached": False, "sec": round(time.perf_counter() - t0, 2)}


def render_chunks_to_pages(
    chunk_targets: list[tuple[int, int, Path]],
    *,
    mineru_py: Path,
    pdf_offset: int | None,
) -> list[dict]:
    helper = r"""
import json, pathlib, sys
import docvortex

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
out_path = pathlib.Path(sys.argv[2])
results = []

for start_p, end_p, cpath in manifest["chunks"]:
    raw = json.loads(pathlib.Path(cpath).read_text(encoding="utf-8"))
    pages = raw.get("pages", [])
    for idx, pg in enumerate(pages):
        pdf_page = start_p + idx
        ocr_nums, headers = [], []
        for blk in pg.get("blocks", []):
            btype = blk.get("type")
            if btype in ("page_number", "number", "footer"):
                val = " ".join((c.get("content", "") if isinstance(c, dict) else str(c)) for c in (blk.get("content") or [])).strip()
                if val:
                    ocr_nums.append(val)
            elif btype == "header":
                val = " ".join((c.get("content", "") if isinstance(c, dict) else str(c)) for c in (blk.get("content") or [])).strip()
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
    with tempfile.TemporaryDirectory(prefix="pdf2md-render-") as td:
        in_p = Path(td) / "in.json"
        out_p = Path(td) / "out.json"
        in_p.write_text(json.dumps({"chunks": [[s, e, str(p)] for s, e, p in chunk_targets]}, ensure_ascii=False), encoding="utf-8")
        subprocess.run([str(mineru_py), "-c", helper, str(in_p), str(out_p)], check=True)
        pages = json.loads(out_p.read_text(encoding="utf-8"))
    for pg in pages:
        pg["book_page"] = compute_book_page_label(pg["pdf_page"], pg.get("ocr_page"), pdf_offset)
    return pages


def format_markdown(
    *,
    title: str,
    source_pdf: Path,
    source_sha: str,
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
        f"source_sha256: {source_sha}",
        f"total_pdf_pages: {total_pdf_pages}",
        f"converted_pages: {len(pages)}",
        f"pdf_offset: {pdf_offset if pdf_offset is not None else 'null'}",
        f"engine: \"MinerU-4.2-ONNX-{tier}\"",
        f"generated_at: {datetime.now(timezone.utc).isoformat()}",
        "---",
        "",
        f"# {title}",
        "",
        f"- **原始文件**：`{source_pdf}`",
        f"- **总页数**：`{total_pdf_pages}`（已转写 `{len(pages)}` 页）",
        "",
        "---",
        "",
    ]
    for pg in pages:
        pdf_p = pg["pdf_page"]
        book_p = pg["book_page"]
        ocr_p = pg.get("ocr_page") or ""
        hdr = (pg.get("header") or "").replace("-->", "—>")
        meta = [f"pdf_page: {pdf_p}", f"book_page: {book_p}"]
        if ocr_p:
            meta.append(f"ocr_page: {ocr_p}")
        if hdr:
            meta.append(f"header: {hdr}")
        lines.append(f"<!-- {' | '.join(meta)} -->")
        book_disp = f"原书 p.{book_p}" if book_p != "unnumbered" else "原书未标页码"
        hdr_disp = f" · {hdr}" if hdr else ""
        lines.append(f"### [PDF p.{pdf_p} | {book_disp}{hdr_disp}]")
        lines.append("")
        body = (pg.get("markdown") or "").strip()
        lines.append(body if body else "*[空白页或纯插图页]*")
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


def cmd_verify(md_path: Path, expected_pages: int | None = None) -> dict:
    md_real = Path(md_path).expanduser().resolve(strict=True)
    text = md_real.read_text(encoding="utf-8")
    anchor_re = re.compile(r"^<!--\s*pdf_page:\s*(\d+)\s*\|\s*book_page:\s*([^|>]+?)(?:\s*\|.*?)?-->$", re.MULTILINE)
    matches = list(anchor_re.finditer(text))
    pdf_pages = [int(m.group(1)) for m in matches]
    book_pages = [m.group(2).strip() for m in matches]

    non_empty = 0
    for idx, m in enumerate(matches):
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        seg = text[start:end]
        if "*[空白页或纯插图页]*" not in seg and "*[本页为空白页或纯图像页]*" not in seg and len(seg.strip()) > 30:
            non_empty += 1

    duplicates = len(pdf_pages) - len(set(pdf_pages))
    missing: list[int] = []
    if expected_pages is not None:
        present = set(pdf_pages)
        missing = [p for p in range(1, expected_pages + 1) if p not in present]

    numbered_book_pages = sum(1 for b in book_pages if b != "unnumbered")
    ok = (
        len(pdf_pages) > 0
        and duplicates == 0
        and len(missing) == 0
        and (expected_pages is None or len(pdf_pages) == expected_pages)
    )
    return {
        "ok": ok,
        "markdown_path": str(md_real),
        "bytes": md_real.stat().st_size,
        "verified_pages": len(pdf_pages),
        "expected_pages": expected_pages,
        "missing_pages": missing[:20],
        "duplicate_page_count": duplicates,
        "non_empty_text_pages": non_empty,
        "book_page_labeled_count": numbered_book_pages,
    }


def cmd_convert(
    pdf_path: Path,
    *,
    vault: Path,
    venv_dir: Path,
    slug: str | None = None,
    title: str | None = None,
    pdf_offset: int | None = None,
    pages_spec: str = "all",
    tier: str = "basic",
    workers: int | None = None,
    chunk_size: int | None = None,
    threads_per_worker: int | None = None,
    out_file: Path | None = None,
) -> dict:
    doc = cmd_doctor(venv_dir)
    if not doc["ok"]:
        raise RuntimeError("MinerU runtime or ONNX models not ready. Run `pdf2md.py setup` first.")

    py, kit = venv_paths(venv_dir)
    pdf_real = Path(pdf_path).expanduser().resolve(strict=True)
    vault_real = Path(vault).expanduser().resolve()

    rec = doc["recommended"]
    eff_workers = workers if workers is not None else rec["workers"]
    eff_chunk = chunk_size if chunk_size is not None else rec["chunk_size"]
    eff_threads = threads_per_worker if threads_per_worker is not None else rec["threads_per_worker"]

    # Hard safety cap against swap thrashing (only clamp auto mode unless severely low <2.0 GB)
    if workers is None:
        min_w2 = 5.0 if platform.system() == "Windows" else 7.5
        if doc["ram_available_gb"] < min_w2 and eff_workers > 1:
            eff_workers = 1
        if doc["ram_available_gb"] < 3.5 and eff_workers > 1:
            eff_workers = 1
    elif doc["ram_available_gb"] < 2.0 and eff_workers > 1:
        eff_workers = 1

    sha_before = sha256_file(pdf_real)
    total_pages, _ = pdf_page_count_and_text_probe(pdf_real, py)
    ranges = parse_page_selection(pages_spec, total_pages)
    chunks = split_chunks(ranges, eff_chunk)

    book_slug = slugify(slug or pdf_real.stem)
    book_title = (title or pdf_real.stem).strip()
    cdir = cache_dir_for(vault_real, book_slug, sha_before, tier)
    cdir.mkdir(parents=True, exist_ok=True)

    chunk_targets = [(s, e, cdir / f"chunk_{s:04d}_{e:04d}.json") for s, e in chunks]
    t0 = time.perf_counter()
    newly_computed_pages = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, eff_workers)) as pool:
        futs = {
            pool.submit(
                run_one_chunk,
                pdf_path=pdf_real,
                start_page=s,
                end_page=e,
                out_json=cp,
                mineru_kit=kit,
                tier=tier,
                threads_per_worker=eff_threads,
            ): (s, e)
            for s, e, cp in chunk_targets
        }
        done_n = 0
        for fut in concurrent.futures.as_completed(futs):
            info = fut.result()
            done_n += 1
            if not info["cached"]:
                newly_computed_pages += info["end"] - info["start"] + 1
            print(
                f"[{done_n}/{len(chunk_targets)}] pages {info['start']}-{info['end']} "
                f"({'cached' if info['cached'] else str(info['sec']) + 's'})",
                file=sys.stderr,
                flush=True,
            )

    rendered = render_chunks_to_pages(chunk_targets, mineru_py=py, pdf_offset=pdf_offset)
    md_text = format_markdown(
        title=book_title,
        source_pdf=pdf_real,
        source_sha=sha_before,
        total_pdf_pages=total_pages,
        pdf_offset=pdf_offset,
        tier=tier,
        pages=rendered,
    )

    target_md = Path(out_file).expanduser().resolve() if out_file else (vault_real / "raw" / "books" / f"{book_slug}.md")
    target_md.parent.mkdir(parents=True, exist_ok=True)
    target_md.write_text(md_text, encoding="utf-8")

    if sha256_file(pdf_real) != sha_before:
        raise RuntimeError("Source PDF modified unexpectedly during OCR")

    elapsed = round(time.perf_counter() - t0, 2)
    expected_v = total_pages if pages_spec.strip().lower() in ("", "all") else None
    ver = cmd_verify(target_md, expected_pages=expected_v)

    return {
        "ok": ver["ok"],
        "title": book_title,
        "source_pdf": str(pdf_real),
        "output_markdown": str(target_md),
        "total_pdf_pages": total_pages,
        "converted_pages": len(rendered),
        "newly_computed_pages": newly_computed_pages,
        "workers_used": eff_workers,
        "chunk_size_used": eff_chunk,
        "elapsed_sec": elapsed,
        "elapsed_min": round(elapsed / 60.0, 2),
        "sec_per_page": round(elapsed / max(1, len(rendered)), 3),
        "cost_rmb": 0.0,
        "llm_tokens_used": 0,
        "verification": ver,
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    p_doc = sub.add_parser("doctor", help="Check local runtime, RAM, and ONNX models")
    p_doc.add_argument("--venv", type=Path, default=default_mineru_venv())

    p_set = sub.add_parser("setup", help="Install MinerU 4.2 + local ONNX models automatically")
    p_set.add_argument("--venv", type=Path, default=default_mineru_venv())
    p_set.add_argument("--source", choices=("auto", "modelscope", "huggingface"), default="auto")
    p_set.add_argument("--models-from", type=Path, default=None, help="Optional local directory or .tar archive of MinerU-4_models_onnx")

    p_est = sub.add_parser("estimate", help="1-second preflight ETA and resume check")
    p_est.add_argument("pdf", type=Path)
    p_est.add_argument("--vault", type=Path, default=default_vault_dir())
    p_est.add_argument("--venv", type=Path, default=default_mineru_venv())
    p_est.add_argument("--slug", type=str, default=None)
    p_est.add_argument("--pages", type=str, default="all")
    p_est.add_argument("--workers", type=int, default=None)
    p_est.add_argument("--chunk-size", type=int, default=25)

    p_conv = sub.add_parser("convert", help="Convert scanned PDF to page-anchored Markdown")
    p_conv.add_argument("pdf", type=Path)
    p_conv.add_argument("--vault", type=Path, default=default_vault_dir())
    p_conv.add_argument("--venv", type=Path, default=default_mineru_venv())
    p_conv.add_argument("--slug", type=str, default=None)
    p_conv.add_argument("--title", type=str, default=None)
    p_conv.add_argument("--pdf-offset", type=int, default=None)
    p_conv.add_argument("--pages", type=str, default="all")
    p_conv.add_argument("--tier", choices=("basic", "standard", "flash", "advanced"), default="basic")
    p_conv.add_argument("--workers", type=int, default=None)
    p_conv.add_argument("--chunk-size", type=int, default=None)
    p_conv.add_argument("--threads-per-worker", type=int, default=None)
    p_conv.add_argument("--out", type=Path, default=None)

    p_ver = sub.add_parser("verify", help="Verify converted Markdown completeness and page markers")
    p_ver.add_argument("markdown", type=Path)
    p_ver.add_argument("--expected-pages", type=int, default=None)

    args = p.parse_args(argv)
    try:
        if args.cmd == "doctor":
            res = cmd_doctor(args.venv)
        elif args.cmd == "setup":
            res = cmd_setup(args.venv, source=args.source, models_from=args.models_from)
        elif args.cmd == "estimate":
            res = cmd_estimate(args.pdf, vault=args.vault, venv_dir=args.venv, slug=args.slug, pages_spec=args.pages, workers=args.workers, chunk_size=args.chunk_size)
        elif args.cmd == "convert":
            res = cmd_convert(
                args.pdf,
                vault=args.vault,
                venv_dir=args.venv,
                slug=args.slug,
                title=args.title,
                pdf_offset=args.pdf_offset,
                pages_spec=args.pages,
                tier=args.tier,
                workers=args.workers,
                chunk_size=args.chunk_size,
                threads_per_worker=args.threads_per_worker,
                out_file=args.out,
            )
        elif args.cmd == "verify":
            res = cmd_verify(args.markdown, expected_pages=args.expected_pages)
        else:
            raise ValueError(f"Unknown command: {args.cmd}")
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok", True) else 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
