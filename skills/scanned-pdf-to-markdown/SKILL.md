---
name: scanned-pdf-to-markdown
description: Convert scanned or image-based PDF books and papers into page-anchored Markdown (`PDF p.X | 原书 p.Y`) inside Obsidian `brain/raw/books/` using free local MinerU 4.2 ONNX models with 0 LLM tokens, preflight ETA estimation, background dispatch, live status, OS completion notification, and automatic resume.触发词：扫描版PDF转Markdown、图片PDF转md、电子书入库raw/books。
---

# Scanned PDF to Markdown (LLM-Wiki Step 1 · v2.0)

将扫描版/图片版 PDF 免费离线转换为**保留精确双页码（PDF页码 + 全书众数校准原书页码）、表格与注文**的只读保护 Markdown，默认存入 `~/Documents/brain/raw/books/`，中断后重跑秒级断点接续。

脚本路径（Mac / Linux）：`python3 ~/.agents/skills/scanned-pdf-to-markdown/scripts/pdf2md.py`  
脚本路径（Windows）：`python "%USERPROFILE%\.agents\skills\scanned-pdf-to-markdown\scripts\pdf2md.py"`

## 固定四步执行协议（任何模型必须严格按顺序调用，禁止跳步、禁止谎报完成、禁止直接编辑生成的 `.md`）

> **效率红线**：直接相信 `doctor / estimate / convert / verify` 返回的 JSON，**禁止自行翻查缓存目录、环境变量或源码**，拿到 `requires_user_confirmation == false` 后下一步必须直接运行 `convert`。

### 第 1 步：环境自检（若未安装则先询问或执行首装）
```bash
python3 ~/.agents/skills/scanned-pdf-to-markdown/scripts/pdf2md.py doctor
```
- 若返回 `"ok": true`：直接进入第 2 步。
- 若返回 `"ok": false`：告知用户需首次自动安装本地免费 `MinerU 4.2 ONNX` 模型（约 805MB，一次性），经用户同意（或用户已要求安装）后执行：
  ```bash
  python3 ~/.agents/skills/scanned-pdf-to-markdown/scripts/pdf2md.py setup
  ```

### 第 2 步：1 秒预估耗时与断点检测（必做！）
```bash
python3 ~/.agents/skills/scanned-pdf-to-markdown/scripts/pdf2md.py estimate "<PDF绝对路径>"
```
读取返回的 JSON（自动识别 `PDF2MD_VAULT` 或默认 `~/Documents/brain`）：
- **短任务直出（`requires_user_confirmation == false`，即预计 `≤ 60 秒`）**：无需打断询问，直接执行第 3 步并交付验收结果。
- **长任务先报时等授权（`requires_user_confirmation == true`，即预计 `> 60 秒`）**：
  - **若用户当前消息未明确授权立即开跑长任务**：**严禁擅自启动 `convert`**！必须先向用户汇报 JSON 中的 `human_message`（总页数、已缓存页数、待转页数、预计分钟数、预估依据、0元/0 Token、目标路径），等待用户回复确认后再进入第 3 步。
  - **一旦用户确认（或用户原话已明确说“直接跑/继续转完”）**：将第 3 步作为后台异步任务执行（过程中可随时用 `pdf2md.py status "<PDF绝对路径>"` 查看实时剩余时间）。

### 第 3 步：执行转换（自动内存限流 + 每 25 页断点续传 + 系统弹窗通知 + 只读落盘）
```bash
python3 ~/.agents/skills/scanned-pdf-to-markdown/scripts/pdf2md.py convert "<PDF绝对路径>"
```
- 可选参数：`--slug "自定义文件名"`、`--title "书名"`、`--pdf-offset 12`（默认无需手填，脚本自动按全书众数推算原书页码并识别罗马数字前言）。
- 中途中断后，再次执行同一条 `convert` 命令即可自动跳过已完成的 25 页分块秒级续传。
- 输出的 `.md` 默认加 `0444` 只读锁，**严禁 Agent 手动改写或“润色”生成的原始书稿**。

### 第 4 步：确定性验收并如实汇报（必做！）
核查 `convert` 返回 JSON 中的 `verification`（或单独运行 `pdf2md.py verify "<生成的md路径>" --expected-pages <总页数>`）：
- 只有当 `"ok": true` 且 `"missing_pages": []` 时才允许报告完成！
- 向用户如实汇报：① 输出 Markdown 绝对路径与 `sha256`；② 已验收总页数（如 `545/545 页，0 漏页`）与自动修正的 OCR 错页码数；③ 实际耗时与本地 `0` 元 / `0` 模型转换费用。
