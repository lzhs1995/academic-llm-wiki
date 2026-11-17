---
type: opportunity
title: OPP-01：新波次面板下多子女家内支持水平与离散度拓展
origin_source:
- '[[wiki/papers/示例论文_夫妻资源与双系代际支持|示例论文：夫妻配对资源与双系代际支持]]'
- '[[wiki/datasets/CHARLS与CFPS跨波次配对及跳答防坑卡|CHARLS与CFPS跨波次配对及跳答防坑卡]]'
limitation_kind: 测量多维化与新波次数据延伸
candidate_dataset:
- CHARLS
- CFPS
unlock_condition: 完成父母报告者去重、跨波稳定子女身份核验与各维度有效分母只读探针表
state: verifying
evidence_review: source_checked
evidence:
- source_id: src-5d198bec0e539cc37417
  version: 116938b8ad0220f448edf600d552f2bb70308f1ad25b2bdc9a3b2659a7731bd5
  path: raw/demo/panel_survey_notes.md
  locator: lines:5-5
  quote: 长幼差亦须明确“最年长减最年幼”与“最年长减其余子女均值”是不同估计对象，必须分别命名。
---

# OPP-01：新波次面板下多子女家内支持水平与离散度拓展

返回 [[wiki/index|Wiki 路由目录]] · [[wiki/variables/家内多维差异构造字典(均值-标准差-性别差-长幼差)|构造字典]]

- **当前状态**：`verifying`（每日 10:00 自动监测 CHARLS / CFPS 官网更新；在未完成微观探针核验前不提前标为 `data_ready`）。
- **最小核查步骤**：输出 `eligibility_flow.csv`、`missingness.csv` 与 `within_family_metrics.csv`，核实有效簇数与均值—方差有界依赖。

> "长幼差亦须明确“最年长减最年幼”与“最年长减其余子女均值”是不同估计对象，必须分别命名。" — raw/demo/panel_survey_notes.md
