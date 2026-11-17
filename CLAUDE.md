# 个人学术研究知识库共同规则（Shared Research Vault Schema）

本规则文件由 **Claude Code**、**Codex CLI**、**WorkBuddy**、**Antigravity CLI** 等多智能体共同读取（`AGENTS.md` 与 `GEMINI.md` 均通过软链接指向本文件，维护单一事实源）。

## 一、角色与三级按需读取原则

1. **人机分工**：研究者负责挑材料、提问题与最终拍板；Agent 负责在证据约束下做摘要、互链、状态记账与结构自检。
2. **三级按需加载（严防上下文膨胀）**：
   - **第 1 级（默认必读）**：先读 `wiki/index.md`（≤200 行路由目录）与 `wiki/status/` 下的项目进度及研究共识；
   - **第 2 级（按任务点名读卡）**：每次初选 3–6 张直接相关的 `wiki/` 知识卡片（涉及代码修改时同步读对应的 `datasets/` 与 `variables/` 防坑卡）；
   - **第 3 级（回查原始材料）**：仅当卡片信息不足或需要核对精确系数、题号、原句时，定向回查 `raw/` 快照或原稿。
3. **只读事实源红线**：`raw/`、`inbox/` 与 `.source-snapshots/` 为只读证据层，禁止手工或让模型直接覆写原件。

## 二、四类核心卡片的最小契约

- **方法与数据卡（`methods/` · `datasets/` · `variables/`）**：写清研究问题、分析单位、时间/波次结构、既有脚本入口、可迁移部分、**换数据后必须重做的部分**（如问卷跳答路由、跨波次名册槽位漂移、标准化分母差异）。
- **机会卡（`opportunities/`）**：记录旧论文研究局限、候选延伸方向、`unlock_condition`（解锁前置条件）与最小核查步骤。`data_ready` 仅代表数据与测量已实测核实，不等于自动立项；未核实前保持 `open` 或 `verifying`。
- **文风与采用语料卡（`style/`）**：严格区分 `global`（个人通用底线）、`dissertation`（学位论文体例）、`cssci_journal` / `ssci_journal`（期刊体例建议）与 `candidate`（待拍板候选润色）。所有润色严守**五大科学不变量**（系数方向、标准误/区间、p/q 值、样本量与分析单位、识别与因果强度）；写得准确克制的句子明确保留。
- **项目状态卡（`status/`）**：严格区分「候选建议」「已拍板决定」「已执行落盘」「已独立核验」，不拿文件修改时间（mtime）或文件名推断当前权威母稿。

## 三、日常工作流与收尾口令

1. **`migrate-check`（方法与素材迁移）**：输出「可复用资产 / 必须重做 / 待实测验证」三栏清单并附来源版本。
2. **`radar`（局限与官源查新）**：结合 `.vault-meta/research-inbox/` 待办队列（每日 10:00 自动巡检 CHARLS / CFPS 官网页面变化）与 `opportunities/` 检查解锁条件。
3. **`polish`（文风一步到位润色）**：先读 `wiki/style/` 与真实采用语料；不显著不等于排除机制，含 Zotero 活域段落仅改域外纯文本。
4. **`closeout`（标准收尾口令：「收尾，把这次确认的事实和新想法记进 brain」）**：
   - 将本次任务**已实测确认**的变量口径、代码坑点或文风裁决登记入 `sources/manifest.json` 并更新对应 `wiki/` 卡片；
   - 将尚未验证的**新想法或局限延伸**记入 `opportunities/`（标 `open/verifying`）或通过 `tools/research-watch.py enqueue --status candidate` 放入待办；
   - 必须运行确定性校验器通过后方可提交：
     ```bash
     python3 tools/wiki-lint.py --strict
     ```
