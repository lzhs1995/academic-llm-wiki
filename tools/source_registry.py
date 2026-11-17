#!/usr/bin/env python3
"""轻量来源登记：显式完整批次、内容寻址快照、可定位证据；不裁决科学含义。"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata

DEFAULT_VAULT = Path(__file__).resolve().parent.parent
SCHEMA = "research-source-registry/v1"
VALIDATION_CONTEXT_ENV = "RESEARCH_SOURCE_VALIDATION_CONTEXT"


class RegistryError(ValueError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(data) -> bytes:
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def load_registry(vault: Path = DEFAULT_VAULT) -> dict:
    path = Path(vault) / "sources/manifest.json"
    if not path.exists():
        return {"schema": SCHEMA, "sources": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA or not isinstance(data.get("sources"), dict):
        raise RegistryError("Unsupported source manifest")
    return data


@dataclass
class SourceSpec:
    logical_id: str
    source_path: Path
    source_type: str
    adoption_status: str = "source_only"
    locator: str = "document"
    content: bytes | None = None
    suffix: str = ".md"
    aliases: list[str] = field(default_factory=list)
    dependencies: list[Path] = field(default_factory=list)
    extractor: str = "identity/v1"
    metadata: dict = field(default_factory=dict)
    expected_hashes: dict[str, str] = field(default_factory=dict)


def _path(path, vault):
    p = Path(path)
    return p.resolve() if p.is_absolute() else (Path(vault) / p).resolve()


def _versions(manifest):
    for source in manifest["sources"].values():
        yield from source["versions"]


def _validation_original_root(vault):
    """Ephemeral checkout context supplied by research-commit; provenance stays unchanged."""
    raw = os.environ.get(VALIDATION_CONTEXT_ENV)
    if raw is None:
        return None
    context = json.loads(raw)
    if not isinstance(context, dict) or set(context) != {"original_root", "export_root"}:
        raise RegistryError("invalid candidate source validation context")
    roots = []
    for name in ("original_root", "export_root"):
        value = context[name]
        if not isinstance(value, str):
            raise RegistryError("invalid candidate source validation root")
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts or path != path.resolve():
            raise RegistryError("candidate source roots must be canonical absolute paths")
        roots.append(path)
    original, exported = roots
    if exported != vault or original == exported:
        raise RegistryError("candidate source context does not match validation vault")
    return original


def _validation_path(path, vault, original_root):
    """Map only absolute paths within the explicitly supplied original vault."""
    supplied = Path(path)
    if ".." in supplied.parts:
        raise RegistryError("path traversal is not a registered source")
    if supplied.is_absolute():
        lexical = (vault / supplied.relative_to(original_root)
                   if original_root is not None and supplied.is_relative_to(original_root) else supplied)
    else:
        lexical = vault / supplied
    if lexical != lexical.resolve():
        raise RegistryError("source validation path must not traverse symlinks")
    return lexical


def lookup(reference_path, *, vault=DEFAULT_VAULT, manifest=None):
    """只认登记的快照或别名；不按文件名相似、任意绝对路径或模型记忆找源。"""
    vault = Path(vault).resolve()
    manifest = load_registry(vault) if manifest is None else manifest
    original_root = _validation_original_root(vault)
    try:
        lexical = _validation_path(reference_path, vault, original_root)
    except RegistryError:
        return None
    if not lexical.is_relative_to(vault):
        return None
    matches = []
    for record in _versions(manifest):
        snapshot = Path(record['snapshot_path'])
        snapshot_lexical = vault / snapshot
        if (snapshot.is_absolute() or '..' in snapshot.parts or
                not snapshot_lexical.is_relative_to(vault / '.source-snapshots') or
                snapshot_lexical != snapshot_lexical.resolve()):
            raise RegistryError('invalid registered snapshot boundary or symlink')
        allowed = [record["snapshot_path"], *record.get("aliases", [])]
        if any(_validation_path(p, vault, original_root) == lexical for p in allowed):
            matches.append(record)
    if len(matches) > 1:
        raise RegistryError(f"Ambiguous registered reference: {reference_path}")
    return matches[0] if matches else None


def _normalize(text):
    return " ".join(unicodedata.normalize("NFC", text).split())


def _select(text: str, locator):
    """返回定位内容。未知定位绝不降级为全文包含判定。"""
    if locator in (None, "document", "full_document"):
        return text
    if isinstance(locator, str):
        match = re.fullmatch(r"lines:(\d+)-(\d+)", locator)
        if match:
            start, end = map(int, match.groups())
            lines = text.splitlines()
            if not 1 <= start <= end <= len(lines):
                raise RegistryError("locator_out_of_range")
            return "\n".join(lines[start - 1:end])
        if locator.startswith("json:/"):
            node = json.loads(text)
            for token in locator[6:].split("/"):
                token = token.replace("~1", "/").replace("~0", "~")
                node = node[int(token)] if isinstance(node, list) else node[token]
            return node if isinstance(node, str) else json.dumps(node, ensure_ascii=False)
        raise RegistryError("unsupported_locator")
    if isinstance(locator, dict) and set(locator) <= {"record_id", "field", "adoption_status"}:
        if not locator.get("record_id") or not locator.get("field"):
            raise RegistryError("unsupported_locator")
        payload = json.loads(text)
        rows = payload if isinstance(payload, list) else payload["rows"]
        found = [r for r in rows if r.get("record_id") == locator["record_id"]]
        if len(found) != 1:
            raise RegistryError("record_id_missing_or_ambiguous")
        record = found[0]
        if locator.get("adoption_status") and record.get("adoption_status") != locator["adoption_status"]:
            raise RegistryError("adoption_status_mismatch")
        value = record[locator["field"]]
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    raise RegistryError("unsupported_locator")


def validate_evidence(reference_path, quote=None, *, vault=DEFAULT_VAULT, manifest=None,
                      source_id=None, version=None, locator=None):
    vault = Path(vault).resolve()
    result = {"ok": False, "status": "unregistered", "errors": [], "record": None}
    try:
        record = lookup(reference_path, vault=vault, manifest=manifest)
    except (RegistryError, OSError, ValueError) as exc:
        result["errors"].append(str(exc))
        return result
    if record is None:
        result["errors"].append("reference has no registered source version")
        return result
    result["record"] = record
    if (source_id and source_id != record["source_id"]) or (version and version != record["version"]):
        result.update(status="version_mismatch", errors=["declared source/version does not match path"])
        return result
    try:
        original_root = _validation_original_root(vault)
        snap = _validation_path(record["snapshot_path"], vault, original_root)
        content = snap.read_bytes()
        actual = _validation_path(reference_path, vault, original_root).read_bytes()
    except (OSError, RegistryError) as exc:
        result.update(status="missing", errors=[str(exc)])
        return result
    if sha256(content) != record["extracted_sha256"] or sha256(actual) != record["extracted_sha256"]:
        result.update(status="hash_mismatch", errors=["registered extraction or alias bytes changed"])
        return result
    # 历史副本是登记时观察到的来源，不伪造它最初摄取时的上游身份。
    stale = []
    for origin in record["origins"]:
        try:
            if ".." in Path(origin["path"]).parts:
                raise RegistryError("source provenance path traversal")
            path = _validation_path(origin["path"], vault, original_root)
            if sha256(path.read_bytes()) != origin["sha256"]:
                stale.append(origin["path"])
        except (OSError, RegistryError):
            stale.append(origin["path"])
    if stale:
        result.update(status="stale", errors=[f"upstream changed/missing: {p}" for p in stale])
        return result
    try:
        selected = _select(content.decode("utf-8"), locator)
        if quote is not None and (not quote.strip() or _normalize(quote) not in _normalize(selected)):
            result.update(status="quote_not_in_locator", errors=["QUOTE NOT IN SOURCE at declared locator"])
            return result
    except (ValueError, UnicodeError, KeyError, IndexError, TypeError) as exc:
        result.update(status="invalid_locator", errors=[str(exc)])
        return result
    result.update(ok=True, status="valid")
    return result


def prepare_batch(specs: list[SourceSpec], *, vault=DEFAULT_VAULT, manifest=None):
    """全部输入读完/校验后才能发布；任何必需来源缺失直接失败。"""
    vault = Path(vault).resolve()
    old = load_registry(vault) if manifest is None else manifest
    new = copy.deepcopy(old)
    writes = []
    ids = set()
    for spec in specs:
        if spec.logical_id in ids:
            raise RegistryError(f"duplicate logical_id: {spec.logical_id}")
        ids.add(spec.logical_id)
        origins = []
        original = None
        expected_hashes = {str(Path(p).resolve()): digest for p, digest in spec.expected_hashes.items()}
        for source in dict.fromkeys([spec.source_path, *spec.dependencies]):
            real = Path(source).resolve(strict=True)
            payload = real.read_bytes()
            expected = expected_hashes.get(str(real))
            if expected and sha256(payload) != expected:
                raise RegistryError(f"source changed during extraction: {real}")
            if original is None:
                original = payload
            origins.append({"path": str(real), "sha256": sha256(payload), "bytes": len(payload)})
        content = original if spec.content is None else spec.content
        source_id = "src-" + sha256(spec.logical_id.encode())[:20]
        identity = {"origins": origins, "extracted_sha256": sha256(content),
                    "extractor": spec.extractor, "source_type": spec.source_type,
                    "adoption_status": spec.adoption_status, "locator": spec.locator,
                    "metadata": spec.metadata}
        version = sha256(canonical(identity))
        if not re.fullmatch(r"\.[a-z0-9]+", spec.suffix):
            raise RegistryError("invalid snapshot suffix")
        relative = f".source-snapshots/{source_id}/{version}/content{spec.suffix}"
        record = {"source_id": source_id, "version": version, "logical_id": spec.logical_id,
                  "source_path": origins[0]["path"], "source_sha256": origins[0]["sha256"],
                  "extracted_sha256": sha256(content), "bytes": len(content),
                  "snapshot_path": relative, "source_type": spec.source_type,
                  "adoption_status": spec.adoption_status, "locator": spec.locator,
                  "extractor": spec.extractor, "origins": origins,
                  "aliases": spec.aliases, "metadata": spec.metadata,
                  "captured_at": datetime.now(timezone.utc).isoformat()}
        entry = new["sources"].setdefault(source_id, {"logical_id": spec.logical_id, "versions": []})
        existing = next((x for x in entry["versions"] if x["version"] == version), None)
        if existing:
            stored = vault / existing["snapshot_path"]
            if not stored.is_file() or sha256(stored.read_bytes()) != existing["extracted_sha256"]:
                raise RegistryError(f"existing immutable snapshot missing/corrupt: {stored}")
            if existing["aliases"] != spec.aliases:
                raise RegistryError("cannot silently rebind registered aliases")
        else:
            for alias in spec.aliases:
                alias_path = Path(alias)
                lexical = alias_path if alias_path.is_absolute() else vault / alias_path
                if ".." in alias_path.parts or lexical != lexical.resolve() or not lexical.is_relative_to(vault):
                    raise RegistryError(f"invalid alias: {alias}")
                occupied = lookup(alias, vault=vault, manifest=new)
                if occupied:
                    raise RegistryError(f"alias already pinned; new version must use snapshot path: {alias}")
                if sha256(_path(alias, vault).read_bytes()) != sha256(content):
                    raise RegistryError(f"alias content does not equal extraction: {alias}")
            entry["versions"].append(record)
            writes.append((vault / relative, content))
        entry["latest_version"] = version
    return {"manifest": new, "writes": writes, "changed": new != old}


def publish_batch(specs: list[SourceSpec], *, vault=DEFAULT_VAULT, write=False):
    vault = Path(vault).resolve()
    if not write:
        prepared = prepare_batch(specs, vault=vault)
        return {"changed": prepared["changed"], "new_versions": len(prepared["writes"]), "manifest": prepared["manifest"]}
    # 先预检，缺必要源时连锁目录都不新建。
    prepare_batch(specs, vault=vault)
    directory = vault / "sources"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".registry.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        prepared = prepare_batch(specs, vault=vault)
        if not prepared["changed"]:
            return {"changed": False, "new_versions": 0, "manifest": prepared["manifest"]}
        for path, content in prepared["writes"]:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("xb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                path.chmod(0o444)
            except FileExistsError:
                if path.read_bytes() != content:
                    raise RegistryError(f"immutable snapshot collision: {path}")
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".manifest-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(canonical(prepared["manifest"]))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, directory / "manifest.json")
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return {"changed": True, "new_versions": len(prepared["writes"]), "manifest": prepared["manifest"]}
