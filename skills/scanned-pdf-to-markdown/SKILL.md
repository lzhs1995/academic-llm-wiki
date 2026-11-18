---
name: scanned-pdf-to-markdown
description: Convert scanned or image-based PDF books and papers into page-anchored Markdown (`PDF p.X | 原书 p.Y`) inside Obsidian `brain/raw/books/` using free local MinerU 4.2 ONNX models with 0 LLM tokens, preflight ETA estimation, background dispatch, and automatic resume.触发词：扫描版PDF转Markdown、图片PDF转md、电子书入库raw/books。
---

# Scanned PDF to Markdown (LLM-Wiki Step 1)

将扫描版/图片版 PDF 免费离线转换为**保留双页码（PDF页码 + 原书印刷页码）、表格与脚注**的 Markdown，默认存入 `~/Documents/brain/raw/books/`，中断后重跑自动断点接续。

脚本路径（Mac / Linux）：`python3 ~/.agents/skills/scanned-pdf-to-markdown/scripts/pdf2md.py`
脚本路径（Windows）：`python "%USERPROFILE%\.agents\skills\scanned-pdf-to-markdown\scripts\pdf2md.py"`

## 固定四步执行协议（任何模型严格照此顺序执行，禁止跳步或手敲原生复杂参数）

### 第 1 步：环境自检（若未安装则自动首装）
运行：
```bash
python3 ~/.agents/skills/scanned-pdf-to-markdown/scripts/pdf2md.py doctor
```
- 若返回 `"ok": true`，直接进入第 2 步。
- 若返回 `"ok": false`，先告知用户首次配置需自动安装本地免费 `MinerU 4.2 ONNX` 模型（约 805MB），执行：
  ```bash
  python3 ~/.agents/skills/scanned-pdf-to-markdown/scripts/pdf2md.py setup
  ```

### 第 2 步：1 秒预估耗时（必做）
运行：
```bash
python3 ~/.agents/skills/scanned-pdf-to-markdown/scripts/pdf2md.py estimate "<PDF绝对路径>" --vault "~/Documents/brain"
```
读取返回的 JSON：
- **短篇快速直出（`requires_user_confirmation == false`，即预计 `≤ 60 秒`）**：无需打断用户询问，直接执行第 3 步并交付结果。
- **长篇先报时后派发（`requires_user_confirmation == true`，即预计 `> 60 秒`）**：
  - 若用户当前消息尚未明确授权启动长任务，直接将 JSON 中的 `human_message`（总页数、待转页数、预计分钟数、0元/0 Token、目标路径）发给用户请求确认；
  - 一旦用户确认（或用户原话已明确要求直接跑完），将第 3 步作为**后台异步任务 / 子任务**启动，不阻塞用户当前对话。

### 第 3 步：执行转换（自动内存保护 + 每 25 页断点缓存续传）
```bash
python3 ~/.agents/skills/scanned-pdf-to-markdown/scripts/pdf2md.py convert "<PDF绝对路径>" --vault "~/Documents/brain"
```
- 可选参数：`--slug "自定义文件名"`、`--title "书名"`、`--pdf-offset 12`（若已知正文第1页位于PDF第13页；不填也会自动识别每页印刷页码）。
- 若中途中断，再次运行完全相同的命令即可自动跳过已完成的 25 页分块秒级续传。

### 第 4 步：确定性验收并主动通知用户
`convert` 返回的 JSON 内嵌 `verification` 字段（也可单独跑 `pdf2md.py verify "<生成的md路径>" --expected-pages <总页数>`）。
确认 `"ok": true` 且 `"missing_pages": []` 后，立即在对话中通知用户：
1. 生成的 Markdown 绝对路径（默认 `~/Documents/brain/raw/books/<书名>.md`）；
2. 已验收页数（如 `545 / 545 页，0 漏页`）与实际耗时；
3. 费用说明：本地模型转换，`0` 元 / `0` Token。
