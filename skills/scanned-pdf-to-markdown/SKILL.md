---
name: scanned-pdf-to-markdown
description: Convert scanned or image-based PDF books and papers into page-anchored Markdown (`PDF p.X | 原书 p.Y`) inside Obsidian `brain/raw/books/` using free local MinerU 4.2 ONNX models with 0 LLM tokens, preflight ETA, background run, live status, OS completion notification, and automatic resume.触发词：扫描版PDF转Markdown、图片PDF转md、电子书入库raw/books。
---

# Scanned PDF to Markdown (LLM-Wiki Step 1 · v2.2)

把扫描版/图片版 PDF 在本机免费转成 Markdown：每页带 PDF 页码和原书印刷页码，表格转为结构化 Markdown/HTML 表，插图与数据图表（`image` / `chart`）按版面检测坐标自动裁切为 PNG 附件存入 `raw/books/assets/<书名>/` 并内嵌链接，默认存进 `~/Documents/brain/raw/books/<书名>.md`（设了 `PDF2MD_VAULT` 就存进那个库），文件只读；中断后重跑同一条命令会跳过已转完的分块。

脚本（下文记作 `pdf2md`）：
- Mac / Linux：`python3 ~/.agents/skills/scanned-pdf-to-markdown/scripts/pdf2md.py`
- Windows：`python "%USERPROFILE%\.agents\skills\scanned-pdf-to-markdown\scripts\pdf2md.py"`

## 规则（任何模型都照做）

- 只按下面 4 步调用脚本，直接用它返回的 JSON；不要去翻缓存目录、源码或改环境变量。
- 不要编辑、"润色"或删除生成的 `.md` 和 `.vault-meta/` 里的缓存。
- 不要自己加 `--overwrite`。脚本拒绝覆盖一个不是它上次生成的文件时，把报错原样告诉用户，由用户决定换 `--slug` 还是替换。
- 返回 `"ok": false` 或报错时，把 `error` 原文告诉用户，不要说"已完成"。

## 第 1 步：环境自检

```bash
pdf2md doctor
```
- `"ok": true` → 第 2 步。
- `"ok": false` → 告诉用户需要一次性安装本地 MinerU 和模型（约 1.4 GB 磁盘），用户同意（或已要求安装）后运行 `pdf2md setup`，再重跑 `doctor`。

## 第 2 步：预估时间（必做）

```bash
pdf2md estimate "<PDF绝对路径>"
```
- `requires_user_confirmation == false`（预计 ≤ 60 秒）→ 直接第 3 步。
- `requires_user_confirmation == true` → 用户这条消息没明确说"直接转"时，先把 `human_message` 原样发给用户（页数、已缓存页数、预计分钟数、预估依据、当前内存和负载提示、输出路径），等用户确认再做第 3 步。

## 第 3 步：转换

短任务直接前台运行：
```bash
pdf2md convert "<PDF绝对路径>"
```

长任务放后台，不要让用户等着：
- **Claude Code**：用 Bash 工具的 `run_in_background: true` 运行上面这条命令。跑完时会自动收到完成通知，再读输出 JSON 做第 4 步。
- **其他客户端（Codex、Gemini/agy、Cursor 等）**：加 `--background`，命令立即返回进程号和 `status_command`：
  ```bash
  pdf2md convert "<PDF绝对路径>" --background
  ```
  跑完时系统会弹桌面通知。用户问进度或你要验收时运行 `pdf2md status "<PDF绝对路径>"`：`state` 为 `running` 时报 `completed_pages / selected_pages` 和 `live_eta_min`；`done` 时用其中的 `verification` 做第 4 步；`interrupted` 或 `error` 时如实告诉用户，重跑同一条 convert 会从断点继续。

可选参数：`--slug 文件名`、`--title 书名`、`--pdf-offset N`（只在用户明确告诉你"正文第 1 页是 PDF 第 N+1 页"时用；平常脚本自动推算）。

## 第 4 步：验收并如实汇报

看 `convert` 输出或 `status` 里的 `verification`（也可单跑 `pdf2md verify "<md路径>" --expected-pages <总页数>`）。只有 `"ok": true` 且 `missing_pages` 为空才算完成。告诉用户：
1. 输出文件绝对路径和 `sha256`；
2. 验收页数（如 `545/545 页，0 漏页`）、实际耗时、`0` 元 / `0` Token；
3. 页码情况：`book_page_basis_counts`。`numbering_breaks` 不为空时列出这些位置（页码在这里重新起头或跳号，如插图页、书末广告）。

## 页码标记怎么读

每页开头：
```
<!-- pdf_page: 37 | book_page: 25 | basis: printed | ocr_page: 25 | header: 差序格局 -->
### [PDF p.37 | 原书 p.25 · 差序格局]
```
- `book_page` 由一段连续页共同的"PDF 页码 − 印刷页码"差值算出，单页识别出的数字只用来投票和核对，所以 `12I`、`III`、装订字母 `B` 不会变成页码。罗马数字前言、书末另起页码的附页（`back-3`）、开头就是 p.231 的论文节选都能识别。
- `basis`：`printed` 本页读到的印刷页码与推算一致；`corrected` 本页读到的数字不对，已按前后页改正；`inferred` 本页没印页码（如章首页），夹在有页码的页之间，可靠；`extrapolated` 本页没印页码、在一段页码的头尾往外推出来（如扉页算 p.1），标题里写"无印刷页码，推算"，引用这一页前先看原图。
- `unnumbered`：推不出可靠页码（封面、空白页、读不出数字的前言页）。
