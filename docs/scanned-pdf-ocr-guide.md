# 扫描版中英文学术书籍入库与防错直引指南（Apple Silicon 实测版）

针对人文社科研究中大量存在的**纯图片扫描版 PDF 专著/古籍/论文**（内嵌文字层为 `0` 字符），本仓库内置了经中英文真实扫描专著同场实测筛选出的最优本地流水线：

1. **一次性离线转写**：`tools/ingest-scanned-pdf.py`（基于 `MinerU 4.2+` ONNX + `middle_json` 逐页重组），将纯图像 PDF 多进程并发转换为**同时保留 PDF 物理页码、OCR 原书印刷页码、章节页眉、无框线表格与独立注文**的 Markdown（存入 `raw/books/`），并自动在 `sources/manifest.json` 登记原 PDF 的 `SHA-256` 不可变指纹。
2. **带着问题定向抽卡**：让 Claude Code / Codex / WorkBuddy / Antigravity 阅读 `raw/books/<书名>.md` 相关章节，提炼结构化卡片写入 `wiki/`，**强制绑定原书页码、PDF 物理页码与快照精确行号**（`lines:START-END`）。
3. **双引号直引单页原图终核**：凡拟加双引号直接写进论文的正文引语，调用 `tools/verify-pdf-quote.py` 秒级导出原扫描 PDF 对应单页高清 PNG，由人工或 Agent 多模态视觉逐字核对字形，彻底防住 OCR 同音/形近错字（如中文“权利/权力”“己/已”“世代间/世代问”“拨/拔”，或 19 世纪英文断墨字符）。

---

## 一、8 套方案真实扫描书同场实测对比（Apple Silicon M5 Pro）

测试样本：
- **中文纯扫描专著**：20 世纪社会学经典合集（545 页，`0` 内嵌字符，约 180 DPI 灰度扫描，含脚注与大量形近词）
- **英文纯扫描古籍**：1884 年版 18 世纪英国工业革命讲义（390 页，`0` 内嵌字符，600 DPI CCITT 二值传真扫描，含无框线历史人口统计表、双栏小字脚注与断墨衬线体）

| 参评工具 / 方案 | GitHub 星标（实核） | 开源许可 | 纯扫描中文准确率（12项形近/生僻词探针） | 纯扫描古英文准确率（11项统计/断墨/注文探针） | M5 Pro 实测纯推理速度 | 无框线表格与注文分离 | 原书页码锚点 | 实测结论与踩坑真相 |
|---|---|---|---|---|---|---|---|---|
| **`MinerU 4.2`（`basic` ONNX + `middle_json` 封装）** | `83.9k` | AGPL-3.0 衍生 | **`11/12`（最优）** | **`10/11`（长行零乱码）** | 单进程 `6.2–7.7s/页`<br/>**5 进程并发约 `1.6–2.0s/页`** | **极强**（无框线表转 `<table>`；注文标 `page_footnote`） | **原生提取印刷页码** + PDF 页码 | **🏆 综合冠军（本库默认）**：无需 Java、无需后台常驻服务、中英双优、自动分离页眉与原书页码 |
| **`Docling` 独立版（`RapidOCR --lang ch`）** | `70.7k` | MIT | `11/12` | `9/11`（600 DPI 英文宽行偶发漏识乱码） | 中 `5.4s/页`<br/>英 `11.4s/页` | 强（Markdown 表格 + 注文列表） | 支持分页占位符（不单独抽原书页码） | **MIT 许可备选**：直接调 Python 比套壳 OpenDataLoader 快 **2–3 倍**，但英文二值宽行偶发丢词 |
| **`OpenDataLoader-PDF [Hybrid]`** | `30.3k` | Apache-2.0 | `11/12` | `9/11`（底层即 Docling，同现英文宽行乱码） | **中 `16.5s/页`<br/>英 `15.1s/页`** | 强（底层调 `docling-fast`） | 支持 `%page-number%` 分隔符（仅 PDF 页码） | **被公开跑分高估**：本地模式（`0.015s/页`）对纯扫描版提取为 **0 字**；混合模式实为 Java 调本地 Docling HTTP 服务，纯扫描书比宣称的 `0.46s/页` **慢约 35 倍**，且官方 `--help` 的 `english,chinese` 参数会静默忽略中文 |
| **`RapidOCR (PP-OCRv6) + pypdfium2` 直通** | — | Apache-2.0 | `11/12` | `9/11`（宽行偶发乱码） | 单进程 `2.6–3.3s/页`（零额外模型下载） | **无**（无框线表格被打散成竖列） | 仅 PDF 页码 | **超轻量备选**：仅 50MB 轮子开箱即跑，适合纯文字无表格的中文扫描件快速初筛 |
| **`OCRmyPDF + MarkItDown`** | `35.3k` / `196.2k` | MPL-2.0 / MIT | `4/12`（中文惨败） | `10/11` | 8 核并发 `2.2–2.7s/页` | 弱（页眉与跨页脚注易混入正文） | 无原生分页标记 | 底层基于 Tesseract，英文稳但中文形近错字极多（如“愚和病→思和病”“阖第→国第”），缺中文字体时还会生成两倍体积的透明层 PDF |
| **`Tesseract CLI` + `pdftoppm`** | — | Apache-2.0 | `4/12` | **`11/11`** | 中 `12.0s/页`<br/>英 `3.1s/页` | 无（页眉脚注混入段落） | 仅脚本外层拼接 PDF 页码 | 纯英文老文献单页识别稳，但中文扫描书不可用 |
| **Mac 原生 `Vision / ocrmac`** | `555` | MIT / 系统内置 | 沙盒受阻 | 沙盒受阻 | — | 无 | 仅外层拼接 | **Agent 沙盒真坑**：在 Claude Code / Codex / Antigravity 的受限子进程沙盒中会触发 `CVPixelBuffer -6662` 硬件缓冲报错；且苹果 `PDFKit` 解码老式 CCITT 扫描流会直接渲染出空白页 |
| **`MarkItDown` 直读 / `OpenDataLoader` 本地模式** | `196.2k` / `30.3k` | MIT / Apache-2.0 | **`0/12`（0 字）** | **`0/11`（0 字）** | `<0.2s/页` | — | — | **仅适合自带文字层的数字原生 PDF / Word / PPT**，对纯图片扫描书完全无效 |
| **`Marker 2.2`（`surya-ocr`）** | `41.0k` | Apache-2.0 代码 + 权重限制 | 首次运行强依赖拉取数 GB Surya 权重 | 同左 | CPU 极慢（官方基准 `~53.9s/页`） | 强（公式转 LaTeX 好） | 支持 JSON 分页 | 适合有独立显卡且大量数学公式的英文理工科 PDF；社科纯文字/表格扫描书在无独显或弱代理环境下性价比低 |

---

## 二、一次性环境安装（仅需做一次，约 5 分钟）

在终端（推荐使用 `uv` 隔离安装，不污染系统 Python）：

```bash
# 1. 安装 poppler（供单页高清原图核验使用）与 uv
brew install poppler uv

# 2. 创建独立 MinerU 运行环境
mkdir -p ~/.local/share/research-vault
uv venv ~/.local/share/research-vault/mineru-venv --python 3.12
uv pip install --python ~/.local/share/research-vault/mineru-venv/bin/python "mineru>=4.2,<5"

# 3. 预下载轻量 ONNX 模型（约 805 MB；国内无代理走 modelscope，挂海外代理建议先卸载 hf-xet 走 huggingface）
uv pip uninstall --python ~/.local/share/research-vault/mineru-venv/bin/python hf-xet
~/.local/share/research-vault/mineru-venv/bin/mineru-kit models download --tier basic --source modelscope -v
```

> **提示**：一旦 `~/.mineru/models/MinerU-4_models_onnx` 下载完成，`tools/ingest-scanned-pdf.py` 会自动开启 `HF_HUB_OFFLINE=1` 纯离线高速运行，后续转书完全不联网。

---

## 三、日常标准三步工作流

### 第 1 步：把扫描版 PDF 一键转成带双页码锚点的 Markdown 并登记快照

```bash
cd ~/Documents/brain

# --pdf-offset 填“正文第1页所在的PDF页码减1”（例如原书第1页在PDF第13页，就填 12）
python3 tools/ingest-scanned-pdf.py \
  "/绝对路径/某某扫描书.pdf" \
  --slug "作者_年份_书名简称" \
  --title "作者（年份）《完整书名》" \
  --pdf-offset 12 \
  --workers 5 \
  --chunk-size 100
```

脚本会自动：
- 多进程并发识别全书（支持断点续传：中途 Ctrl+C 后重跑会自动跳过已完成的页面分块）；
- 输出 `raw/books/作者_年份_书名简称.md`，每一页均带有精确机器锚点与人类可读页头：
  ```markdown
  <!-- pdf_page: 172 | book_page: 160 | ocr_page: 160 | header: 第二章 双系抚育 -->
  ### [PDF p.172 | 原书 p.160 · 第二章 双系抚育]
  ```
- 在 `sources/manifest.json` 与 `.source-snapshots/` 生成不可变快照（原扫描 PDF 保持只读零改动）。

### 第 2 步：让 Agent 带着具体研究问题定向抽卡片（不要整本泛泛总结）

在 Claude Code / Codex / WorkBuddy / Antigravity 中直接下达口令：

> `请读 raw/books/作者_年份_书名简称.md 中与【你的具体研究问题，如：双系抚育与单系偏重的张力】相关的章节，在 wiki/research/ 下新建一张概念/文献对话卡，强制写明 PDF 页码、原书页码及逐字原话（绑定 manifest 版本与精确 lines 行号），并跑通 python3 tools/wiki-lint.py --strict。`

### 第 3 步：论文正式加双引号直引前，单页原图视觉防错复核

当你要把卡片里的某句原话加双引号写进正式论文时，对 Agent 说一句话（或手动运行）：

```bash
python3 tools/verify-pdf-quote.py "/绝对路径/某某扫描书.pdf" --book-page 25 --pdf-offset 12
```

该命令会在 0.3 秒内把原扫描 PDF 对应那一页导出为 250 DPI 高清图片（存放在 `.vault-meta/quote-renders/`），让 Agent 直接看一眼原书字形图片，100% 拦截“以**己**为中心 → 以**已**为中心”“**拨**拨清楚 → **拔**拔清楚”“**权**利 → **权**力”等一切 OCR 残余形近错字。

---

## 四、Token 成本拆解与「便宜/本地模型」四级分工策略

日常维护扫描书卡片**绝不应该让昂贵的旗舰大模型硬啃整本原书**。按以下四道工序切分后，单本 500 页专著入库与制卡成本可降低 **95% 以上**：

| 工序 | 动作 | 大模型 Token 消耗 | 推荐模型档位 | 硬件与质量边界 |
|---|---|---|---|---|
| **① 扫描 PDF 转 Markdown + 登记指纹** | `python3 tools/ingest-scanned-pdf.py` | **`0` Token** | **无需任何 LLM**（纯本地 Python + ONNX） | 本机空闲内存 <14 GiB 时脚本自动限流 `--workers 2 --chunk-size 32`，防止触发 macOS Swap 换页 |
| **② 按问题查章节、提炼初稿卡片** | 先 `grep` 标题定位页码，**每次仅读取 10–25 页**（严禁整本几十万字全塞进上下文） | 约 `10k–20k` Input + `1.5k` Output | **超低价云模型**（`DeepSeek Flash` / `Haiku` / `GPT Flash/Mini`）或**本机 `Gemma 4`** | 结构化摘录与跑 `wiki-lint.py --strict` 对便宜模型毫无压力；若跑本地大模型请避开 OCR 高峰期以免争抢统一内存 |
| **③ 论文双引号直引前的单页原图核字** | `python3 tools/verify-pdf-quote.py` 导出单页 PNG 对照字形 | 约 `1.5k` 图像 Input + `200` Output | **高分中文视觉多模态模型**（`Haiku` / `GPT` 视觉轻量版 / `Gemma 4 Vision`）或**人眼扫视 3 秒** | 纯文本模型无法看图；针对扫描模糊处的“己/已”等一笔之差，多模态初审 + 人工扫一眼 PNG 是零失误双保险 |
| **④ 跨文献理论对话与论文正文润色** | 仅读取 `wiki/` 中提炼完毕的 3–6 张卡片（共 `3k–6k` tokens） | 约 `5k–12k` Input + `2k` Output | **旗舰主力模型**（`Claude Opus/Sonnet` / `GPT Pro`） | 旗舰模型只读浓缩卡片写引言或改稿，既省钱又彻底规避超长上下文注意力衰减 |

