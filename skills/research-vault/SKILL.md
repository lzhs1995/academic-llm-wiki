---
name: research-vault
description: 跨 Agent（Claude Code / Codex / WorkBuddy / Antigravity）共享个人学术研究知识库工作流：方法与数据防坑复用、研究局限查新、个人文风与采用语料润色、跨会话研究状态同步。
---

# Research Vault Skill

共同规则唯一维护在仓库根目录 `CLAUDE.md`。进入研究任务时按以下顺序按需加载：

1. 读取 `CLAUDE.md` 与二级路由表 `wiki/index.md`（≤200 行）；
2. 按任务点名读取 3–6 张相关卡片（`status/`、`papers/`、`methods/`、`datasets/`、`variables/`、`opportunities/`、`style/`）；
3. 检查 `.vault-meta/research-inbox/items/` 是否有每日 10:00 官源巡检（CHARLS/CFPS）或上次任务留下的待办项；
4. 当用户输入 **「收尾，把这次确认的事实和新想法记进 brain」** 时：
   - 已确认事实/踩坑 → 更新对应卡片并登记来源指纹；
   - 未验证新想法 → 写入 `wiki/opportunities/`（标 `open` 或 `verifying`）或入队 `research-watch.py enqueue`；
   - 执行确定性结构与引文门禁：
     ```bash
     python3 tools/wiki-lint.py --strict
     ```
   - 通过 `0 错误, 0 警告` 后使用 `tools/research-commit.py` 按精确文件白名单提交。
