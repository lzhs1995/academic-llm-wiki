#!/usr/bin/env python3
"""Check quotation text, registered source versions and evidence; not semantic proof."""
from __future__ import annotations
import argparse
from pathlib import Path
import re
import sys
import unicodedata
import yaml
from wiki_checks import active, frontmatter, strip_fences, SOURCE_QUOTE_RE

ROOT = Path(__file__).resolve().parent.parent
QUOTE_RE = SOURCE_QUOTE_RE

def normalize(text):
    return ' '.join(unicodedata.normalize('NFC', text).split())

def validate_page(page, root, manifest=None, require_registry=True):
    from source_registry import validate_evidence
    errors, checked = [], 0
    try:
        text = page.read_text(encoding='utf-8')
        fm, _ = frontmatter(text)
    except (OSError, ValueError, UnicodeError, yaml.YAMLError) as exc:
        return [f'{page}: {exc}'], 0
    body = strip_fences(text)
    matches = list(QUOTE_RE.finditer(body))
    parsed_lines = {body[:m.start()].count('\n')+1 for m in matches}
    for no, line in enumerate(body.splitlines(), 1):
        if re.match(r'^>\s*[“"]', line) and no not in parsed_lines:
            errors.append(f'{page.name}:{no}: malformed source quotation')
    evidence = fm.get('evidence', [])
    if not isinstance(evidence, list):
        errors.append(f'{page.name}: evidence must be a list')
        evidence = []
    if active(page, root) and not matches and not evidence:
        errors.append(f'{page.name}: active factual/candidate card needs source evidence')
    elif require_registry and active(page, root) and not evidence:
        errors.append(f'{page.name}: active card needs version-bound evidence metadata')
    for match in matches:
        src, quote = match.group('src'), match.group('quote')
        if require_registry and active(page, root):
            result = validate_evidence(src, quote=quote, vault=root, manifest=manifest)
            if not result['ok']:
                errors.append(f'{page.name}: {src}: {result["status"]}: {result["errors"]}')
            if not any(isinstance(item, dict) and isinstance(item.get('path'), str)
                       and (root / item['path']).absolute() == (root / src).absolute()
                       and isinstance(item.get('quote'), str) and normalize(item['quote']) == normalize(quote)
                       for item in evidence):
                errors.append(f'{page.name}: quotation lacks matching version-bound evidence: {src}')
        else:
            path = (root / src).resolve()
            allowed = [root / 'raw', root / 'inbox', root / '.source-snapshots']
            if not any(path.is_relative_to(p.resolve()) for p in allowed) or not path.is_file():
                errors.append(f'{page.name}: unavailable/outside legacy source {src}')
            elif normalize(quote) not in normalize(path.read_text(encoding='utf-8')):
                errors.append(f'{page.name}: QUOTE NOT IN SOURCE: {src}')
        checked += 1
    for item in evidence:
        if not isinstance(item, dict) or any(not item.get(k) for k in ('path', 'source_id', 'version', 'locator')):
            errors.append(f'{page.name}: evidence needs path/source_id/version/locator')
            continue
        if (any(not isinstance(item[k], str) for k in ('path', 'source_id', 'version'))
                or not isinstance(item['locator'], (str, dict))
                or ('quote' in item and not isinstance(item['quote'], str))):
            errors.append(f'{page.name}: invalid evidence field types')
            continue
        direction, locator = item.get('direction'), item['locator']
        if direction is not None:
            if not isinstance(direction, str):
                errors.append(f'{page.name}: invalid evidence direction type')
                continue
            field = locator.get('field') if isinstance(locator, dict) else None
            state = locator.get('adoption_status') if isinstance(locator, dict) else None
            expected = 'after' if direction in {'after', 'accepted', 'retained'} else 'before'
            if direction not in {'before', 'after', 'accepted', 'retained'} or field != expected:
                errors.append(f'{page.name}: direction does not match locator field')
            if direction in {'after', 'accepted', 'retained'} and state not in {'verified_in_final', 'retained_in_final'}:
                errors.append(f'{page.name}: after/accepted direction needs verified adoption locator')
            if direction == 'retained' and state != 'retained_in_final':
                errors.append(f'{page.name}: retained direction needs retained adoption locator')
        direction = item.get('direction')
        locator = item['locator']
        if direction in {'before', 'after', 'accepted', 'retained'}:
            field = 'before' if direction == 'before' else 'after'
            expected_status = {'accepted': 'verified_in_final', 'retained': 'retained_in_final'}.get(direction)
            if not isinstance(locator, dict) or locator.get('field') != field or (expected_status and locator.get('adoption_status') != expected_status):
                errors.append(f'{page.name}: evidence direction conflicts with record locator/adoption')
                continue
        result = validate_evidence(item['path'], quote=item.get('quote'), vault=root, manifest=manifest,
                                   source_id=item['source_id'], version=item['version'], locator=item['locator'])
        if not result['ok']:
            errors.append(f'{page.name}: declared evidence {result["status"]}: {result["errors"]}')
        checked += 1
    return errors, checked

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--list', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        from source_registry import load_registry
        manifest = load_registry(vault=root)
    except Exception as exc:
        print(f'quote-check: source registry unavailable: {exc}', file=sys.stderr)
        return 2
    errors, checked, quoted = [], 0, 0
    for page in sorted((root / 'wiki').rglob('*.md')):
        if args.list:
            for m in QUOTE_RE.finditer(strip_fences(page.read_text())):
                print(f'{page.relative_to(root)} [{m.group("src")}] {m.group("quote")[:60]}')
            continue
        errs, count = validate_page(page, root, manifest)
        errors.extend(errs)
        checked += count
        quoted += bool(count)
    for error in errors:
        print(error, file=sys.stderr)
    print(f'quote-check: {len(errors)} 个问题 / {checked} 引用与证据项 / {quoted} 页；仅文本、版本与定位一致性，科学解释另行复核', file=sys.stderr)
    return 2 if errors else 0

if __name__ == '__main__':
    sys.exit(main())
