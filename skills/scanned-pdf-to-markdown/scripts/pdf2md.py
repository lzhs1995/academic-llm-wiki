#!/usr/bin/env python3
"""Cross-platform (macOS & Windows) Scanned PDF -> Page-Anchored Markdown engine for LLM-Wiki.

Subcommands (designed so even lightweight/cheap Agent models execute 100% deterministically):
  doctor    Check OS, CPU, free RAM, load average, MinerU venv & local ONNX models
  setup     One-shot isolated installation of MinerU 4.2 + basic ONNX models (auto ModelScope/HF)
  estimate  1-second preflight: page count, cached resume progress, ETA from this machine's measured history
  convert   Convert scanned PDF -> <vault>/raw/books/<slug>.md with auto RAM guard, chunk resume, status.json & 0444 lock
  status    Progress / live ETA / result of a running or finished convert (reads status.json)
  verify    Deterministic check of page completeness, modal page-label validity, read-only protection and output SHA-256

Book page labels (原书页码) are computed from the modal PDF->print offset detected across the book
(plus Roman frontmatter and optional publisher backmatter sequences); single-page OCR page numbers
are used only as votes and cross-checks, preventing misreads like '12I', 'III', 'B', or '福'.
"""
from __future__ import annotations

import argparse
import collections
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
import socket
import stat as stat_mod
import statistics
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request

SKILL_VERSION = "2.0.0"
OUTPUT_ENGINE_TAG = "scanned-pdf-to-markdown/2.0.0"

ROMAN_RE = re.compile(
    r"^(?=[mdclxvi]+$)m{0,3}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$",
    re.IGNORECASE,
)
VALID_BOOK_PAGE_RE = re.compile(
    r"^(?:unnumbered|[1-9]\d{0,4}|back-[1-9]\d{0,3}|(?=[mdclxvi]+$)m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3}))$"
)


def default_mineru_venv() -> Path:
    if os.environ.get("PDF2MD_VENV"):
        return Path(os.environ["PDF2MD_VENV"]).expanduser()
    if platform.system() == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "research-vault" / "mineru-venv"
    return Path.home() / ".local" / "share" / "research-vault" / "mineru-venv"


def default_vault_dir() -> Path:
    if os.environ.get("PDF2MD_VAULT"):
        return Path(os.environ["PDF2MD_VAULT"]).expanduser()
    return Path.home() / "Documents" / "brain"


def state_dir() -> Path:
    """Per-machine state (measured speed history & active job status). Outside the vault."""
    if os.environ.get("PDF2MD_STATE"):
        return Path(os.environ["PDF2MD_STATE"]).expanduser()
    if platform.system() == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "scanned-pdf-to-markdown"
    return Path.home() / ".local" / "state" / "scanned-pdf-to-markdown"


def load_average() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except (AttributeError, OSError):
        return None


def venv_paths(venv_dir: Path) -> tuple[Path, Path]:
    if platform.system() == "Windows":
        return venv_dir / "Scripts" / "python.exe", venv_dir / "Scripts" / "mineru-kit.exe"
    return venv_dir / "bin" / "python", venv_dir / "bin" / "mineru-kit"


def onnx_model_dir() -> Path:
    if os.environ.get("PDF2MD_MODELS_DIR"):
        return Path(os.environ["PDF2MD_MODELS_DIR"]).expanduser()
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
        if avail_ram_gb >= 10.0 and cpu_count >= 12:
            return 3, 25, max(2, min(4, cpu_count // 3))
        if avail_ram_gb >= 5.0 and cpu_count >= 8:
            return 2, 25, max(2, min(4, cpu_count // 2))
        return 1, 25, max(2, min(6, cpu_count))
    if avail_ram_gb >= 14.0 and cpu_count >= 12:
        return 3, 25, max(2, min(4, cpu_count // 3))
    if avail_ram_gb >= 8.0 and cpu_count >= 8:
        return 2, 25, max(2, min(4, cpu_count // 2))
    return 1, 25, max(2, min(6, cpu_count))


def estimate_sec_per_page(workers: int) -> float:
    """Fallback rate when this machine has no measured history yet (whole-book wall clock, basic tier).

    Reference whole-book & slice measurements (2026-11-18/19):
      Windows i7-1260P 16 GB, w2: 545-page Chinese book 5.17 s/page (47.0m), 390-page English book 4.66 s/page (30.3m).
      Mac M5 Pro 48 GB (light load): 6.2-7.7 s/page single process; 5.5-7.9 s/page whole book.
      The same Mac under extreme load (load average 400-900, 20GB+ swap): 33-88 s/page.
    The live ETA in `status` replaces this number as soon as the first chunk finishes.
    """
    is_apple_silicon = platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64")
    if is_apple_silicon:
        return {1: 7.0, 2: 6.5}.get(workers, 6.0)
    return {1: 11.7, 2: 5.2}.get(workers, 5.0)


def rate_history_path() -> Path:
    return state_dir() / "rates.jsonl"


def record_rate(row: dict) -> None:
    try:
        p = rate_history_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def measured_sec_per_page(tier: str, workers: int) -> tuple[float | None, str]:
    """Median of this host's last 5 measured runs with the same tier and workers."""
    host = socket.gethostname()
    rows = []
    try:
        for line in rate_history_path().read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("host") == host and d.get("tier") == tier and d.get("workers") == workers and d.get("sec_per_page"):
                rows.append(d)
    except OSError:
        return None, "none"
    if not rows:
        return None, "none"
    last = rows[-5:]
    return round(statistics.median(r["sec_per_page"] for r in last), 2), f"本机实测 {len(last)} 次中位数"


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


def roman_to_int(token: str) -> int | None:
    s = token.strip().lower()
    if not s or not ROMAN_RE.match(s):
        return None
    vals = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    total = 0
    prev = 0
    for ch in reversed(s):
        v = vals[ch]
        if v < prev:
            total -= v
        else:
            total += v
            prev = v
    return total if 1 <= total <= 80 else None


def int_to_roman(num: int) -> str:
    pairs = [
        (50, "l"), (40, "xl"), (10, "x"), (9, "ix"),
        (5, "v"), (4, "iv"), (1, "i"),
    ]
    out = []
    rem = num
    for val, sym in pairs:
        while rem >= val:
            out.append(sym)
            rem -= val
    return "".join(out)


def extract_clean_arabic(raw: str | None) -> int | None:
    if not raw:
        return None
    s = raw.strip()
    if re.fullmatch(r"[1-9]\d{0,3}", s):
        return int(s)
    return None


def infer_pagination(pages: list[dict], explicit_offset: int | None = None) -> dict:
    """Assign accurate `book_page` labels across `pages` using modal PDF->print offset + Roman frontmatter."""
    if not pages:
        return {"body_offset": explicit_offset, "roman_offset": None, "back_offset": None, "ocr_divergences": []}

    arabic_votes: list[tuple[int, int]] = []
    for pg in pages:
        pdf_p = pg["pdf_page"]
        # Prefer dedicated page_number/number blocks over generic footers
        cands = pg.get("page_num_candidates") or ([pg["ocr_page"]] if pg.get("ocr_page") else [])
        for tok in cands:
            val = extract_clean_arabic(tok)
            if val is not None and pdf_p - val >= 0:
                arabic_votes.append((pdf_p, val))
                break

    body_offset: int | None = explicit_offset
    body_start = 10**9
    body_end = -1
    back_offset: int | None = None
    back_start = 10**9
    back_end = -1

    if explicit_offset is not None:
        body_start = max(1, explicit_offset + 1)
        matched = [pdf_p for pdf_p, val in arabic_votes if pdf_p - val == explicit_offset]
        body_end = max(matched) if (len(pages) > 30 and matched) else max(pg["pdf_page"] for pg in pages)
    elif arabic_votes:
        diff_counts = collections.Counter(pdf_p - val for pdf_p, val in arabic_votes)
        best_d, best_cnt = diff_counts.most_common(1)[0]
        min_votes = 3 if len(pages) > 15 else 1
        if best_cnt >= min_votes:
            body_offset = best_d
            matched = [pdf_p for pdf_p, val in arabic_votes if pdf_p - val == body_offset]
            body_start = max(1, body_offset + 1)
            if len(pages) <= 30:
                body_end = max(pg["pdf_page"] for pg in pages)
            else:
                body_end = max(matched)
                # Detect secondary publisher catalogue / appendix restarted numbering after body_end
                after_votes = [(pdf_p, val) for pdf_p, val in arabic_votes if pdf_p > body_end and pdf_p - val >= body_end]
                if after_votes:
                    back_counts = collections.Counter(pdf_p - val for pdf_p, val in after_votes)
                    b_d, b_cnt = back_counts.most_common(1)[0]
                    if b_cnt >= 3:
                        back_offset = b_d
                        back_matched = [pdf_p for pdf_p, val in after_votes if pdf_p - val == back_offset]
                        back_start = max(body_end + 1, back_offset + 1)
                        back_end = max(back_matched)

    # Detect Roman-numeral frontmatter before body_start
    roman_offset: int | None = None
    roman_start = 10**9
    roman_end = -1
    front_limit = body_start if body_offset is not None else (max(pg["pdf_page"] for pg in pages) + 1)
    roman_votes: list[tuple[int, int]] = []
    single_roman_pages: dict[int, str] = {}

    for pg in pages:
        pdf_p = pg["pdf_page"]
        if pdf_p >= front_limit:
            continue
        tokens: list[str] = list(pg.get("page_num_candidates") or [])
        if pg.get("ocr_page"):
            tokens.append(pg["ocr_page"])
        if pg.get("header"):
            for part in re.split(r"[|\s·.,;]+", pg["header"]):
                if part:
                    tokens.append(part)
        for tok in tokens:
            rv = roman_to_int(tok)
            if rv is not None and 1 <= rv <= 60 and pdf_p - rv >= 0:
                # Ignore single-letter 'I'/'i' unless corroborated by multi-page offset consensus
                if len(tok.strip()) >= 2 and tok.strip().islower():
                    single_roman_pages.setdefault(pdf_p, int_to_roman(rv))
                roman_votes.append((pdf_p, rv))
                break

    if roman_votes:
        rcounts = collections.Counter(pdf_p - rv for pdf_p, rv in roman_votes)
        r_d, r_cnt = rcounts.most_common(1)[0]
        if r_cnt >= 3:
            roman_offset = r_d
            rmatched = [pdf_p for pdf_p, rv in roman_votes if pdf_p - rv == roman_offset]
            roman_start = max(roman_offset + 1, min(rmatched))
            roman_end = max(rmatched)

    divergences: list[dict] = []
    for pg in pages:
        pdf_p = pg["pdf_page"]
        raw_ocr = (pg.get("ocr_page") or "").strip()
        label = "unnumbered"
        if body_offset is not None and body_start <= pdf_p <= body_end:
            calc = pdf_p - body_offset
            if calc >= 1:
                label = str(calc)
                if raw_ocr and raw_ocr != label:
                    divergences.append({"pdf_page": pdf_p, "book_page": label, "ocr_raw": raw_ocr[:40]})
        elif back_offset is not None and back_start <= pdf_p <= back_end:
            calc_b = pdf_p - back_offset
            if calc_b >= 1:
                label = f"back-{calc_b}"
                if raw_ocr and raw_ocr != str(calc_b):
                    divergences.append({"pdf_page": pdf_p, "book_page": label, "ocr_raw": raw_ocr[:40]})
        elif roman_offset is not None and roman_start <= pdf_p <= roman_end:
            calc_r = pdf_p - roman_offset
            if 1 <= calc_r <= 60:
                label = int_to_roman(calc_r)
        elif pdf_p in single_roman_pages:
            label = single_roman_pages[pdf_p]
        pg["book_page"] = label

    return {
        "body_offset": body_offset,
        "roman_offset": roman_offset,
        "back_offset": back_offset,
        "ocr_divergence_count": len(divergences),
        "ocr_divergences_sample": divergences[:25],
    }


def compute_book_page_label(pdf_page: int, ocr_page: str | None, pdf_offset: int | None) -> str:
    """Single-page helper: pdf_offset ALWAYS wins over noisy single-page OCR."""
    if pdf_offset is not None:
        calc = pdf_page - pdf_offset
        if calc >= 1:
            return str(calc)
        if ocr_page:
            rv = roman_to_int(ocr_page)
            if rv is not None and len(ocr_page.strip()) >= 2 and ocr_page.strip().islower():
                return int_to_roman(rv)
        return "unnumbered"
    val = extract_clean_arabic(ocr_page)
    if val is not None:
        return str(val)
    if ocr_page:
        rv = roman_to_int(ocr_page)
        if rv is not None and len(ocr_page.strip()) >= 2 and ocr_page.strip().islower():
            return int_to_roman(rv)
    return "unnumbered"


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


def status_file_for_slug(book_slug: str) -> Path:
    return state_dir() / "jobs" / f"{book_slug}.json"


def write_json_atomic(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def set_file_readonly(path: Path, readonly: bool = True) -> bool:
    """Set 0444 read-only mode so downstream cheap LLMs cannot accidentally edit raw book Markdown."""
    try:
        if not path.exists():
            return False
        if readonly:
            os.chmod(path, stat_mod.S_IRUSR | stat_mod.S_IRGRP | stat_mod.S_IROTH)
        else:
            os.chmod(path, stat_mod.S_IRUSR | stat_mod.S_IWUSR | stat_mod.S_IRGRP | stat_mod.S_IROTH)
        return True
    except OSError:
        return False


def is_file_readonly(path: Path) -> bool:
    try:
        return not os.access(path, os.W_OK) or (stat_mod.S_IMODE(path.stat().st_mode) & stat_mod.S_IWUSR == 0)
    except OSError:
        return False


def send_system_notification(title: str, message: str) -> bool:
    """Fire-and-forget OS notification (macOS osascript / Windows Toast fallback) for clients without task hooks."""
    if os.environ.get("PDF2MD_NO_NOTIFY") == "1":
        return False
    sysname = platform.system()
    try:
        if sysname == "Darwin":
            safe_t = title.replace("\\", "\\\\").replace('"', '\\"')
            safe_m = message.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(
                ["osascript", "-e", f'display notification "{safe_m}" with title "{safe_t}"'],
                capture_output=True,
                timeout=3,
            )
            return True
        if sysname == "Windows":
            ps_code = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$n = New-Object System.Windows.Forms.NotifyIcon; "
                "$n.Icon = [System.Drawing.SystemIcons]::Information; "
                "$n.Visible = $true; "
                f"$n.ShowBalloonTip(5000, {json.dumps(title)}, {json.dumps(message)}, [System.Windows.Forms.ToolTipIcon]::Info); "
                "Start-Sleep -Milliseconds 800; $n.Dispose()"
            )
            subprocess.run(["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_code], capture_output=True, timeout=4)
            return True
    except Exception:
        pass
    return False


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
    hist_spp, hist_basis = measured_sec_per_page("basic", w)
    return {
        "ok": bool(venv_ok and mod_ok),
        "skill_version": SKILL_VERSION,
        "os": platform.system(),
        "arch": platform.machine(),
        "cpu_logical_cores": cpus,
        "ram_total_gb": tot_gb,
        "ram_available_gb": avail_gb,
        "load_average_1m": load_average(),
        "uv_path": find_uv(),
        "mineru_venv": str(venv_dir),
        "venv_installed": venv_ok,
        "onnx_models_dir": str(onnx_model_dir()),
        "onnx_models_ready": mod_ok,
        "recommended": {
            "workers": w,
            "chunk_size": csz,
            "threads_per_worker": thr,
            "est_sec_per_page": hist_spp or estimate_sec_per_page(w),
            "est_basis": hist_basis if hist_spp else "reference_fallback",
        },
    }


def choose_pypi_index() -> str | None:
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
        subprocess.run([uv, "venv", "--clear", str(venv_dir), "--python", "3.12"], check=True)
        pip_cmd = [uv, "pip", "install", "--python", str(py), "-U", "mineru>=4.2,<5"]
        if pypi_index:
            pip_cmd.extend(["--index-url", pypi_index])
        res = subprocess.run(pip_cmd)
        if res.returncode != 0 and not pypi_index:
            pypi_index = "https://pypi.tuna.tsinghua.edu.cn/simple"
            subprocess.run(pip_cmd + ["--index-url", pypi_index], check=True)
        elif res.returncode != 0:
            raise RuntimeError(f"uv pip install mineru failed with code {res.returncode}")
    subprocess.run([uv, "pip", "uninstall", "--python", str(py), "hf-xet"], capture_output=True)

    chosen_source = "already_cached"
    if models_from is not None:
        import_models_from_path(models_from)
        chosen_source = f"local:{Path(models_from).name}"
    elif not models_ready():
        attempts: list[tuple[str, dict]] = []
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
    cdir = cache_dir_for(Path(vault).expanduser().resolve(), book_slug, pdf_sha, tier)

    cached_pages = 0
    selected_pages = 0
    for s, e in chunks:
        cnt = e - s + 1
        selected_pages += cnt
        if chunk_is_valid(cdir / f"chunk_{s:04d}_{e:04d}.json", cnt):
            cached_pages += cnt

    eff_workers = workers if workers is not None else doc["recommended"]["workers"]
    spp, basis = measured_sec_per_page(tier, eff_workers)
    if spp is None:
        spp = estimate_sec_per_page(eff_workers)
        basis = "本机尚无实测记录，暂用参考机基准（首个分块跑完后可调 status 查看实测剩余时间）"
    rem_pages = max(0, selected_pages - cached_pages)
    est_sec = round(rem_pages * spp + (15 if rem_pages > 0 else 2.0), 1)
    est_min = round(est_sec / 60.0, 1)
    out_md = Path(vault).expanduser().resolve() / "raw" / "books" / f"{book_slug}.md"
    load1 = doc.get("load_average_1m")
    cpus = doc["cpu_logical_cores"]
    load_warning = None
    if load1 is not None and load1 > 2 * cpus:
        load_warning = f"本机当前负载较高（1分钟平均负载 {load1}，{cpus} 核），实际耗时可能高于基准预估。"

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
        "estimate_basis": basis,
        "ram_available_gb": doc["ram_available_gb"],
        "load_average_1m": load1,
        "load_warning": load_warning,
        "estimated_seconds": est_sec,
        "estimated_minutes": est_min,
        "requires_user_confirmation": est_sec > 60.0,
        "output_markdown": str(out_md),
        "output_exists": out_md.exists(),
        "cost_rmb": 0.0,
        "llm_tokens_used": 0,
        "human_message": (
            f"《{pdf_real.stem}》共 {total_pages} 页（断点缓存已完成 {cached_pages} 页，待转 {rem_pages} 页）。"
            f"使用本机免费 MinerU ONNX 模型（{eff_workers} 并发），预计耗时约 {est_min} 分钟（0 元 / 0 Token）。"
            f"预估依据：{spp} 秒/页（{basis}；当前空闲内存 {doc['ram_available_gb']} GB）。"
            + (f"注意：{load_warning}" if load_warning else "")
            + f"完成后将写入只读保护文件 {out_md}。"
        ),
    }


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
    env["MINERU_MODEL_SMALL_BACKEND"] = "onnx"
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
) -> tuple[list[dict], dict]:
    helper = r"""
import json, pathlib, sys
import docvortex

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
out_path = pathlib.Path(sys.argv[2])
results = []

def flatten_text(blk):
    parts = []
    for c in (blk.get("content") or []):
        if isinstance(c, dict):
            parts.append(str(c.get("content", "")))
        else:
            parts.append(str(c))
    return " ".join(parts).strip()

for start_p, end_p, cpath in manifest["chunks"]:
    raw = json.loads(pathlib.Path(cpath).read_text(encoding="utf-8"))
    pages = raw.get("pages", [])
    for idx, pg in enumerate(pages):
        pdf_page = start_p + idx
        strict_nums, footers, headers = [], [], []
        for blk in pg.get("blocks", []):
            btype = blk.get("type")
            if btype in ("page_number", "number"):
                val = flatten_text(blk)
                if val:
                    strict_nums.append(val)
            elif btype == "footer":
                val = flatten_text(blk)
                if val:
                    footers.append(val)
            elif btype == "header":
                val = flatten_text(blk)
                if val:
                    headers.append(val)
        cands = strict_nums + [ft for ft in footers if len(ft) <= 10]
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
            "ocr_page": cands[0] if cands else None,
            "page_num_candidates": cands,
            "header": " | ".join(headers) if headers else None,
            "markdown": md,
        })

out_path.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
"""
    with tempfile.TemporaryDirectory(prefix="pdf2md-render-") as td:
        in_p = Path(td) / "in.json"
        out_p = Path(td) / "out.json"
        in_p.write_text(json.dumps({"chunks": [[s, e, str(p)] for s, e, p in chunk_targets]}, ensure_ascii=False), encoding="utf-8")
        subprocess.run(
            [str(mineru_py), "-c", helper, str(in_p), str(out_p)],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        pages = json.loads(out_p.read_text(encoding="utf-8"))
    pag_info = infer_pagination(pages, explicit_offset=pdf_offset)
    return pages, pag_info


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
        f"engine: {json.dumps(f'MinerU-4.2-ONNX-{tier} ({OUTPUT_ENGINE_TAG})')}",
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
        ocr_p = (pg.get("ocr_page") or "").replace("-->", "—>").replace("|", "/")
        hdr = (pg.get("header") or "").replace("-->", "—>")
        meta = [f"pdf_page: {pdf_p}", f"book_page: {book_p}"]
        if ocr_p and len(ocr_p) <= 20:
            meta.append(f"ocr_page: {ocr_p}")
        if hdr:
            meta.append(f"header: {hdr}")
        lines.append(f"<!-- {' | '.join(meta)} -->")
        if book_p == "unnumbered":
            book_disp = "原书未标页码"
        elif book_p.startswith("back-"):
            book_disp = f"原书附页 p.{book_p[5:]}"
        else:
            book_disp = f"原书 p.{book_p}"
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
    anchor_re = re.compile(
        r"^<!--\s*pdf_page:\s*(\d+)\s*\|\s*book_page:\s*([^|>]+?)(?:\s*\|\s*ocr_page:\s*([^|>]+?))?(?:\s*\|\s*header:\s*([^>]+?))?\s*-->$",
        re.MULTILINE,
    )
    matches = list(anchor_re.finditer(text))
    pdf_pages = [int(m.group(1)) for m in matches]
    book_pages = [m.group(2).strip() for m in matches]
    ocr_pages = [(m.group(3) or "").strip() for m in matches]

    non_empty = 0
    invalid_labels: list[dict] = []
    ocr_corrected_pages: list[dict] = []
    for idx, m in enumerate(matches):
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        seg = text[start:end]
        if "*[空白页或纯插图页]*" not in seg and "*[本页为空白页或纯图像页]*" not in seg and len(seg.strip()) > 30:
            non_empty += 1
        bp = book_pages[idx]
        op = ocr_pages[idx]
        if not VALID_BOOK_PAGE_RE.match(bp):
            invalid_labels.append({"pdf_page": pdf_pages[idx], "book_page": bp})
        elif bp.isdigit() and op and op != bp:
            ocr_corrected_pages.append({"pdf_page": pdf_pages[idx], "book_page": bp, "ocr_raw": op})

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
        and len(invalid_labels) == 0
        and (expected_pages is None or len(pdf_pages) == expected_pages)
    )
    return {
        "ok": ok,
        "markdown_path": str(md_real),
        "sha256": sha256_file(md_real),
        "bytes": md_real.stat().st_size,
        "read_only_protected": is_file_readonly(md_real),
        "verified_pages": len(pdf_pages),
        "expected_pages": expected_pages,
        "missing_pages": missing[:20],
        "duplicate_page_count": duplicates,
        "invalid_book_page_labels": invalid_labels[:20],
        "non_empty_text_pages": non_empty,
        "book_page_labeled_count": numbered_book_pages,
        "ocr_misreads_corrected_count": len(ocr_corrected_pages),
        "ocr_misreads_corrected_sample": ocr_corrected_pages[:15],
    }


def cmd_status(target: str, *, vault: Path) -> dict:
    p = Path(target).expanduser()
    if p.is_file() and p.name == "status.json":
        return json.loads(p.read_text(encoding="utf-8"))
    slug = slugify(p.stem if p.suffix.lower() == ".pdf" else target)
    st_path = status_file_for_slug(slug)
    if st_path.is_file():
        return json.loads(st_path.read_text(encoding="utf-8"))
    # Also scan vault ocr-cache for status.json matching slug
    croot = Path(vault).expanduser().resolve() / ".vault-meta" / "ocr-cache"
    if croot.is_dir():
        matches = sorted(croot.glob(f"{slug}_*/status.json"), key=lambda x: x.stat().st_mtime, reverse=True)
        if matches:
            return json.loads(matches[0].read_text(encoding="utf-8"))
    return {"ok": False, "state": "not_found", "slug": slug}


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
    notify: bool = True,
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
    target_md = Path(out_file).expanduser().resolve() if out_file else (vault_real / "raw" / "books" / f"{book_slug}.md")

    chunk_targets = [(s, e, cdir / f"chunk_{s:04d}_{e:04d}.json") for s, e in chunks]
    selected_pages = sum(e - s + 1 for s, e, _ in chunk_targets)
    t0 = time.perf_counter()
    newly_computed_pages = 0
    completed_pages = 0
    status_lock = threading.Lock()
    job_status_path = status_file_for_slug(book_slug)
    cache_status_path = cdir / "status.json"

    def publish_status(state: str, extra: dict | None = None) -> None:
        elapsed_now = round(time.perf_counter() - t0, 2)
        live_spp = round(elapsed_now / newly_computed_pages, 2) if newly_computed_pages > 0 else rec["est_sec_per_page"]
        rem = max(0, selected_pages - completed_pages)
        payload = {
            "ok": state != "error",
            "state": state,
            "slug": book_slug,
            "title": book_title,
            "source_pdf": str(pdf_real),
            "output_markdown": str(target_md),
            "completed_pages": completed_pages,
            "selected_pages": selected_pages,
            "newly_computed_pages": newly_computed_pages,
            "workers_used": eff_workers,
            "elapsed_sec": elapsed_now,
            "live_sec_per_page": live_spp,
            "live_eta_sec": round(rem * live_spp, 1),
            "live_eta_min": round((rem * live_spp) / 60.0, 1),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            payload.update(extra)
        write_json_atomic(cache_status_path, payload)
        write_json_atomic(job_status_path, payload)

    publish_status("running")

    try:
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
                cnt = info["end"] - info["start"] + 1
                with status_lock:
                    done_n += 1
                    completed_pages += cnt
                    if not info["cached"]:
                        newly_computed_pages += cnt
                    publish_status("running")
                print(
                    f"[{done_n}/{len(chunk_targets)}] pages {info['start']}-{info['end']} "
                    f"({'cached' if info['cached'] else str(info['sec']) + 's'})",
                    file=sys.stderr,
                    flush=True,
                )

        rendered, pag_info = render_chunks_to_pages(chunk_targets, mineru_py=py, pdf_offset=pdf_offset)
        effective_offset = pag_info.get("body_offset")
        md_text = format_markdown(
            title=book_title,
            source_pdf=pdf_real,
            source_sha=sha_before,
            total_pdf_pages=total_pages,
            pdf_offset=effective_offset,
            tier=tier,
            pages=rendered,
        )

        target_md.parent.mkdir(parents=True, exist_ok=True)
        if target_md.exists():
            set_file_readonly(target_md, readonly=False)
        target_md.write_text(md_text, encoding="utf-8")
        set_file_readonly(target_md, readonly=True)

        if sha256_file(pdf_real) != sha_before:
            raise RuntimeError("Source PDF modified unexpectedly during OCR")

        elapsed = round(time.perf_counter() - t0, 2)
        expected_v = total_pages if pages_spec.strip().lower() in ("", "all") else None
        ver = cmd_verify(target_md, expected_pages=expected_v)

        if newly_computed_pages >= 5:
            record_rate({
                "ts": datetime.now(timezone.utc).isoformat(),
                "host": socket.gethostname(),
                "os": platform.system(),
                "arch": platform.machine(),
                "tier": tier,
                "workers": eff_workers,
                "pages": newly_computed_pages,
                "elapsed_sec": elapsed,
                "sec_per_page": round(elapsed / newly_computed_pages, 3),
                "load_average_1m": load_average(),
                "ram_available_gb": doc["ram_available_gb"],
            })

        notified = False
        if notify and (newly_computed_pages > 0 or elapsed > 5.0):
            notified = send_system_notification(
                "扫描版 PDF 转 Markdown 已完成",
                f"《{book_title}》已验收 {ver['verified_pages']} 页，耗时 {round(elapsed / 60.0, 1)} 分钟。",
            )

        result = {
            "ok": ver["ok"],
            "title": book_title,
            "source_pdf": str(pdf_real),
            "output_markdown": str(target_md),
            "total_pdf_pages": total_pages,
            "converted_pages": len(rendered),
            "newly_computed_pages": newly_computed_pages,
            "pdf_offset_used": effective_offset,
            "roman_offset_used": pag_info.get("roman_offset"),
            "back_offset_used": pag_info.get("back_offset"),
            "workers_used": eff_workers,
            "chunk_size_used": eff_chunk,
            "elapsed_sec": elapsed,
            "elapsed_min": round(elapsed / 60.0, 2),
            "sec_per_page": round(elapsed / max(1, newly_computed_pages or len(rendered)), 3),
            "system_notification_sent": notified,
            "cost_rmb": 0.0,
            "llm_tokens_used": 0,
            "verification": ver,
        }
        publish_status("done", {"verification": ver, "pdf_offset_used": effective_offset})
        return result
    except Exception as exc:
        publish_status("error", {"error": str(exc)})
        raise


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    p_doc = sub.add_parser("doctor", help="Check local runtime, RAM, load average, and ONNX models")
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
    p_conv.add_argument("--no-notify", action="store_true", help="Suppress OS desktop notification")

    p_stat = sub.add_parser("status", help="Read live progress / ETA of a running or finished convert")
    p_stat.add_argument("target", type=str, help="PDF path, slug, or status.json path")
    p_stat.add_argument("--vault", type=Path, default=default_vault_dir())

    p_ver = sub.add_parser("verify", help="Verify converted Markdown completeness, page labels, and read-only lock")
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
                notify=not args.no_notify,
            )
        elif args.cmd == "status":
            res = cmd_status(args.target, vault=args.vault)
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
