# Academic LLM Wiki — 面向社科定量研究者的个人学术知识库架构

基于 Andrej Karpathy 提出的 **LLM Wiki** 思路搭建的本地学术研究知识库（Personal Research Vault）脚手架与确定性防幻觉工具链：

- **项目结构**：**Obsidian** 当 IDE（给人看的前端浏览视图），**Claude Code / Codex / WorkBuddy / Antigravity** 等 Agent 当程序员（负责读写与维护），**本地 Markdown 文件夹**当代码库（持续积累可复用的研究资产）。
- **人机关系**：**人**负责挑材料、提问题、做学术判断与最终拍板；**Agent** 负责在证据约束下摘要、互链、记账与自检。
- **运作逻辑**：让 Agent 把真实项目里**读过、踩过坑、核实过**的结论写成结构化卡片存下来；下次开新会话先读目录与卡片，卡片不够再回查原稿或原始代码。

---

## 一、实话实说：什么是“智商税”，什么是真有用？

围绕博士论文与多篇定量小论文（CFPS / CHARLS 等微观面板数据）实战折腾一轮后，最真实的体感是：

### ❌ 容易踩的“智商税”误区
1. **“所有历史草稿一股脑导入，自动长成第二大脑”**：几百份过程稿混在一起只会让过期判断污染定稿结论（Garbage in, garbage out）。
2. **迷恋炫酷的双链星系图**：图谱连线好看但对写论文几乎零帮助，真正有用的是结构化属性表（Obsidian Bases）和准确的原文定位。
3. **指望 AI 自动从旧局限里替你拍板新顶刊选题**：研究品味与可行性判断永远在人，知识库只能帮你**备料与排雷**。
4. **让大模型“自己写卡片、自己审自己”**：AI 极易把“统计不显著”顺嘴写成“排除了某机制”，或把“未核实的候选想法”写成“已完成定稿”——**校验必须移出大模型，交给确定性脚本**。

### ✅ 真正回本、高频见效的三件事
1. **个人文风与真实改稿语料沉淀**：把以前论文真实采纳的“修订版 vs 清洁定稿”前后对照、禁用词（如剔除 AI 常见的工程台账腔与政论口号腔）、变量术语红线、Word/Zotero 活域保护规则写成几页固定卡片，新开任何 Agent 润色都能大幅减少重复调教。
2. **代码与调查数据防坑经验复用**：做微观追踪面板（如 CHARLS / CFPS）时，半年前踩过的**跳答路由丢样本、跨波次子女名册槽位（Roster Slot）漂移、旧代码里“相对离散系数”不能直接当普通样本标准差**等细节，查卡即可精准预警；配合定时脚本每天早上 `10:00` 自动巡检 CHARLS / CFPS 官网页面变化并放入待办队列，及时提醒查新。
3. **跨 Agent 共享记忆（零复制粘贴切换）**：根目录维护唯一真源 `CLAUDE.md`，同目录 `AGENTS.md`（供 Codex / WorkBuddy 读取）与 `GEMINI.md`（供 Antigravity 读取）均通过软链接指向它，并在全局规则留好入口。任何一个 Agent 更新落盘了卡片，其他 Agent 下次读库拿到的就是最新研究共识与版本总账。

---

## 二、仓库目录结构

```text
academic-llm-wiki/
├── CLAUDE.md                        # 跨 Agent 唯一共同宪章（核心规则真源）
├── AGENTS.md -> CLAUDE.md           # Codex / WorkBuddy 等读取的同源软链
├── GEMINI.md -> CLAUDE.md           # Antigravity / Gemini CLI 读取的同源软链
├── 00_研究总控台.md                  # Obsidian 一屏总入口
├── Bases/                           # Obsidian 1.14+ 原生动态数据库表格视图
│   ├── 研究资产五维矩阵.base         # 论文五维矩阵 / 方法复用库 / 数据与变量字典
│   ├── 新增长点雷达.base             # 研究局限转化机会池（按 verifying / data_ready 分组）
│   └── 文风与术语规则.base           # 个人文风语料与常用 Prompt 视图
├── wiki/                            # Agent 编译维护的结构化知识卡（日常只读这里）
│   ├── index.md                     # 二级路由总表（硬上限 ≤ 200 行，防撑爆上下文）
│   ├── status/                      # 跨 Agent 研究人格共识 + 在研项目权威版本总账
│   ├── papers/                      # 已完成/在研论文五维画像卡
│   ├── methods/                     # 定量方法卡（含“可迁移”与“换数据必须重做”清单）
│   ├── datasets/                    # CFPS / CHARLS 等跨波次清洗与跳答防坑卡
│   ├── variables/                   # 核心构念与家内多维差异（均值/SD/性别差/长幼差）字典
│   ├── opportunities/               # 研究局限与新增长点候选卡（带 unlock_condition）
│   ├── style/                       # 个人文风宪法、真实采用语料对照、Word/Zotero 防坑铁律
│   └── prompts/                     # 高复用科研工作流口令模板
├── raw/                             # 只读原始证据/笔记快照层（禁止模型直接改写）
├── sources/manifest.json            # 来源 SHA-256 指纹与精确行号/记录定位注册表
├── .source-snapshots/               # 内容寻址不可变来源快照
├── skills/
│   ├── research-vault/SKILL.md      # 学术知识库读写与防幻觉规则技能包
│   └── scanned-pdf-to-markdown/     # 扫描版/纯图片 PDF 一句话免费离线转带页码 Markdown 技能包（Mac + Windows）
├── config/research-watch.json       # CHARLS / CFPS 官网每日 10:00 变更巡检配置
├── tools/                           # 零 AI 依赖的纯 Python 确定性校验与维护脚本
│   ├── wiki-lint.py                 # 全库体检：双链/锚点 + YAML 契约 + Bases 过滤 + 引文联检
│   ├── quote-check.py               # 逐字引文与来源版本一致性核验（防模型编造引文）
│   ├── source_registry.py           # 来源指纹登记与定位切片核对
│   ├── ingest-scanned-pdf.py        # 扫描版中英文 PDF 多进程 OCR 转双页码锚点 Markdown 并登记快照
│   ├── verify-pdf-quote.py          # 论文双引号直引前秒级导出原扫描 PDF 单页高清 PNG 供字形终核
│   ├── research-watch.py            # 官网更新定时巡检 + 任务收尾候选入队（无 LLM 调用）
│   ├── research-commit.py           # 多 Agent 并发安全的精确文件白名单 Git 提交器
│   ├── build-research-nav.py        # 从卡片属性自动同步 wiki/index.md 路由目录
│   └── launchd/                     # macOS 每日 10:00 自动巡检 LaunchAgent 模板
├── docs/
│   └── scanned-pdf-ocr-guide.md     # 8 款扫描 PDF 转 Markdown 工具真书实测横评与三步操作手册
└── tests/                           # 确定性工具链单元测试套件
```

---

## 三、3 分钟快速上手

### 1. 克隆并用 Obsidian 打开
```bash
git clone https://github.com/lzhs1995/academic-llm-wiki.git ~/Documents/brain
cd ~/Documents/brain
pip install pyyaml
python3 tools/wiki-lint.py --strict
```
打开 Obsidian → `Open folder as vault` → 选择 `~/Documents/brain`，点开 `00_研究总控台.md` 即可浏览示范卡片与 `Bases/` 动态表格。

### 2. 挂载跨 Agent 技能与全局入口（Claude Code / WorkBuddy / Codex / Antigravity）
```bash
# 将技能软链至各客户端技能目录（按你实际使用的工具选装）
mkdir -p ~/.agents/skills ~/.claude/skills ~/.gemini/skills
ln -sfn ~/Documents/brain/skills/research-vault ~/.agents/skills/research-vault
ln -sfn ~/Documents/brain/skills/research-vault ~/.claude/skills/research-vault
ln -sfn ~/Documents/brain/skills/research-vault ~/.gemini/skills/research-vault
ln -sfn ~/Documents/brain/skills/scanned-pdf-to-markdown ~/.agents/skills/scanned-pdf-to-markdown
ln -sfn ~/Documents/brain/skills/scanned-pdf-to-markdown ~/.claude/skills/scanned-pdf-to-markdown
ln -sfn ~/Documents/brain/skills/scanned-pdf-to-markdown ~/.gemini/skills/scanned-pdf-to-markdown
```
在你的全局规则文件（如 `~/.claude/CLAUDE.md`、`~/.codex/AGENTS.md` 或 WorkBuddy 的全局指令设置）中加一行薄入口即可：
> `涉及论文选题、方法数据复用、文稿润色或研究进度更新时，先读 ~/Documents/brain/CLAUDE.md 与 ~/Documents/brain/wiki/index.md（按需点名读 3–6 张卡）。`

### 3. （可选）启用每日早上 10:00 自动检查 CHARLS / CFPS 官网更新（macOS）
将 `tools/launchd/com.example.research-vault-watch.plist` 中的 `/ABSOLUTE/PATH/TO/brain` 替换为你的实际绝对路径，拷入 `~/Library/LaunchAgents/` 后加载。一旦官网页面发生更新，会自动写入 `.vault-meta/research-inbox/items/` 待办队列，下次开研究任务时提醒你查新。

### 4. 扫描版 / 纯图片 PDF 专著一句话入库（Mac & Windows 零成本本地转换）
针对内嵌文字层为 0 的中英文扫描版电子书或论文，挂载 `skills/scanned-pdf-to-markdown` 后，**无需去终端手敲长串命令**，直接在 Claude Code / Codex / WorkBuddy / Antigravity 对话中说一句话：
> `把 "/path/to/扫描版书.pdf" 转成带页码的 Markdown 存进 brain/raw/books/`

Agent 会自动按四步协议完成：
1. **首次自动装好本地免费模型**（`MinerU 4.2` 轻量 ONNX，`0` 元 API 费、`0` 大模型 Token，转换质量与所选 Agent 模型贵贱完全无关）；
2. **1 秒预估耗时**：短篇（≤1 分钟）直接出结果；长篇专著先报预估分钟数，获准后作为**后台子任务**执行，不阻塞当前对话；
3. **带双页码落盘 + 自动断点续传**：自动存入 `raw/books/<书名>.md`，每页保留 `PDF p.X | 原书 p.Y` 双页码锚点与表格/注文，每 25 页独立存档，中断重跑秒级续传；
4. **确定性自动验收并通知**：逐页核对 `1..N` 页无漏页后主动回报路径与耗时。
👉 **8 套工具横评 + Mac M5 Pro 与 Windows i7-1260P 轻薄本双真机实测速度/质量表见：[docs/scanned-pdf-ocr-guide.md](./docs/scanned-pdf-ocr-guide.md)**。

---

## 四、日常最低成本使用方式（不用手动维护卡片！）

平时写论文、跑回归完全不用分心当“笔记管理员”，只在**遇到真实深坑**或**阶段任务收尾**时用两句话：

1. **每次论文任务结束时，对当前 Agent 发一句口令**：
   > `收尾，把这次确认的事实和新想法记进 brain`
2. **随时在终端花 1 秒钟跑确定性结构与引文体检**：
   ```bash
   cd ~/Documents/brain && python3 tools/wiki-lint.py --strict
   ```
   示例输出：
   ```text
   BASE: 研究资产五维矩阵.base/论文五维全景矩阵: 1 matching cards (UI not checked)
   BASE: 研究资产五维矩阵.base/定量方法复用库: 1 matching cards (UI not checked)
   BASE: 研究资产五维矩阵.base/数据集与变量操作化字典: 2 matching cards (UI not checked)
   BASE: 新增长点雷达.base/全部新增长点雷达（按状态分组）: 1 matching cards (UI not checked)
   BASE: 新增长点雷达.base/数据与测量已核查（立项另行决定）: 0 matching cards (UI not checked)
   BASE: 新增长点雷达.base/待样本核查与新波次解锁: 1 matching cards (UI not checked)
   BASE: 文风与术语规则.base/个人文风、采用语料与排版经验: 1 matching cards (UI not checked)
   BASE: 文风与术语规则.base/科研高复用Prompt模板库: 1 matching cards (UI not checked)
   wiki-lint: 0 错误, 0 警告 | 活跃卡片 9 | 双链 44 | 本地链接 0
   quote-check: 0 个问题 / 18 引用与证据项 / 9 页；仅文本、版本与定位一致性，科学解释另行复核
   ```

---

## 五、参考与致谢

本架构在探索过程中参考并受益于以下优秀开源项目与理念：
- **Andrej Karpathy** 关于 *LLM Wiki*（Obsidian as IDE, LLM as programmer, Markdown wiki as codebase）的讨论
- [AgriciDaniel/claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian) — 基于 Karpathy LLM Wiki 模式的 Claude Code + Obsidian 知识库工作流
- [obsidianmd/obsidian-skills](https://github.com/obsidianmd/obsidian-skills) — Obsidian 官方 Agent Skills（Markdown、Bases、Canvas 规范）
- [tobi/qmd](https://github.com/tobi/qmd) — 本地混合检索引擎（当卡片积累至百张以上规模时，可无缝接入做本地语义+关键词检索增强）

## License

[MIT](./LICENSE)
