#!/usr/bin/env python3
"""官方页面变化监测和任务收尾队列；只收集证据，不运行 LLM 或提升研究决策。

  python3 tools/research-watch.py watch                 # 联网预演，不写库
  python3 tools/research-watch.py watch --write --due   # 台湾周一 08:30 到期/补检
  python3 tools/research-watch.py enqueue --project NAME --source SOURCE \
      --evidence /absolute/report.md --status candidate --write
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import email.parser
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

VAULT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = VAULT / "config/research-watch.json"
TAIPEI = ZoneInfo("Asia/Taipei")
# 配置可以收窄这些范围，不能添加任意外站。只接受 HTTPS 443。
OFFICIAL_SCOPES = {
    "charls.pku.edu.cn": "/",
    "isss.pku.edu.cn": "/cfps/",
    "www.isss.pku.edu.cn": "/cfps/",
}
CURL = "/usr/bin/curl"


class WatchError(RuntimeError):
    pass


class PolicyError(WatchError):
    pass


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def canonical_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 4096 or re.search(r"[\x00-\x20\x7f\\]", url):
        raise PolicyError("URL 含空白、控制符、反斜杠或过长")
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443) or not parts.hostname):
            raise PolicyError("仅允许无凭据的 HTTPS 443 URL")
    except ValueError as exc:
        raise PolicyError("URL 无法解析") from exc
    path = parts.path or "/"
    decoded = path
    for _ in range(4):
        updated = unquote(decoded)
        if updated == decoded:
            break
        decoded = updated
    if ("%" in decoded or "\\" in decoded or re.search(r"[\x00-\x1f\x7f]", decoded)
            or any(p in (".", "..") for p in decoded.split("/"))):
        raise PolicyError("URL 路径包含不明确的转义或目录穿越")
    return urlunsplit(("https", parts.hostname.lower(), path, parts.query, ""))


def within(url: str, prefix: str) -> bool:
    value, scope = urlsplit(url), urlsplit(prefix)
    path, boundary = unquote(value.path), unquote(scope.path).rstrip("/")
    return value.hostname == scope.hostname and (not boundary or path == boundary or path.startswith(boundary + "/"))


def allowed_url(url: str, allowlist: list[str]) -> str:
    url = canonical_url(url)
    parts = urlsplit(url)
    root = OFFICIAL_SCOPES.get(parts.hostname)
    if root is None or not within(url, f"https://{parts.hostname}{root}"):
        raise PolicyError("URL 超出内置官源范围")
    if not any(within(url, prefix) for prefix in allowlist):
        raise PolicyError("URL 超出配置 allowlist")
    return url


def load_config(path: Path) -> dict:
    if path.stat().st_size > 65536:
        raise WatchError("配置超过 64 KiB")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if cfg.get("version") != 1 or cfg.get("timezone") != "Asia/Taipei":
        raise WatchError("配置必须为 version=1 / Asia/Taipei")
    sched = cfg.get("schedule")
    valid_daily = (
        isinstance(sched, dict)
        and sched.get("cadence") == "daily"
        and set(sched) == {"cadence", "hour", "minute"}
        and type(sched.get("hour")) is int and 0 <= sched["hour"] <= 23
        and type(sched.get("minute")) is int and 0 <= sched["minute"] <= 59
    )
    valid_weekly = sched == {"weekday": 0, "hour": 8, "minute": 30}
    if not (valid_daily or valid_weekly):
        raise WatchError("schedule 仅允许每日定时 {'cadence':'daily','hour':H,'minute':M} 或每周一 08:30 Asia/Taipei")
    for key, lo, hi in [("timeout_seconds", 1, 30), ("max_bytes", 1, 5242880),
                        ("retries", 0, 2), ("max_redirects", 0, 5)]:
        if type(cfg.get(key)) is not int or not lo <= cfg[key] <= hi:
            raise WatchError(f"{key} 必须是 {lo}..{hi} 的整数")
    scopes = cfg.get("allowlist")
    if not isinstance(scopes, list) or not scopes or len(scopes) > 10:
        raise WatchError("allowlist 必须是非空且不超过 10 项的列表")
    cfg["allowlist"] = [canonical_url(value) for value in scopes]
    for scope in cfg["allowlist"]:
        allowed_url(scope, cfg["allowlist"])
        if urlsplit(scope).query:
            raise WatchError("allowlist 范围不能带查询参数")
    sources = cfg.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 20:
        raise WatchError("sources 必须包含 1..20 个来源")
    ids, urls = set(), set()
    for source in sources:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", source.get("id", "")):
            raise WatchError("来源 id 格式错误")
        if not isinstance(source.get("project"), str) or not source["project"].strip():
            raise WatchError("来源必须指定 project")
        source["url"] = allowed_url(source["url"], cfg["allowlist"])
        if source["id"] in ids or source["url"] in urls:
            raise WatchError("重复来源 id 或 URL")
        ids.add(source["id"])
        urls.add(source["url"])
    return cfg


def curl_once(url: str, timeout: float, max_bytes: int) -> tuple[int, dict, bytes]:
    """不自动跳转；管道读取有大小上限，进程有总时限，stderr 上限 8 KiB。"""
    with tempfile.TemporaryDirectory(prefix="research-watch-http-") as tmp:
        headers_path = Path(tmp) / "headers"
        command = [CURL, "--disable", "--silent", "--show-error", "--proto", "=https",
                   "--max-time", str(timeout), "--connect-timeout", str(min(10, timeout)),
                   "--max-filesize", str(max_bytes), "--max-redirs", "0",
                   "--header", "Accept-Encoding: identity", "--user-agent", "research-vault-watch/1.0",
                   "--dump-header", str(headers_path), "--url", url]
        deadline = time.monotonic() + timeout
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
            chunks = {"body": bytearray(), "stderr": bytearray()}
            try:
                with selectors.DefaultSelector() as selected:
                    selected.register(proc.stdout, selectors.EVENT_READ, "body")
                    selected.register(proc.stderr, selectors.EVENT_READ, "stderr")
                    while selected.get_map():
                        left = deadline - time.monotonic()
                        if left <= 0:
                            raise WatchError("HTTP 请求超过总时限")
                        for key, _ in selected.select(min(left, 0.2)):
                            data = os.read(key.fileobj.fileno(), 65536)
                            if not data:
                                selected.unregister(key.fileobj)
                                continue
                            chunks[key.data].extend(data)
                            limit = max_bytes if key.data == "body" else 8192
                            if len(chunks[key.data]) > limit:
                                raise WatchError(f"HTTP {key.data} 超过大小上限")
                        if headers_path.exists() and headers_path.stat().st_size > 65536:
                            raise WatchError("HTTP headers 超过 64 KiB")
                proc.wait(timeout=max(0.01, deadline - time.monotonic()))
                if proc.returncode:
                    message = chunks["stderr"].decode("utf-8", errors="replace").strip()[:500]
                    raise WatchError(f"curl exit {proc.returncode}: {message}")
            except BaseException:
                proc.kill()
                proc.wait()
                raise
        header_bytes = headers_path.read_bytes()
        if len(header_bytes) > 65536:
            raise WatchError("HTTP headers 超过 64 KiB")
        # 忽略代理 CONNECT/100 Continue，仅取最后一个 HTTP 头区。
        blocks = [b for b in re.split(rb"\r?\n\r?\n", header_bytes) if b.startswith(b"HTTP/")]
        if not blocks:
            raise WatchError("缺少 HTTP 响应头")
        head = blocks[-1].decode("iso-8859-1")
        first, _, rest = head.partition("\n")
        match = re.match(r"HTTP/\S+\s+(\d{3})", first)
        if not match:
            raise WatchError("HTTP 状态无法解析")
        parsed = email.parser.Parser().parsestr(rest)
        headers = {key.lower(): value for key, value in parsed.items()}
        if len(parsed.get_all("Location", [])) > 1:
            raise PolicyError("多个 Location，拒绝不明确跳转")
        return int(match[1]), headers, bytes(chunks["body"])


def fetch_once(url: str, cfg: dict) -> dict:
    url = allowed_url(url, cfg["allowlist"])
    chain, seen = [], set()
    deadline = time.monotonic() + cfg["timeout_seconds"]
    for _ in range(cfg["max_redirects"] + 1):
        if url in seen:
            raise PolicyError("HTTP 跳转循环")
        seen.add(url)
        left = deadline - time.monotonic()
        if left <= 0:
            raise WatchError("HTTP 跳转链超过总时限")
        status, headers, body = curl_once(url, left, cfg["max_bytes"])
        chain.append({"url": url, "http_status": status})
        if status in (301, 302, 303, 307, 308):
            if not headers.get("location"):
                raise WatchError("HTTP 跳转缺少 Location")
            # 跳转目标必须先验证，绝不先访问再判断。
            url = allowed_url(urljoin(url, headers["location"]), cfg["allowlist"])
            continue
        if status != 200:
            raise WatchError(f"HTTP {status}，未完成内容检查")
        if not body:
            raise WatchError("HTTP 200 但响应为空")
        if headers.get("content-encoding", "identity").lower() not in ("", "identity"):
            raise WatchError("服务端忽略 identity 编码，拒绝压缩响应")
        return {"body": body, "final_url": url, "content_type": headers.get("content-type", ""),
                "redirect_chain": chain, "http_status": status}
    raise PolicyError("HTTP 跳转次数超过上限")


def fetch(url: str, cfg: dict) -> tuple[dict | None, list[str]]:
    errors = []
    for _ in range(cfg["retries"] + 1):
        try:
            return fetch_once(url, cfg), errors
        except PolicyError as exc:
            errors.append(str(exc)[:500])
            break
        except (WatchError, OSError, subprocess.SubprocessError) as exc:
            errors.append(str(exc)[:500])
    return None, errors


def due_week(now: dt.datetime) -> str:
    if now.tzinfo is None:
        raise WatchError("时间必须带时区")
    local = now.astimezone(TAIPEI)
    monday = local.date() - dt.timedelta(days=local.weekday())
    scheduled = dt.datetime.combine(monday, dt.time(8, 30), TAIPEI)
    if local < scheduled:
        monday -= dt.timedelta(days=7)
    return monday.isoformat()


def due_day(now: dt.datetime, hour: int = 10, minute: int = 0) -> str:
    if now.tzinfo is None:
        raise WatchError("时间必须带时区")
    local = now.astimezone(TAIPEI)
    day = local.date()
    scheduled = dt.datetime.combine(day, dt.time(hour, minute), TAIPEI)
    if local < scheduled:
        day -= dt.timedelta(days=1)
    return day.isoformat()


def due_slot(now: dt.datetime, schedule: dict | None) -> str:
    if isinstance(schedule, dict) and schedule.get("cadence") == "daily":
        return f"day:{due_day(now, schedule['hour'], schedule['minute'])}"
    return f"week:{due_week(now)}"


def checked_path(path: Path, root: Path) -> Path:
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise WatchError(f"拒绝软链或队列外写入：{path}")
    return path


def file_sha(path: Path, root: Path) -> str | None:
    checked_path(path, root)
    return sha(path.read_bytes()) if path.exists() else None


def atomic_write(path: Path, data: bytes, root: Path, expected: str | None) -> None:
    if file_sha(path, root) != expected:
        raise WatchError(f"目标已被并发修改：{path}")
    if expected == sha(data):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".watch-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if file_sha(path, root) != expected:
            raise WatchError(f"目标已被并发修改：{path}")
        if expected is None:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def inbox_lock(root: Path, write: bool):
    lock = root / "watch.lock"
    if not write and not lock.exists():
        yield
        return
    checked_path(lock, root)
    if write:
        root.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b" if write else "rb") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WatchError("另一监测或收尾入队正在运行") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def queue_item(root: Path, record: dict, identity: dict, write: bool) -> tuple[str, bool]:
    item_id = sha(encoded(identity))
    path = root / "items" / f"{item_id}.json"
    current = file_sha(path, root)
    if current is not None:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("identity") != identity or existing.get("id") != item_id:
            raise WatchError(f"队列项目内容与身份不匹配：{path}")
        return str(path), False
    if write:
        atomic_write(path, encoded({**record, "id": item_id, "identity": identity}), root, None)
    return str(path), True


def watch(cfg: dict, root: Path, *, write: bool, due: bool, now: dt.datetime | None = None) -> dict:
    now = (now or dt.datetime.now(TAIPEI)).astimezone(TAIPEI)
    sched = cfg.get("schedule", {})
    is_daily = isinstance(sched, dict) and sched.get("cadence") == "daily"
    stamp, week = now.isoformat(), due_week(now)
    slot = due_slot(now, sched)
    day = due_day(now, sched.get("hour", 10), sched.get("minute", 0)) if is_daily else None
    with inbox_lock(root, write):
        state_path = root / "watch-state.json"
        expected = file_sha(state_path, root)
        state = json.loads(state_path.read_text()) if expected else {"version": 1, "sources": {}}
        if state.get("version") != 1 or not isinstance(state.get("sources"), dict):
            raise WatchError("无法识别监测状态")
        source_keys = [sha(source["url"].encode()) for source in cfg["sources"]]
        def _src_done(entry: dict) -> bool:
            return entry.get("last_slot") == slot or (not is_daily and entry.get("last_week") == week)
        state_done = state.get("last_slot") == slot or (not is_daily and state.get("last_week") == week)
        already = state_done and all(_src_done(state["sources"].get(key, {})) for key in source_keys)
        run = {"version": 1, "kind": "official_watch", "observed_at": stamp, "timezone": "Asia/Taipei",
               "due_week": week, "due_slot": slot, "write": write, "due": due, "sources": []}
        if is_daily:
            run["due_day"] = day
        if due and already:
            return {**run, "schedule": "already_checked", "result": "skipped"}
        for source, key in zip(cfg["sources"], source_keys):
            url = source["url"]
            result, errors = fetch(url, cfg)
            item = {"source_id": source["id"], "url": url, "checked_at": stamp,
                    "attempts": len(errors) + int(result is not None), "attempt_errors": errors}
            if result is None:
                item.update(status="error", event="unavailable", error=errors[-1] if errors else "抓取失败")
                run["sources"].append(item)
                continue
            body = result["body"]
            result = {key: value for key, value in result.items() if key != "body"}
            content_hash = sha(body)
            old = state["sources"].get(key, {})
            seen = set(old.get("seen_sha256", []))
            event = "snapshot" if not old else ("unchanged" if old["sha256"] == content_hash
                                                 else "known_content" if content_hash in seen else "changed")
            snapshot = root / "snapshots" / key / f"{content_hash}.bin"
            if write:
                existing = file_sha(snapshot, root)
                if existing is not None and existing != content_hash:
                    raise WatchError(f"内容寻址快照被修改：{snapshot}")
                atomic_write(snapshot, body, root, existing)
            item.update(result, status="nochange" if event == "unchanged" else "success", event=event,
                        sha256=content_hash, bytes=len(body), snapshot=str(snapshot), queued=False)
            if event == "changed":
                record = {"kind": "official_page_change", "status": "candidate", "project": source["project"],
                          "source": url, "evidence": [{"path": str(snapshot), "sha256": content_hash}],
                          "previous_sha256": old["sha256"], "observed_at": stamp, "final_url": result["final_url"],
                          "research_decision": "not_evaluated",
                          "note": "检测到官方页面字节变化；尚未核实新发布、内容意义或研究可行性。"}
                path, created = queue_item(root, record, {"kind": "official_page_change", "url": url,
                                                         "sha256": content_hash}, write)
                item.update(queue_item=path, queued=created)
            seen.add(content_hash)
            state["sources"][key] = {"source_id": source["id"], "url": url, "sha256": content_hash,
                                      "seen_sha256": sorted(seen), "last_success": stamp, "last_week": week,
                                      "last_slot": slot, "snapshot": str(snapshot), "final_url": result["final_url"]}
            run["sources"].append(item)
        failed = any(item["status"] == "error" for item in run["sources"])
        run["result"] = "error" if failed else "success"
        if not failed:
            state["last_week"] = week
            state["last_slot"] = slot
        state["last_attempt"] = stamp
        if write:
            run_id = now.strftime("%Y%m%dT%H%M%S%f%z") + "-" + sha(encoded(run))[:12]
            log_path = root / "runs" / f"{run_id}.json"
            run["log"] = str(log_path)
            atomic_write(log_path, encoded(run), root, None)
            atomic_write(state_path, encoded(state), root, expected)
        return run


def enqueue(root: Path, *, project: str, source: str, evidence: list[Path], status: str,
            write: bool, now: dt.datetime | None = None) -> dict:
    if status not in ("candidate", "confirmed_evidence"):
        raise WatchError("status 只能为 candidate 或 confirmed_evidence")
    if not project.strip() or len(project) > 200 or not source.strip() or len(source) > 4096:
        raise WatchError("project/source 为空或过长")
    if not evidence or len(evidence) > 20:
        raise WatchError("必须提供 1..20 个本地证据文件")
    proofs = []
    for path in evidence:
        if not path.is_absolute() or not path.is_file():
            raise WatchError(f"证据必须是存在的绝对文件路径：{path}")
        resolved = path.resolve()
        before = resolved.stat()
        hashed = hashlib.sha256()
        with resolved.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                hashed.update(chunk)
        after = resolved.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise WatchError(f"取证期间文件改变：{path}")
        proofs.append({"path": str(resolved), "sha256": hashed.hexdigest(), "bytes": before.st_size})
    proofs = sorted({p["path"]: p for p in proofs}.values(), key=lambda p: p["path"])
    identity = {"kind": "task_closeout", "project": project.strip(), "source": source.strip(),
                "evidence": proofs, "status": status}
    record = {**identity, "observed_at": (now or dt.datetime.now(TAIPEI)).isoformat(),
              "research_decision": "not_evaluated",
              "note": "confirmed_evidence 仅标记提交者已确认此证据，不自动形成研究决策。"}
    with inbox_lock(root, write):
        path, created = queue_item(root, record, identity, write)
    return {"result": "queued" if write and created else "preview" if not write and created else "duplicate",
            "write": write, "queue_item": path, "record": record}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--inbox", type=Path, default=VAULT / ".vault-meta/research-inbox")
    commands = parser.add_subparsers(dest="command", required=True)
    watch_parser = commands.add_parser("watch", help="联网检查官方来源")
    watch_parser.add_argument("--due", action="store_true", help="仅检查尚未完成的台湾周一周期")
    watch_parser.add_argument("--write", action="store_true", help="保存快照、日志及候选；默认只预演")
    enqueue_parser = commands.add_parser("enqueue", help="任务收尾证据入队，不调用 LLM")
    enqueue_parser.add_argument("--project", required=True)
    enqueue_parser.add_argument("--source", required=True)
    enqueue_parser.add_argument("--evidence", type=Path, action="append", required=True)
    enqueue_parser.add_argument("--status", choices=("candidate", "confirmed_evidence"), default="candidate")
    enqueue_parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    root = args.inbox.absolute()
    try:
        if args.command == "watch":
            result = watch(load_config(args.config), root, write=args.write, due=args.due)
        else:
            result = enqueue(root, project=args.project, source=args.source, evidence=args.evidence,
                             status=args.status, write=args.write)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result["result"] == "error" else 0
    except (WatchError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"result": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
