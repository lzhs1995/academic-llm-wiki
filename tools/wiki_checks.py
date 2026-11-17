"""Deterministic Markdown checks; passing does not establish scientific truth."""
from __future__ import annotations
from collections import defaultdict
from pathlib import Path
import re
import urllib.parse
import yaml

ACTIVE_DIRS = ('status', 'papers', 'methods', 'variables', 'datasets', 'opportunities', 'style', 'prompts', 'research')
EXCLUDED = {'.git', '.obsidian', '.claude', '.vault-meta', '.source-snapshots', 'raw', 'inbox', 'tests', 'reports'}
WIKILINK = re.compile(r'!?\[\[([^\]|]+)(?:\|[^\]]*)?\]\]')
SOURCE_QUOTE_RE = re.compile(r'^>\s*[“"](?P<quote>.+?)[”"]\s*[—–-]+\s*(?P<src>(?:(?:inbox|raw|sources|\.source-snapshots)/|/).+?)\s*$', re.M)

def frontmatter(text):
    match = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)', text, re.S)
    if not match:
        return {}, text
    data = yaml.safe_load(match.group(1)) or {}
    if not isinstance(data, dict):
        raise ValueError('frontmatter must be a mapping')
    return data, text[match.end():]

def strip_fences(text):
    lines, fence = [], None
    for line in text.splitlines():
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if fence:
            if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = None
            lines.append('')
        elif marker:
            fence = marker.group(1)
            lines.append('')
        else:
            lines.append(line)
    return '\n'.join(lines)

def strip_code(text):
    return re.sub(r'`[^`\n]+`', '', strip_fences(text))

def navigation_text(text):
    """Quoted source text is checked by quote-check, not treated as page navigation."""
    return strip_code(SOURCE_QUOTE_RE.sub('', text))

def active(path, root):
    parts = path.relative_to(root).parts
    return len(parts) >= 3 and parts[0] == 'wiki' and parts[1] in ACTIVE_DIRS

def markdown_targets(text):
    """Inline destinations, including balanced parentheses in local filenames."""
    for match in re.finditer(r'!?\[[^\]\n]*\]\(', text):
        i, depth, escaped = match.end(), 1, False
        start = i
        while i < len(text) and depth:
            c = text[i]
            if escaped:
                escaped = False
            elif c == '\\':
                escaped = True
            elif c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
            i += 1
        if depth == 0:
            target = text[start:i-1].strip()
            if target.startswith('<') and '>' in target:
                target = target[1:target.index('>')]
            else:
                target = re.split(r'\s+[\"\']', target, maxsplit=1)[0]
            yield target

class PageIndex:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.paths = set()
        self.names = defaultdict(set)
        for path in self.root.rglob('*'):
            rel = path.relative_to(self.root)
            if rel.parts[0] == 'sources' or not path.is_file() or any(p in EXCLUDED or p.startswith('.') for p in rel.parts):
                continue
            if path.suffix not in {'.md', '.base', '.canvas', '.png', '.jpg', '.jpeg', '.svg', '.pdf', '.webp'}:
                continue
            self.paths.add(path)
            self.names[path.name].add(path)
            if path.suffix in {'.md', '.base', '.canvas'}:
                self.names[path.stem].add(path)

    def resolve(self, target, origin):
        raw, _, anchor = urllib.parse.unquote(target).partition('#')
        raw = raw.strip()
        if not raw:
            return origin, anchor
        if '/' in raw:
            candidates = set()
            bases = [self.root / raw] if not raw.startswith('.') else [origin.parent / raw]
            for base in bases:
                for p in (base, Path(str(base) + '.md')):
                    resolved = p.resolve()
                    if resolved.is_relative_to(self.root) and resolved in self.paths:
                        candidates.add(resolved)
        else:
            candidates = self.names.get(raw, set())
        if len(candidates) != 1:
            raise ValueError(('ambiguous' if candidates else 'missing') + f' wikilink [[{target}]]')
        return next(iter(candidates)), anchor

def check_anchor(path, anchor):
    if not anchor or path.suffix != '.md':
        return
    text = path.read_text(encoding='utf-8')
    if anchor.startswith('^'):
        found = re.search(r'(?:^|\s)\^' + re.escape(anchor[1:]) + r'\s*$', text, re.M)
    else:
        headers = re.findall(r'^#{1,6}\s+(.+?)\s*#*\s*$', text, re.M)
        found = anchor in headers or anchor in {re.sub(r'[^\w\s-]', '', h).lower().replace(' ', '-') for h in headers}
    if not found:
        raise ValueError(f'missing anchor #{anchor} in {path.name}')

def filter_matches(expr, path, metadata, root):
    """Evaluate only the supported Bases subset; unknown expressions fail closed."""
    if expr is None:
        return True
    if isinstance(expr, dict):
        if len(expr) != 1:
            raise ValueError('unsupported Bases filter mapping')
        key, value = next(iter(expr.items()))
        if key not in {'and', 'or', 'not'}:
            raise ValueError(f'unsupported Bases operator {key}')
        items = value if isinstance(value, list) else [value]
        results = [filter_matches(x, path, metadata, root) for x in items]
        return all(results) if key == 'and' else any(results) if key == 'or' else not any(results)
    if not isinstance(expr, str):
        raise ValueError('unsupported Bases filter type')
    folder = re.fullmatch(r'file\.inFolder\("([^"]+)"\)', expr.strip())
    if folder:
        return path.relative_to(root).as_posix().startswith(folder.group(1).rstrip('/') + '/')
    match = re.fullmatch(r'(file\.ext|note\.[\w_]+)\s*(==|!=)\s*"([^"]*)"', expr.strip())
    if match:
        key, operator, expected = match.groups()
        actual = path.suffix[1:] if key == 'file.ext' else metadata.get(key[5:])
        return actual == expected if operator == '==' else actual != expected
    raise ValueError(f'Bases filter not evaluated: {expr}')
