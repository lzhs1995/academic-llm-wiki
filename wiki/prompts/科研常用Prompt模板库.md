---
type: prompt
title: 科研常用Prompt模板库
scenarios:
- 方法与代码跨波次迁移
- 个人文风一步到位润色
- 任务收尾经验入库
verified_status: illustrative
evidence:
- source_id: src-2281781c4cc193204d47
  version: 0c8436e3c81426180e32b26b5f6d45f5a00fe7db4b451fe6dc9989d2c6a45bd1
  path: raw/demo/revision_corpus_notes.md
  locator: lines:4-4
  quote: 合并重复句式，区间含零仅陈述未获统计支持，不改写为“证明无效”
---

# 科研常用 Prompt 模板库

返回 [[wiki/index|Wiki 路由目录]]

## 1. 任务收尾一键沉淀（最常用）
```text
收尾，把这次确认的事实和新想法记进 brain
```

## 2. 旧方法往新数据迁移排雷
```text
先读 CLAUDE.md 和 wiki/index.md，按 migrate-check 评估把【旧方法/指标】迁移到【新数据波次】：分栏列出可复用代码、必须重做的跳答/身份核查项与未验证边界。
```

## 3. 按个人文风一步到位润色
```text
先读 wiki/style/ 下的文风与采用语料卡，润色以下段落：保留全部统计数字与不确定性限定，写得好的句子原样保留，含 Zotero 活域处只给域外替换文本。
```

> "合并重复句式，区间含零仅陈述未获统计支持，不改写为“证明无效”" — raw/demo/revision_corpus_notes.md
