# 扫描版中英文 PDF 转带页码 Markdown 实测指南（Mac & Windows 双端实测版）

面向搭建 **LLM-Wiki（Obsidian + Agent）** 的研究者，遇到内嵌文字层为 `0` 字符的**纯图片/扫描版 PDF 书籍或论文**时，第一步的核心需求只有五件事：
1. **默认保留双页码**（`PDF p.X | 原书 p.Y`），方便后续论文引用溯源；
2. **默认纯本地免费模型转换**（`0` 元 API 费、`0` 大模型 Token）；
3. **默认在 Agent 对话里一句话触发**（通过 `scanned-pdf-to-markdown` Skill 自动执行，不用去终端敲一长串复杂参数）；
4. **默认存入 Obsidian 的 `brain/raw/books/` 且支持中断秒级断点续传**；
5. **默认先 1 秒预估耗时**：短篇（≤1 分钟）直接出结果；长篇专著先报预估分钟数，获准后作为后台子任务运行并在完成后主动通知。

---

## 一、为什么上游 `MinerU` 原项目外还需要包一层薄 Skill？

直接核查上游 `opendatalab/MinerU` 官方仓库及其自带的 `skills/mineru/SKILL.md` 后，实测发现原版 CLI 与官方 Skill 不能直接开箱满足知识库入库需求：

| 痛点维度 | 上游 `MinerU` 原版 CLI / 官方 Skill | 本仓库 `scanned-pdf-to-markdown` Skill (`pdf2md.py`) |
|---|---|---|
| **页码保留** | `mineru-kit parse -o book.md` 默认**删光全部页码**拼成无页码长文（实测 `has_page_markers: false`）；官方 Skill 仅每次临时读 10 页 JSON，不落盘全书 Markdown | 逐页解析 `middle_json`，每页自动生成机器注释与可读双页头：`### [PDF p.37 \| 原书 p.25 · 差序格局]` |
| **耗时预估与后台派发** | 无预估机制，直接前台阻塞数十分钟 | `estimate` 子命令 1 秒返回页数、断点进度与预计分钟数；`>1 分钟` 先询问后转后台子任务 |
| **断点续传** | 中途断电或中断需整本重跑 | 每 25 页原子落盘 JSON 缓存，中断后重跑在 `~1.3 秒` 内跳过已完成分片 |
| **防卡死内存保护** | 手动多开进程在空闲内存不足时会打爆 macOS / Windows 虚拟内存引发数倍降速 | `doctor` 自动检测实时可用物理内存，Mac / Windows 按平台动态选择并发度 |
| **为什么选 Skill 不选 MCP** | — | MCP 需常驻后台进程且每轮对话白占上下文 Token；`Skill + 单文件标准库脚本` 平时 `0` 内存 `0` Token，且 Claude Code / Codex / WorkBuddy / Antigravity 原生通用 |

---

## 二、Mac 高配本 vs Windows 16G 轻薄本 真机实测数据一览

测试样本（均为内嵌文字层 `0` 字符的纯扫描 PDF）：
- **中文扫描专著**：20 世纪社会学经典合集（**545 页**，`38.6 MB`，约 180 DPI 灰度扫描，含脚注与大量形近词）
- **英文扫描古籍**：1884 年版英国工业革命讲义（**390 页**，`43.0 MB`，600 DPI CCITT 传真二值扫描，含无框线历史人口统计表与双栏小字注文）

### 1. 双平台真机硬件与转换耗时对照表

| 实测指标 | Mac 高配本（Apple M5 Pro · 15核 · 48GB） | Windows 主流轻薄本（Intel i7-1260P · 12核16线程 · 16GB · Iris Xe） |
|---|---|---|
| **底层加速引擎** | `ONNX Runtime` ARM64 CPU | `onnxruntime-directml 1.24.4`（无重型 PyTorch 依赖） |
| **首次环境安装耗时** | Python 包约 `25 秒` + 模型 `805 MB`（一次性） | 清华镜像安装 118 个包实测 **`39.6 秒`** + 模型 `805 MB`（一次性） |
| **单并发速度（`workers=1`，后台极低占用）** | 中文 **`6.17 秒/页`** · 英文 **`7.66 秒/页`** | 中文 **`11.49 秒/页`** · 英文 **`11.88 秒/页`** |
| **双并发速度（`workers=2`，日常推荐档）** | 约 **`3.5–4.0 秒/页`**（需空闲内存 `≥8 GB`） | 实测 **`5.80 秒/页`**（仅需空闲内存 `≥5.5 GB`，提速 **1.98 倍**） |
| **高并发满血档（空闲内存充裕时）** | 5 并发约 **`1.6–2.0 秒/页`** | 3 并发约 **`4.3 秒/页`**（需空闲内存 `≥10 GB`） |
| **转一篇 20 页扫描论文耗时** | 约 **`1.2–2.0 分钟`** | **`1.9 分钟`**（双并发实测 `116.0 秒`）/ 单并发 `3.8 分钟` |
| **转完 390 页英文扫描全书耗时** | 约 **`25 分钟`**（双并发）/ 满血约 `13 分钟` | 约 **`38 分钟`**（双并发）/ 单并发约 `77 分钟` |
| **转完 545 页中文扫描全书耗时** | 约 **`32 分钟`**（双并发）/ 满血约 `16 分钟` | 约 **`53 分钟`**（双并发）/ 单并发约 `104 分钟` |
| **中断后断点续传重扫耗时（20页已缓存）** | **`< 1.0 秒`** | 实测 **`1.26 秒`**（`0` 页重算） |
| **API 费用 / 大模型 Token 消耗** | **`0 元` / `0 Token`** | **`0 元` / `0 Token`** |
| **中英质量探针得分（23项形近词/古籍表）** | 中文 **`11/12`** · 英文 **`9/11`** · 表格完整 · 双页码 `100%` | 中文 **`11/12`** · 英文 **`9/11`** · 表格完整 · 双页码 **`100%` 完全一致** |

---

## 三、低智力 / 超低价模型（Flash / Lite / Haiku 级）盲测结论

针对用户关心的核心问题：**“如果在 Claude Code / WorkBuddy 里换成 DeepSeek Flash、Haiku 或本地轻量小模型，能不能独立跑通安装、预估、执行、验收？会不会降低转换质量？”**

我们在真实环境中启动最低档轻量模型（`flash_lite`）仅凭一句自然语言口令进行了全链路盲测：
1. **对转换正文质量的影响 = `0%`（绝对零差异）**：PDF 页面识别、无框线表格还原、原书页码提取与 Markdown 拼装 100% 由本地 `MinerU ONNX` 模型与确定性 Python 代码完成，大模型完全不触碰书本正文。实测最低档轻量模型产出的页面正文与顶配旗舰模型 **字节级 100% 一致（`EXACT_BYTE_MATCH: True`）**。
2. **对执行与验收成功率的影响 = `100%` 零报错跑通**：因为排坑逻辑（国内 `ModelScope` / 镜像自动切换、空闲内存并发保护、25 页断点缓存、`1..N` 漏页硬校验）已全部封装进 `pdf2md.py` 的 `doctor → estimate → convert → verify` 四个傻瓜式 JSON 子命令，便宜模型整趟只需消耗约 `1k–2k` 调度 Token（折合人民币不到 **1 分钱**）即可稳稳交付。

---

## 四、8 套主流工具同场横评真相（为什么最终只留 `MinerU 4.2 ONNX`）

| 参评工具 / 方案 | GitHub 星标 | 开源许可 | 纯扫描中文（12项） | 纯扫描古英文（11项） | M5 Pro 单进程速度 | 表格与注文分离 | 原书页码提取 | 实测踩坑真相 |
|---|---|---|---|---|---|---|---|---|
| **`MinerU 4.2`（`basic` ONNX + `pdf2md.py`）** | `83.9k` | AGPL-3.0 衍生 | **`11/12`** | **`9~10/11`** | `6.2–7.7s/页` | **极强**（无框线表转 `<table>`） | **PDF页码 + 原书页码** | **🏆 综合冠军**：跨 Mac/Windows 一致、无需 Java、无需常驻后台服务 |
| **`Docling` 独立版（`RapidOCR`）** | `70.7k` | MIT | `11/12` | `9/11` | 中 `5.4s/页` · 英 `11.4s/页` | 强 | 仅分页符 | MIT 备选，但 600 DPI 英文宽行偶发漏识 |
| **`OpenDataLoader-PDF [Hybrid]`** | `30.3k` | Apache-2.0 | `11/12` | `9/11` | **中 `16.5s/页` · 英 `15.1s/页`** | 强（底层即 Docling） | 仅 PDF 页码 | 本地模式（`0.015s/页`）对扫描件提取为 **0 字**；混合模式需装 Java + 常驻 HTTP 服务，比宣传慢约 35 倍 |
| **`RapidOCR (PP-OCRv6)` 直通** | — | Apache-2.0 | `11/12` | `9/11` | `2.6–3.3s/页` | **无**（表格被打散） | 仅 PDF 页码 | 适合无表格纯文字扫描件快速初筛 |
| **`OCRmyPDF + MarkItDown`** | `35.3k` / `196.2k` | MPL / MIT | `4/12`（中文惨败） | `10/11` | 8核 `2.2–2.7s/页` | 弱 | 无分页标记 | 中文形近错字极多（如“愚和病→思和病”） |
| **`Tesseract CLI`** | — | Apache-2.0 | `4/12` | **`11/11`** | 中 `12.0s/页` · 英 `3.1s/页` | 无 | 仅外层拼页码 | 纯英文老文献稳，中文不可用 |
| **Mac 原生 `Vision / ocrmac`** | `555` | MIT | 沙盒报错 | 沙盒报错 | — | 无 | — | 在 Agent 子进程沙盒内易触发 `CVPixelBuffer -6662`，且 `PDFKit` 解码老 CCITT 扫描流会出空白页 |
| **`MarkItDown` 直读** | `196.2k` | MIT | **`0/12`（0 字）** | **`0/11`（0 字）** | `<0.01s/页` | — | — | **仅支持自带文字层的原生电子文档**，对扫描件完全无效 |

---

## 五、使用方法（一句话全自动）

### 1. 挂载 Skill（仅需一次）
- **macOS / Linux**：
  ```bash
  mkdir -p ~/.agents/skills ~/.claude/skills ~/.gemini/skills
  ln -sfn ~/Documents/brain/skills/scanned-pdf-to-markdown ~/.agents/skills/scanned-pdf-to-markdown
  ln -sfn ~/Documents/brain/skills/scanned-pdf-to-markdown ~/.claude/skills/scanned-pdf-to-markdown
  ln -sfn ~/Documents/brain/skills/scanned-pdf-to-markdown ~/.gemini/skills/scanned-pdf-to-markdown
  ```
- **Windows（PowerShell）**：
  ```powershell
  New-Item -ItemType Directory -Force -Path "$HOME\.agents\skills", "$HOME\.claude\skills" | Out-Null
  Copy-Item -Recurse -Force "$HOME\Documents\brain\skills\scanned-pdf-to-markdown" "$HOME\.agents\skills\"
  Copy-Item -Recurse -Force "$HOME\Documents\brain\skills\scanned-pdf-to-markdown" "$HOME\.claude\skills\"
  ```

### 2. 日常在 Agent 中一句话调用
直接在对话框里对 Agent 说：
> `把 "/绝对路径/某某扫描书.pdf" 转成带页码的 Markdown 放进 brain/raw/books/`

输出格式示例（位于 `~/Documents/brain/raw/books/某某扫描书.md`）：
```markdown
<!-- pdf_page: 37 | book_page: 25 | ocr_page: 25 | header: 差序格局 -->
### [PDF p.37 | 原书 p.25 · 差序格局]

我们的格局不是一捆一捆扎清楚的柴，而是好像把一块石头丢在水面上所发生的一圈圈推出去的波纹。每个人都是他社会影响所推出去的圈子的中心。
```
