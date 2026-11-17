#!/usr/bin/env python3
"""从 wiki/ 卡片 YAML 属性确定性生成二级路由目录 wiki/index.md，不臆造完成状态。"""
from __future__ import annotations
import argparse
from pathlib import Path
from wiki_checks import ACTIVE_DIRS, frontmatter

ROOT = Path(__file__).resolve().parents[1]
LABELS = {
    'status': '项目与研究共识',
    'papers': '论文资产',
    'methods': '方法复用',
    'variables': '变量与测量',
    'datasets': '数据与版本',
    'opportunities': '局限与研究机会',
    'style': '风格与已采用语料',
    'prompts': '工作模板',
    'research': '在研课题核查',
}


def build_index(root: Path) -> str:
    lines = [
        '---',
        'type: meta',
        'title: 研究知识库路由',
        'updated: 2026-11-18',
        '---',
        '',
        '# 研究知识库路由（Wiki Index）',
        '',
        'Agent 开始任务时先读本页定位相关卡片；初次按需读取 3–6 张，证据不足再回查 `raw/` 或登记源快照。',
        '[[00_研究总控台|研究总控台]] · [[wiki/log|维护日志]]',
        '',
    ]
    for directory in ACTIVE_DIRS:
        folder = root / 'wiki' / directory
        items = []
        if folder.is_dir():
            for p in sorted(folder.rglob('*.md')):
                fm, _ = frontmatter(p.read_text(encoding='utf-8'))
                name = fm.get('title') or p.stem
                state = (fm.get('state') or fm.get('verified_status') or fm.get('status')
                         or fm.get('evidence_review') or '见卡片适用范围')
                rel = p.relative_to(root).with_suffix('').as_posix()
                items.append(f'- [[{rel}|{name}]] — {state}')
        if items:
            lines += [f'## {LABELS[directory]}', ''] + items + ['']
    return '\n'.join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    text = build_index(ROOT)
    dest = ROOT / 'wiki/index.md'
    changed = not dest.exists() or dest.read_text(encoding='utf-8') != text
    if args.write and changed:
        dest.write_text(text, encoding='utf-8')
    print('wiki/index.md', 'changed' if changed else 'unchanged')


if __name__ == '__main__':
    main()
