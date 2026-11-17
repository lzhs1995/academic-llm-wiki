#!/usr/bin/env python3
"""Validate exact links, recursive card schemas, supported Bases filters and source versions."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys
import urllib.parse
import yaml
from wiki_checks import PageIndex, WIKILINK, active, check_anchor, filter_matches, frontmatter, markdown_targets, strip_code, navigation_text

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_FIELDS = {
    'paper': ('project_id', 'status', 'dataset', 'design', 'domain', 'measure_form', 'methods'),
    'method': ('family', 'software', 'used_in', 'verified_status'),
    'variable': ('construct_group', 'datasets', 'role'),
    'dataset': ('survey', 'waves', 'unit'),
    'opportunity': ('origin_source', 'limitation_kind', 'candidate_dataset', 'unlock_condition', 'state'),
    'style': ('scope', 'rule_count', 'verified_from'),
    'prompt': ('scenarios', 'verified_status'),
    'status': ('updated',),
    'research': ('project_id', 'status', 'updated'),
}
ENUMS = {
    'state': {'open', 'data_ready', 'verifying', 'viable', 'parked', 'adopted', 'blocked', 'superseded'},
    'scope': {'global', 'dissertation', 'cssci_journal', 'ssci_journal', 'formatting_guard', 'project', 'candidate'},
    'verified_status': {'confirmed', 'source_checked', 'partially_verified', 'pending', 'candidate', 'needs_review', 'illustrative', 'not_tested', 'tested'},
    'evidence_review': {'source_checked', 'needs_review', 'candidate', 'mixed', 'historical'},
}
LIST_FIELDS = {'dataset', 'domain', 'methods', 'software', 'used_in', 'datasets', 'waves', 'origin_source', 'candidate_dataset', 'scenarios'}

def audit(root, strict=False):
    errors, warnings, views = [], [], []
    index = PageIndex(root)
    idx = root / 'wiki/index.md'
    indexed = set()
    if not idx.exists():
        errors.append('missing wiki/index.md')
    else:
        text = idx.read_text(encoding='utf-8')
        if len(text.splitlines()) > 200:
            warnings.append('wiki/index.md exceeds 200 lines; split routing by topic')
        for target in WIKILINK.findall(strip_code(text)):
            try:
                indexed.add(index.resolve(target, idx)[0])
            except ValueError as exc:
                errors.append(str(exc))
    pages = sorted((root/'wiki').rglob('*.md')) + [p for p in (root/'00_研究总控台.md', root/'index.md') if p.exists()]
    metadata, links, files = {}, 0, 0
    for page in pages:
        rel = page.relative_to(root).as_posix()
        try:
            text = page.read_text(encoding='utf-8')
            fm, body = frontmatter(text)
        except Exception as exc:
            errors.append(f'{rel}: invalid YAML/text: {exc}')
            continue
        clean = navigation_text(body)
        for target in WIKILINK.findall(clean):
            links += 1
            try:
                destination, anchor = index.resolve(target, page)
                check_anchor(destination, anchor)
            except (ValueError, OSError) as exc:
                errors.append(f'{rel}: {exc}')
        for url in markdown_targets(clean):
            parts = urllib.parse.urlsplit(url)
            if parts.scheme and parts.scheme != 'file':
                continue
            if not parts.path and not parts.fragment:
                continue
            files += 1
            path = Path(urllib.parse.unquote(parts.path))
            if not parts.path:
                path = page
            elif not path.is_absolute():
                path = page.parent / path
            if not path.exists():
                errors.append(f'{rel}: local link missing: {path}')
            else:
                try:
                    check_anchor(path, urllib.parse.unquote(parts.fragment))
                except ValueError as exc:
                    errors.append(f'{rel}: {exc}')
        if not active(page, root):
            continue
        metadata[page] = fm
        if page not in indexed:
            (errors if strict else warnings).append(f'{rel}: card absent from routing index')
        kind = fm.get('type')
        if kind not in REQUIRED_FIELDS:
            errors.append(f'{rel}: unknown type={kind!r}')
            continue
        missing = [key for key in REQUIRED_FIELDS[kind] if fm.get(key) in (None, '', [])]
        if missing:
            errors.append(f'{rel}: missing fields {missing}')
        for key in LIST_FIELDS.intersection(fm):
            if not isinstance(fm[key], list):
                errors.append(f'{rel}: {key} must be list')
        for key, values in ENUMS.items():
            if key in fm and (not isinstance(fm[key], str) or fm[key] not in values):
                errors.append(f'{rel}: unsupported {key}={fm[key]!r}')
        if kind == 'style' and (not isinstance(fm.get('rule_count'), int) or isinstance(fm.get('rule_count'), bool) or fm['rule_count'] < 0):
            errors.append(f'{rel}: rule_count must be nonnegative integer')
        if kind == 'opportunity' and fm.get('state') in {'data_ready', 'viable', 'adopted'}:
            checks = fm.get('readiness', {})
            required = ['data', 'measurement'] + (['method', 'literature'] if fm['state'] in {'viable', 'adopted'} else [])
            if not isinstance(checks, dict) or any(checks.get(k) != 'verified' for k in required):
                errors.append(f'{rel}: promoted opportunity requires verified readiness {required}')
            if not fm.get('evidence'):
                errors.append(f'{rel}: promoted opportunity requires version-bound evidence')
    for filename in ('研究资产五维矩阵.base', '新增长点雷达.base', '文风与术语规则.base'):
        path = root / 'Bases' / filename
        try:
            data = yaml.safe_load(path.read_text())
            if not isinstance(data, dict) or not isinstance(data.get('views'), list) or not data['views']:
                raise ValueError('missing views list')
            for view in data['views']:
                matches = []
                for page, fm in metadata.items():
                    global_match = filter_matches(data.get('filters'), page, fm, root)
                    view_match = filter_matches(view.get('filters'), page, fm, root)
                    if global_match and view_match:
                        matches.append(page.relative_to(root).as_posix())
                views.append({'base': filename, 'view': view.get('name'), 'count': len(matches), 'pages': matches})
                if not matches and filename != '新增长点雷达.base':
                    errors.append(f'{filename}/{view.get("name")}: expected populated view is empty')
        except (OSError, ValueError, yaml.YAMLError) as exc:
            errors.append(f'{filename}: filter not validated: {exc}')
    return {'errors': errors, 'warnings': warnings, 'active_cards': len(metadata), 'wikilinks': links, 'local_links': files, 'views': views}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--strict', action='store_true')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    result = audit(args.root.resolve(), args.strict)
    qc = subprocess.run([sys.executable, str(Path(__file__).with_name('quote-check.py')), '--root', str(args.root.resolve())], capture_output=True, text=True)
    result['quotation_check'] = qc.stderr.strip()
    if qc.returncode:
        result['errors'].append('quote-check failed: ' + qc.stderr.strip())
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for error in result['errors']:
            print('ERROR: ' + error, file=sys.stderr)
        for warning in result['warnings']:
            print('WARN: ' + warning, file=sys.stderr)
        for view in result['views']:
            print(f'BASE: {view["base"]}/{view["view"]}: {view["count"]} matching cards (UI not checked)')
        print(f'wiki-lint: {len(result["errors"])} 错误, {len(result["warnings"])} 警告 | 活跃卡片 {result["active_cards"]} | 双链 {result["wikilinks"]} | 本地链接 {result["local_links"]}')
        print(result['quotation_check'])
    return 1 if result['errors'] else 0

if __name__ == '__main__':
    sys.exit(main())
